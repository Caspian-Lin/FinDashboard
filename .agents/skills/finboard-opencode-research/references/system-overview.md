# FinBoard 系统架构概览

本文件给研究 Agent 一份 FinBoard 全景认知,重点说明**研究域 vs 实盘域的隔离**,
以及 MCP 工具只覆盖研究域。

## 系统定位

FinDashboard(FinBoard)是**可实盘交易的模块化单体量化交易系统**:

- **架构**:模块化单体(非微服务),不引入 Kafka / Kubernetes / 分布式事务
- **技术栈**:Python 交易进程 + PostgreSQL
- **范围**:单账户、单券商接口、单市场(A 股或期货二选一)
- **研究 / 回测 / 模拟 / 实盘严格隔离**

## Monorepo 包结构(研究域 vs 实盘域)

```
packages/
  finboard-shared/        — 领域模型 / 类型 / 异常 / ID 生成         [共享]
  finboard-persistence/   — SQLAlchemy ORM / Repository / Alembic    [共享]
  finboard-data/          — 历史行情数据(akshare/tushare)            [研究]
  finboard-backtest/      — 回测引擎 / 因子实验室 / 策略规格 / ResearchRun / AI 助手  [研究]
  finboard-simulation/    — 模拟盘(独立 simulation_* 表)            [研究]
  finboard-mcp/           — MCP Server(向 Agent 暴露研究工具)        [研究-AI 接口]
  finboard-opencode/      — OpenCode 研究运行时集成                   [研究-AI 接口]
  finboard-broker/        — BrokerAdapter / MarketDataAdapter 抽象   [实盘]
  finboard-broker-qmt/    — QMT 适配器(Windows-only)                [实盘]
  finboard-broker-ctp/    — CTP 适配器(期货)                         [实盘]
  finboard-core/          — TradingKernel / OrderManager / 状态机    [实盘]
  finboard-risk/          — PreTradeChecker / KillSwitch             [实盘]
  finboard-reconcile/     — Reconciliation / Recovery Engine         [实盘]
  finboard-scheduler/     — 定时任务调度                              [实盘]
  finboard-app/           — 组装根 / CLI / 配置                       [实盘-入口]
  finboard-api/           — FastAPI REST + WebSocket API              [共享-入口]
```

**MCP 工具只接入【研究】域包**(`finboard-data` / `finboard-backtest` /
`finboard-simulation`),**永不接入实盘域包**。

## 数据库表隔离

### 研究域表(MCP / Agent 可访问)
- `research_runs` / `research_run_artifacts` —— ResearchRun 生命周期与逐阶段产物
- `factor_hypotheses` / `factor_hypothesis_experiments` —— 因子假设与实验
- `ai_drafts` / `ai_audit_events` —— AI 草案与审计
- `research_memories` —— 研究长期记忆(#110)
- `simulation_*` —— 模拟盘独立表(订单 / 成交 / 持仓 / 资金 / 审计)

### 实盘域表(MCP / Agent **永不**访问)
- `orders` / `fills` / `positions` —— 实盘订单 / 成交 / 持仓
- `audit_logs` —— 实盘审计

> **红线**:研究运行使用 `RR-` ID 与独立表,模拟运行使用 `SIM-*` ID 与
> `simulation_*` 表,**均不得写入实盘 `orders` / `fills` / `positions`**。

## 研究 vs 实盘 的晋级路径

研究产物不能直接上实盘,必须走完整流程:

```
研究 → 回测 → 样本外 → 行情回放 → 模拟交易 → 影子交易 → 小资金实盘 → 扩大资金
```

- 每一步都有独立验证与人工确认
- **LLM / Agent 的产出必须经过完整流程才可上实盘**
- 模拟盘**禁止自动晋级**影子盘 / 实盘

## AI 接口层(研究 Agent 的两个入口)

### 1. MCP Server(`finboard-mcp`,#108)
- 把研究能力以受控 MCP 工具暴露给 OpenCode 等外置 Agent 运行时
- 113 个已实现工具(#124-#141 扩展完成;#160 移除 ai.* 后 FinBoard 零内置 LLM)
- 权限矩阵:研究写操作 agent 自主执行;实盘能力永久不注册
- 统一信封 `ToolEnvelope` + 审计事件(structlog + 内存副本)

### 2. OpenCode 研究运行时(`finboard-opencode`,#109/#118)
- 把 OpenCode Web 作为受控研究 Agent 运行时(Docker 容器隔离)
- OpenCode 通过 `finboard.*` MCP 工具访问研究能力
- 不连接实盘 broker / 账户 / 订单 / 持仓 / Kill Switch
- `opencode_web_enabled` 默认关闭,回滚方案为关闭入口

## 安全红线(对 Agent 的硬约束)

Agent(包括本 MCP / OpenCode 运行时)**不允许**:
- 直接连接实盘账户
- 直接发送订单 / 撤单
- 修改账户持仓
- 绕过风控
- 在实盘运行时动态生成代码并立即执行
- 操作 Kill Switch

`ResearchAssistant`(`finboard-backtest`)是 AI 助手的唯一入口,强制
`assert_research_only_request` 拒绝越权请求与提示词注入。
