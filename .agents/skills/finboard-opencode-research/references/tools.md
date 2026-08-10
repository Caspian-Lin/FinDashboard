# FinBoard MCP 工具契约(详细)

本文件列出 `finboard-researcher` agent 可用的全部 `finboard.*` MCP 工具。
所有工具返回统一信封 `ToolEnvelope`:
`operation_id` / `status`(ok|denied|error) / `data` /
`error` / `provenance` / `idempotency_key`。

当前已实现 50 个工具(✅);planned 工具(🔒 #127-#128)尚未实现,
列出契约供 agent 知晓未来能力边界。

## 权限矩阵(#122:研究写操作自主执行)

| 类别 | 只读/自主 | 永久不可用 |
|------|----------|-----------|
| 只读查询(run.* / instrument.* / dataset.* / data.* / tushare.*) | ✅ | |
| 研究记忆(memory.*) | ✅ | |
| AI 草案(ai.*:假设/策略) | ✅(产出草案,可追溯) | |
| 因子实验室(factor.* / feature_snapshot.*,✅ #125) | ✅ | |
| 策略规格(strategy.* / preset.*,✅ #126) | ✅(含 validate/draft/publish/rollback) | |
| 回测 / 模拟 / portfolio | 🔒 #127-#128(扩展中) | |
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

## finboard.instrument.* / finboard.dataset.* / finboard.data.* / finboard.tushare.*(✅ #124 只读)

数据查询工具,复用现有 repository / domain 类。只读,自动允许。

### finboard_instrument_list
列出标的元数据(分页 / 搜索)。
- 参数:`market?` / `instrument_type?` / `exchange?` / `listing_board?: list[str]` /
  `status?: str = "active"`(传 None 查全部)/ `q?`(代码或名称模糊)/
  `limit?: int = 200` / `offset?: int = 0`
- 返回:`{items: list[{code, name, market, instrument_type, exchange, listing_board,
  list_date, delist_date, status, sector, industry}], total, limit, offset}`

### finboard_instrument_get
查询单个标的详情(按代码)。
- 参数:`code: str`
- 返回:标的 dict(同上 item 结构);未找到 → `not_found`

### finboard_instrument_search
模糊搜索标的(按代码或名称,仅 active)。
- 参数:`q: str` / `limit?: int = 50`
- 返回:`list[标的 dict]`

### finboard_dataset_release_list
列出已发布的研究数据集版本(版本化、时点安全、不可变)。
- 参数:`dataset_name?` / `source?` / `quality_status?("passed"|"warnings")` /
  `limit?: int = 50`
- 返回:`list[{release_id, dataset_name, source, version, period, symbol_count,
  row_count, coverage_pct, capabilities, quality_status, ...}]`

### finboard_dataset_release_get
查询数据集发布详情(含逐标的覆盖、资产规则、能力缺口)。
- 参数:`release_id: str`
- 返回:完整 release manifest + symbol_count/row_count/coverage_pct;未找到 → `not_found`

### finboard_dataset_manifest_list
列出数据集发布清单(dataset manifests)。
- 参数:`dataset_name?` / `quality_status?` / `limit?: int = 50`
- 返回:`list[{id, dataset_name, source, version, start_date, end_date, row_count,
  symbol_count, coverage_pct, quality_status, quality_report, published_at, ...}]`

### finboard_data_cache_status
查询行情缓存状态(分页)。扫描 `data_cache/*.parquet`,只读 Parquet footer。
- 参数:`limit?: int = 200` / `offset?: int = 0` / `q?` / `period?` / `adjust?` /
  `listing_board?: list[str]`
- 返回:`{items: list[{symbol, listing_board, period, adjust, bar_count, first_date,
  last_date, last_close, source}], total, limit, offset}`

### finboard_data_quality_check
检查已缓存行情数据的质量(缺失 / 异常 / 重复)。
- 参数:`symbols?: str`(逗号分隔,不传则检查全部)/ `adjust?: str = "qfq"`
- 返回:`list[{symbol, total_bars, anomaly_count, duplicate_count, sources,
  anomalies, passed, primary_source, error?}]`(逐 code 容错)

### finboard_tushare_quota
查询 Tushare API 配额状态。
- 参数:无
- 返回:`{date, requests_per_minute, daily_limit, used, remaining}`

## finboard.factor.* / finboard.feature_snapshot.*(✅ #125)

因子实验室工具(7 只读 + 4 写)。写操作尊重 `mcp_readonly_only` 开关。

### finboard_factor_catalog
查询因子目录(版本化实现清单,26 个 alpha/risk/market_input 因子)。
- 参数:`role?: str`(alpha|risk|market_input)
- 返回:`list[{name, version, role, preference, frequency, source_fields, economic_hypothesis, checksum, ...}]`
- 无 DB 依赖,直接返回内存目录。

### finboard_feature_snapshot_list
列出已发布的特征快照(版本化、时点化、不可变)。
- 参数:`dataset_release_id?: str`、`limit?: int = 100`
- 返回:`list[{snapshot_id, dataset_release_id, decision_at, checksum, observations, ...}]`

### finboard_feature_snapshot_get
查询单个特征快照详情(含完整 observations 因子值)。
- 参数:`snapshot_id: str`
- 返回:`{snapshot_id, ..., observations, checksum}`;未找到返回 `not_found`。

### finboard_feature_snapshot_create **[写]**
同步构建并发布价格特征快照(从冻结数据发布计算因子值)。
- 参数:`dataset_release_id: str`、`decision_at: str`(ISO datetime,必须在发布范围内)
- 写操作,`mcp_readonly_only=true` 时拒绝。大发布建议用异步 `job_start`。
- 返回:`{snapshot_id, ..., observations, checksum}`

### finboard_feature_snapshot_job_start **[写]**
异步启动特征快照计算任务。
- 参数:同 `feature_snapshot_create`
- 返回:`{job_id, status, progress_pct, ...}`(初始 status 为 queued/running)
- 轮询模式:调用后用 `job_status` 查询,直到 succeeded(含 snapshot_id)或 failed(含 error)。
- 同一时刻只允许一个任务排队/运行,冲突返回 `conflict`。

### finboard_feature_snapshot_job_status
查询异步特征快照任务进度。
- 参数:`job_id: str`
- 返回:`{job_id, status(queued|running|succeeded|failed), progress_pct, elapsed_seconds, estimated_remaining_seconds, snapshot_id, error}`;未找到返回 `not_found`。

### finboard_factor_signal_list
列出因子信号(版本化、带研究状态)。
- 参数:`factor_name?: str`、`research_status?: str`(hypothesis|validated_oos|rejected)、`limit?: int = 100`
- 返回:`list[{signal_id, factor_name, factor_version, feature_snapshot_id, research_status, items, validation_experiment_id, ...}]`

### finboard_factor_signal_get
查询单个因子信号详情(含完整 items 逐标的信号)。
- 参数:`signal_id: str`
- 返回:`{signal_id, ..., items, checksum}`;未找到返回 `not_found`。

### finboard_factor_experiment_list
列出因子实验(冻结、带状态机和验证终态)。
- 参数:`status?: str`、`comparison_group?: str`、`limit?: int = 100`
- 返回:`list[{experiment_id, hypothesis, factor_names, plan, status, validation_experiment_id, result, failure_reason, ...}]`

### finboard_factor_experiment_get
查询单个因子实验详情(含 plan/result/failure_reason)。
- 参数:`experiment_id: str`
- 返回:`{experiment_id, ..., plan, result}`;未找到返回 `not_found`。

### finboard_factor_experiment_create **[写]**
冻结因子实验注册(不启动回测/模拟盘)。
- 参数:`hypothesis: str`、`factor_names: list[str]`、`dataset_release_id: str`、`feature_snapshot_id: str`、`plan: {in_sample_start, in_sample_end, oos_start, oos_end, trial_budget, benchmark_symbol, transaction_cost_bps, quantiles?}`、`comparison_group: str`、`validation_experiment_id?: str`
- snapshot 与 release 必须匹配,否则 `conflict`。
- 返回:`{experiment_id, ..., status: hypothesis}`

### finboard_factor_experiment_sync_validation **[写]**
同步因子实验的 #57 机器验证终态(不接受调用者传入 passed_oos)。
- 参数:`experiment_id: str`
- 读取绑定的 validation_experiment_id 的 trial 结果,推进状态机。
- 返回:`{experiment_id, ..., status, result?}`;冲突返回 `conflict`。

### finboard_factor_experiment_sync_validation **[写]**
同步因子实验的 #57 机器验证终态(不接受调用者传入 passed_oos)。
- 参数:`experiment_id: str`
- 读取绑定的 validation_experiment_id 的 trial 结果,推进状态机。
- 返回:`{experiment_id, ..., status, result?}`;冲突返回 `conflict`。

## finboard.strategy.* / finboard.preset.*(✅ #126)

策略规格工具(8 只读 + 8 写)。无代码版本化规格生命周期,写操作尊重
`mcp_readonly_only` 开关。**不接受 Python 源码 / 模块路径 / 可执行表达式**——
规格由白名单组件组合而成,经 Pydantic schema 与编译器双重校验。

### finboard_strategy_registry
查询策略规格注册表(可用策略 kind / 特征源 / 算子 / 生命周期阶段)。
- 参数:无
- 返回:`{strategies, feature_sources, operators, lifecycle_stages,
  publication_starts_run: false, accepts_python: false}`
- 无 DB 依赖。

### finboard_strategy_template
生成指定 kind 的策略规格模板(无代码起点)。
- 参数:`kind: str`、`strategy_id: str`、`dataset_release_ids: list[str]`
- 返回:完整 `ResearchStrategySpec` payload;未知 kind → `invalid_argument`

### finboard_strategy_list
列出策略规格(每个 strategy_id 的最新版本)。
- 参数:`limit?: int = 100`
- 返回:`list[{strategy_id, version, status, checksum, spec, ...}]`

### finboard_strategy_history
查询指定策略的全部版本历史。
- 参数:`strategy_id: str`
- 返回:`list[版本]`;策略不存在 → `not_found`

### finboard_strategy_version_get
查询单个版本详情。
- 参数:`strategy_id: str`、`version: int`
- 返回:`{strategy_id, version, ..., spec, checksum}`;未找到 → `not_found`

### finboard_strategy_diff
计算两个版本间的结构化 diff。
- 参数:`strategy_id: str`、`from_version: int`、`to_version: int`
- 返回:`{strategy_id, from_version, to_version, changes: [{path, before, after}]}`;
  版本不存在 → `not_found`

### finboard_preset_list
列出全部策略参数预设(内置策略 kind 的已保存参数组合)。
- 参数:无
- 返回:`list[{id, name, strategy, params, selection, created_at, updated_at}]`

### finboard_preset_get
查询单个策略参数预设。
- 参数:`preset_id: int`
- 返回:预设 dict;未找到 → `not_found`

### finboard_strategy_validate **[写]**
纯计算:编译/校验策略规格,**不持久化**。agent 反复修改规格 → validate 预览 →
满意后 `draft_create`。
- 参数:`spec: dict`、`disabled_factors?: list[str]`
- 返回:`{valid, checksum, feature_order, required_factor_sources,
  required_datasets, dataset_release_ids, lifecycle_stages, can_execute}`

### finboard_strategy_draft_create **[写]**
保存策略规格草稿版本(change_type=create,首版本)。
- 参数:`spec: dict`、`expected_version?: int`
- 先 validate 再持久化;版本冲突 → `conflict`

### finboard_strategy_supersede **[写]**
为已存在的策略创建后继草稿(change_type=supersede)。
- 参数:`strategy_id: str`、`spec: dict`、`expected_version: int`
- path strategy_id 必须与 spec.strategy_id 一致,否则 `invalid_argument`

### finboard_strategy_publish **[写]**
发布策略规格的指定版本(draft→published)。
- 参数:`strategy_id: str`、`version: int`、`expected_version: int`
- 发布前重新编译校验;版本不存在 → `not_found`,状态转换非法 → `conflict`

### finboard_strategy_rollback **[写]**
回滚策略规格到指定目标版本(change_type=rollback)。
- 参数:`strategy_id: str`、`target_version: int`、`expected_version: int`
- 回滚前重新编译校验目标;目标不存在 → `not_found`

### finboard_preset_create **[写]**
创建策略参数预设(参数经对应策略 params_model 校验)。
- 参数:`name: str`、`strategy: str`、`params?: dict`、`selection?: dict`
- 名称唯一;冲突 → `conflict`;未知 kind / 参数非法 → `invalid_argument`

### finboard_preset_update **[写]**
更新策略参数预设(仅传入字段更新)。
- 参数:`preset_id: int`、`name?`、`strategy?`、`params?`、`selection?`
- 预设不存在 → `not_found`;名称冲突 → `conflict`

### finboard_preset_delete **[写]**
删除策略参数预设。
- 参数:`preset_id: int`
- 返回:`{deleted: true, preset_id}`;未找到 → `not_found`

## Planned 工具(🔒 尚未实现,对应 issue)

以下工具尚未实现,列出契约供 agent 知晓未来能力边界。扩展顺序见
`packages/finboard-mcp/ROADMAP.md`。

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
