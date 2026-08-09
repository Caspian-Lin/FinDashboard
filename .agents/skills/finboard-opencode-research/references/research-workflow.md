# 研究流程全景详解

FinBoard 的研究流程是一条从原始数据到模拟盘评估的闭环。本文件描述每一步的
输入、输出、对应的 FinBoard 模块与(当前或 planned 的)MCP 工具。

> 流程图:数据获取 → 因子分析 → 策略规格 → 回测 → 模拟盘 → 评估

## 步骤 1:数据获取

**目的**:获取历史行情 / 标的元数据,为下游分析提供输入。

- **数据源**:akshare(免费)/ tushare(需 token),通过 `finboard-data` 包接入
- **存储**:行情缓存(Bar 表)+ 标的元数据(Instrument 表)+ 数据集发布(Dataset Release)
- **质量**:数据缺失 / 异常检测 / 覆盖度报告

| MCP 工具 | 状态 |
|----------|------|
| `finboard_instrument_list` / `_get` / `_search` | ✅ #124 |
| `finboard_dataset_release_list` / `_get` | ✅ #124 |
| `finboard_dataset_manifest_list` | ✅ #124 |
| `finboard_data_cache_status` / `_quality_check` | ✅ #124 |
| `finboard_tushare_quota` | ✅ #124 |

## 步骤 2:因子分析

**目的**:从原始数据构造因子,评估其预测能力。

- **因子实验室**(`finboard_backtest.factor_lab`):声明式因子定义
- **因子目录**:白名单候选池(无代码规格驱动,禁止 Python)
- **因子快照**:版本化、可追溯(factor snapshot,带 checksum)
- **因子实验**:登记假设 → 机器验证终态(`MachineValidationOutcome`)绑定

| MCP 工具 | 状态 |
|----------|------|
| `finboard.factor.catalog` / `.snapshot` / `.signal` / `.experiment` | 🔒 #125 |
| `finboard.ai.propose_hypothesis`(生成因子假设草案) | ✅ 已实现 |

## 步骤 3:策略规格

**目的**:用无代码版本化规格组合候选池 / 因子 / 信号 / 目标仓位 / 风控 / 成交假设。

- **策略规格**(`finboard_backtest.strategy_spec`):JSON schema + 白名单约束
- **版本化**:每次发布不可变(immutable),带 checksum
- **禁止**:网页 / MCP 提交 Python / 模块路径 / 可执行表达式

| MCP 工具 | 状态 |
|----------|------|
| `finboard.strategy.registry` / `.template` / `.validate` / `.draft` / `.publish` | 🔒 #126 |
| `finboard.ai.propose_strategy_draft` / `.propose_strategy_diff` | ✅ 已实现 |

## 步骤 4:回测

**目的**:在历史数据上回放策略,用纸面撮合评估绩效。

- **回测引擎**(`finboard_backtest.engine`):行情回放 + 纸面撮合
- **ResearchRun**:统一研究运行生命周期(`RR-` ID,独立表,不写实盘订单)
- **逐阶段 artifact**:manifest / 决策 / 成交 / 绩效,带 trace_id 与 checksum

| MCP 工具 | 状态 |
|----------|------|
| `finboard.run.list` / `.get` / `.artifacts`(查询 ResearchRun) | ✅ 已实现 |
| `finboard.backtest.*` / `finboard.run.create`(创建 + 启动回测) | 🔒 #127 |

## 步骤 5:模拟盘

**目的**:在持久化隔离环境中运行已发布策略,验证实时行为。

- **模拟盘**(`finboard-simulation` 包):独立 `simulation_*` 表(`SIM-` ID)
- **严格边界**:只接受已发布策略和 completed `ResearchRun` 的结构化目标仓位;
  禁止导入 Broker / QMT / CTP,禁止直接创建订单或修改持仓,
  禁止自动晋级影子盘 / 实盘
- **隔离**:订单 / 成交 / 持仓 / 资金 / 时钟与审计全部在独立模拟表

| MCP 工具 | 状态 |
|----------|------|
| `finboard.simulation.*`(查询 / 启动模拟盘) | 🔒 #127 |

## 步骤 6:评估

**目的**:用绩效指标和归因分析判断策略是否可晋级。

- **绩效分析**(`finboard_backtest.metrics`):夏普 / 回撤 / 胜率等
- **归因**:收益分解(因子贡献 / 行业暴露 / 个股选择)
- **正式组合风控**(`PortfolioPipelineAdapter`):long-only `ResearchRun` 由其从
  冻结候选池 / 特征 / 信号生成目标、硬约束、风险退出、三档资金可行性;
  风险贡献上限启用后缺少协方差、数学不可行或不收敛必须在生成研究订单前
  失败关闭

| MCP 工具 | 状态 |
|----------|------|
| `finboard.portfolio.allocate` / `.sizing` / `.feasibility` / `.attribution` | 🔒 #128 |
| `finboard.run.get`(查询 ResearchRun result 含绩效) | ✅ 已实现 |

## 当前 agent 能做什么

agent 的闭环能力(截至 #124):

1. **数据查询**:标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额
   (✅ `finboard.instrument.*` / `.dataset.*` / `.data.*` / `.tushare.*`)
2. **查询**:ResearchRun 列表 / 详情 / artifact(✅ `finboard.run.*`)
3. **生成草案**:因子假设 / 策略草案 / 策略 diff(✅ `finboard.ai.propose_*`)
4. **问答**:金融 / 研究问题,引用项目来源(✅ `finboard.ai.ask`)
5. **记忆**:跨会话积累研究上下文(✅ `finboard.memory.*`)

**不能直接做**(需告知用户限制):
- 查询因子值 / 策略注册表 / 模拟盘状态(🔒 #125-#127)
- 创建 / 启动回测 / 模拟盘 / ResearchRun(🔒 #127)
- portfolio 计算(🔒 #128)

**替代路径**:用 `finboard.ai.ask` 回答研究问题(底层 ResearchAssistant 可读研究
上下文),但结果以 AI 草案形式呈现,需人工核对。
