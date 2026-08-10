# FinBoard MCP 工具扩展路线图

本文件记录 `finboard-mcp` 包的 MCP 工具分阶段扩展计划。每个阶段对应一个 issue,
按研究流程顺序(数据 → 因子 → 策略 → 回测 → 模拟 → portfolio)推进。

## 当前状态(2026-08)

**已实现 34 个工具**(issue #108 / #110 / #124 / #125):

| 命名空间 | 工具数 | 工具 | 能力 |
|----------|--------|------|------|
| `finboard.run.*` | 3 | list / get / artifacts | ResearchRun 只读查询 |
| `finboard.ai.*` | 4 | ask / propose_hypothesis / propose_strategy_draft / propose_strategy_diff | AI 草案(问答 / 因子假设 / 策略) |
| `finboard.memory.*` | 7 | remember / list / get / forget / correct / confirm / archive | 研究长期记忆 |
| 数据查询 | 9 | instrument list/get/search、dataset_release list/get、dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota | 标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额(✅ #124) |
| 因子实验室 | 11 | factor_catalog、feature_snapshot list/get/create/job_start/job_status、factor_signal list/get、factor_experiment list/get/create/sync_validation | 因子目录 / 特征快照 / 因子信号 / 因子实验(7 只读 + 4 写,✅ #125) |

## Planned 阶段(#126-#128)

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

### #126 策略规格工具 `finboard.strategy.*`
- **优先级**:中
- **工具**:
  - `finboard_strategy_registry` —— 策略注册表
  - `finboard_strategy_template` —— 无代码策略模板
  - `finboard_strategy_validate` —— 策略规格校验
  - `finboard_strategy_draft` —— 策略草案(agent 可自主生成)
  - `finboard_strategy_publish` —— 发布策略版本
- **复用**:`finboard_backtest.strategy_spec`
- **权限**:只读 + 研究写(draft / publish),自主执行

### #127 回测 + 模拟盘 + 研究运行工具
- **优先级**:中
- **工具**:
  - `finboard_run_create` —— 创建 ResearchRun(agent 可自主执行)
  - `finboard_backtest_*` —— 回测(行情回放 + 纸面撮合)
  - `finboard_simulation_*` —— 模拟盘(查询 / 启动)
- **复用**:`finboard-backtest` engine + `finboard-simulation`
- **权限**:研究写,自主执行;**禁止**自动晋级实盘

### #128 portfolio 计算工具 `finboard.portfolio.*`
- **优先级**:低
- **工具**:
  - `finboard_portfolio_allocate` —— 目标仓位生成
  - `finboard_portfolio_sizing` —— 资金分配 / 三档可行性
  - `finboard_portfolio_feasibility` —— 硬约束 + 风险贡献上限
  - `finboard_portfolio_attribution` —— 归因分析
- **复用**:`finboard_backtest.portfolio` + `PortfolioPipelineAdapter`
- **权限**:研究写,自主执行

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
