# FinBoard MCP 工具扩展路线图

本文件记录 `finboard-mcp` 包的 MCP 工具分阶段扩展计划。每个阶段对应一个 issue,
按研究流程顺序(数据 → 因子 → 策略 → 回测 → 模拟 → portfolio)推进。

## 当前状态(2026-08)

**已实现 104 个工具**(issue #108 / #110 / #124 / #125 / #126 / #127 / #128 / #136 / #137 / #138):

| 命名空间 | 工具数 | 工具 | 能力 |
|----------|--------|------|------|
| `finboard.run.*` | 7 | list / get / artifacts(只读);queue / cancel / replay / lineage(写) | ResearchRun 查询 + 生命周期(3 只读 + 4 写,✅ #127) |
| `finboard.ai.*` | 4 | ask / propose_hypothesis / propose_strategy_draft / propose_strategy_diff | AI 草案(问答 / 因子假设 / 策略) |
| `finboard.memory.*` | 7 | remember / list / get / forget / correct / confirm / archive | 研究长期记忆 |
| 数据查询 | 9 | instrument list/get/search、dataset_release list/get、dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota | 标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额(✅ #124) |
| 因子实验室 | 12 | factor_catalog、feature_snapshot list/get/create/job_start/job_status、factor_signal list/get、factor_experiment list/get/create/sync_validation | 因子目录 / 特征快照 / 因子信号 / 因子实验(8 只读 + 4 写,✅ #125;job_start/status 在 #136 迁移到持久化队列) |
| 策略规格 | 16 | strategy registry/template/list/history/version_get/diff、preset list/get(只读);strategy validate/draft_create/supersede/publish/rollback、preset create/update/delete(写) | 无代码版本化生命周期(8 只读 + 8 写,✅ #126) |
| 回测 | 5 | backtest_strategy_list、backtest_history_list/get(只读);backtest_run(同步)、backtest_history_delete(写) | 行情回放 + 纸面撮合(3 只读 + 2 写,✅ #127) |
| 模拟盘 | 18 | sim_account list/get/create、sim_session list/get/create/start/pause/stop/reset、sim_orders/fills/positions/ledger/audit/report(只读);sim_decision_submit、sim_order_cancel(写) | 持久化隔离模拟盘(10 只读 + 8 写,✅ #127) |
| portfolio | 4 | portfolio_allocate / sizing / feasibility / attribution | 组合计算(纯计算,无 DB 写入,✅ #128) |
| `finboard.job.*` | 4 | job list/get(只读);job enqueue/cancel(写) | 统一后台任务队列监控与提交(2 只读 + 2 写,✅ #136) |
| 数据写操作 | 12 | data_fetch(同步)、fetch_all/sync_universe/bulk_download_start/quality_repair/dataset_release_publish(任务化)、data_config_get/update、etf_sync/batch_confirm/update/review_queue | 数据准备闭环:拉取/批量下载/同步/质量修复/数据集发布/调度配置/ETF 元数据(2 只读 + 10 写,✅ #137) |
| #57 验证实验 | 6 | validation_experiment create/list/get/reject/add_trial/delete | OOS 样本外验证实验元数据 CRUD(2 只读 + 4 写,✅ #138),给因子实验的 validation_experiment_id 提供源头 |

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
- **模拟盘**(17,9 只读 + 8 写):sim_account list/get/create、
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
`data_write.py` 即可,不影响 REST / ResearchAssistant / 现有只读 MCP 工具 /
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
ResearchAssistant / REST 入口与研究产物 / 审计。
