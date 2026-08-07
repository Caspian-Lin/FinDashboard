# FinBoard MCP 工具契约(详细)

本文件列出 `finboard-researcher` agent 可用的全部 `finboard.*` MCP 工具。
所有工具返回统一信封 `ToolEnvelope`:
`operation_id` / `status`(ok|denied|error|pending_approval) / `data` /
`error` / `provenance` / `idempotency_key`。

## 权限矩阵

| 类别 | 自动允许 | 审批门 | 永久不可用 |
|------|---------|--------|-----------|
| 只读查询 | ✓ | | |
| 研究记忆 | ✓ | | |
| AI 草案(假设/策略) | | ✓(产出草案,需人工审批) | |
| 创建 Run/回测/模拟盘 | | ✓(pending_approval) | |
| 实盘(下单/撤单/持仓/Kill Switch/broker/凭证) | | | ✗ |

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

## finboard.ai.*(草案,需人工审批)

底层 `ResearchAssistant` 已强制 `assert_research_only_request`(拒绝越权与注入)
与 `sanitize_prompt`(抹掉凭证)。MCP 层 `prompt` 参数脱敏后入审计。

### finboard_ai_ask
金融问答(引用来源 / 声明不确定性)。
- 参数:`prompt: str`
- 返回:`data`(AnswerResult)+ `provenance`

### finboard_ai_propose_hypothesis
生成因子假设草案(需人工审批后登记)。
- 参数:`prompt: str`
- 返回:`data`(FactorHypothesis 草案)+ `provenance`

### finboard_ai_propose_strategy_draft
生成无代码策略组件草案(受白名单约束,需人工审批)。
- 参数:`prompt: str`

### finboard_ai_propose_strategy_diff
生成策略版本 diff 草案(需审批后通过策略规格 API 正式化)。
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
