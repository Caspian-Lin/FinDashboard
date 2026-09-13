---
name: issue-189-backtest-async
description: MCP backtest_run strategy 形态异步化 — run_async 命名、工作量估算与自动阈值(issue #189)
metadata:
  type: project
---

# Issue #189:MCP backtest_run strategy 形态异步化

`finboard_backtest_run`(strategy 形态,MCP 工具)支持异步执行:

- **`run_async: bool | None = None`**(工具参数)。`true` → 入队 `kind=backtest_run`
  后台任务返回 job_id;`false` → 强制同步;省略 → 按估算工作量自动切换。
- **自动阈值 settings**:`backtest_auto_async_symbol_days`(默认 15000,`0`=关闭
  自动切换)。工作量估算 = `len(symbols) x round((end-start).days*5/7)`。
- 异步 payload 与 REST `POST /api/backtest/run` 完全同构:`{request, provider_name}`
  (queue=`data`),幂等键 `backtest:{strategy}:{sha256(request,sort_keys)[:24]}`
  同口径,可跨入口去重;由既有 `BacktestRunExecutor` 消费,成功后
  `result_ref=str(run_id)`,`finboard_backtest_history_get(run_id)` 查结果。
- 响应含 `async_mode`(explicit / auto_threshold / …)、`symbol_days_estimate`、
  `auto_async_threshold`,便于 agent 核对「为什么异步」并自校准。
- 参数校验前置:非法策略/参数/selection 在入队前同步 `invalid_argument`(与
  grid #175 一致),不入队;幂等冲突 `conflict`。

**Why(设计与踩坑):**
- **`async` 是 Python 保留字,MCP 工具参数名 = JSON schema 属性名**,@mcp.tool 直接
  内省函数签名生成 schema,无法声明 `async` 属性 → 用社区惯例转义名 `run_async`,
  描述里讲清语义。这是「加布尔开关式工具参数」的通用限制。
- 实测校准:issue 报告 100 只 x 2081 天超 MCP 30s 客户端超时、30 只 x 2081 天约
  11 分钟 → ≈10ms/段,30s 预算 ≈ 3000 段。默认 15000 让两个真实故障案例
  (208k / 62k 段)即使忘传 run_async 也自动切异步;显式 run_async=true 是决定性
  手段,自动切换只是兜底启发式。
- 单元测试模拟幂等冲突时 `IntegrityError("dup")` 会抛 TypeError
  (DBAPIError.__init__ 需 `(stmt, params, orig)` 三参),须
  `IntegrityError("stmt", {}, Exception("dup"))`。
- 集成测试 `payload["provider_name"]` 断言勿硬编码 `akshare`:本机/CI 的
  `data_provider` 随 .env/环境变化,应取 `app.settings.data_provider`。
- `_do` 内提前用 `FactorSelectionParams` 的分支必须显式 import(同步路径是在其
  下方 import 的,作用域不共享)。
- 模块级 engine fixture 的集成测试在 teardown 才清表,测试间共享
  `background_jobs`/`backtest_runs`;幂等重提交断言按本测试 key 数,勿数全表。

**How to apply:** 研究/数据域「同步耗时 → 后台 job」改造沿用此模式:复用对应 kind
executor + 与 REST 同构 payload/幂等键、入队前同步校验、响应给 job 指针 + 轮询
指引;新加布尔工具参数遇保留字用转义名 + description 澄清。回滚:阈值为 0 即关
自动切换,既有同步调用方行为不变。
