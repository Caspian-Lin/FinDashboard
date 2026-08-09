# FinBoard MCP 工具契约(详细)

本文件列出 `finboard-researcher` agent 可用的全部 `finboard.*` MCP 工具。
所有工具返回统一信封 `ToolEnvelope`:
`operation_id` / `status`(ok|denied|error) / `data` /
`error` / `provenance` / `idempotency_key`。

当前已实现 14 个工具(✅);planned 工具(🔒 #124-#128)尚未实现,
列出契约供 agent 知晓未来能力边界。

## 权限矩阵(#122:研究写操作自主执行)

| 类别 | 只读/自主 | 永久不可用 |
|------|----------|-----------|
| 只读查询(run.*) | ✅ | |
| 研究记忆(memory.*) | ✅ | |
| AI 草案(ai.*:假设/策略) | ✅(产出草案,可追溯) | |
| 数据查询 / 因子 / 策略规格 / 回测 / 模拟 / portfolio | 🔒 #124-#128(扩展中) | |
| 实盘(下单/撤单/持仓/Kill Switch/broker/凭证) | | ✗ |

## finboard.run.*(只读)

### finboard_run_list
列出 ResearchRun。
- 参数:`statuses?: list[str]`、`strategy_kind?: str`、`limit?: int = 50`
- 返回:`list[{run_id, strategy_id, strategy_kind, status, ...}]`

### finboard_run_get
查询单个 ResearchRun 详情(含 manifest / result)。
- 参数:`run_id: str`
- 返回:`{run_id, ..., manifest, result, error_summary}`

### finboard_run_artifacts
列出某 ResearchRun 的逐阶段 artifact。
- 参数:`run_id: str`
- 返回:`list[{artifact_id, sequence, stage, trace_id, payload}]`

## finboard.ai.*(AI 草案,可追溯)

底层 `ResearchAssistant` 已强制 `assert_research_only_request`(拒绝实盘越权与注入)
与 `sanitize_prompt`(抹掉凭证)。MCP 层 `prompt` 参数脱敏后入审计。
AI 草案(`DraftStatus: proposed→approved→consumed/rejected`)可追溯但不再 block 写操作。

### finboard_ai_ask
金融问答(引用来源 / 声明不确定性)。
- 参数:`prompt: str`
- 返回:`data`(AnswerResult)+ `provenance`

### finboard_ai_propose_hypothesis
生成因子假设草案(可追溯,无需审批即可登记)。
- 参数:`prompt: str`
- 返回:`data`(FactorHypothesis 草案)+ `provenance`

### finboard_ai_propose_strategy_draft
生成无代码策略组件草案(受白名单约束,可追溯)。
- 参数:`prompt: str`

### finboard_ai_propose_strategy_diff
生成策略版本 diff 草案(可通过策略规格 API 正式化)。
- 参数:`prompt: str`

## finboard.memory.*(研究记忆,自动允许)

记忆工具只操作独立 `research_memories` 表,不触及实盘,也不创建 Run/回测/模拟盘,
因此自动允许。`source_refs` 只引用产物,**不修改产物本身**。

`source_refs` 元素结构:`{kind: str, ref_id: str, label?: str}`。
`kind` 常见值:`research_run` / `strategy` / `simulation` / `dataset` /
`experiment` / `hypothesis` / `factor` / `backtest`。

`memory_type`:`note` / `insight` / `correction` / `confirmation`。

### finboard_memory_remember
记住一条研究记忆(status=active)。
- 参数:`memory_type: str`、`content: str`、`source_refs?: list[dict]`、
  `tags?: list[str]`、`conversation_id?: str`
- 返回:完整记忆对象

### finboard_memory_list
列表查询(可选过滤)。
- 参数:`status?`、`memory_type?`、`source_kind?`、`source_ref?`、`tag?`、
  `conversation_id?`、`limit?: int = 100`
- 返回:`list[记忆对象]`

### finboard_memory_get
查询单条记忆详情(含 forgotten/archived)。
- 参数:`memory_id: str`

### finboard_memory_forget
软删除(status=forgotten,保留审计)。
- 参数:`memory_id: str`

### finboard_memory_correct
纠正:新建 active 记忆,旧记忆标记 forgotten,经 `supersedes_id` 形成纠正链。
`source_refs`/`tags` 不传则继承旧记忆。
- 参数:`memory_id: str`、`content: str`、`source_refs?`、`tags?`、`conversation_id?`

### finboard_memory_confirm
确认记忆有效(标记 `confirmed_by`/`confirmed_at`)。
- 参数:`memory_id: str`、`actor?: str`

### finboard_memory_archive
归档(status=archived)。
- 参数:`memory_id: str`

## Planned 工具(🔒 尚未实现,对应 issue)

以下工具尚未实现,列出契约供 agent 知晓未来能力边界。扩展顺序见
`packages/finboard-mcp/ROADMAP.md`。

### 🔒 #124 数据查询工具 `finboard.data.*`
- `finboard_data_instruments` —— 列出/搜索标的元数据(代码 / 名称 / 市场)
- `finboard_data_datasets` —— 列出已发布数据集(版本 / 状态 / checksum)
- `finboard_data_releases` —— 查询数据发布版本
- `finboard_data_cache_status` —— 行情缓存覆盖度与质量
- `finboard_data_quality` —— 数据质量报告(缺失 / 异常)

### 🔒 #125 因子工具 `finboard.factor.*`
- `finboard_factor_catalog` —— 因子目录(白名单候选池)
- `finboard_factor_snapshot` —— 因子快照(版本化,可追溯)
- `finboard_factor_signal` —— 信号计算(无代码规格驱动)
- `finboard_factor_experiment` —— 因子实验(登记 / 机器验证终态绑定)

### 🔒 #126 策略规格工具 `finboard.strategy.*`
- `finboard_strategy_registry` —— 策略注册表
- `finboard_strategy_template` —— 无代码策略模板
- `finboard_strategy_validate` —— 策略规格校验(schema / 白名单)
- `finboard_strategy_draft` —— 策略草案(agent 可自主生成)
- `finboard_strategy_publish` —— 发布策略版本(版本化,不可变)

### 🔒 #127 回测 + 模拟盘 + 研究运行工具
- `finboard_backtest_*` —— 回测(行情回放 + 纸面撮合 + 绩效分析)
- `finboard_simulation_*` —— 模拟盘(独立 simulation_* 表,持久化)
- `finboard_run_create` —— 创建 ResearchRun(agent 可自主执行)

### 🔒 #128 portfolio 计算工具 `finboard.portfolio.*`
- `finboard_portfolio_allocate` —— 目标仓位生成
- `finboard_portfolio_sizing` —— 资金分配 / 三档可行性
- `finboard_portfolio_feasibility` —— 硬约束 + 风险贡献上限
- `finboard_portfolio_attribution` —— 归因分析

> 所有 planned 工具同样遵守权限边界:研究写操作 agent 自主执行,
> 不触及实盘 broker / 账户 / 订单 / 持仓 / Kill Switch。
