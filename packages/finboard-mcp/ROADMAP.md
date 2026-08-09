# FinBoard MCP 工具扩展路线图

本文件记录 `finboard-mcp` 包的 MCP 工具分阶段扩展计划。每个阶段对应一个 issue,
按研究流程顺序(数据 → 因子 → 策略 → 回测 → 模拟 → portfolio)推进。

## 当前状态(2026-08)

**已实现 14 个工具**(issue #108 / #110):

| 命名空间 | 工具数 | 工具 | 能力 |
|----------|--------|------|------|
| `finboard.run.*` | 3 | list / get / artifacts | ResearchRun 只读查询 |
| `finboard.ai.*` | 4 | ask / propose_hypothesis / propose_strategy_draft / propose_strategy_diff | AI 草案(问答 / 因子假设 / 策略) |
| `finboard.memory.*` | 7 | remember / list / get / forget / correct / confirm / archive | 研究长期记忆 |

## Planned 阶段(#124-#128)

### #124 数据查询工具 `finboard.data.*`
- **优先级**:高(研究流程的输入,其他阶段依赖)
- **工具**:
  - `finboard_data_instruments` —— 标的元数据(代码 / 名称 / 市场)
  - `finboard_data_datasets` —— 已发布数据集(版本 / 状态 / checksum)
  - `finboard_data_releases` —— 数据发布版本
  - `finboard_data_cache_status` —— 行情缓存覆盖度
  - `finboard_data_quality` —— 数据质量报告(缺失 / 异常)
- **复用**:`finboard-data` 包 + `finboard-api` data routes
- **权限**:只读,自动允许

### #125 因子工具 `finboard.factor.*`
- **优先级**:高(策略构建的前置)
- **工具**:
  - `finboard_factor_catalog` —— 因子目录(白名单候选池)
  - `finboard_factor_snapshot` —— 因子快照(版本化)
  - `finboard_factor_signal` —— 信号计算(无代码规格驱动)
  - `finboard_factor_experiment` —— 因子实验(机器验证终态绑定)
- **复用**:`finboard_backtest.factor_lab` + `factor_research`
- **权限**:只读 + 研究写(快照 / 实验登记),自主执行

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
