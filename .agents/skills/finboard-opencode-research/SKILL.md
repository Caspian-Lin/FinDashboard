---
description: FinBoard 研究 Skill —— 指导 OpenCode 研究 Agent 的工作流、工具选择、权限边界与来源引用。用于量化研究、因子分析、回测/模拟盘分析、研究记忆。不用于实盘交易或代码编辑。
---

# FinBoard 研究 Skill

本 Skill 指导 `finboard-researcher` agent 通过 `finboard.*` MCP 工具进行受控
量化研究。

> **权限由 OpenCode 配置 + FinBoard 服务端策略共同控制,本 Skill 不授予任何
> 工具权限。** Skill 只描述「应该做什么」,工具是否可调用由
> `.opencode/opencode.json`(agent permission)与 FinBoard MCP 服务端策略决定。

## 核心原则(HARD RULES)

1. **只读直接调用** —— 数据 / 因子 / ResearchRun / 模拟盘查询可直接调用。
2. **研究写操作可自主执行**(#122) —— 创建因子 / 快照 / 策略 / 运行回测 / 发布数据 /
   启动模拟盘等研究写操作 agent 可通过 MCP **自主执行**,无需人工审批。
   不触及交易安全红线(不连 broker / 账户 / 订单 / 持仓)。
3. **实盘能力永久不可用** —— 下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker /
   凭证探测不在工具集中。需要它们 = 走错了路。
4. **不生成代码** —— 策略是无代码版本化规格,禁止生成 Python / 模块路径 /
   可执行表达式。

## 工具选择(快速参考)

当前已实现 123 个工具。标注 ✅(可用) / 🔒(planned,对应 issue 尚未实现):

| 场景 | 工具 | 状态 | 权限 |
|------|------|------|------|
| 查询 / 管理 ResearchRun | `finboard.run.*`(list/get/artifacts 只读 + queue/cancel/replay/lineage 写,7 个) | ✅ #127+#183 | 3 只读 + 4 自主执行 |
| 记住 / 查询 / 纠正研究记忆 | `finboard.memory.*`(7 个) | ✅ | 直接执行 |
| 标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额 | `finboard.instrument.*` / `.dataset.*` / `.data.*` / `.tushare.*`(9 个) | ✅ #124 | 只读 |
| 数据准备(拉取/同步/发布/修复/ETF/配置) | `finboard.data_write.*` / `.etf.*`(12 个) | ✅ #137 | 2 只读 + 10 自主执行 |
| 因子目录 / 特征快照 / 因子信号 / 因子实验 | `finboard.factor.*` / `.feature_snapshot.*`(12 个) | ✅ #125 | 8 只读 + 4 自主执行 |
| 策略规格 / 预设(registry/template/validate/draft/publish/rollback/diff/preset CRUD;feature_graph 可按 `u_<name>` 引用沙箱用户因子,仅 active 可引用,#217) | `finboard.strategy.*` / `.preset.*`(16 个) | ✅ #126 | 8 只读 + 8 自主执行 |
| 回测(双形态:strategy 事件驱动回测——默认小规模同步,run_async=true 或规模达阈值自动入队后台任务返回 job_id(#189,避免 MCP 30s 超时后响应丢失);strategy_spec 路由已发布规格入队 research_run 多期再平衡回放;benchmark_symbol 显式基准(指数日线 akshare 免积分),基准缺失返回 null 不静默 0.0;selection.factor_version 仅 "v1"(可用因子集=因子目录投影子集,#214);批量网格提交/聚合对比——grid_get 默认不返回曲线,显式 equity_mode=summary/full 才返回,公共字段在网格头部只出现一次(#190/#206);history CRUD——history_get fills 默认有界 200 条分页、history_list symbols 前 10 只+计数(#206)) | `finboard.backtest.*`(7 个) | ✅ #127+#174+#175+#183+#184+#189+#190 | 4 只读 + 3 自主执行 |
| 模拟盘(账户/会话生命周期/决策/行情投递/晋级评估/归档/订单/成交/持仓/账本/审计/报告) | `finboard.sim.*`(21 个) | ✅ #127+#139 | 10 只读 + 11 自主执行 |
| portfolio 计算(allocate/sizing/feasibility/attribution) | `finboard.portfolio.*`(4 个) | ✅ #128 | 纯计算,自主执行 |
| 后台任务队列监控与提交/归档 | `finboard.job.*`(list/get 只读 + enqueue/cancel/archive/unarchive 写,6 个) | ✅ #136+#221 | 2 只读 + 4 自主执行 |
| #57 验证实验(创建/列表/详情/拒绝/登记 trial/删除) | `finboard.validation_experiment.*`(6 个) | ✅ #138 | 2 只读 + 4 自主执行 |
| 自选股(创建/查询标的组、增删标的) | `finboard.watchlist.*`(7 个) | ✅ #140 | 2 只读 + 5 自主执行 |
| 报告聚合与导出(ResearchRun/回测报告、导出 CSV/Markdown 文件;report_run 默认 summary 聚合计数、report_backtest fills 分页有界,#206) | `finboard.report.*`(3 个) | ✅ #141+#206 | 3 只读 |
| 研究代码提交(agent 策略/因子代码入口;静态校验+版本化存储,只存不执行;web 通道仍禁代码) | `finboard.research_code.*`(4 个) | ✅ #215 | 2 只读 + 2 自主执行 |
| 研究代码沙箱执行(一次性 Docker 容器跑 factor.compute;PIT 物理隔离挂载、断网/只读/限额/超时 kill;run 三向引用审计;成功输出过质量门后落库为 `u_<name>` 因子快照,可被 research run 引用,run report 携带 factor_screen 筛选指标(#217);需 research_sandbox_enabled + Docker 镜像) | `finboard.research_code_run` / `_get`(2 个) | ✅ #216+#217 | 1 只读 + 1 自主执行 |
| 用户代码策略执行(agent 编写的策略代码进入回测:spec `strategy_kind=user_code` + `code_artifact` 引用 active artifact;`finboard_run_queue` 声明 `rebalance_frequency=multi_period` 走逐决策日沙箱 `decide(ctx)→目标权重`(当前权重回显 + 约束视图),复用 #91 组合管线;report 附 `sandbox_provenance`(commit+镜像 digest);与 multi_factor 同屏可比;入队门控 active/commit/沙箱开关,#218) | `finboard_run_queue` + `finboard_strategy_*`(复用,无新工具) | ✅ #218 | 复用 run_queue 写权限 |

> 详细工具契约见 `references/tools.md`;扩展计划见
> `packages/finboard-mcp/ROADMAP.md`。

## 研究工作流(简版)

1. **理解问题** —— 查询相关数据 / ResearchRun / 模拟盘。
2. **形成假设** —— 你自己(OpenCode LLM)直接分析并给出可检验的研究假设;
   需要机器验证时创建 #57 验证实验(`finboard.validation_experiment.*`)。
3. **记忆上下文** —— 用 `finboard.memory.remember` 记住关键发现,关联研究产物。
4. **引用来源** —— 所有结论引用 ResearchRun / 数据集 / 模拟盘产物 ID。

> 详细工作流与决策树见 `references/workflow.md`;
> 完整研究流程(数据→因子→策略→回测→模拟→评估)见 `references/research-workflow.md`。

## 来源引用规则

- 金融答案**必须引用项目来源**(ResearchRun ID / 数据集版本 / 模拟盘 ID)。
- 数据不足时明确声明「数据不足」,**绝不编造数字**。

## 记忆使用规则(简版)

- 用 `finboard.memory.remember` 记住跨会话需要的研究上下文。
- `source_refs` 关联研究产物(只引用,**不修改产物本身**)。
- 发现错误用 `finboard.memory.correct`(形成纠正链),**不要直接删除**。
- 过时但仍有参考价值的记忆用 `finboard.memory.archive`,仅在确需移除时用
  `finboard.memory.forget`(软删除,保留审计)。

> 详细记忆规则与生命周期见 `references/memory.md`。

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/tools.md` | 123 个 MCP 工具完整契约(参数 / 返回 / 场景) |
| `references/workflow.md` | 研究工作流决策树 + 标准研究循环 |
| `references/research-workflow.md` | 完整研究流程详解(数据→因子→策略→回测→模拟→评估) |
| `references/memory.md` | 研究记忆使用规则与生命周期 |
| `references/system-overview.md` | FinBoard 系统架构概览(包结构 / 模块职责 / 研究 vs 实盘隔离) |
