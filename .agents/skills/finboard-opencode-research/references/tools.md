# FinBoard MCP 工具契约(详细)

本文件列出 `finboard-researcher` agent 可用的全部 `finboard.*` MCP 工具。
所有工具返回统一信封 `ToolEnvelope`:
`operation_id` / `status`(ok|denied|error) / `data` /
`error` / `provenance` / `idempotency_key`。

当前已实现 113 个工具(✅)。所有工具遵守权限边界:研究写操作 agent 自主执行,
不触及实盘 broker / 账户 / 订单 / 持仓 / Kill Switch。

## 权限矩阵(#122:研究写操作自主执行)

| 类别 | 只读/自主 | 永久不可用 |
|------|----------|-----------|
| 只读查询(run.* / instrument.* / dataset.* / data.* / tushare.*) | ✅ | |
| 研究记忆(memory.*) | ✅ | |
| 因子实验室(factor.* / feature_snapshot.*,✅ #125) | ✅ | |
| 策略规格(strategy.* / preset.*,✅ #126) | ✅(含 validate/draft/publish/rollback) | |
| 回测(backtest.*,✅ #127) | ✅(含同步运行 / 历史 CRUD) | |
| 模拟盘(sim.*,✅ #127+#139) | ✅(账户/会话/决策/行情投递/评估/归档/订单/报告) | |
| ResearchRun 生命周期(run.* 写,✅ #127) | ✅(queue/cancel/replay/lineage) | |
| portfolio(portfolio.*,✅ #128) | ✅(纯计算:allocate/sizing/feasibility/attribution) | |
| 后台任务队列(job.*,✅ #136) | ✅(list/get 只读 + enqueue/cancel 写) | |
| 数据写操作(data_write.* / etf.*,✅ #137) | ✅(拉取/同步/发布/修复/ETF/配置) | |
| #57 验证实验(validation_experiment.*,✅ #138) | ✅(create/reject/add_trial/delete 写) | |
| 自选股(watchlist.*,✅ #140) | ✅(create/update/delete/add_symbols/remove_symbol 写) | |
| 报告聚合与导出(report.*,✅ #141) | ✅(3 只读:聚合 run/backtest + 导出 CSV/Markdown 文件) | |
| 实盘(下单/撤单/持仓/Kill Switch/broker/凭证) | | ✗ |

## finboard.run.*(只读 + ✅ #127 写)

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

### finboard_run_queue(✅ #127 + #170,写)
冻结输入 + 登记 queued ResearchRun(**不执行回测**,执行由离线 worker 完成)。
issue #170 起 `multi_factor` 已发布规格可由 worker 端到端执行
(信号引擎 + 组合流水线 → 14 stage artifacts → COMPLETED);其余 strategy
kind(etf_rotation / mean_reversion / convertible_double_low / futures_tsmom /
ma_cross)仍报 not_implemented。执行失败(如快照缺因子源)时 `finboard_run_get`
可见 error_code / error_summary,`research_runs` 不停留在 QUEUED。
- 参数:`payload: dict`(字段:idempotency_key / strategy_id / strategy_version /
  dataset_release_ids / factor_snapshot_ids / parameters / validation_config /
  portfolio_config / risk_config / execution_config / fee_config /
  benchmark_config / code_version / initial_capital / requested_by)
- 返回:ResearchRun 详情(含 manifest)
- 错误:`invalid_argument`(schema 校验 / 数据发布不匹配)、`not_found`(策略规格
  版本不存在)、`conflict`(策略未发布 / 幂等冲突)

### finboard_run_cancel(✅ #127,写)
取消 ResearchRun(queued/running/interrupted/failed → cancelled)。
- 参数:`run_id: str`
- 返回:更新后的 ResearchRun 详情
- 错误:`conflict`(非法状态转换)

### finboard_run_replay(✅ #127,写)
复制 completed ResearchRun 为新 queued 运行(**不执行**)。
- 参数:`run_id: str`、`idempotency_key: str`、`requested_by: str`
- 返回:新 ResearchRun 详情(`replay_of_run_id` 指向源)
- 错误:`not_found`(源不存在)、`conflict`(源未 completed / 幂等冲突)

### finboard_run_lineage
查询某 ResearchRun 内指定 trace_id 的 artifact 血缘(BFS 向上遍历 parent)。
- 参数:`run_id: str`、`trace_id: str`
- 返回:`{run_id, leaf_trace_id, artifacts: [{artifact_id, stage, trace_id, parent_trace_ids, payload, ...}]}`
- 错误:`not_found`(trace 不存在)

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

## finboard.data_write.* / finboard.etf.*(✅ #137,数据准备闭环)

数据写操作工具(2 只读 + 10 写)。补全 #124 只读数据查询之外的数据准备能力,
打通「数据→因子→策略」闭环第一步:agent 能拉 K 线、发布数据集、修复质量缺陷、
同步 ETF 元数据、读写调度器配置。写操作尊重 `mcp_readonly_only` 开关。
不连 broker / 账户 / 订单 / 持仓。

任务化工具(fetch_all / sync_universe / bulk_download_start / quality_repair /
dataset_release_publish)登记 `queued` 任务返回 `job_id`,实际执行由 worker 消费;
进度 / 状态 / 取消统一用 `finboard_job_get(job_id)` / `finboard_job_cancel(job_id)`
轮询(#136)。idempotency_key 与 REST 语义端点完全一致,因此 agent 与 REST 提交
同一任务会命中同一 job_id。

### finboard_data_fetch **[写,同步]**
拉取单个标的的日 K 线并写入缓存(主源失败自动用备用源重试)。不进队列。
- 参数:`symbol` / `start` / `end`(ISO 日期)/ `adjust?`(默认 qfq)/ `source?`
- 返回:`{symbol, bar_count, first_date, last_date, source, fallback_used,
  fallback_source, lifecycle_events, lifecycle_sync_failed, lifecycle_sync_error}`
- 错误:`invalid_argument`(未知行情源)/ `unavailable`(主源及备用源均不可用)

### finboard_data_fetch_all **[写,任务化]**
登记标的池批量缓存更新任务(symbols.yaml),返回 202 + `job_id`(不等待执行)。
- 参数:无
- 返回:`JobOut`(`kind=fetch_all`,`queue=data`)+ `created`(首次提交 true / 幂等命中 false)
- 进度:用 `finboard_job_get(job_id)` 轮询

### finboard_data_sync_universe **[写,任务化]**
登记全市场标的同步任务(akshare 发现 → 写 instruments 表),返回 202 + `job_id`。
- 参数:无
- 返回:`JobOut`(`kind=data_sync`)
- 进度:用 `finboard_job_get(job_id)` 轮询

### finboard_data_bulk_download_start **[写,任务化]**
登记批量历史数据拉取任务(按市场/类型/交易所筛选),返回 202 + `job_id`。
- 参数:`market?`(默认 a_share)/ `instrument_type?` / `exchange?` /
  `listing_boards?` / `start?`(默认 2015-01-01)/ `source?`
- 返回:`JobOut`(`kind=bulk_download`)
- 进度:用 `finboard_job_get(job_id)` 轮询(阶段如 `bulk_download:fetching`)

### finboard_data_quality_repair **[写,任务化]**
登记批量缓存异常 bar 修复任务(读缓存→质量检查→拉取修复→重写),返回 202 + `job_id`。
- 参数:`symbols`(标的代码列表,必填)/ `source?`(默认 akshare)/ `adjust?`(默认 qfq)
- 返回:`JobOut`(`kind=quality_repair`)
- 错误:`invalid_argument`(symbols 为空)
- 进度:用 `finboard_job_get(job_id)` 轮询

### finboard_dataset_release_publish **[写,任务化 ⭐ 研究闭环关键节点]**
登记数据集冻结发布任务(原子 rename + DB 登记 + 标的资产类型校验),返回 202 + `job_id`。
成功后 `result_ref=release_id`,用 `finboard_job_get` 拿到 release_id 后再
`finboard_dataset_release_get` 查发布详情。**这是研究闭环关键节点——agent 不发布
数据集就无法排队研究运行。**
- 参数:`release_id` / `symbols`(列表)/ `version` / `start_date` / `end_date` /
  `dataset_name?`(默认 multi_asset_daily_bars)/ `release_kind?`
  (a_share_tushare|multi_asset_mixed,默认 a_share_tushare)/ `source?` /
  `adjustment?`(qfq|hqfq|none,默认 qfq)/ `required_capabilities?`
  (stock|bond|convertible|futures|etf:index|etf:cross_border|etf:commodity|etf:bond)
- 返回:`JobOut`(`kind=dataset_publish`)
- 错误:`invalid_argument`(schema 校验:release_id/version pattern、symbols 非空不重复、
  日期顺序)/ `conflict`(幂等冲突)

### finboard_data_config_get(只读)
查询定时任务调度器配置(读 `data_config.json`)。
- 参数:无
- 返回:`{sync_enabled, sync_time, download_enabled, download_time,
  download_lookback_days, download_markets, download_types, data_provider}`

### finboard_data_config_update **[写]**
更新定时任务调度器配置(合并写入 `data_config.json`,只更新非空字段)。
- 参数:`sync_enabled?` / `sync_time?` / `download_enabled?` / `download_time?` /
  `download_lookback_days?` / `download_markets?` / `download_types?`(均可选)
- 返回:更新后的完整配置(同 `config_get` 结构)

### finboard_etf_sync **[写]**
批量同步 ETF 元数据(akshare → 分类 → 写库 / 预览)。默认 `dry_run=True` 只预览。
- 参数:`dry_run?`(默认 True 只预览不写入,显式 False 才写库)/ `enrich_codes?`
  (额外拉单基金档案补充的标的列表)
- 返回:`{total, to_insert, to_update, skipped_override, needs_review,
  auto_adopted, dry_run}`
- 错误:`unavailable`(数据源依赖未安装 / 同步失败)

### finboard_etf_batch_confirm **[写]**
批量确认待复核 ETF(needs_review → manually_confirmed)。
- 参数:`codes`(ETF 代码列表,1-2000)/ `reason?`
- 返回:`{confirmed: 确认数量}`
- 错误:`invalid_argument`(codes 为空)

### finboard_etf_update **[写]**
人工修正研究用 ETF 多维分类(写审计流水,设 `manual_override=True`,后续自动同步不再覆盖)。
- 参数:`code` / `reason`(必填,变更理由)/ `execution_profile?`
  (domestic_equity_etf|cross_border_etf|bond_etf|money_market_etf|commodity_etf)/
  `underlying_market?`(domestic|hk|overseas|global)/ `strategy_type?`(index|active)/
  `underlying_index?`
- 返回:更新后的 ETF 元数据 dict(22 字段)
- 错误:`not_found`(instrument 不存在)/ `conflict`(非 ETF)

### finboard_etf_review_queue(只读)
查询 ETF 元数据待复核队列(默认查 needs_review)。
- 参数:`review_status?`(auto_adopted|needs_review|manually_confirmed|
  manually_overridden,默认 needs_review,传 None 查全部)/ `limit?`(1-2000,默认 200)
- 返回:`list[ETF 元数据 dict]`

## finboard.factor.* / finboard.feature_snapshot.*(✅ #125)

因子实验室工具(8 只读 + 4 写,共 12 个)。写操作尊重 `mcp_readonly_only` 开关。

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
异步登记特征快照计算任务到统一 `background_jobs` 队列(issue #136/#144 适配,
由独立 worker 消费,不再进程内执行)。
- 参数:同 `feature_snapshot_create`
- 返回:`JobOut`(`job_id, status=queued, progress_done/total, phase, result_ref, ...`)
- 轮询模式:调用后用 `feature_snapshot_job_status(job_id)` 或
  `finboard_job_get(job_id)` 查询,直到 succeeded(`result_ref=snapshot_id`)
  或 failed(含 `error_*`)。
- worker 单并发保证同一时刻只一个 feature_snapshot 任务;幂等冲突返回 `conflict`。

### finboard_feature_snapshot_job_status
查询特征快照任务进度(读持久化 `background_jobs` 表)。
- 参数:`job_id: str`
- 返回:`JobOut`(`status/progress_done/progress_total/phase/result_ref(=snapshot_id)/
  error_code/error_summary`);未找到返回 `not_found`。
- 等价于 `finboard_job_get(job_id)`,保留语义化命名。

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

## finboard.validation_experiment.*(✅ #138)

#57 机器验证实验(OOS 样本外验证)元数据 CRUD,暴露 REST
`/api/research/experiments` 的 6 个端点。与因子实验(登记簿)是**两套独立但
耦合的系统**:因子实验通过 `validation_experiment_id` 引用本批工具创建的 #57
实验,再经 `finboard_factor_experiment_sync_validation` 同步终态,补齐 OOS
过拟合控制闭环。实验执行由离线 ValidationRunner 完成(不在 MCP 内触发);
揭盲端点(`unseal-final`)未实现。写操作尊重 `mcp_readonly_only` 开关。

### finboard_validation_experiment_create **[写]**
创建 #57 机器验证实验 —— 假设 / 计划 / 门一次性冻结(创建后 hypothesis
不可修改,变更需新建实验并设 `supersedes_id`)。
- 参数:
  - `hypothesis: str`(≥10 字符)
  - `version_stamp: {matching_model_version, asset_rules_version,
    strategy_kind}`(必填);`factor_version?`、`dataset_versions?`、
    `selection_config?`
  - `plan: {mode(rolling|expanding), train_start, train_end,
    validation_start, validation_end, test_start, test_end}`(必填);
    `train_window_days?=504`、`test_window_days?=63`、`step_days?=63`、
    `trial_budget?=50`、`random_seed?=0`、`benchmark_symbol?`
  - `thresholds?: {min_in_sample_sharpe?=1.0, min_oos_sharpe?=0.5,
    max_oos_drawdown?=0.25, min_oos_calmar?=0.5,
    min_oos_information_ratio?=0.0, max_param_sensitivity_sharpe_drop?=0.5,
    min_pbo_pass?=True, max_pbo?=0.5, min_deflated_sharpe?=0.0,
    min_probabilistic_sharpe?=0.95}`
  - `robustness?: {neighbourhood_steps?=5, neighbourhood_relative_step?=0.1,
    cost_multipliers?=[1.0,2.0,3.0], slippage_stress_bps?=[0,5,10,20],
    execution_delay_bars?=[1,2],
    stress_phases?=[2018-Q4,2020-Q1,2022-Q1,2024-Q1]}`
  - `strategy_params_space?: dict`、`supersedes_id?: str`、`notes?: str`
- 返回:`{experiment_id, hypothesis, version_checksum, plan, thresholds,
  robustness, status: hypothesis, trials_used: 0, ...}`
- 错误:`invalid_argument`(schema 不完整 / 计划日期非法 / 假设过短)

### finboard_validation_experiment_list
列出 #57 机器验证实验(可选按状态过滤)。
- 参数:`status?: str`(hypothesis|in_sample|validated_oos|rejected|superseded)、
  `limit?: int = 100`
- 返回:`list[{experiment_id, hypothesis, version_checksum, plan, thresholds,
  robustness, status, trials_used, final_test_unsealed, ...}]`
- 错误:`invalid_argument`(非法状态)

### finboard_validation_experiment_get
查询单个 #57 验证实验详情 + **全部 trial(包括 FAILED / REJECTED —— 多重试验
修正需要真实试验总数)**。
- 参数:`experiment_id: str`
- 返回:`{experiment_id, ..., trials: [{trial_id, trial_index, parameters,
  status, failure_reason, ...}]}`;未找到返回 `not_found`。

### finboard_validation_experiment_reject **[写]**
主动拒绝 #57 实验(设置 REJECTED + 原因,不可回退)。
- 参数:`experiment_id: str`、`reason: str`(不能为空)
- 返回:更新后的实验;`validated_oos` / `superseded` 终态不可拒绝 →
  `conflict`(409 语义);未找到 → `not_found`。

### finboard_validation_experiment_add_trial **[写]**
手动登记一次 trial(不通过 runner 自动跑;外部 worker / CLI 跑完回测后回写
结果,失败也算试验)。
- 参数:`experiment_id: str`、`parameters: dict`、
  `status?: str = "candidate"`(candidate|running|selected|rejected|failed|
  skipped)、`failure_reason?: str`
- 返回:`{trial_id(前缀 {experiment_id}-mcp-), trial_index, parameters,
  status, ...}`;预算耗尽或终态实验 → `conflict`(409 语义);未找到 →
  `not_found`。

### finboard_validation_experiment_delete **[写]**
删除 #57 实验(级联删除全部 trial,不可恢复)。
- 参数:`experiment_id: str`
- 返回:`{deleted: true, experiment_id}`;未找到返回 `not_found`。

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

## finboard.backtest.*(✅ #127)

回测引擎(行情回放 + 纸面撮合)。复用 `BacktestEngine` +
`BacktestRunRepository` + `list_strategy_definitions`。

### finboard_backtest_strategy_list(只读)
列出支持回测的内置策略及参数 schema(`supports_backtest=true`)。
- 参数:无
- 返回:`list[{kind, name, description, supports_backtest, params: [...]}]`
- 仅 `ma_cross` 支持回测

### finboard_backtest_run **[写]**
同步运行回测,返回 metrics/equity/fills 并落库。纸面撮合,**不发真实订单**。
- 参数:`strategy: str`、`symbols: list[str]`、`start: str`、`end: str`、
  `capital: str = "100000"`、`adjust: str = "qfq"`、`params: dict`、
  `selection: dict`、`commission_rate: str`、`commission_min: str`、
  `stamp_tax_rate: str`、`slippage_bps: str`、
  `equity_mode: str = "summary"`(summary 降采样到 max_points 个关键点,首末
  点保留;full 返回完整曲线)、`max_points: int = 200`
- `selection.inputs_mode`(#173):
  - `research_db`(默认):从 research 数据表读 profile/daily_metrics/
    financial_indicators/industry_memberships
  - `bars`:纯价格因子(momentum/volatility_20d)从回测行情计算,不要求
    daily_metrics 发布;dataset 未发布降级为 snapshot warnings,不整日
    SKIPPED;ST/上市天数过滤在 instrument_profiles 缺失时降级不生效
  - `snapshot`:因子值直接来自冻结 FeatureSnapshot,需 `snapshot_ids:
    list[str]`;观测按 available_at <= decision_at 过滤
- 返回:`{run_id, metrics, equity_curve, equity_point_count, fills, summary,
  selection_snapshots(snapshot 含 warnings 降级提示), ...}`
- 错误:`invalid_argument`(策略不支持回测 / 参数校验失败 / equity_mode 非法 /
  snapshot 模式缺 snapshot_ids)、`permission_denied`(只读模式)、
  `unavailable`(数据源连接错误)

### finboard_backtest_history_list(只读)
列出最近回测历史记录(摘要,不含完整 equity/fills)。
- 参数:`limit?: int = 50`
- 返回:`list[{id, strategy, symbols, start, end, capital, metrics, ...}]`

### finboard_backtest_history_get(只读)
查询单条回测历史详情(equity 默认降采样,fills 支持分页)。
- 参数:`run_id: int`、`equity_mode: str = "summary"`、`max_points: int = 200`、
  `fills_limit: int | None`(不传返回全部)、`fills_offset: int = 0`
- 返回:`{id, ..., equity_curve, equity_point_count, fills, fills_total,
  fills_offset, summary, selection_snapshots, ...}`
- 错误:`not_found`、`invalid_argument`(equity_mode 非法)

### finboard_backtest_history_delete **[写]**
删除一条回测历史记录。
- 参数:`run_id: int`
- 返回:`{run_id, deleted: true}`
- 错误:`permission_denied`(只读模式)、`not_found`

## finboard.sim.*(✅ #127 + #139)

持久化模拟盘(独立 `simulation_*` 表,`SIM-` ID)。复用 `SimulationService` +
`SimulationRepository` + `simulation_schemas`。
**边界(#83)不变**:只接受结构化目标仓位(→ 生成订单),不直接创建订单 /
修改持仓;不连 broker;不自动晋级实盘。模拟域异常映射:
`SimulationNotFoundError`→`not_found`、`SimulationConflictError`/
`SimulationTransitionError`→`conflict`、`SimulationRiskError`→`invalid_argument`。

### finboard_sim_account_list(只读)
列出模拟账户。
- 参数:`limit?: int = 100`
- 返回:`list[{simulation_account_id, name, status, currency, cash, equity, ...}]`

### finboard_sim_account_get(只读)
查询单个模拟账户详情。ID 须以 `SIM-A-` 开头。
- 参数:`account_id: str`
- 错误:`invalid_argument`(ID 前缀)、`not_found`

### finboard_sim_account_create **[写]**
创建模拟账户。
- 参数:`name: str`、`initial_cash: str`(>0)、`actor: str`、`currency: str = "CNY"`
- 返回:`SimulationAccountOut`
- 错误:`invalid_argument`(校验失败)

### finboard_sim_session_list(只读)
列出模拟会话。
- 参数:`account_id?: str`、`status?: list[str]`、`limit?: int = 100`
- 返回:`list[SimulationSessionOut]`
- 错误:`invalid_argument`(未知 status)

### finboard_sim_session_get(只读)
查询单个模拟会话详情。ID 须以 `SIM-S-` 开头。
- 参数:`session_id: str`
- 错误:`invalid_argument`、`not_found`

### finboard_sim_session_create **[写]**
创建模拟会话(绑定已发布策略版本 + completed ResearchRun + 数据发布)。
- 参数:`simulation_account_id: str`(SIM-A-)、`strategy_id: str`、
  `strategy_version: int`、`validation_run_id: str`(RR-)、`data_release_id: str`、
  `source_mode: str`、`actor: str`、`matching?: dict`、`risk?: dict`、`clock_speed: str = "1"`
- 返回:`SimulationSessionOut`
- 错误:`invalid_argument`、`conflict`(账户非 active / 已有活动会话 / 策略未发布)

### finboard_sim_session_start **[写]**
启动模拟会话(created/paused → running)。
- 参数:`session_id: str`、`actor: str`
- 错误:`conflict`(非法状态转换)

### finboard_sim_session_pause **[写]**
暂停模拟会话(running → paused)。
- 参数:`session_id: str`、`actor: str`

### finboard_sim_session_stop **[写]**
停止模拟会话(→ stopped,撤全部活动单 + 重估)。
- 参数:`session_id: str`、`actor: str`

### finboard_sim_session_archive **[写]**
归档模拟会话(stopped → archived,账户同步归档,记 `session_archived` 审计)。
- 参数:`session_id: str`、`actor: str`
- 错误:`conflict`(非 stopped 状态转换);归档后仅 reset 可用

### finboard_sim_session_reset **[写]**
重置模拟会话(stopped/archived → 新账户 + 新会话,原会话不动,经
`reset_of_session_id` 关联)。
- 参数:`session_id: str`、`actor: str`、`initial_cash?: str`
- 返回:`{account: SimulationAccountOut, session: SimulationSessionOut}`

### finboard_sim_decision_submit **[写]** ⭐
提交结构化目标仓位决策(→ 模拟 runner 生成订单)。**agent 不直接创建订单**。
- 参数:`session_id: str`、`decision: dict`(字段:decision_id / source_run_id(RR-)/
  source_decision_id / targets[{symbol, target_quantity(期望总仓位,非增量),
  signal_trace_id, reason, ...}] / actor)
- 返回:`{decision: SimulationDecisionOut, orders: list[SimulationOrderOut], duplicate: bool}`
- 幂等:相同 decision_id + checksum 返回已有 + 其订单(duplicate=true)

### finboard_sim_market_event **[写]** ⭐
投递单条 OHLCV K 线驱动撮合(仅 running 会话接受)——**agent 推进模拟盘撮合
的唯一入口**。对应 `POST /api/simulation/sessions/:id/market-events`。
- 参数:`session_id: str`、`bar: dict`(字段:source_event_id(幂等)/symbol/market/
  period(`1d`)/timestamp(ISO 带时区)/open/high/low/close/volume/amount/
  actor/contract_id(期货必填))
- 返回:`SimulationProcessOut`:`{source_event_id, duplicate, fill_ids,
  rejected_order_ids, equity, clock_at}`
- 幂等:同 source_event_id + 同内容 → duplicate=true;同 id 不同内容 → conflict
- 错误:`conflict`(非 running 会话 / 市场时钟倒退 / checksum 冲突)、`invalid_argument`(bar 校验失败)

### finboard_sim_session_evaluate **[写]**
模拟晋级评估(仅 stopped 会话)。对应 `POST /api/simulation/sessions/:id/evaluate`。
- 参数:`session_id: str`、`actor: str`、`minimum_trading_days: int = 2`(≥2)
- 返回:`dict`:promotion_status(eligible/failed)、trading_days、max_drawdown、
  minimum_trading_days、automatic_live_promotion(**恒 false**,不自动晋级实盘/影子盘)
- 错误:`conflict`(非 stopped 会话)

### finboard_sim_order_cancel **[写]**
撤销模拟订单(running/paused 会话内的活动单)。
- 参数:`session_id: str`、`order_id: str`、`actor: str`
- 返回:`SimulationOrderOut`

### finboard_sim_orders(只读)
列出模拟会话的订单。
- 参数:`session_id: str`、`status?: list[str]`、`symbol?: str`、`limit?: int = 1000`

### finboard_sim_fills(只读)
列出模拟会话的成交。
- 参数:`session_id: str`、`limit?: int = 1000`

### finboard_sim_positions(只读)
列出模拟会话(账户)的持仓。
- 参数:`session_id: str`

### finboard_sim_ledger(只读)
列出模拟会话的账本流水。
- 参数:`session_id: str`、`limit?: int = 1000`

### finboard_sim_audit(只读)
列出模拟会话的审计事件。
- 参数:`session_id: str`、`limit?: int = 1000`

### finboard_sim_report(只读)
生成模拟会话绩效报告。
- 参数:`session_id: str`
- 返回:`dict`(含 initial_cash/final_equity/simulation_return/max_drawdown/
  order_count/fill_count/`simulation_is_not_return_proof=true`/
  `automatic_live_promotion=false`)

## finboard.portfolio.*(✅ #128)

组合计算(纯计算,无 DB 写入,无副作用)。复用 `finboard_backtest.portfolio`
(`build_portfolio` / `solve_sizing` / `evaluate_capital_tiers` /
`compute_attribution`)。输入参数与 REST `POST /api/portfolio/*` 一致。
归为研究写(经 `mcp_readonly_only` 门控),agent 可自主执行。

### finboard_portfolio_allocate **[写·纯计算]**
目标权重分配(equal_weight / inverse_volatility / erc)+ 约束 + 风险报告。
对应 `POST /api/portfolio/allocate`,调 `build_portfolio`。
- 参数:`signals: list[{symbol, score, confidence?}]`、`as_of: str`(ISO 日期)、
  `method: str = "equal_weight"`、`strategy_id? = "mcp"`、
  `max_weight_per_asset? = 0.25`、`max_weight_per_sleeve? = 0.40`、
  `min_cash_buffer? = 0.05`、`max_leverage? = 1.0`、`long_only? = true`、
  `target_volatility?`、`max_volatility?`、`rebalance_threshold? = 0.05`、
  `min_weight_to_trade? = 0.001`、`returns_by_ticker?: dict[str, list[float]]`、
  `sleeve_map?: dict[str,str]`、`disabled_symbols?: list[str]`、
  `current_weights?: dict[str,float]`、`target_gross_exposure?`、
  `betas?: dict[str,float]`、`max_drawdown? = 0.0`、
  `max_risk_contribution? = 1.0`、`conflict_policy? = "net"`、
  `covariance_failure_mode? = "fail_closed"`
- 返回:`{weights, weights_before_constraints, cash_buffer, gross_weight,
  net_weight, configured_max_leverage, n_assets, contract_version,
  covariance_shrinkage, covariance_fallback_used, adjustments, risk}`
- 错误:`invalid_argument`(signals 为空 / 日期非法 / 约束非法 /
  AllocationError / 协方差失败 fail_closed)、`permission_denied`(只读模式)

### finboard_portfolio_sizing **[写·纯计算]**
离散手数 sizing(目标权重 → 可执行手数 + 费用 / 保证金 / 滑点)。
对应 `POST /api/portfolio/sizing`,调 `solve_sizing`。
- 参数:`weights: dict[str,float]`、`as_of: str`、`capital: float`、
  `lot_info: list[{code, lot_size?, multiplier?, margin_rate?,
  commission_rate?, commission_min?, stamp_tax_rate?, slippage_bps?,
  max_participation?, available_volume?, tradable?, unavailable_reason?}]`、
  `prices: dict[str,float]`、`strategy_id? = "mcp"`、`max_leverage? = 1.0`、
  `long_only? = true`、`commission_rate? = 0.0003`、`commission_min? = 5.0`、
  `stamp_tax_rate? = 0.0005`
- 返回:`{trades, total_capital, cash_before, cash_after, est_commission,
  est_tax, total_turnover, n_active_trades, est_slippage, margin_required}`
- 错误:`invalid_argument`(TargetWeight / SizingError,如权重非法 / 标的缺价格)、
  `permission_denied`(只读模式)

### finboard_portfolio_feasibility **[写·纯计算]**
固定资金档位(10万/20万/50万)可行性评估。
对应 `POST /api/portfolio/feasibility`,调 `evaluate_capital_tiers`。
- 参数:`weights: dict[str,float]`、`as_of: str`、
  `lot_info: list[{...}]`(同 sizing)、`prices: dict[str,float]`、
  `strategy_id? = "mcp"`、`max_leverage? = 1.0`、`long_only? = true`
- 返回:`list[{tier, capital, feasible, cash_utilization, tracking_error,
  unfillable_symbols, capacity_pressure, margin_required, estimated_costs,
  reasons}]`(每档一项)
- 错误:`invalid_argument`(TargetWeight / SizingError)、
  `permission_denied`(只读模式)

### finboard_portfolio_attribution **[写·纯计算]**
绩效归因分解(需协方差)。
对应 `POST /api/portfolio/attribution`,调 `compute_attribution`。
- 参数:`weights_history: list[dict[str,float]]`、
  `returns_by_ticker: dict[str, list[float]]`(需 ≥30 观测值)、
  `sleeve_map?: dict[str,str]`
- 返回:`{by_asset, by_sleeve, total_return, total_risk, total_turnover,
  max_drawdown, cash_utilization, leverage_ratio}`
- 错误:`invalid_argument`(weights_history 为空 / 协方差估计失败)、
  `permission_denied`(只读模式)

## finboard.job.*(✅ #136)

统一后台任务队列监控与提交。复用 `BackgroundJobRepository` + `background_jobs`
表(`BJ-` ID,与实盘 orders/fills/positions 完全隔离)。**任务队列只服务
研究/数据/回测类任务**;实盘交易内核(盘前检查/收盘撤单/日终核对/Broker 心跳/
Kill Switch)由专用 Scheduler 执行,不进入统一队列。

`enqueue` 的 kind 白名单:`echo` / `research_run` / `feature_snapshot` /
`bulk_download` / `dataset_publish` / `backtest_run` / `data_sync` /
`fetch_all` / `quality_repair` / `research_data_sync`(全是研究/数据域,
不含实盘能力)。

`research_data_sync`(issue #171):research 数据表(估值 / 财务 / 行业)摄取
编排。payload:`{datasets?: [profiles, daily_metrics, financial_indicators,
industry_memberships](默认全部), start_date, end_date(ISO), symbols?:
[str]}`。逐标的接口自动限流(tushare_budget)并按确定性 dataset_version
断点续跑;预算耗尽退避重试,未配 token / 未装 SDK fail-fast。

写操作尊重 `mcp_readonly_only` 开关。

### finboard_job_list(只读)
列出后台任务(最近优先)。
- 参数:`kind?: list[str]`、`status?: list[str]`
  (queued|running|retry_waiting|succeeded|failed|cancel_requested|cancelled|
  interrupted)、`queue?: list[str]`、`limit?: int = 100`(1-500)
- 返回:`list[JobOut]`(`job_id/kind/queue/status/priority/payload/
  progress_done/progress_total/phase/result_ref/error_*/attempt/max_attempts/
  worker_id/heartbeat_at/lease_until/requested_by/created_at/started_at/
  finished_at/updated_at`)
- 错误:`invalid_argument`(未知 status)

### finboard_job_get(只读)
查询单个后台任务详情。
- 参数:`job_id: str`
- 返回:`JobOut`(成功后 `result_ref` 携带产物引用,如特征快照的 snapshot_id)
- 错误:`not_found`

### finboard_job_enqueue **[写]**
登记一个 queued 后台任务并立即返回 202 + job_id(不等待执行,由独立 worker 消费)。
- 参数:`kind: str`(白名单)、`idempotency_key: str`(8-128 字符)、
  `requested_by: str`、`queue?: str = "default"`、`payload?: dict`(任务参数,
  结构取决于 kind)、`priority?: int = 0`(-1000..1000)、`max_attempts?: int = 3`(1..10)
- 返回:`JobOut + created`(首次提交 true / 幂等命中 false)
- 错误:`permission_denied`(只读模式)、`invalid_argument`(kind 不在白名单 /
  schema 校验失败)、`conflict`(幂等冲突 / 重复 idempotency_key)
- kind payload 契约示例:
  - `feature_snapshot`:`{dataset_release_id, decision_at}` → `result_ref=snapshot_id`
  - `research_run`:`{run_id, strategy_kind}` → 与 `finboard_run_queue` 双写
  - `bulk_download`:`{market, source, start, instrument_type}`
  - `dataset_publish`:`{release_id, release_kind, symbols, version, start_date, end_date}`
  - `backtest_run`:`{request, provider_name}` → `result_ref=str(run_id)`
  - `research_data_sync`:`{datasets, start_date, end_date, symbols}` → 研究
    数据表摄取(batch 发布后 selection 可命中)

### finboard_job_cancel **[写]**
请求协作式取消后台任务(running → cancel_requested,executor checkpoint 时退出)。
- 参数:`job_id: str`
- 返回:更新后的 `JobOut`
- 已在终态(succeeded/failed/cancelled/interrupted)的任务返回当前状态不报错。
- 错误:`permission_denied`(只读模式)、`not_found`

## finboard.watchlist.*(✅ #140)

自选股标的组(watchlist)—— 用户标的组:保存常用回测标的集合,为回测 / 研究
准备标的池(前端 Backtest.tsx 已用 watchlist 作为回测标的选择器,#39)。
复用 `WatchlistRepository`(REST `routes/watchlist.py` 直接调用 repository,
无独立 service 层,MCP 与 REST 同层同行为)。`symbol_code` 是普通
`String(20)` 代码(如 `000001.SZ`),无外键约束指向 instruments;删除级联
(DB 外键 `ondelete=CASCADE`)。与 strategies / simulation 无关联,独立用户
管理查找列表。写操作尊重 `mcp_readonly_only` 开关。

### finboard_watchlist_list(只读)
列出全部标的组(含成员数)。
- 参数:无
- 返回:`list[{id, name, description, item_count, created_at}]`

### finboard_watchlist_get(只读)
获取标的组详情(含成员列表)。
- 参数:`watchlist_id: int`
- 返回:`{id, name, description, item_count, created_at, symbols: list[str]}`
- 错误:`not_found`(标的组不存在)

### finboard_watchlist_create **[写]**
创建标的组。
- 参数:`name: str`、`description?: str`
- 返回:`WatchlistOut`(item_count=0)
- 错误:`permission_denied`(只读模式)

### finboard_watchlist_update **[写]**
更新标的组名称 / 描述(**partial:只更新提供的字段,不传的字段保持原值**;
REST PUT 是全量语义,这里更安全)。
- 参数:`watchlist_id: int`、`name?: str`、`description?: str`(均可选)
- 返回:`WatchlistOut`
- 错误:`permission_denied`(只读模式)、`not_found`

### finboard_watchlist_delete **[写]**
删除标的组(DB 外键级联删除成员)。
- 参数:`watchlist_id: int`
- 返回:`{deleted: true, watchlist_id}`
- 错误:`permission_denied`(只读模式)、`not_found`

### finboard_watchlist_add_symbols **[写]**
向标的组添加标的(**输入按序自动去重 + 已存在自动跳过**,不触发
`(watchlist_id, symbol_code)` 唯一约束冲突)。
- 参数:`watchlist_id: int`、`symbols: list[str]`(如 `["000001.SZ", "510300.SH"]`)
- 返回:`WatchlistDetailOut`(含最新 symbols)
- 错误:`permission_denied`(只读模式)、`not_found`

### finboard_watchlist_remove_symbol **[写]**
从标的组移除单个标的(不存在时为空操作,与 REST 一致)。
- 参数:`watchlist_id: int`、`symbol_code: str`
- 返回:`WatchlistDetailOut`(含最新 symbols)
- 错误:`permission_denied`(只读模式)、`not_found`

## finboard.report.*(✅ #141)

报告聚合与导出(3 只读)。聚合逻辑复用 `ResearchRunRepository` /
`BacktestRunRepository`,渲染在 `finboard_mcp/reporting.py`(纯标准库 csv +
字符串模板,零新依赖;PDF 留后续 issue)。导出文件写入 `FINBOARD_EXPORT_DIR`
(缺省系统临时目录下 `finboard_exports`),返回绝对路径,不持久化到 DB。
模拟盘报告生成已有 `finboard_sim_report`,导出暂不覆盖。

### finboard_report_run(只读)
聚合单个 ResearchRun 报告:run 元信息 + result 指标(ResearchRunReport 扁平
字段)+ 全部 artifacts(含 report / equity / decisions 各阶段 payload)。
- 参数:`run_id: str`(RR-)
- 返回:`{run_id, status, strategy_id, strategy_kind, created_at,
  completed_at, metrics, artifact_count, artifacts: [{artifact_id, sequence,
  stage, decision_id, trace_id, checksum, payload}]}`
- 错误:`not_found`(研究运行不存在)

### finboard_report_backtest(只读)
聚合单条回测历史报告:运行元信息 + metrics + equity_curve + fills + summary
(标准化结构;equity 默认降采样,issue #172)。
- 参数:`run_id: int`、`equity_mode: str = "summary"`、`max_points: int = 200`
- 返回:`{run_id, strategy, symbols, start, end, capital, adjust, created_at,
  metrics, equity_curve: [{date, equity, benchmark?}], equity_point_count,
  fills: [{date, symbol, side, quantity, price, commission}], summary}`
- 错误:`not_found`(回测记录不存在)、`invalid_argument`(equity_mode 非法)

### finboard_report_export(只读)
把报告聚合后导出为文件,返回绝对路径 + 元信息。
- 参数:`kind: str`(run|backtest)、`id: str`(run_id 或回测记录 id)、
  `format: str`(csv|markdown)
- 返回:`{path(绝对路径), kind, id, format, size_bytes, lines}`
- CSV:UTF-8 BOM(Excel 打开中文不乱码),每节 `# 标题` 注释行 + 表头 + 行,
  节间空行;Markdown:`#` 标题 + `##` 分节表格
- 错误:`invalid_argument`(未知 kind / format / 非整数回测 id)、
  `not_found`(run/backtest 不存在)
