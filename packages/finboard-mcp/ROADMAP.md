# FinBoard MCP 工具扩展路线图

本文件记录 `finboard-mcp` 包的 MCP 工具分阶段扩展计划。每个阶段对应一个 issue,
按研究流程顺序(数据 → 因子 → 策略 → 回测 → 模拟 → portfolio)推进。

## 当前状态(2026-08)

**已实现 117 个工具**(issue #108 / #110 / #124 / #125 / #126 / #127 / #128 / #136 / #137 / #138 / #139 / #140 / #141;#170 / #171 / #172 / #173 / #174 / #175 / #183 / #184 / #186 / #189 / #190 / #203 / #221 为既有工具的执行语义与契约增强 / 新增网格工具):

| 命名空间 | 工具数 | 工具 | 能力 |
|----------|--------|------|------|
| `finboard.run.*` | 7 | list / get / artifacts(只读);queue / cancel / replay / lineage(写) | ResearchRun 查询 + 生命周期(3 只读 + 4 写,✅ #127;list/get/queue 返回 execution_mode single_shot\|multi_period,#183;queue 入队 universe 候选池非空预检,空池秒级 invalid_argument 附排除统计与缺失字段,#186;single_shot 缺冻结快照入队秒级拒绝附 execution_mode 与缺失因子源,multi_period 必须显式声明 rebalance_frequency,#203) |
| `finboard.ai.*` | 4 | ask / propose_hypothesis / propose_strategy_draft / propose_strategy_diff | AI 草案(问答 / 因子假设 / 策略) |
| `finboard.memory.*` | 7 | remember / list / get / forget / correct / confirm / archive | 研究长期记忆 |
| 数据查询 | 9 | instrument list/get/search、dataset_release list/get、dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota | 标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额(✅ #124) |
| 因子实验室 | 12 | factor_catalog、feature_snapshot list/get/create/job_start/job_status、factor_signal list/get、factor_experiment list/get/create/sync_validation | 因子目录 / 特征快照 / 因子信号 / 因子实验(8 只读 + 4 写,✅ #125;job_start/status 在 #136 迁移到持久化队列) |
| 策略规格 | 16 | strategy registry/template/list/history/version_get/diff、preset list/get(只读);strategy validate/draft_create/supersede/publish/rollback、preset create/update/delete(写) | 无代码版本化生命周期(8 只读 + 8 写,✅ #126);validate 返回 universe_precheck(依赖字段缺失 warning + 空池预览,#186) |
| 回测 | 7 | backtest_strategy_list、backtest_history_list/get、backtest_grid_get(只读);backtest_run(同步/异步 + strategy_spec 路由)、backtest_history_delete、backtest_grid_submit(写) | 行情回放 + 纸面撮合 + 批量参数网格(4 只读 + 3 写,✅ #127 + #175;strategy_spec 形态多期再平衡回放 execution_mode,#183;benchmark_symbol 显式基准 + 基准缺失 null,#184;strategy_spec 形态入队 universe 候选池预检,#186;strategy 形态 run_async=true 或按估算工作量自动切换入队 backtest_run 后台任务返回 job_id,#189;selection.factor_version 报错列合法枚举 + grid_get 默认不返回曲线(显式 equity_mode 才返回),#190) |
| 模拟盘 | 21 | sim_account list/get/create、sim_session list/get/create/start/pause/stop/archive/reset、sim_orders/fills/positions/ledger/audit/report(只读);sim_decision_submit、sim_market_event、sim_session_evaluate、sim_order_cancel(写) | 持久化隔离模拟盘(10 只读 + 11 写,✅ #127 + #139) |
| portfolio | 4 | portfolio_allocate / sizing / feasibility / attribution | 组合计算(纯计算,无 DB 写入,✅ #128) |
| `finboard.job.*` | 6 | job list/get(只读);job enqueue/cancel/archive/unarchive(写) | 统一后台任务队列监控与提交(2 只读 + 4 写,✅ #136;archive/unarchive 归档隐藏不删除 + `archived=exclude\|only\|all` 列表过滤 + 批量归档回计数,✅ #221) |
| 数据写操作 | 12 | data_fetch(同步)、fetch_all/sync_universe/bulk_download_start/quality_repair/dataset_release_publish(任务化)、data_config_get/update、etf_sync/batch_confirm/update/review_queue | 数据准备闭环:拉取/批量下载/同步/质量修复/数据集发布/调度配置/ETF 元数据(2 只读 + 10 写,✅ #137) |
| #57 验证实验 | 6 | validation_experiment create/list/get/reject/add_trial/delete | OOS 样本外验证实验元数据 CRUD(2 只读 + 4 写,✅ #138),给因子实验的 validation_experiment_id 提供源头 |
| 自选股 | 7 | watchlist list/get(只读);create/update/delete/add_symbols/remove_symbol(写) | 用户标的组管理:保存常用回测标的集合(2 只读 + 5 写,✅ #140) |
| 报告聚合与导出 | 3 | report_run / report_backtest(只读聚合);report_export(导出 CSV/Markdown 文件) | 可交付报告:聚合 ResearchRun/回测报告并导出文件(3 只读,✅ #141) |

## Planned 阶段(#128)

### ✅ #124 数据查询工具(已完成)
9 个只读工具:instrument list/get/search、dataset_release list/get、
dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota。
复用 `InstrumentRepository` / `ResearchDatasetReleaseRepository` /
`ParquetCache` / `BarQualityChecker` / `shared_tushare_budget`。

### ✅ #125 因子工具(已完成)
11 个工具(7 只读 + 4 写):
- 只读:factor_catalog、feature_snapshot list/get、job_status、
  factor_signal list/get、factor_experiment list/get
- 写:feature_snapshot create(同步构建)/ job_start(异步任务)、
  factor_experiment create(冻结实验)/ sync_validation(同步 #57 终态)

复用 `factor_lab_catalog` / `FeatureSnapshotRepository` /
`FactorSignalRepository` / `FactorExperimentRepository` /
`FactorExperimentValidationService` / `build_price_feature_snapshot` /
`FeatureSnapshotJobManager`。`FeatureSnapshotJobManager` 从 `finboard-api`
迁移到 `finboard-backtest`(纯标准库实现,API 与 MCP 共享,避免循环依赖)。

### ✅ #126 策略规格工具(已完成)
16 个工具(8 只读 + 8 写):
- 只读:strategy registry/template/list/history/version_get/diff、
  preset list/get
- 写:strategy validate(纯计算,不持久化)/ draft_create / supersede /
  publish / rollback、preset create/update/delete

复用 `compile_registered_strategy_spec` / `build_strategy_template` /
`list_strategy_capabilities` / `structured_diff` /
`ResearchStrategySpecRepository` / `StrategyPresetRepository` /
`get_strategy_definition`。MCP 层直接调用 repository + 编译函数(无 FastAPI
依赖);preset 参数校验复用 `get_strategy_definition(kind).params_model`,
绕开 API 路由的 `validate_strategy_params_for_api`(HTTPException 耦合)。
版本生命周期完整:draft → publish → supersede → rollback。

### #127 回测 + 模拟盘 + 研究运行工具(已完成)
26 个工具(12 只读 + 14 写):
- **回测**(5,3 只读 + 2 写):backtest_strategy_list(可用策略 + 参数 schema)、
  backtest_run(同步运行,返回 metrics/equity/fills/snapshots)、
  backtest_history_list/get、backtest_history_delete
- **模拟盘**(18,10 只读 + 8 写):sim_account list/get/create、
  sim_session list/get/create/start/pause/stop/reset、
  sim_orders/fills/positions/ledger/audit/report(只读);
  sim_decision_submit(结构化目标仓位 → 生成订单,不直接创建订单)、
  sim_order_cancel(写)
- **研究运行写**(4,扩展 ``finboard.run.*``):queue(冻结 + 登记 queued)、
  cancel、replay(复制 completed)、lineage(artifact 血缘)

复用 `BacktestEngine` / `BacktestRunRepository` / `SimulationService` /
`SimulationRepository` / `ResearchRunCoordinator` /
`SqlAlchemyResearchRunStore`。模拟盘边界(#83)不变:只接受结构化目标仓位
(不直接创建订单)、不连 broker、不自动晋级实盘。模拟域异常
(``SimulationNotFoundError`` / ``SimulationConflictError`` /
``SimulationTransitionError`` / ``SimulationRiskError``)映射为对应
``McpToolError`` kind(not_found / conflict / invalid_argument)。
ResearchRun 写工具(queue/cancel/replay/lineage)复用与 API 路由相同的
manifest 冻结 + coordinator 模式,不在 HTTP/MCP 请求内执行回测本身(由
离线 worker 调 ``ResearchRunCoordinator.execute`` 完成)。

### #128 portfolio 计算工具(已完成)
4 个纯计算工具(无 DB 写入,无副作用):
- `finboard_portfolio_allocate` —— 目标权重分配(equal_weight /
  inverse_volatility / erc)+ 约束 + 风险报告
- `finboard_portfolio_sizing` —— 离散手数 sizing(目标权重 → 可执行手数 +
  费用 / 保证金 / 滑点)
- `finboard_portfolio_feasibility` —— 固定资金档位(10万/20万/50万)可行性评估
- `finboard_portfolio_attribution` —— 绩效归因分解(需协方差,≥30 观测)

复用 `finboard_backtest.portfolio`(`build_portfolio` / `solve_sizing` /
`evaluate_capital_tiers` / `compute_attribution`),输入参数与 REST
`POST /api/portfolio/*` 一致。归为研究写(经 `mcp_readonly_only` 门控,
与 `strategy_validate` 同级——纯计算但不属于只读查询类)。异常映射:
`AllocationError` / `SizingError` / 协方差失败 / `ValueError` →
`invalid_argument`;`permission_denied`(只读模式)。`to_jsonable` 对嵌套
dataclass 列表逐元素序列化(顶层是 list 时 `dataclasses.asdict` 不递归)。

### ✅ #136 任务队列工具(已完成)
4 个工具(2 只读 + 2 写),把 #117/#142/#143/#144 建立的持久化
`background_jobs` 队列以受控 MCP 工具暴露:
- 只读:`finboard_job_list`(list_recent,可选 kind/status/queue 过滤)、
  `finboard_job_get`(查单个任务详情,含 result_ref 产物引用)
- 写:`finboard_job_enqueue`(登记 queued 任务,kind 白名单全是研究/数据域)、
  `finboard_job_cancel`(协作式取消,终态幂等返回当前状态)

复用 `BackgroundJobRepository` + `JobOut` schema + `generate_background_job_id`,
不裸 SQL。`enqueue` 的 `_ALLOWED_KINDS` 白名单:`echo` / `research_run` /
`feature_snapshot` / `bulk_download` / `dataset_publish` / `backtest_run` /
`data_sync` / `fetch_all` / `quality_repair`(全是研究/数据域,不含实盘能力)。

同时把 `feature_snapshot_job_start/status`(#125)从已弃用的进程内
`FeatureSnapshotJobManager` 迁移到持久化队列(复用 `enqueue_job` +
`BackgroundJobRepository.get`),与 #144 落地的 REST 口径一致。

边界:任务队列只服务研究/数据/回测类任务;交易内核(盘前检查/收盘撤单/
日终核对/Broker 心跳/Kill Switch)由专用 `finboard-scheduler` asyncio 调度
执行,不进入统一队列。回滚:移除 4 个 `finboard_job_*` 工具即可,不影响
#142/#143/#144 的 worker/任务表/REST 接口。

**#221 扩展(归档,2 写 + list 过滤)**:新增 `finboard_job_archive`(单个
job_id 或按 kinds/statuses/finished_before 批量,批量只回 archived_count,
#206 精神)与 `finboard_job_unarchive`;`finboard_job_list` 加
`archived=exclude(默认)|only|all`。归档是 `background_jobs.archived_at`
展示维度:隐藏不删除、单查始终可达、仅终态可归档、归档即冻结(worker
`requeue_due` 跳过)。回滚:移除 2 个新工具 + archived 参数即可。

### ✅ #137 数据写操作工具(已完成)
12 个工具(2 只读 + 10 写),补全 #124 只读数据查询之外的数据准备能力,
打通「数据→因子→策略」闭环第一步:

- **任务化长耗时(5,写)**:`data_fetch_all` / `data_sync_universe` /
  `data_bulk_download_start` / `data_quality_repair` /
  `dataset_release_publish` —— 复用 `BackgroundJobRepository.create_or_get`
  登记 `queued` 任务并立即返回 202 + `job_id`,与 REST 语义端点口径一致
  (idempotency_key 公式相同 → agent 与 REST 提交同一任务命中同一 job_id)。
  进度/状态/取消统一走 `finboard_job_get` / `finboard_job_cancel`(#136)。
- **同步短任务(1,写)**:`data_fetch` 拉单个标的(主源失败 fallback),直接写
  `ParquetCache`,不进队列(与 REST 一致)。
- **配置读写(2)**:`data_config_get`(只读,读 `data_config.json`)/
  `data_config_update`(写,合并字段)。
- **ETF 元数据闭环(4)**:`etf_sync`(默认 `dry_run=True` 预览)/
  `etf_batch_confirm` / `etf_update`(人工覆盖,写审计流水)/
  `etf_review_queue`(只读)。

复用 `EtfMetadataRepository` / `ParquetCache` / provider /
`ResearchDatasetReleaseCreate`(schema 校验)。`data_config_*` 复刻 REST 的
`_load_config` / `_save_config`(在 `asyncio.to_thread` 内执行避免阻塞事件循环)。
不实现 `bulk_download_status` —— #117 已合并,REST 状态端点已下线,状态查询走
`finboard_job_get`。

边界:写工具受 `_require_write_enabled`(`mcp_readonly_only`)守卫;
`config_get` / `etf_review_queue` 只读自动允许。`requested_by` 统一用
`mcp:<kind>` 前缀(审计区分入口,REST=`api:`,MCP=`mcp:`)。不连 broker /
账户 / 订单 / 持仓。回滚:移除 `register_data_write_tools(mcp)` 调用 +
`data_write.py` 即可,不影响 REST / 现有只读 MCP 工具 /
#117 队列。

### ✅ #138 验证实验工具(已完成)
6 个工具(2 只读 + 4 写),把 REST `/api/research/experiments`(#57 OOS
样本外验证实验系统)的 6 个端点暴露为 MCP 工具,补齐 OOS 过拟合控制闭环:

- 只读:`finboard_validation_experiment_list`(可选 status 过滤)、
  `finboard_validation_experiment_get`(详情 + 全部 trial,含 FAILED/REJECTED)
- 写:`finboard_validation_experiment_create`(假设/计划/门一次性冻结,
  thresholds/robustness 全部有默认值)、`reject`(REJECTED + 原因,
  终态冲突 → conflict)、`add_trial`(登记 trial,预算/终态冲突 → conflict)、
  `delete`(级联删除 trial)

复用 `ResearchExperimentRepository` / `ResearchTrialRepository` /
`new_experiment` / `transition_status` / `increment_trials_used`。
与因子实验(`finboard.factor.experiment.*`,#78/#125)是**两套独立但耦合的
系统**:因子实验通过 `validation_experiment_id` 引用本批工具创建的 #57 实验,
再经 `factor_experiment_sync_validation` 同步终态 —— agent 现在能跑通
「创建验证实验 → 登记 trial → 因子实验引用 → sync 终态」完整链路。

边界:本批只做实验元数据 CRUD,不触发 `ValidationRunner` 执行(长耗时执行
任务化见 #117/#136);揭盲端点(`unseal-final`)未实现,不在本批覆盖(需后续
单独补路由 + MCP 工具)。trial_id 用 `{experiment_id}-mcp-{uuid}` 前缀
(审计区分入口)。不触及交易安全红线。回滚:移除
`register_validation_experiment_tools(mcp)` 调用 + `validation_experiments.py`
即可,不影响 REST 端点 / 因子实验工具 / 研究产物。

### ✅ #139 模拟盘补全工具(已完成)
3 个写工具(2 写生命周期 + 1 行情投递),补全 #127 之后的模拟盘生命周期缺口,
agent 现在能跑通完整生命周期:创建账户+会话 → start → 投 K 线撮合 → 提交
决策 → stop → evaluate → archive:

- `finboard_sim_session_archive` —— `POST /api/simulation/sessions/:id/archive`:
  stopped → archived,账户同步归档(`account.status=ARCHIVED`),记
  `session_archived` 审计;归档后仅 reset 可用。
- `finboard_sim_market_event`(⭐)—— `POST /api/simulation/sessions/:id/
  market-events`:投递单条 OHLCV K 线(`SimulationBarIn`)驱动撮合引擎,
  仅 running 会话接受;按 `source_event_id` 幂等(同 id + 同 checksum →
  `duplicate=true`,同 id 不同内容 → conflict),市场时钟禁止倒退;返回
  `SimulationProcessOut`(duplicate/fill_ids/rejected_order_ids/equity/
  clock_at)。**这是 agent 推进模拟盘撮合的唯一入口。**
- `finboard_sim_session_evaluate` —— `POST /api/simulation/sessions/:id/
  evaluate`:仅 stopped 会话,计算交易日数 + failed 事件 + max_drawdown,
  设 `promotion_status=ELIGIBLE/FAILED`;`minimum_trading_days` 默认 2;
  `automatic_live_promotion` 恒 False(遵守 #83 边界,不自动晋级实盘/影子盘)。

复用 `SimulationService.process_bar / evaluate_session / transition_session`
+ `simulation_schemas` 的 `SimulationBarIn / SimulationProcessOut /
`SimulationEvaluateIn`。写工具受 `_require_write_enabled` 守卫。批量行情
回放(如回测数据驱动模拟)不在本批,后续可扩展批量工具或接入 #117 任务化。
不触及交易安全红线。回滚:移除 `sim_session_archive` / `sim_market_event` /
`sim_session_evaluate` 三个工具即可,不影响 REST 端点 / 领域服务 / 既有工具。

### ✅ #140 自选股工具(已完成)
7 个工具(2 只读 + 5 写),把 REST `/api/watchlists`(routes/watchlist.py)的 7 个
端点暴露为 MCP 工具,agent 可管理「用户标的组」—— 保存常用回测标的集合,
为回测 / 研究准备标的池(前端 Backtest.tsx 已用 watchlist 作为回测标的
选择器,#39):

- 只读:`finboard_watchlist_list`(全部标的组 + 成员数)、
  `finboard_watchlist_get`(详情 + 成员列表 symbols)
- 写:`finboard_watchlist_create`(name + description?)、`update`(partial:只更新
  提供的字段,不传保持原值 —— 比 REST PUT 全量语义更安全)、`delete`(DB
  外键 `ondelete=CASCADE` 级联删除成员)、`add_symbols`(输入按序去重 +
  已存在跳过,不触发 `(watchlist_id, symbol_code)` 唯一约束冲突)、
  `remove_symbol`(不存在时为空操作,与 REST 一致)

复用 `WatchlistRepository`(REST 路由直接调用 repository,无独立 service 层,
MCP 与 REST 同层同行为);404 语义 → `not_found`;`symbol_code` 是普通
`String(20)` 代码(如 `000001.SZ`),无外键约束指向 instruments。写工具受
`_require_write_enabled`(`mcp_readonly_only`)守卫。自选股与 strategies /
simulation 无关联,是独立用户管理查找列表,不触及交易安全红线。回滚:
移除 `register_watchlist_tools(mcp)` 调用 + `watchlists.py` 即可,不影响
REST 端点 / 既有工具。

### ✅ #141 报告聚合与导出工具(已完成)
3 个只读工具,让 agent 能「生成可交付报告」—— 聚合研究运行 / 回测报告并导出
为文件(全仓此前无任何 PDF/CSV/Excel 导出能力):

- `finboard_report_run` —— 聚合单个 ResearchRun:run 元信息 + `result` 指标
  (ResearchRunReport 扁平字段)+ 全部 artifacts(含 report / equity / decisions
  各阶段 payload),结构化为 envelope.data
- `finboard_report_backtest` —— 聚合单条回测历史:运行元信息 + metrics +
  equity_curve + fills + summary(标准化结构)
- `finboard_report_export` —— 把聚合报告导出为 **CSV / Markdown** 文件,写入
  `FINBOARD_EXPORT_DIR`(缺省系统临时目录下 `finboard_exports`),返回绝对路径
  与文件元信息;只读(不写 DB)

聚合与渲染在 `finboard_mcp/reporting.py`(纯标准库 `csv` + 字符串模板,**零新
依赖**,PDF 留后续 issue);复用 `ResearchRunRepository` / `BacktestRunRepository`。
CSV 带 UTF-8 BOM(Excel 打开中文不乱码)、每节 `# 标题` 注释行 + 表头 + 行、
节间空行;Markdown 为 `##` 分节表格。模拟盘报告导出不在本批(`finboard_sim_report`
已覆盖生成,导出可后续扩展 `kind=sim`)。与 #117/#136 协同:报告生成若长耗时
(如含归因重算)可后续任务化。不触及交易安全红线。回滚:移除
`register_report_tools(mcp)` 调用 + `reports.py` / `reporting.py` 即可。

### ✅ #170 multi_factor 信号引擎(已完成)
已发布 multi_factor 规格经 `finboard_run_queue` → worker 端到端执行(信号引擎
+ 组合流水线 → 14 stage artifacts → COMPLETED);执行失败(如快照缺因子源)时
`finboard_run_get` 可见错误码与摘要,`research_runs` 不停留在 QUEUED。其余
strategy kind(etf_rotation 等)仍明确报 not_implemented。实现位于
`finboard_backtest.research_run.signal_engine`(FeatureGraph 算子 +
SignalRules comparator 求值 + universe 过滤 + 协方差估计),worker 侧
`build_signal_engine_adapter_factory` 按 kind 分发。

### ✅ #171 research_data_sync 任务(已完成)
`finboard_job_enqueue(kind=research_data_sync)` 编排 research 数据表
(估值 / 财务 / 行业)摄取:profiles 全量一次、daily 逐交易日、financial /
industry 逐标的;逐标的接口接入 tushare_budget 限流,确定性 dataset_version
断点续跑,单类失败不覆盖已发布数据,重复执行幂等。kind 白名单同步
`_INSTRUCTIONS` / Skill `tools.md` / REST `/api/jobs`。根元包启用
`finboard-data[tushare]` extra(未装 SDK / 未配 token fail-fast 报错可操作)。

### ✅ #172 回测返回体积控制(已完成)
`finboard_backtest_run` / `finboard_backtest_history_get` /
`finboard_report_backtest` 三处统一 `equity_mode: summary | full`(默认
summary):equity 降采样(首末点保留、时序单调、点数 ≤ max_points,默认 200),
`full` 与现状完全一致;`history_get` 支持 fills 分页(fills_limit /
fills_offset / fills_total)。降采样在 `finboard_mcp/downsample.py`,纯展示层
变换,落库仍存全量。

### ✅ #173 selection 输入模式(bars / snapshot)(已完成)
`backtest_run` 的 `selection.inputs_mode` 支持 `research_db`(默认)/
`bars` / `snapshot`:`bars` 模式纯价格因子(momentum / volatility_20d)从回测
行情计算,不要求 daily_metrics 发布,dataset 未发布降级为快照 warnings 而非
整日 SKIPPED;`snapshot` 模式用 `snapshot_ids` 直接消费冻结 FeatureSnapshot
观测(available_at <= decision_at 过滤)。`required_datasets` 按所选因子依赖
推导。`_INSTRUCTIONS` / Skill `tools.md` 已同步。

### ✅ #175 批量参数网格回测(已完成)
2 个新工具(1 写 + 1 只读),支撑「合理实验」:一个假设下多组参数对比择优,
替代逐个手跑 `backtest_run` 再人工对比:

- `finboard_backtest_grid_submit`(写)—— 一次提交 N 组参数:组合展开
  (显式列表 `params_list` 优先 / 笛卡尔积 `params_grid`)+ **上限封顶**
  (默认 20,硬上限 50,超限 `invalid_argument`,防误操作打爆队列)→
  逐组合参数校验(复用 `_validate_backtest_params`,任一组合非法即拒,不落库
  不入队)→ 网格定义与 N 个 `kind=backtest_run` job **同一事务**落库
  (全有或全无)→ 返回 grid_id + job 指针。网格定义持久化在新增
  `backtest_grid_runs` 表(独立产物表,不建外键;`grid_id` 为 `BTG-` 前缀),
  `grid_idempotency_key` 幂等重提交返回同一网格(组合定义不同 → conflict)。
  每个 job payload 带 `grid: {grid_id, combo_index}` 归属标记(executor 忽略)。
- `finboard_backtest_grid_get`(只读)—— 按 grid_id 聚合对比表:逐组合读 job
  状态 + result_ref 回测记录 → 指标矩阵(`metric_fields` 规范列序,收益/回撤/
  夏普/胜率/超额/换手/费用等)+ 关键指标竞争排名与最优标注(全部「越高越好」,
  max_drawdown 负值越高=回撤越小;同值同排名)+ **失败组合带错误码单列**
  (failed/cancelled/interrupted/产物缺失,不影响成功组合返回)。
  `complete=false` 表示还有组合未到终态。equity 曲线默认**不返回**(#190:
  `equity_mode=none`,只留 `equity_point_count` 点数提示,9 组合 x 200 点约
  100-200KB 不再默认灌爆响应);显式 `summary`/`full` 才降采样/全量返回
  (summary 复用 #172 降采样形态,默认 200 点,首末保留)。顺带修 worker 兜底日志:ExecutorError
  是 dataclass,`str()` 为空导致任务行 error_summary 丢失 —— 优先取
  `.summary`。

边界:研究域 only(#136)—— 只入队白名单内的 `backtest_run` 任务,不连
broker / 账户 / 订单 / 持仓;`_INSTRUCTIONS` / Skill `SKILL.md` +
`tools.md` 已同步(工具总数 113 → 115)。回滚:移除
`register_grid_tools(mcp)` 调用 + `grid.py` + 迁移 downgrade
(`drop_table backtest_grid_runs`)即可,不影响既有回测 / 队列工具。

### ✅ #187 冻结发布多数据集联合发布(已完成)
`dataset_release_publish` 的 `release_kind` 扩为 4 值,补上研究数据冻结发布
(工具契约已同步):

- `release_kind=daily_metrics|financial_indicators`:从 `research_*` 表
  (research_data_sync 摄取)冻结基本面/财务指标发布——字段白名单
  (`DAILY_METRICS_FIELDS` / `FINANCIAL_INDICATORS_FIELDS`)、parquet 冻结
  (每标的按 kind 目录)、质量门(覆盖率 + 元数据完整;`all_null_fields` 仅作
  可见 warning)、同名 dataset 版本递增 `schema_version`。A 股校验要求
  `source=tushare`,`adjustment` 固定 `none`。
- 联合消费:因子快照可引用 bars 主发布 + research 发布的联合集,产出
  pb / 市值 / 换手 / ROE 等基本面因子;`strategy_validate` 的
  `dataset_release_ids` 可同时引用多份发布(universe 预检仍落 bars 主发布)。
- `_INSTRUCTIONS` / Skill `tools.md` 已同步。回滚:不接受新 release_kind
  即回退,不影响既有 bars 发布(REST/MCP 同源)。

### ✅ #206 研究/回测 MCP 返回瘦身(已完成)
按 P0→P3 逐级瘦身,默认响应从 MB 级降到 KB 级,决策信息量不变。全部为纯
展示层变换,不改语义、不动落库(与 #172 equity 降采样同先例):

- **P0 详情接口 `view=summary|detail`,默认 summary**:
  - `dataset_release_get`:summary 复用 `_release_summary_to_dict`(头部 +
    capabilities + 覆盖统计),**省略逐标的 instruments 数组**(全市场发布
    实测 ~97MB);detail 走 `as_dict()`(诊断用)。
  - `run_get` / `report_run`:summary 把 universe 逐标的判定聚合为
    `{total, included, excluded_by_reason}`(服务端聚合,与全量判定一致)、
    fills 按决策计数、metrics 剔除 equity_curve(以点数提示),不序列化
    manifest/result/UNIVERSE/FILLS 全量 payload;detail 保留现行为。
- **P0 fills 分页有界**:`backtest_history_get` 默认 `fills_limit=200`
  (`fills_total` 元数据已有);`report_backtest` 补 `fills_limit/offset`
  分页(默认 200,`null` 全量);REST/MCP 导出文件保持全量。
- **P1 写操作回显瘦身**:`run_queue` / `strategy_draft_create` /
  `strategy_publish` 返回精简回执(id/version/status/checksum/
  execution_mode/created_at + detail_hint),全文走对应 get;
  `backtest_run(strategy_spec)` 路由同步适配。
- **P1 grid 公共字段上提**:`grid_get` 网格级公共字段(strategy/symbols/
  start/end/capital/adjust/params=base_params)在头部只出现一次,combo 去掉
  params(组合差异由 label 承载,完整参数 = params + label)。
- **P2**:`backtest_history_list` symbols 前 10 只 + `symbol_count`;
  `job_get` 加 `view=none|summary|detail`(none=轮询最小集,summary 默认剥
  payload);信封 `ToolEnvelope` 挂 pydantic 自定义序列化,序列化时省略恒为
  null 的可选字段(MCP SDK structured content 路径生效,工具/测试拿到的仍是
  dataclass 实例)。
- **P3 幂等短路**:`job_get` 返回附 `data_hash`(状态指纹),轮询回传未变即
  `{unchanged: true, data_hash, status}` 不重发全量。

`_INSTRUCTIONS` / Skill `SKILL.md` + `tools.md` 已同步。回滚:默认 view/fills
上限恢复全量即可(各参数均显式可传),信封序列化不影响 dataclass 消费方。

## 扩展原则(适用于所有阶段)

1. **复用现有 service / repository**,MCP 层不直接裸 SQL
2. **统一信封** `ToolEnvelope`(operation_id / status / data / error /
   provenance / idempotency_key)
3. **审计**:每次工具调用记录 structlog 结构化日志 + 内存副本,
   不记录 API Key / 原始凭证 / 未脱敏思考内容
4. **权限边界**:研究写操作 agent 自主执行(#122);实盘能力
   (下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)**永久不注册**
5. **同步更新**:每个 issue 完成时必须同步更新
   - `server.py` 的 `_INSTRUCTIONS`(工具清单)
   - Skill `references/tools.md`(工具契约)
   - Skill `SKILL.md`(工具选择表)

## 回滚方案

MCP 是受控入口,回滚方案为**关闭 MCP 入口**(不启动 server),不影响现有
REST 入口与研究产物 / 审计。
