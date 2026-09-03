# FinBoard MCP 工具契约(详细)

本文件列出 `finboard-researcher` agent 可用的全部 `finboard.*` MCP 工具。
所有工具返回统一信封 `ToolEnvelope`:
`operation_id` / `status`(ok|denied|error) / `data` /
`error` / `provenance` / `idempotency_key`。

当前已实现 125 个工具(✅)。所有工具遵守权限边界:研究写操作 agent 自主执行,
不触及实盘 broker / 账户 / 订单 / 持仓 / Kill Switch。

## 权限矩阵(#122:研究写操作自主执行)

| 类别 | 只读/自主 | 永久不可用 |
|------|----------|-----------|
| 只读查询(run.* / instrument.* / dataset.* / data.* / tushare.*) | ✅ | |
| 研究记忆(memory.*) | ✅ | |
| 因子实验室(factor.* / feature_snapshot.*,✅ #125) | ✅ | |
| 策略规格(strategy.* / preset.*,✅ #126) | ✅(含 validate/draft/publish/rollback) | |
| 回测(backtest.*,✅ #127+#174+#175) | ✅(同步运行 + strategy_spec 路由 research_run + 批量网格提交/聚合 + 历史 CRUD) | |
| 模拟盘(sim.*,✅ #127+#139) | ✅(账户/会话/决策/行情投递/评估/归档/订单/报告) | |
| ResearchRun 生命周期(run.* 写,✅ #127) | ✅(queue/cancel/replay/lineage) | |
| portfolio(portfolio.*,✅ #128) | ✅(纯计算:allocate/sizing/feasibility/attribution) | |
| 后台任务队列(job.*,✅ #136+#221) | ✅(list/get 只读 + enqueue/cancel/archive/unarchive 写) | |
| 数据写操作(data_write.* / etf.*,✅ #137) | ✅(拉取/同步/发布/修复/ETF/配置) | |
| #57 验证实验(validation_experiment.*,✅ #138+#233) | ✅(create/reject/add_trial/delete/run 写) | |
| 自选股(watchlist.*,✅ #140) | ✅(create/update/delete/add_symbols/remove_symbol 写) | |
| 报告聚合与导出(report.*,✅ #141) | ✅(3 只读:聚合 run/backtest + 导出 CSV/Markdown 文件) | |
| 实盘(下单/撤单/持仓/Kill Switch/broker/凭证) | | ✗ |

## finboard.run.*(只读 + ✅ #127 写)

### finboard_run_list
列出 ResearchRun。
- 参数:`statuses?: list[str]`、`strategy_kind?: str`、`limit?: int = 50`
- 返回:`list[{run_id, strategy_id, strategy_kind, status, execution_mode, ...}]`
  (`execution_mode`: single_shot | multi_period,#183)

### finboard_run_get
查询单个 ResearchRun。
- 参数:`run_id: str` / `view?: "summary"|"detail" = "summary"`
- 返回(默认 summary,#206):`{run_id, status, ..., metrics(剔 equity_curve,
  附 equity_point_count), universe: {total, included, excluded_by_reason},
  fills: {total, by_decision}, artifact_count, execution_mode}` —— 不序列化
  manifest/result 全量
- 返回(view=detail):`{run_id, ..., manifest, result, error_summary, execution_mode}`
  (result 多期回放含 `annualized_return` 与 `equity_curve` 全区间每日权益曲线,#183;
  逐标的全量 payload 可达 MB 级,诊断用)
- 排障(#263):status=failed/rejected 时 error_summary 头部自带定位头
  `[stage=...; decision=...; decision_index=...; release=<bars 主发布>;
  dataset_releases=...]`(stage 为 `decision_load` 表示输入构建期失败,
  具体值如 `signals` 表示该 stage 持久化期失败;decision 为失败期次决策日),
  原始异常消息在尾部——先读头部定位「哪个决策点、哪个 stage、哪个发布」,
  再按需用 `finboard_run_artifacts` 下钻;超长错误经保头保尾截断,
  头部上下文仍完整可读。

### finboard_run_artifacts
列出某 ResearchRun 的逐阶段 artifact。
- 参数:`run_id: str`
- 返回:`list[{artifact_id, sequence, stage, trace_id, payload}]`

### finboard_run_queue(✅ #127 + #170 + #183,写)
冻结输入 + 登记 queued ResearchRun(**不执行回测**,执行由离线 worker 完成)。
issue #170 起 `multi_factor` 已发布规格可由 worker 端到端执行
(信号引擎 + 组合流水线 → 14 stage artifacts → COMPLETED);issue #218 起
`user_code`(沙箱策略代码,见「用户代码策略执行」节)同样端到端;
其余 strategy kind(etf_rotation / mean_reversion / convertible_double_low /
futures_tsmom / ma_cross)仍报 not_implemented。执行失败(如快照缺因子源)时 `finboard_run_get`
可见 error_code / error_summary,`research_runs` 不停留在 QUEUED。
issue #183 起 `parameters.rebalance_frequency`(monthly|quarterly)启用**多期
再平衡回放**:按冻结发布交易日历每期重算 universe/features/signals 与组合,
决策间每日 mark-to-market 产出全区间权益曲线与绩效指标(总收益/年化/夏普/
最大回撤),返回值与 report 标注 `execution_mode=multi_period`。**multi_period
必须显式声明 `rebalance_frequency`**;未声明即 single_shot,其决策时点**只能**
来自冻结因子快照 —— 缺快照入队秒级 `invalid_argument`(报错附
`execution_mode` 与缺失因子源,#203),非法频率值同样在入队时拒绝。
「多期不要求预建快照」**仅限**决策日推导与价格因子(momentum/volatility
等按发布每期重算);基本面因子(pb/ROE 等)仍 PIT 取自冻结快照 / 研究数据
发布(daily_metrics/financial_indicators,#187)。**multi_period 特征可用性
入队即判(#253)**:规格 identity 源必须可由多期供给派生(标准价格特征 /
`close` / attached 研究发布按 kind 派生的特征 / 快照观测),声明 pb/roe 等
财务因子而未附加对应研究数据发布 → 入队秒级 `invalid_argument`,报错具名
缺失特征与所需发布 kind —— 不再拖到执行期才报「identity 节点缺少数据源」。
研究数据发布的 manifest 冻结 `derived_features`(可派生特征集合),旧发布
按 kind 回退判定。
- 参数:`payload: dict`(JSON 对象,模板与取值来源,必填标 *):
  - `idempotency_key`*: 8-128 字符去重键;重提交返回同一 run
  - `strategy_id`*: 已发布策略规格 id(`finboard_strategy_list` / registry 查询)
  - `strategy_version`*: 整数 >=1(策略规格版本)
  - `dataset_release_ids`*: 冻结数据发布 release_id 列表,**必须与策略验证计划完全一致**
    (`finboard_dataset_release_list` 查询)
  - `factor_snapshot_ids`: 冻结特征快照 snapshot_id 列表(`finboard_feature_snapshot_list`
    查询);**single_shot 必填**(决策时点只能来自快照,缺快照入队即拒,#203);
    multi_period 声明频率后价格因子不需要,基本面因子仍需快照/研究数据发布
  - `parameters`: `{}` —— 不声明即 single_shot(需冻结快照);声明
    `rebalance_frequency=monthly|quarterly` 触发多期回放(#183),非法值入队即拒
  - `validation_config` / `execution_config` /
    `fee_config` / `benchmark_config`: `{}` —— 政策覆盖,一般留空
  - `portfolio_config` / `risk_config`: `{}` —— 组合约束 / 风险退出分区覆盖
    (#303,键位不可混):
    `portfolio_config` 管组合约束(overrides 直接就是键值),如
    `{"max_risk_contribution": 0.5}`(默认 0.35,合法域 0<值<=1,=1 关闭该
    约束;隐含买入池 n>=ceil(1/值))、`risk_factor_limits`(#266,可声明风险
    因子 active 暴露上限 `[{factor, max_active_exposure}]`,因子名与冻结特征
    feature_id 同名,如 market_beta / size_exposure / 行业 one-hot 列名,
    暴露缺失降级为具名 warning,不可满足执行期 fail-closed);
    `risk_config` 管风险退出(stop-loss 等),形态 `{"rules": [{"rule_type":
    "price_stop_loss", "enabled": true, "threshold": 0.08}]}`,按 rule_type 与
    规格策略同名合并、覆盖同名键(未声明字段继承基准值;新 rule_type 须给
    全字段含 rationale);manifest 原样冻结,覆盖只发生在消费端
  - `code_version`*: 7-64 字符,**本 run 自身的代码版本标识**(如 FinBoard git
    commit),冻结进 manifest/checksum 供追溯;**与数据集发布的 code_version 同名
    但互不校验**,别拿数据集 git hash 顶替
  - `initial_capital`*: 100000-500000 数字
  - `requested_by`*: 归属人(如 `user:xxx` / `agent:mcp`)
  - `actor_type`: `"human"`(固定;LLM 不能触发运行)
  (`parameters.rebalance_frequency ∈ {monthly, quarterly}` 时 multi_period)
- 返回:精简回执(#206)`{run_id, job_id, strategy_id, strategy_kind, status,
  checksum(manifest_checksum), execution_mode, created_at, view: "ack"}`;
  全量详情走 `finboard_run_get(run_id)`(含 manifest 与 execution_mode)
- **入队预检(issue #186)**:入队时对主数据发布的 instruments 做 universe
  候选池非空校验(与 data_sync 后的元数据状态一致);空池秒级
  `invalid_argument`,错误信息附各过滤条件的排除统计(如
  `listing_age_below_minimum=512`)与缺失字段名(如 `list_date`),不再等执行期
  跑完后报泛化错误。写策略后先 `finboard_strategy_validate` 看
  `universe_precheck`,再入队即可避免这类空转。
  **评估域(#254)**:声明 `universe.explicit_symbols` 时预检只评估
  explicit ∩ 发布标的(`total_candidates` 即交集规模),全市场评估仅在未声明
  explicit 时进行;声明但发布中缺失的标的发具名 warning
  `universe_explicit_symbol_missing`,不静默忽略。
- **组合可行性预检(issue #303)**:入队解析生效 `max_risk_contribution`
  (portfolio_config.overrides 可覆盖,默认 0.35)后,静态候选池
  `< ceil(1/阈值)` 秒级 `invalid_argument`(默认 0.35 隐含买入池 >=3,
  候选只有 2 只的 screen run 此前会白跑 1-2 小时才在组合阶段 REJECTED),
  错误附排除统计、生效阈值与 portfolio_config 键位修复路径;
  `max_risk_contribution` 非法值与 `risk_config.overrides` 形态错误同样入队
  即拒。逐期真实买入池入队期不可精确预知,运行期 fail-closed 兜底不变。
- 错误:`invalid_argument`(schema 校验 / 数据发布不匹配 / rebalance_frequency
  非法 / 候选池为空 / 组合可行性预检失败 #303)、`not_found`(策略规格版本不存在)、`conflict`(策略未发布 / 幂等冲突)

### finboard_run_cancel(✅ #127,写)
取消 ResearchRun(queued/running/interrupted/failed → cancelled)。
- 参数:`run_id: str`
- 返回:更新后的 ResearchRun 详情
- 错误:`conflict`(非法状态转换)

### finboard_run_replay(✅ #127,写;#305 放开 interrupted)
复制 completed 或 interrupted ResearchRun 为新 queued 运行(**不执行**)。
- 参数:`run_id: str`、`idempotency_key: str`、`requested_by: str`
- 返回:新 ResearchRun 详情(`replay_of_run_id` + manifest `replay_source_status`
  标注血缘;REST 等价入口 `POST /api/research/runs/{run_id}/replay`)
- **interrupted 即事故恢复通道(#305)**:run 被标 interrupted(error_summary
  指向本工具)后,**一条命令按冻结输入恢复** —— 新 run 自动继承原 manifest
  全部冻结输入(dataset_release_ids / factor_snapshots 全部 ID,零手工重填),
  `input_checksum` 与源一致;源 run 不被复活,也不复活原 job
  (job 侧自动恢复由 `requeue_due` 覆盖)
- completed 源保持确定性重放对照(结果漂移判 `non_deterministic_replay`);
  cancelled 是显式用户意图,拒绝重放
- 错误:`not_found`(源不存在)、`conflict`(源状态不可重放(仅
  completed/interrupted 可重放)/ 幂等冲突)

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

**保留 tags(#268 协议约定)**:`research-plan` / `research-round` /
`finding-confirmed` / `finding-refuted` —— 跨会话结论检索入口(会话启动协议
按 tag 过滤;轮次收尾与结论登记必带,详见 `memory.md` / `workflow.md`)。
`tags` 本身是自由字段,以上四个值有协议含义,不挪作他用。

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
查询数据集发布详情。
- 参数:`release_id: str` / `view?: "summary"|"detail" = "summary"` /
  `symbols?: list[str]`(≤500 只,#238)
- 返回(默认 summary,#206):头部字段 + capabilities + 覆盖统计
  (symbol_count/row_count/coverage_pct),**不含逐标的 instruments 数组**
  (全市场发布可达几十 MB,防截断)
- 返回(view=detail):完整 as_dict()(含逐标的覆盖、资产规则、能力缺口,诊断用)
- 返回(传 symbols):summary + `symbol_check {requested, matched, missing}`
  成员核对(判断 N 只标的是否在发布内,**免拉全量 detail**);超 500 只 → `invalid_argument`;
  未找到 → `not_found`

### finboard_dataset_release_diff
两份发布标的集 diff(#252,发布后自检)。
- 参数:`release_id: str` / `other_release_id: str` / `preview_limit?: int = 200`(1-1000)
- 返回:`{symbol_count_a, symbol_count_b, common_count, only_in_release_count,
  only_in_other_count, only_in_release, only_in_other, preview_limit, truncated,
  consistent}`——计数精确,差集字典序有界预览
- 用途:研究数据发布(如 financial_indicators)发布后 vs 同区间 bars 主发布
  核对并集一致性;不一致时用 `finboard_dataset_release_publish` 带
  `consistency_baseline_release_id` 重发布(差集具名;`consistency_fail_on_mismatch=true`
  秒级失败 code=symbol_set_mismatch)。执行期对缺标的容忍(具名 warning,
  因子值 null),发布期核对可提前拦截
- 未找到 → `not_found`
- 提示:重发布时标的集用 `symbols_from_release` 复制基线发布(#261),
  免手工重抄全市场清单——复制语义保证与基线一致,消灭漏配缺口来源

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
自动包含基准指数登记(#256):`instrument_type=index`(受控登记表
`BENCHMARK_INDEX_REGISTRY`,含沪深300/中证500/中证1000/创业板指等 9 只);
#265 起同时从东财可转债一览登记 `instrument_type=convertible`
(11xxxx.SH/12xxxx.SZ,存续转债,list_date 由 convertible_profiles 回填);
#267 起同时从受控登记表登记 IF/IH/IC/IM 期货主连 `instrument_type=futures`
(market=future,主连仅研究信号/基准、不可当作可成交合约)。
- 参数:无
- 返回:`JobOut`(`kind=data_sync`)
- 进度:用 `finboard_job_get(job_id)` 轮询

### finboard_data_bulk_download_start **[写,任务化]**
登记批量历史数据拉取任务(按市场/类型/交易所筛选),返回 202 + `job_id`。
- 参数:`market?`(默认 a_share;期货用 future)/ `instrument_type?`(`stock|etf|index|
  convertible|futures`;index=#256 基准指数,日线走 akshare 指数接口;convertible=
  #265 转债,走 tushare `cb_daily`,akshare 源 fail-visible 拒绝;futures=
  #267 期货主连(如 IF0.CFFEX),需配 market=future,走 akshare 新浪
  `futures_main_sina`,tushare 源 fail-visible 拒绝,主连仅研究信号/基准
  不可当作可成交合约;
  `source=tushare` 对 stock/convertible 之外报 `tushare_scope_mismatch`)/
  `exchange?` /
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
- 标的集三选一(#261,互斥,同时声明 `invalid_argument`):
  `symbols`(内联列表)/ `symbols_from_release`(复制既有可用发布的冻结标的集,
  全市场发布/跟进发布首选,免手工维护巨型清单)/ `full_market=true`
  (instruments 表全活跃标的按 kind 展开:股票单源只取 A 股股票,
  multi_asset_mixed 取股票+ETF+指数+转债(#265)+期货主连(#267),convertible_metrics
  只取转债)
- 其他参数:`release_id` / `version` / `start_date` / `end_date` /
  `dataset_name?`(默认 multi_asset_daily_bars)/ `release_kind?`
  (a_share_tushare|multi_asset_mixed|daily_metrics|financial_indicators|
  convertible_metrics,默认 a_share_tushare)/ `source?` /
  `adjustment?`(qfq|hqfq|none,默认 qfq;daily_metrics/financial_indicators/
  convertible_metrics 固定 none)/
  `required_capabilities?`
  (stock|bond|convertible|futures|etf:index|etf:cross_border|etf:commodity|etf:bond)
- 返回:`JobOut`(`kind=dataset_publish`)
- 错误:`invalid_argument`(schema 校验:release_id/version pattern、日期顺序;
  #261 来源解析:来源发布不存在 `source_release_not_found` / 不可用
  `source_release_not_usable` / 展开为空 `full_market_empty`)/ `conflict`(幂等冲突)
- `release_kind=daily_metrics|financial_indicators` 时从 research_* 表冻结
  基本面/财务指标发布(issue #187),与 bars 发布(dataset_release_ids 含 bars 主发布 +
  research 发布)联合供因子快照取数;schedule(data_sync)与发布任务报告缺失字段统计。
- `release_kind=convertible_metrics`(#265)只接受 A 股可转债标的(非转债
  `convertible_scope_violation`),从本地缓存 bars x 冻结转股价元数据
  (`convertible_metadata`)计算转股价值/转股溢价率并冻结为带日期观测;
  元数据缺失 `convertible_metadata_missing`(先跑 research_data_sync 的
  convertible_profiles)。质量报告 `convertible_instruments` 块可见转债标的数
  与评级/到期日缺失计数。

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
查询因子混合目录:builtin(26 个 alpha/risk/market_input 因子)+
user_defined(沙箱执行的自定义因子,#217)。
**builtin 目录(#214 起)是因子定义的唯一事实来源**——v1 选股规则目录
(`finboard_data.factors.FACTOR_CATALOG`,8 因子)逐字段从这里投影生成,
selection 可用因子集 = 其子集(market_cap/pb/turnover_rate/momentum/
volatility_20d/roe/gross_profit_margin/revenue_yoy)。
user_defined 条目来自 `research_code_artifacts`(kind=factor),标注
artifact commit、status 与 promotion_status;引用名为 `u_<artifact_name>`,**仅
status=active 且 promotion_status=passed 可被规格引用**(retired/未晋级后入队秒级拒绝),观测来自
`finboard_research_code_run` 落库的快照。
- 参数:`role?: str`(alpha|risk|market_input,仅过滤 builtin)、
  `include_user_defined?: bool = true`
- 返回:`list[{name, origin: builtin|user_defined, version, role, ...}]`;
  user_defined 条目另含 `{artifact_name, status, commit, artifact_id,
  code_checksum, created_at, note}`
- builtin 部分无 DB 依赖;user_defined 查 `research_code_artifacts` 表。

### finboard_feature_snapshot_list
列出特征快照(版本化、时点化、不可变)。**默认 header-only(#309)**:
不含 observations 逐条值——大快照单条可达 MB 级,默认全量会被 MCP 客户端截断。
- 参数:`dataset_release_id?: str`、
  `source_run_id?: str`(按产出 run 精确过滤,**RCR→快照映射一条查询完成**,
  替代逐个单查)、`include_observations?: bool = false`(显式 true 才返回
  完整 observations,旧行为,响应大)、`limit?: int = 100`
- 返回(header):`list[{snapshot_id, dataset_release_id, source_run_id,
  decision_at, published_at, framework_version, feature_names, symbol_count,
  observation_count, code_version, checksum, ...}]`
  (include_observations=true 时每条另含 observations)

### finboard_feature_snapshot_get
查询单个特征快照详情(含完整 observations 因子值;list 瘦身后取全量的常规路径)。
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

## finboard.validation_experiment.*(✅ #138 + #233)

#57 机器验证实验(OOS 样本外验证)元数据 CRUD + 执行入队,暴露 REST
`/api/research/experiments` 的 6 个端点。与因子实验(登记簿)是**两套独立但
耦合的系统**:因子实验通过 `validation_experiment_id` 引用本批工具创建的 #57
实验,再经 `finboard_factor_experiment_sync_validation` 同步终态,补齐 OOS
过拟合控制闭环。实验执行(#233)经 `finboard_validation_experiment_run`
任务化:`kind=validation_experiment` 后台任务由 worker 单并发执行
ValidationRunner(IS → walk-forward → 一次性揭盲),`finboard_job_get`
轮询(result_ref=experiment_id;validated_oos → succeeded,rejected → failed
附阈值原因)。写操作尊重 `mcp_readonly_only` 开关。

**结论语义(#310)**:list/get 返回派生 `oos_outcome`
(supported|not_supported|inconclusive)—— `validated_oos` 只代表 **OOS
流程完成**,不代表**假设获支持**:#245 决策 A(screen 门取 abs(rank_ic))下
best trial OOS 被拒不阻止揭盲,此时 status=validated_oos 而
oos_outcome=not_supported;全 trial OOS 被拒仍揭盲时打具名 WARNING
`unseal_with_rejected_trials` 并写入实验 notes(明示 final test 揭盲机会
消耗在被拒配置上),不硬阻断、状态机与揭盲一次性语义零变化。解读规则:
supported=best trial OOS 门过且揭盲达标;not_supported=best trial OOS 被
拒或揭盲未达标;inconclusive=无 trial / OOS 无可判定证据 / 流程未走完。
晋级证据 `gates.validation.oos_outcome` 同步携带该字段。

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
  robustness, status, trials_used, final_test_unsealed, oos_outcome(#310),
  ...}]`
- 错误:`invalid_argument`(非法状态)

### finboard_validation_experiment_get
查询单个 #57 验证实验详情 + **全部 trial(包括 FAILED / REJECTED —— 多重试验
修正需要真实试验总数)**。
- 参数:`experiment_id: str`
- 返回:`{experiment_id, ..., oos_outcome(#310: 派生结论语义,validated_oos
  也可能是 not_supported), trials: [{trial_id, trial_index, parameters,
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

### finboard_validation_experiment_run **[写]**(✅ #233)
入队执行 #57 实验:`kind=validation_experiment` 后台任务(worker 单并发)。
按冻结计划跑 IS 参数搜索(`strategy_params_space` 网格)→ walk-forward OOS
(稳健性 + DSR/PSR/PBO)→ **一次性揭盲最终测试集**;逐 trial 落库(失败也算
试验),断点续跑不重复计数;揭盲不可重做(终态后重复执行秒级拒绝)。
- 参数:`experiment_id: str`、`idempotency_key?: str`(默认
  `validation_experiment:<experiment_id>`)、`requested_by?: str`(默认
  `agent:mcp`)
- 返回:`{job_id, kind, experiment_id, status, created, idempotency_key,
  detail_hint}`;`finboard_job_get` 轮询
- 前置:实验的 `version_stamp.selection_config` 须声明
  `validation_trial_runner: {strategy, symbols, provider?, params?, capital?}`
  (注册表策略回测;capital 必须为数值 int/float/数字字符串,params 必须为对象,
  strategy 必须是注册表策略名;缺省执行期报 `trial_runner_unconfigured`,
  声明了但配置不合法在首个 trial 前报 `trial_runner_invalid_config`)
- 错误:不存在 `not_found`;终态 / 已揭盲 `conflict`(揭盲不可重做);
  预算耗尽 `invalid_argument`;执行意外中断 `experiment_execution_failed`
  (实验状态保留,修复后可重新入队断点续跑)
- 场景:#219 晋级链的 OOS 门入口 —— 完成后把 experiment_id 传给
  `finboard_research_code_promote`

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
  required_datasets, dataset_release_ids, lifecycle_stages, can_execute,
  universe_precheck}`
  - `universe_precheck`(issue #186/#213):`{total_candidates, included, excluded,
    is_empty, excluded_by_condition, missing_fields, warnings, explicit_total,
    explicit_missing}`,来自主数据发布 instruments 的静态评估 ——
    - 评估域(#254):声明 `universe.explicit_symbols` 时只评估 explicit ∩
      发布标的,`total_candidates` 即交集规模(不再对全市场产出
      `not_in_explicit_symbols` 噪音);声明但发布中缺失的标的计进
      `explicit_missing` 并发 `universe_explicit_symbol_missing` warning ——
      全部缺失时 `is_empty=true`,入队必然秒级失败;
    - `warnings` 是具名过滤降级提示(filter 依赖字段缺失):如
      `universe_listing_days_unavailable`(min_listing_days 依赖 list_date,
      发布 list_date 全空 → 将过滤全部标的)、`universe_delist_metadata_unavailable`
      (exclude_delisted 无 delist_date)、`universe_st_pit_approximate`
      (exclude_st 名称历史不覆盖决策日,回退当前名称近似判定)、
      `universe_st_filter_inactive`(exclude_st 无名称数据,ST 过滤不生效)、
      `universe_average_amount_unavailable`(min_average_amount 无
      average_amount 特征)、`universe_market_cap_unavailable`
      (min/max_market_cap 无 market_cap 特征——附 daily_metrics 研究发布
      或含 market_cap 观测的因子快照即可解析)、
      `universe_required_field_unavailable` / `universe_ranking_field_unavailable`
      (required_data_fields / ranking.field 无数据源);
    - universe 支持 `min_market_cap`/`max_market_cap`(人民币元,中小盘/大盘
      池过滤,取 daily_metrics 的 market_cap 特征观测);exclude_st 按发布
      instruments 名称历史在决策日 PIT 判定(issue #213);
    - `is_empty=true` 时入队必然秒级失败,先修复元数据(如 data_sync profiles
      回填 list_date)或放宽过滤再入队。

### finboard_strategy_draft_create **[写]**
保存策略规格草稿版本(change_type=create,首版本)。
- 参数:`spec: dict`、`expected_version?: int`
- 先 validate 再持久化;版本冲突 → `conflict`
- 返回精简回执(#206):`{strategy_id, version, status, checksum, created_at,
  view: "ack", detail_hint}` —— 完整 spec 走 `finboard_strategy_version_get`

### finboard_strategy_supersede **[写]**
为已存在的策略创建后继草稿(change_type=supersede)。
- 参数:`strategy_id: str`、`spec: dict`、`expected_version: int`
- path strategy_id 必须与 spec.strategy_id 一致,否则 `invalid_argument`

### finboard_strategy_publish **[写]**
发布策略规格的指定版本(draft→published)。
- 参数:`strategy_id: str`、`version: int`、`expected_version: int`
- 发布前重新编译校验;版本不存在 → `not_found`,状态转换非法 → `conflict`
- 返回精简回执(#206,同 draft_create 形态);完整 spec 走 version_get

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

## finboard.backtest.*(✅ #127 + #174 + #175)

回测引擎(行情回放 + 纸面撮合)。复用 `BacktestEngine` +
`BacktestRunRepository` + `list_strategy_definitions`;已发布规格回测走
research_run 管线轻路由(#174)。

### finboard_backtest_strategy_list(只读)
列出回测双入口(issue #174):
- 参数:无
- 返回:`{builtin_strategies: list[{kind, name, description, supports_backtest,
  params: [...]}], published_specs: list[{strategy_id, strategy_kind, name,
  status, version, version_count, execution_hint}], execution_note: str}`
- 内置策略中仅 `ma_cross` 支持事件驱动回测(`supports_backtest=true`);
  `published_specs` 是已发布研究策略规格(走 research_run 管线,不是
  事件驱动引擎),`execution_hint` 给出直达入口

### finboard_backtest_run **[写]**
双形态(二选一,互斥,同时给出报 `invalid_argument`):
- **形态 1(strategy)**:事件驱动回测(纸面撮合,**不发真实订单**),默认同步
  返回 metrics/equity/fills 并落库。**同步形态只适用于小规模**(数只标的 x 短
  区间,总工作量 ≲ 阈值);大任务必须异步,否则 MCP 客户端会 30s 超时后响应
  丢失(服务端继续跑完落库,只能事后查 history)。**issue #189**:
  `run_async: bool | None = None`——`true` 强制入队 `kind=backtest_run` 后台
  任务返回 job_id、`false` 强制同步、省略时按估算工作量「标的不数 x 交易日」
  自动切换(≥ `backtest_auto_async_symbol_days`(settings,默认 15000,0=关闭
  自动切换)即异步)。异步任务用 `finboard_job_get(job_id)` 轮询:成功后
  `result_ref=str(run_id)`,再用 `finboard_backtest_history_get(run_id)` 查完整
  结果。`async_mode` 返回 explicit / auto_threshold 便于核对决策。
  - 参数:`strategy: str`、`symbols: list[str]`、`start: str`、`end: str`、
    `capital: str = "100000"`、`adjust: str = "qfq"`、`params: dict`、
    `selection: dict`、`commission_rate: str`、`commission_min: str`、
    `stamp_tax_rate: str`、`slippage_bps: str`、
    `benchmark_symbol: str | None = None`(issue #184:显式基准标的代码,如
    `000300.SH`,指数日线自动走 akshare 指数接口,引擎单独拉取基准 bars 计算
    `benchmark_return`/`excess_return`;基准缺失时
    `benchmark_return`/`excess_return` 为 null)、
    `equity_mode: str = "summary"`(summary 降采样到 max_points 个关键点,首末
    点保留;full 返回完整曲线)、`max_points: int = 200`、
    `selection_snapshots: str = "none"`(issue #258:逐决策选股快照裁剪,
    `none` 默认不返回快照列表只回 `selection_snapshot_count`;`summary` 为
    每期决策时点+状态+`selected_symbol_count` 投影;`full` 才回全量列表——
    带 selection 的 run 此字段是单次响应 MB 级的主膨胀点,要审计具体选了哪些
    标的时才传 full)、
    `run_async: bool | None = None`(issue #189)、
    `requested_by: str | None = None`(异步任务归属,默认 agent:mcp:backtest_run)
  - `selection.factor_version` 仅支持 `"v1"`(**选股规则版本**);`"v2"` 等会报
    「不支持的 factor_version」错误并列出合法值。可用因子集见
    `finboard_factor_catalog`(v1 选股目录是其投影子集,#214)。特征快照的
    `framework_version` 是另一契约字段,不传给 selection
  - `selection.inputs_mode`(#173):
    - `research_db`(默认):从 research 数据表读 profile/daily_metrics/
      financial_indicators/industry_memberships。**必需数据集批次未发布时
      入队秒级拒绝(#255)**:`dataset_unpublished:{dataset}` 具名
      `invalid_argument`(摄取 ≠ 发布——`research_data_sync` 质量门通过才
      自动发布;发布状态核验 SQL 与修复步骤见 `docs/research/data-ops.md`);
      执行端(grid 旁路)由 worker 以 `selection_dataset_unpublished` 具名拒绝
    - `bars`:纯价格因子(momentum/volatility_20d)从回测行情计算,不要求
      daily_metrics 发布;dataset 未发布降级为 snapshot warnings,不整日
      SKIPPED;ST/上市天数过滤在 instrument_profiles 缺失时降级不生效
    - `snapshot`:因子值直接来自冻结 FeatureSnapshot,需 `snapshot_ids:
      list[str]`;观测按 available_at <= decision_at 过滤
  - **selection_diagnostics(#255)**:selection 启用时 metrics 附带逐期诊断
    `{total_snapshots, published_snapshots, skipped_snapshots, skip_reasons,
    selection_pool_ever_active, zero_trading_suspected?}`——整期 SKIPPED 的
    0 交易 run 在 summary/诊断中显式标注,不再伪装成功
  - 返回:`{run_id, metrics, equity_curve, equity_point_count, fills, summary,
    selection_snapshots(默认 none 不回,#258;full 时 snapshot 含 warnings
    降级提示), selection_snapshot_count, ...}`(同步;
    metrics 里 sharpe_ratio=主口径 rf=3%/ddof=0,sharpe_rf0=rf=0 对照口径
    ddof=1,risk_free_annual=实际 rf;与 research_run 报告同屏比较 Sharpe 用
    sharpe_rf0 —— issue #262;`benchmark_source`=基准曲线实际来源
    `explicit_symbol:<code>` / `equal_weight_selection_pool`(选股启用时按每期
    选股结果动态等权,#254)/ `equal_weight_static_pool` / `first_symbol`,
    回退来源可见);
  - **timing(#285)**:metrics.timing =
    `{total_elapsed_seconds, data_load_elapsed_seconds, parquet_reads
    {read_ops, read_elapsed_ms, read_bytes, ops_by_entry}}` —— job 级分段
    耗时,判定「慢在 IO 还是计算」;research_run 侧 `finboard_run_get` 的
    result.timing 另有 `decision_load_elapsed_seconds` /
    `decision_execute`(count/min/avg/max + slowest_decision_date)/
    `report_elapsed_seconds`。纯可观测性,不参与任何 checksum;
    异步返回 `{job_id, status, created, idempotency_key, async_mode,
    symbol_days_estimate, auto_async_threshold, execution_path}`
  - `fills[].date` = 该笔成交实际发生的交易日(issue #205 起);此前旧记录
    是任务运行日,按 `created_at` 区分,勿混排分析
- **形态 2(strategy_spec)**:按已发布规格路由入队 research_run 管线(issue
  #174),不阻塞等待完成。
  - 参数:`strategy_spec: {strategy_id: str, version: int}`、
    `queue_payload: dict`(与 `finboard_run_queue` payload 同构,不含
    strategy_id/strategy_version;必填 idempotency_key /
    dataset_release_ids / code_version / initial_capital / requested_by;
    `queue_payload.parameters.rebalance_frequency ∈ {monthly, quarterly}`
    时为多期再平衡回放,#183)
  - 校验:规格版本存在且 `status == "published"`,否则 `invalid_argument`;
    入队时同样做 universe 候选池非空预检(issue #186,与 run_queue 一致),
    空池秒级 `invalid_argument` 并附排除统计与缺失字段
  - 返回:`{run_id, job_id, status, strategy_id, strategy_kind,
    execution_mode, manifest_checksum, execution_path}` —— 已入队异步执行,用
    `finboard_run_get` 或 `finboard_job_get` 轮询进度;`execution_mode` 为
    single_shot(默认)或 multi_period(#183,报告含 annualized_return 与
    全区间每日 equity_curve)
  - 基准收益(#184):research_run 管线按 `benchmark_config.symbol` 从冻结
    发布取行情计算真实 `benchmark_return`/`excess_return`;基准缺失(发布中
    无该标的)时二者为 null + 具名 warning,不再静默 0.0;`queue_payload.
    benchmark_config` 可传 `{"overrides": {"return": <手动值>}}` 作为发布
    无基准行情时的兜底。基准标的必须**在同一个 bars 主发布内**(#256:
    manifest 只允许一个 bars 发布;先用 data_sync 登记指数 →
    bulk_download_start(instrument_type=index)拉指数日线 → 发布
    multi_asset_mixed 时把指数代码一并放进 symbols)。指数自动不进候选池
    (只做基准数据,不可撮合,#256)
- 错误:`invalid_argument`(互斥 / 规格不存在 / 未发布 / 参数校验失败 /
  equity_mode 非法 / snapshot 模式缺 snapshot_ids)、`permission_denied`
  (只读模式)、`conflict`(异步重复 idempotency_key)、`unavailable`
  (同步数据源连接错误;异步任务的数据源失败在 job 的 error_code/error_summary)

### finboard_backtest_history_list(只读)
列出最近回测历史记录(摘要,不含完整 equity/fills)。
- 参数:`limit?: int = 50`
- 返回:`list[{id, strategy, symbols(前 10 只,#206), symbol_count, start,
  end, capital, metrics, ...}]`

### finboard_backtest_history_get(只读)
查询单条回测历史详情(equity 默认降采样,fills 分页,选股快照默认不回)。
- 参数:`run_id: int`、`equity_mode: str = "summary"`、`max_points: int = 200`、
  `fills_limit: int | None = 200`(默认有界 200 条,#206;`null` 返回全部)、
  `fills_offset: int = 0`、
  `selection_snapshots: str = "none"`(issue #258:`none` 默认不返回逐决策
  选股快照只回 `selection_snapshot_count`——带 selection 的 run 此字段是
  单次响应 MB 级的主膨胀点;`summary` 为每期决策时点+状态+
  `selected_symbol_count` 投影;`full` 才回全量,审计具体选了哪些标的时用)
- 返回:`{id, ..., equity_curve, equity_point_count, fills, fills_total,
  fills_offset, summary, selection_snapshots, selection_snapshot_count, ...}`
  (symbols 全量,列表才有预览;
  metrics 含 sharpe_ratio/sharpe_rf0/risk_free_annual 双口径标注,#262)
- 错误:`not_found`、`invalid_argument`(equity_mode / selection_snapshots 非法)

### finboard_backtest_history_delete **[写]**
删除一条回测历史记录。
- 参数:`run_id: int`
- 返回:`{run_id, deleted: true}`
- 错误:`permission_denied`(只读模式)、`not_found`

### finboard_backtest_grid_submit **[写]**
批量参数网格回测提交(issue #175):一次提交 N 组参数,逐组合展开 + 上限封顶
+ 参数校验后,各登记一个 `kind=backtest_run` 后台任务(网格定义与任务**同一
事务**落库,全有或全无),返回 grid_id + job 指针,异步执行。支撑「合理实验」:
一个假设下多组参数对比择优,替代逐个手跑再人工对比。
- 参数:
  - 公共:`strategy: str`、`symbols: list[str]`、`start/end: str`(ISO 日期)、
    `capital: str = "100000"`、`adjust: str = "qfq"`、`params?: dict`(公共参数,
    被组合覆盖)、`selection?: dict`(基础选股配置,被 selection_grid 覆盖)、
    `commission_rate/commission_min/stamp_tax_rate/slippage_bps: str`
  - params 组合维度(二选一,互斥):
    - `params_list: list[dict]`(显式列表,每个元素是一组覆盖参数,优先)
    - `params_grid: dict[str, list]`(笛卡尔积 `{字段: 值列表}`,跨字段全组合;
      展开后总数受上限约束)
  - selection 组合维度(#259):`selection_grid: dict[str, list]`(选股维度
    笛卡尔积 `{selection字段: 值列表}`,如 `ranking_factor`/`momentum_lookback`/
    `max_symbols`,字段名同 `FactorSelectionParams`;与 params 侧做笛卡尔积,
    也可**单独使用**做纯选股扫描——如因子×窗口
    `{"ranking_factor": ["momentum", "pb"], "momentum_lookback": [20, 60]}`);
    逐组合 selection 过 `FactorSelectionParams` 校验,非法组合具名
    `invalid_argument` 不落库不入队
  - 三者(params_list / params_grid / selection_grid)至少提供一个;总组合数
    = params 覆盖 × selection 覆盖
  - `max_combos: int = 20`(组合数上限,硬上限 50,超限 `invalid_argument`,
    防误操作打爆队列)、`grid_idempotency_key: str`(幂等键,≥8 字符,重提交
    返回同一网格 created=false;组合定义不同 → conflict)、`requested_by: str`
- 校验:未知策略 / `supports_backtest=false` / 任一组合参数非法 / selection
  (基础或任一组合)非法 / 日期非法 → `invalid_argument`,不落库不入队
- 返回:`{grid_id, created, strategy, symbols, start, end, capital, adjust,
  combo_count, max_combos, jobs: [{combo_index, label, job_id, params,
  selection?}], polling_note}`(组合携带 selection 覆盖时 jobs/落库 combos
  才含 selection 键;label 在有 selection 覆盖时为 `{params, selection}`
  结构化差异 JSON,无则与历史扁平格式一致)
- 错误:`permission_denied`(只读模式)、`invalid_argument`(形态互斥/上限/
  参数校验)、`conflict`(幂等冲突 / 并发冲突)
- 完成后用 `finboard_backtest_grid_get(grid_id)` 查询聚合对比表,或
  `finboard_job_get(job_id)` 逐任务查询

### finboard_backtest_grid_get(只读)
查询批量参数网格回测的聚合对比表(issue #175):
- 参数:`grid_id: str`、`equity_mode: str = "none"`(**默认不返回 equity 曲线**,
  只保留 `equity_point_count` 点数提示,响应最轻;`summary` 降采样到
  max_points 个关键点,首末点保留;`full` 返回完整曲线;复用 #172 形态 +
  #190 默认响应瘦身)、`max_points: int = 200`
- 返回:
  - 网格元信息(公共字段 strategy/symbols/start/end/capital/adjust/params=
    base_params/selection=基础选股配置 在头部只出现一次,#206/#259;combo
    完整参数 = params/selection + label 的覆盖差异)+ `complete: bool`
    (全部组合到终态)/ `completed_count` /
    `pending_count` / `failed_count`
  - `metric_fields: list[str]`(指标矩阵列:收益/年化/夏普/回撤/胜率/换手/超额/
    费用等,按规范顺序;Sharpe 口径见下方「绩效指标口径」)
  - `combos: list[{combo_index, label, job_id, job_status, run_id?,
    metrics?, equity_curve?(仅显式 equity_mode 时返回), equity_point_count?,
    rank?{指标: 竞争排名,同值
    同排名}, best?{指标: 是否最优}}]`(成功组合带指标矩阵 + 排名 + 最优标注;
    未到终态组合只有 job_status;组合的 params/selection 覆盖差异都由 label
    承载,含 selection 覆盖时 label 为 `{params, selection}` 结构 JSON)
  - `ranking: {指标: {combo_index, label, value}}`(每关键指标最优组合;
    全部「越高越好」,max_drawdown 为负值越高=回撤越小)
  - `failures: list[{combo_index, label, job_id, job_status, error_code,
    error_summary}]`(失败/取消/中断/产物缺失的组合**带错误码单列**,不影响
    成功组合返回)
- 错误:`not_found`(网格不存在)、`invalid_argument`(equity_mode 非法)
- 部分失败不吞错;`complete=false` 时稍后重试;多组合对比择优默认不传
  equity_mode(避免 9 组合 x 200 点 ~100-200KB 响应被截断),确需曲线再显式请求

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
目标权重分配(equal_weight / inverse_volatility / erc / max_ir)+ 约束 +
风险报告。对应 `POST /api/portfolio/allocate`,调 `build_portfolio`。
**max_ir(#266)**:最大 IR(切点)组合 —— LW 协方差 + 信号强度代理预期
超额收益,capped simplex 投影梯度求解(确定性,零随机成分);依赖协方差,
缺协方差/信号全中性按 `covariance_failure_mode` 分流(默认 fail_closed)。
**风险因子中性化(#266)**:`risk_factor_limits`([{factor, max_active_exposure}])
+ `factor_exposures`({因子: {标的: 暴露观测}})启用 active 暴露硬上限
`|Σ(w−baseline)·f| ≤ 阈值`(`neutralization_baseline?` 提供基准权重,缺省
现金基准)—— 只减仓投影、逐项审计(`risk_factor_neutralization` 硬约束行);
暴露观测缺失的因子降级为具名 warning 审计行
(`risk_factor_neutralization_skipped`,passed=false,不静默);
约束本体不可满足报 `invalid_argument`(fail-closed)。research_run 侧经
`portfolio_config.overrides.risk_factor_limits` 声明同一约束(因子名与冻结
特征 feature_id 同名)。
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
  `covariance_failure_mode? = "fail_closed"`、
  `risk_factor_limits?: list[{factor, max_active_exposure}]`(#266)、
  `factor_exposures?: dict[str, dict[str,float]]`(#266)、
  `neutralization_baseline?: dict[str,float]`(#266)
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

## finboard.job.*(✅ #136 + #221)

统一后台任务队列监控与提交。复用 `BackgroundJobRepository` + `background_jobs`
表(`BJ-` ID,与实盘 orders/fills/positions 完全隔离)。**任务队列只服务
研究/数据/回测类任务**;实盘交易内核(盘前检查/收盘撤单/日终核对/Broker 心跳/
Kill Switch)由专用 Scheduler 执行,不进入统一队列。

`enqueue` 的 kind 白名单:`echo` / `research_run` / `feature_snapshot` /
`bulk_download` / `dataset_publish` / `backtest_run` / `data_sync` /
`fetch_all` / `quality_repair` / `research_data_sync`(全是研究/数据域,
不含实盘能力)。

`research_data_sync`(issue #171;#251/#265 扩展):research 数据表(档案 / 估值 /
财务 / 行业 / 名称历史 / 转债条款)摄取编排。payload:`{datasets?: [profiles,
name_changes, convertible_profiles, daily_metrics, financial_indicators,
industry_memberships](默认全部), start_date, end_date(ISO), symbols?: [str]}`。
profiles 同时拉取在市(L)与退市(D)档案(delist_date 上游);name_changes
全市场历史名称变更直接重建 `instrument_names`(半开区间,供 ST-PIT);
convertible_profiles(#265)tushare cb_basic 条款快照 upsert 主数据
`convertible_metadata`(转股价/到期日,评级与集思录强赎事件走 akshare 兜底,
失败降级为 warning 不阻断),顺带回填 `instruments.list_date/delist_date`。
逐标的接口自动限流(tushare_budget)
并按确定性 dataset_version 断点续跑;预算耗尽退避重试,未配 token / 未装
SDK fail-fast。

写操作尊重 `mcp_readonly_only` 开关。

### finboard_job_list(只读)
列出后台任务(最近优先)。
- 参数:`kind?: list[str]`、`status?: list[str]`
  (queued|running|retry_waiting|succeeded|failed|cancel_requested|cancelled|
  interrupted)、`queue?: list[str]`、`limit?: int = 100`(1-500)、
  `archived?: "exclude"|"only"|"all" = "exclude"`(#221:默认只看未归档;
  `only` 只看已归档;`all` 不区分)
- 返回:`list[JobOut]`(`job_id/kind/queue/status/priority/payload/
  progress_done/progress_total/phase/result_ref/error_*/attempt/max_attempts/
  worker_id/heartbeat_at/lease_until/requested_by/created_at/started_at/
  finished_at/archived_at/updated_at`)
- 错误:`invalid_argument`(未知 status / 未知 archived 过滤值)

### finboard_job_get(只读)
查询单个后台任务详情。
- 参数:`job_id: str`、`view?: "none"|"summary"|"detail" = "summary"`、
  `data_hash?: str`
- 返回(默认 summary,#206):JobOut 全字段但**剥离 payload**,附 `data_hash`
  (状态指纹,含 run_status);`view=none` 只回轮询最小集(status/phase/
  progress_*/result_ref/error_*/attempt/run_status);`view=detail` 完整含
  payload(诊断用)。轮询时把上次 `data_hash` 传回:状态未变 →
  `{unchanged: true, data_hash, status, run_status}` 不重发全量(成功后
  `result_ref` 携带产物引用,如特征快照的 snapshot_id)
- **run_status(#306)**:kind=research_run 的任务附关联 `research_runs.status`
  (查不到为 null)——「run interrupted 但 job 仍 running」的两表不一致一眼
  可见;REST `GET /api/jobs/{id}` 同口径,列表端点不 join 恒 null
- **实时进度 phase(#308)**:research_run 轮询期间 phase 即进度 —— 加载期
  `research_run:decision_load k/N`(k=已完成期数、N=推导出的决策期总数,
  multi_period/single_shot 同机制,首帧在 close 矩阵预建前透出),
  决策执行期 `research_run:<stage>#<序号>@<YYYY-MM-DD>`(序号 1-based),
  REPORT/终态保持 `research_run:report` / `research_run:<status>`;
  `progress_done/total` 恒为「stage x decision」工件计数(#188 口径,
  total 随已发现决策递增)。轮询 phase 变化即可区分「正常计算 / 加载中 /
  卡死」,不再只能靠 `(done-1)//13` 反推决策序号
- 错误:`not_found`、`invalid_argument`(view 非法)

### finboard_job_enqueue **[写]**
登记一个 queued 后台任务并立即返回 202 + job_id(不等待执行,由独立 worker 消费)。
- 参数:`kind: str`(白名单)、`idempotency_key: str`(8-128 字符)、
  `requested_by: str`、`queue?: str = "default"`、`payload?: dict`(任务参数,
  结构取决于 kind)、`priority?: int = 0`(-1000..1000)、`max_attempts?: int = 3`(1..10)
- 入队期 payload 契约(#260,REST `POST /api/jobs` 与本工具共用同一校验):
  已注册 `research_data_sync` —— **未知键拒绝**(如误传 `data_types`,
  正确参数名为 `datasets`)、`start_date`/`end_date` 必填(ISO 日期)、
  `datasets` 枚举校验、逐标的数据集(`financial_indicators`/
  `industry_memberships`)在未提供 `symbols` 且 `datasets` 不含 `profiles`
  时拒绝(否则 symbol 池解析为空、任务静默零迭代);执行器入口重放同一契约,
  覆盖旁路入队的存量行
- 返回:`JobOut + created`(首次提交 true / 幂等命中 false)
- 错误:`permission_denied`(只读模式)、`invalid_argument`(kind 不在白名单 /
  schema 校验失败 / payload 契约失败`[unknown_payload_key|
  missing_required_field|invalid_field_value|empty_symbol_pool]`)、
  `conflict`(幂等冲突 / 重复 idempotency_key)
- kind payload 契约示例:
  - `feature_snapshot`:`{dataset_release_id, decision_at}` → `result_ref=snapshot_id`
  - `research_run`:`{run_id, strategy_kind}` → 与 `finboard_run_queue` 双写
  - `bulk_download`:`{market, source, start, instrument_type}`
  - `dataset_publish`:`{release_id, release_kind, symbols, version, start_date, end_date}`
  - `backtest_run`:`{request, provider_name}` → `result_ref=str(run_id)`
  - `research_data_sync`:`{start_date: "YYYY-MM-DD"(必填), end_date:
    "YYYY-MM-DD"(必填), datasets?: [profiles|name_changes|convertible_profiles|
    daily_metrics|financial_indicators|industry_memberships](缺省=全部六类;
    convertible_profiles=#265 转债条款快照), symbols?:
    ["000001.SZ",...](省略时逐标的数据集以 profiles 同步结果为池,此时
    datasets 须含 profiles)}` → 研究数据表摄取(batch 发布后 selection 可命中)

### finboard_job_cancel **[写]**
请求协作式取消后台任务(running → cancel_requested,executor checkpoint 时退出)。
- 参数:`job_id: str`
- 返回:更新后的 `JobOut`
- 已在终态(succeeded/failed/cancelled/interrupted)的任务返回当前状态不报错。
- 错误:`permission_denied`(只读模式)、`not_found`

### finboard_job_archive **[写,#221]**
归档后台任务:从默认列表(`archived=exclude`)隐藏但**不删除**,
`finboard_job_get` 单查与 `archived=only|all` 列表始终可达,可 unarchive 恢复。
仅终态任务可归档;归档即冻结(worker 不再自动重排该任务)。
- 单个:参数 `job_id: str`(幂等,已归档原样返回)→ 返回 `JobOut`
- 批量:省略 `job_id`,参数 `kinds?: list[str]`、`statuses?: list[str]`
  (终态子集,空=全部终态)、`queues?: list[str]`、
  `finished_before?: str`(ISO 时间,只归档完成早于该时刻的)、
  `limit?: int = 100`(1-1000,从旧到新)→ 返回 `{archived_count}`
  (只回计数不回全量,#206 精神)
- `job_id` 与批量过滤参数互斥(同传报 `invalid_argument`)
- 错误:`permission_denied`(只读模式)、`not_found`、`conflict`(非终态)、
  `invalid_argument`(statuses 含非终态 / finished_before 非法 / 参数互斥)

### finboard_job_unarchive **[写,#221]**
取消归档单个后台任务:任务重新出现在默认列表;幂等(未归档原样返回)。
- 参数:`job_id: str` → 返回更新后的 `JobOut`
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
聚合单个 ResearchRun 报告。
- 参数:`run_id: str`(RR-)、`view?: "summary"|"detail" = "summary"`
- 返回(默认 summary,#206):`{run_id, status, strategy_id, strategy_kind,
  created_at, completed_at, metrics(剔 equity_curve), artifact_count, view,
  universe: {total, included, excluded_by_reason}, fills: {total, by_decision}}`
  —— 不序列化逐标的全量 payload
- 返回(view=detail):`{..., artifacts: [{artifact_id, sequence, stage,
  decision_id, trace_id, checksum, payload}]}`(含 report / equity / decisions
  各阶段 payload,可达 MB 级,诊断用)
- 错误:`not_found`(研究运行不存在)、`invalid_argument`(view 非法)

### finboard_report_backtest(只读)
聚合单条回测历史报告:运行元信息 + metrics + equity_curve + fills + summary
(标准化结构;equity 默认降采样,issue #172)。
- 参数:`run_id: int`、`equity_mode: str = "summary"`、`max_points: int = 200`、
  `fills_limit?: int | null = 200`(默认有界,#206;`null` 全量)、
  `fills_offset?: int = 0`
- 返回:`{run_id, strategy, symbols, start, end, capital, adjust, created_at,
  metrics, equity_curve: [{date, equity, benchmark?}], equity_point_count,
  fills: [...](分页), fills_total, fills_offset, summary}`
- 错误:`not_found`(回测记录不存在)、`invalid_argument`(equity_mode 非法)

## 绩效指标口径(issue #262,2026-09-02)

**Sharpe 双口径,跨报告比较必须用 rf=0 口径**:
- 回测(事件驱动)metrics:`sharpe_ratio` = 主口径(rf=`risk_free_annual`
  默认 3%/年,按 rf/252 日化,总体标准差 ddof=0,√252 年化);
  `sharpe_rf0` = rf=0 对照口径(样本标准差 ddof=1)。
- research_run 报告:`sharpe_ratio` = rf=0 / ddof=1 / √252(即引擎的
  `sharpe_rf0` 口径),`risk_free_annual=0.0` 标注实际 rf。
- **同屏比较规则**:引擎报告取 `sharpe_rf0`,研究报告取 `sharpe_ratio`——
  这两个字段同口径。勿拿引擎 `sharpe_ratio` 与研究报告 `sharpe_ratio` 直比
  (rf 与 ddof 双重口径差;低收益策略主口径会被 rf=3% 拖近 0,如 run 277
  年化 3.05% 显示 Sharpe 0.05 的误读)。旧回测记录(2026-09-02 前)无
  `sharpe_rf0`/`risk_free_annual` 键,其 `sharpe_ratio` 恒为主口径。
- mean_reversion / futures_tsmom / validation 统计的 Sharpe 已统一委托
  `finboard_backtest.metrics` 实现;validation 默认口径与引擎主口径一致
  (PBO 排名场景显式 rf=0)。

### finboard_report_export(只读)
把报告聚合后导出为文件,返回绝对路径 + 元信息。
- 参数:`kind: str`(run|backtest)、`id: str`(run_id 或回测记录 id)、
  `format: str`(csv|markdown)
- 返回:`{path(绝对路径), kind, id, format, size_bytes, lines}`
- CSV:UTF-8 BOM(Excel 打开中文不乱码),每节 `# 标题` 注释行 + 表头 + 行,
  节间空行;Markdown:`#` 标题 + `##` 分节表格
- 错误:`invalid_argument`(未知 kind / format / 非整数回测 id)、
  `not_found`(run/backtest 不存在)

## 研究代码仓库与晋级(#215/#219,agent 代码入口:只存储与版本化)

web 通道仍禁代码(无代码规格);仅 MCP agent 通道开放受控代码提交。
提交的代码进入本地 bare git 仓库(`research_code_repo_path`),git 写操作
收敛在服务端(本容器文件系统只读)。提交后只登记 `draft/pending`,执行须走
#216 沙箱(`finboard_research_code_run`,见下节);LLM 产出仍须走 研究→回测→OOS→
模拟→影子→小资金 完整晋级链。正式 composite/模拟盘只消费
`status=active` 且 `promotion_status=passed` 的 artifact。

### 目录约定与静态校验

- 一因子/策略一目录:`factors/<name>/{factor.py, manifest.toml}`、
  `strategies/<name>/{strategy.py, manifest.toml}`,files 的键是相对该目录的路径
- manifest.toml 必填 `[manifest] entry`;`params` 须为表(参数 schema)
- 入口签名:factor 须定义模块级 `compute(data, ...)`,strategy 须定义
  `decide(...)`,至少一个输入参数,不接受 `*args`
- import 白名单:`pandas` / `numpy` / `polars` / `math` / `statistics` /
  `finboard_research_kit`;禁 subprocess/socket/os/sys/ctypes/shutil/importlib、
  禁相对 import、禁 eval/exec/compile/`__import__`/system/popen、
  禁 `open()` 写模式(w/a/x/+)
- 上限:文件数 ≤ 32(settings `research_code_max_files`)、单文件 ≤ 256KB
  (`research_code_max_file_bytes`);只收文本(.py/.toml/.md/.txt/.json),拒二进制

### finboard_research_code_submit(写)
提交一版研究代码,静态校验通过后生成新 commit 并登记
`research_code_artifacts`(created_by=agent:mcp,status=draft,
promotion_status=pending)。同名重复提交生成新 commit,不会替换当前
active 版本。
- 参数:`kind: "factor"|"strategy"`、`name: str`(不含路径分隔符/点号)、
  `files: {相对路径: 文件内容}`
- 返回:`{name, kind, commit, checksum, path, artifact_id, status=draft,
  promotion_status=pending}`
- 错误:`invalid_argument`(逐条列出可操作问题,如
  `[import_not_whitelisted] factor.py: import requests 不在白名单 [...]`)、
  `denied`(mcp_readonly_only)

### finboard_research_code_list(只读)
列出代码产物登记(新→旧,可按 kind/name/status 过滤)。
- 参数:`kind?`、`name?`、`status?`(active|retired|draft)、
  `include_files: bool = false`(结果唯一时附该版本文件)、`commit?`、`limit=100`
- 返回:`{artifacts: [{artifact_id, kind, name, commit, path, checksum,
  status, promotion_status, validation_experiment_id, screen_run_id,
  promotion_evidence, created_by, created_at, updated_at}], count, files?}`

### finboard_research_code_get(只读)
读某版本全部文件 + 提交历史;`diff_from` 传旧 commit 附 unified diff。
- 参数:`kind`、`name`、`commit?`(省略=最新 main)、`diff_from?`
- 返回:`{kind, name, ref, files: {路径: 内容}, history: [{commit, date,
  message, author}], diff?}`
- 错误:`not_found`(代码不存在 / commit 无效)

### finboard_research_code_rollback(写)
把 (kind, name) 的历史 commit 重新登记为 draft(不替换当前 active,git
历史不重写)。旧 commit 必须重新完成 screen + #57 OOS 后才能 promote。
- 参数:`kind`、`name`、`commit`
- 返回:新登记行 `{artifact_id, kind, name, commit, checksum, status=draft,
  promotion_status=pending}`
- 错误:`not_found`(历史 commit 不在登记表)、`denied`(只读模式)

### finboard_research_code_promote(写,✅ #219)
将指定 draft artifact 置为正式 active。必须传同一 artifact 的
`screen_run_id`(ResearchRun 的 `factor_screen`/`strategy_screen`:completed,
或 rejected+partial —— 组合阶段硬约束拒绝但保留的 screen 证据,#304,证据
`execution.source_run_status` 显式标注来源 run 状态;或成功 RCR 的 screen
指标)与 `validation_experiment_id`(#57),且实验为
`validated_oos`、`final_test_unsealed=true`,version_stamp 绑定相同
artifact/name/kind/commit。
- 默认 screen 门:`abs(rank_ic) >= 0.02`、平均换手率 `<= 0.80`、相关性
 绝对值 `<= 0.80`、至少 2 期;阈值随 evidence 冻结。rank_ic 取绝对值是
 有意设计(#245 用户决策 A):门只证「存在非噪声信号」,方向正确性由因子
 目录 preference 与 screen 权益/换窗复测承担——负 IC 方向型因子(如反向
 动量)过门不是缺陷,但采用前应换窗复测方向稳定性并在研究记忆中记录。
- 失败:`promotion_status=failed` 证据保留在 draft,返回具名门失败;
  通过后当前同名 active 自动 retired,并返回 `promotion_status=passed`。
- 晋级 evidence 固定四向引用:code commit、dataset release/checksum、
  参数/checksum、output checksum,并保留沙箱镜像/日志/资源归档位置。
  `gates.validation.oos_outcome`(#310)携带派生 OOS 结论:`validated_oos`
  只是晋级门,OOS 流程完成 ≠ 假设获支持——best trial OOS 被拒不改变门判定
  (#245 决策 A),但 `not_supported` 结论在证据中可见,采用前应自行评估。
- 错误:`invalid_argument`(证据缺失/门失败)、`not_found`(artifact 或实验
  不存在)、`conflict`(非 draft)、`denied`(只读模式)

## 研究代码沙箱执行(#216,一次性 Docker 容器)

通过晋级门的 active 因子代码在一次性 Docker 容器内执行 `factor.compute(ctx) -> scores`
(协议 v1 纯截面函数)。**前置条件**:`research_sandbox_enabled=true` +
Docker Desktop + 已构建镜像 `docker/research-sandbox`(tag 与
finboard-research-kit 版本绑定,默认 `finboard-research-sandbox:0.2.0`;
仓库根 `docker build -f docker/research-sandbox/Dockerfile -t <tag> .`)。

### 执行协议 v1(ctx / 输出)

- `ctx: finboard_research_kit.FactorContext` 只读输入:
  `decision_at`(带时区)、`symbols`(候选池)、`bars`(长表
  symbol/date/open/high/low/close/volume/amount,合并全部 bars 类发布)、
  `daily_metrics` / `financial_indicators`(研究发布 PIT 视图,缺为 None)、
  `params`(payload.params 覆盖 manifest.params 的合并结果)
- 返回:`FactorResult(scores)` / `pd.Series`(index=symbol)/ `dict` /
  `DataFrame(symbol,score)`;NaN 合法(计 nan_ratio),候选池外 symbol /
  重复 symbol / 空结果 = 输出契约违规
- PIT 由物理隔离保证:挂载内容即 decision_at 之前的数据,容器内不存在
  未来数据文件;容器 `--network none`(socket 连任何地址失败)、根
  `--read-only`(写挂载路径失败,输出仅出现在 /out)、非 root、CPU/内存/
  pids 限额、墙钟超时 kill

### finboard_research_code_run(写,入队)
入队 `kind=research_code_run` 后台任务(worker 单并发),返回 job_id。
- 参数:`kind: "factor"`(v1 仅 factor)、`name`、`dataset_release_ids`
  (均已登记且至少一个 bars 类发布)、`decision_at`(带时区 ISO)、
  `commit?`(须=目标 artifact 引用,否则先 rollback)、`artifact_id?`(显式指定
  draft 可用于生成供 screen ResearchRun 使用的快照,默认查询只取
  active+passed)、
  `symbols?`、`params?`
- 返回:`{job_id, status, created, idempotency_key, detail_hint}`
- 轮询:`finboard_job_get`(进度 phase `research_code_run:<stage>`,
  result_ref=RCR-...);终态后 `finboard_research_code_run_get` 取结果
- 错误:`invalid_argument`(沙箱未开启 / kind 非 factor / 无 bars 发布 / active
  artifact 未通过晋级门)、`not_found`(无 active+passed 代码 / 发布不存在)、
  `denied`(只读模式)
- #217:成功输出先过**质量门**(NaN 比例 ≤ `research_sandbox_max_nan_ratio`
  且覆盖率 ≥ `research_sandbox_min_coverage`,默认各 0.5),不合格
  run failed=`quality_gate_failed` 且错误信息指明阈值与实际值;通过则
  有限值观测落库为 feature snapshot(`u_<name>` 因子),见 run_get 的
  `output_snapshot_id`。

### finboard_research_code_run_get(只读)
查询单次执行记录(`research_code_runs`,RCR- 前缀)。
- 参数:`run_id`、`view: "summary"|"detail" = summary`
- 返回 summary:三向引用(code commit / dataset_release_ids /
  scores_checksum)、镜像 digest、status、error_code/summary、exit_code、
  timed_out/oom_killed、usage(峰值内存/CPU)、metrics(coverage/nan_ratio +
  `quality_gate` 段:#217 质量门结果与阈值)、`output_snapshot_id`
  (#217:落库快照引用,可进 research run 的 `factor_snapshot_ids`)
- 返回 detail:另附 `scores_preview`(前 20 行)与容器 `error.json`
- 失败分类:`static_validation_failed` / `runtime_error` / `timeout` /
  `oom_killed` / `output_contract_violation` / `sandbox_unavailable`
  (docker 缺失,可重试)/ `quality_gate_failed`(#217,NaN 超标或覆盖不足)
- 归档:`<workspace>/<RCR-id>/{code,data,out,stdout.txt,stderr.txt,
  container.json,usage.json}`(stdout/stderr/退出码/资源用量完整可查)

### 用户自定义因子引用链(#217 + #234 screen 通道)
把沙箱产出变成可被选股管线引用的一等公民。**首次晋级闭环**(draft 无法被
普通运行引用,必须走 screen 显式绑定通道):
1. `finboard_research_code_submit` 提交因子代码(kind=factor,name 如
   `mom20`)→ artifact draft/promotion_status=pending;
2. `finboard_research_code_run` 沙箱执行(dataset_release_ids +
   decision_at,**至少两个不同 decision_at**——screen 门要求 ≥2 期)→
   质量门通过后快照落库,因子观测名 = `u_mom20`;
3. 建 screen 用规格(feature_graph 按 `u_mom20` 引用)+ 声明
   `screen_artifact_bindings: [{kind: "factor", name: "mom20",
   artifact_id, commit?}]` 显式绑定 draft 产物 → 编译期放行(#234);
   validate → draft → publish 照常;
4. `finboard_run_queue` 入队 screen RR(single_shot;把各次
   `output_snapshot_id` 都放进 `factor_snapshot_ids`):入队按 DB 实绑
   校验(存在/非 retired/name/commit 一致,错绑秒级 invalid_argument),
   并要求绑定产物的快照已在冻结清单;
5. run 完成后 report 的 `factor_screen` 段给出 rank_ic / rank_ic_ir /
   分层收益(5 桶)/ 换手率 / 与既有因子的相关性矩阵;
6. `finboard_validation_experiment_create`(version_stamp 四向绑定该
   artifact)+ `finboard_validation_experiment_run` 跑 OOS →
   `validated_oos`;
7. `finboard_research_code_promote`(screen_run_id=RR id)通过
   IC/换手率/相关性 + 四向绑定门后 → `active/promotion_status=passed`;
8. 晋级后:普通(无绑定)规格即可按 active 名单引用 `u_mom20`;
   `finboard_factor_catalog` 确认 origin=user_defined、status=active、
   promotion_status=passed。非 screen 普通运行引用 draft/retired 仍被
   编译期/入队门秒级拒绝(行为不变)。

## 用户代码策略执行(#218,逐日决策函数)

agent 编写的**策略**代码进入回测。与因子不同,策略代码不落快照,而是经
strategy_spec 引用后由 research run **逐决策日**在沙箱容器内执行:

```python
# strategies/<name>/strategy.py(经 finboard_research_code_submit 提交)
def decide(ctx):
    # ctx: StrategyContext —— decision_at / symbols / bars / daily_metrics /
    #      financial_indicators / params(同 FactorContext),另有:
    #   ctx.current_weights: 引擎回显的当前组合权重(pd.Series,上一决策
    #     成交后的实际持仓市值占比;首轮为空;跨日路径依赖由此覆盖)
    #   ctx.constraints: 组合约束只读视图(max_weight_per_asset / long_only /
    #     max_gross_exposure / min_cash_buffer)
    targets = {...}  # {symbol: 目标权重};权重和可 < 1(持现金)、可为空(观望)
    return targets   # 或 StrategyResult(targets, meta)
```

- 规格侧:`strategy_kind: "user_code"` + `code_artifact: {name, commit?}`
  (引用 kind=strategy 的 active 且 `promotion_status=passed` artifact;commit
  省略 = 当前 active,入队时冻结进 manifest)。`feature_graph` / `signal_rules` 允许为空(特征由代码自行
  计算);其余 kind 携带 code_artifact 非法。web 通道仍禁代码。
  **首次晋级 screen 通道(#234)**:draft 策略 artifact 经规格声明
  `screen_artifact_bindings: [{kind: "strategy", name, artifact_id,
  commit?}]` 显式绑定 → 编译期放行,入队按 DB 实绑校验并把
  commit + artifact_id 冻结进 manifest(code_artifact);screen RR 的
  `strategy_screen` + `sandbox_provenance` 即 promote 证据,四向校验兜底。
- 执行:建议 `parameters.rebalance_frequency=monthly|quarterly`
  (multi_period,决策日由发布日历推导);single_shot 需冻结快照提供决策
  时点。每个决策日一个一次性容器(`--network none` / 只读 / PIT 物理隔离,
  挂载清单含权重回显与约束视图)。
- 权重语义与越权处理:目标权重 → 信号(score=权重)→ **#91 组合管线**
  (硬约束截断审计 / 风险退出 / 三档资金可行性 / 撮合 / 账本)—— 策略只出
  目标权重,不触任何订单语义。**候选池 = universe 过滤后的 included 集**
  (#254,与 multi_factor 引擎同一口径;不再透传发布全 ready 标的);
  decide 输出的池外/缺执行元数据标的**丢弃记 warning**(#218 兜底不变);
  负权重与超上限由管线约束投影**逐项截断并审计**(constraints 阶段可见)。
- 入队门控(REST+MCP 共享):artifact 非 active / commit 与 active 不一致 /
  沙箱未启用 / single_shot 缺快照 → 秒级 `invalid_argument`;
  **screen 绑定(#234)例外**:显式绑定的 draft 产物实绑校验通过即放行。
- report:`sandbox_provenance` 段归档 code commit + 沙箱镜像 digest +
  逐决策 targets checksum;`strategy_screen` 段给出机器 screen 指标
  (rank_ic / 换手 / n_periods,#234);与 multi_factor 同一决策日/候选池口径,
  报告**同屏可比**。
- 晋级:screen RR + `finboard_validation_experiment_run`(#233 OOS 门)→
  `finboard_research_code_promote` → `active/promotion_status=passed`
  → 消费门(普通入队/编译)放行。
- v1 边界:逐日决策函数(decide),**不做**事件驱动 on_bar(日内止损 /
  执行形态研究如需,另行开 issue);纯离线研究域,不连 broker 不下单。
