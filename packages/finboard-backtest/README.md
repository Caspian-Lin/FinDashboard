# finboard-backtest

事件驱动历史回放、纸面撮合、绩效分析、按日因子选股、策略层 Bar 规则选股与
样本外验证流水线。

## 样本外验证流水线(issue #57)

`finboard_backtest.validation` 子包提供研究级**防过拟合**验证流水线,所有新增
收益策略进入模拟 / 影子交易前必须通过此流水线。

### 从假设到揭盲的标准流程

```
[1] 登记实验 ──→ [2] IS 参数搜索 ──→ [3] Walk-forward OOS
       │                                   │
       │                                   ↓
       │                            [4] 稳健性 / 压力测试
       │                                   │
       │                                   ↓
       │                            [5] 统计修正报告
       │                                   │
       ↓                                   ↓
[6] 门判定(Pass / Reject) ←─────────────┘
       │
       ↓
[7] 一次性揭盲最终冻结测试集 ──→ [8] VALIDATED_OOS 或 REJECTED
```

**核心原则**:

* **假设先行冻结**:``ResearchExperiment.hypothesis`` 创建后不可修改,
  修改假设必须新建 superseding experiment;
* **试验预算预先声明**:``trial_budget`` 限制候选总数,失败也算试验;
* **最终揭盲只允许一次**:``final_test_unsealed`` 一旦 True 不可撤销;
* **门在实验前冻结**:``AcceptanceThresholds`` 在创建时确定;
* **统计门只降低虚假发现概率,不承诺未来收益**。

### 数据契约

* `ResearchExperiment` —— 假设、计划、门、揭盲状态;
* `ValidationPlan` —— rolling / expanding walk-forward 时间切分;
* `AcceptanceThresholds` —— IS / OOS / DSR / PSR / PBO 阈值;
* `TrialRecord` —— 单次试验完整记录(包括 FAILED / REJECTED);
* `WindowMetrics` —— 训练 / 验证 / 测试单窗口绩效;
* `RobustnessProbe` —— 邻域 / 成本 / 延迟 / 市场阶段压力测试结果;
* `StatisticalReport` —— DSR / PSR / PBO / bootstrap CI。

### 统计方法来源与局限

| 方法 | 来源 | 适用条件 | 局限 |
|------|------|----------|------|
| Stationary bootstrap CI | Politis & Romano 1994 | 平稳时序 | 对结构性断点不敏感 |
| Deflated Sharpe Ratio | Bailey & López de Prado 2014 | 多重试验修正 | 假设 SR 近似正态 |
| Probabilistic Sharpe | López de Prado 2012 | 单 trial 显著性 | 依赖偏度 / 峰度估计 |
| PBO via CSCV | Bailey et al. 2017 | N ≥ 4 trial + 长样本 | 对 IS/OOS 切分敏感 |

**关键警告**:统计门**不承诺未来收益**。即使 PBO < 0.5 且 DSR > 0.95,策略
仍可能在样本外失败。所有阈值必须在实验前声明,不得事后调整。

### 已知局限

* 当前 ``TrialRunner`` 是抽象接口,实际执行由 CLI / 后台 worker 注入;
  API 端点只持久化实验结构,不直接触发回测。
* ``PBO`` 在样本较短时(n_obs < n_partitions × 2)返回保守估计 0.5;
  应确保每次试验至少 200+ 观测点。
* ``robustness.py`` 中的成本 / 滑点 / 延迟压力配置在 ``RobustnessPlan`` 中
  声明,但实际执行需要 runner 阶段注入对应的回测配置覆盖。
* 该流水线**不触及交易红线**:不连接券商、不发订单、不修改持仓 / 风控。

## 研究级成交语义(issue #56)

`BacktestBroker` 自 issue #56 起采用**研究级成交语义**,消除"同 Bar 收盘穿越"
导致的乐观偏差,并按资产类型分发真实撮合规则。**任何依赖 v1 行为的旧回测都
不可与 v2 结果横向比较** —— `BacktestResult.matching_model.matching_model_version`
显式标记版本,旧 run 自动归类为 `v1 / unvalidated`。

### 信号 → 成交时间线

| 时刻       | 事件                                              |
|------------|---------------------------------------------------|
| T 日收盘   | 策略 `on_market_data` 收到 Bar,生成信号          |
| T 日收盘   | 策略调用 `ctx.submit_order` → broker 收单 → SUBMITTED |
| T+1 Bar    | broker 在新 Bar 的 `open` / `close` / `vwap_proxy` 成交 |
| T+1 Bar    | fill 推送到 `on_order_update`,**策略内部持仓由 fill 更新** |

约束:

* 同 Bar 收单 → 同 Bar 不成交(`MatchingModel.next_bar_only=True`)。
* 拒单 / 部分成交 / 撤单都不会让策略 `_positions` 漂移 —— 持仓**只能**由
  `on_order_update` 中的 fill 驱动。
* `allow_short=false` 时,卖出前严格校验可用持仓,不足则按 `available` 截断
  或 `INSUFFICIENT_POSITION` 拒单,现金 / 持仓都不变。

### 资产规则矩阵

`AssetRuleTable` 按 `(Market, InstrumentType)` 解析规则,未注册的类型 raise
`AssetRuleResolutionError`(**fail closed**)。默认目录覆盖 A 股股票 / 股票 ETF /
指数;新增资产类型必须显式注册,不再回退到 A 股默认值。

| 资产类型       | 手数 | T+N   | 印花税 | 涨跌停 | 价格步长 |
|----------------|------|-------|--------|--------|----------|
| A 股股票       | 100  | T+1   | 万 5(卖) | ±10%   | 0.01     |
| 股票 ETF       | 100  | T+1   | 万 5(卖) | ±10%   | 0.001    |
| 债券 ETF       | 10   | T+1   | 免     | 无     | 0.001    |
| 货币 ETF       | 100  | T+0   | 免     | 无     | 0.001    |
| 跨境 ETF       | 100  | T+0   | 免     | ±10%   | 0.001    |
| 可转债         | 10   | T+0   | 万 5(卖) | ±20%   | 0.001    |
| 期货           | 1 手 | T+0   | 按合约 | 按合约 | 按合约   |

每次规则变更必须 bump `ASSET_RULES_VERSION`;历史回测结果归档在
`BacktestResult.asset_rules.rule_version`,使旧 run 不可直接横向比较。

### 撮合 / 费用 / 滑点假设归档

`BacktestResult` 同时归档:

* `matching_model` —— 撮合模型版本、fill timing、参与率上限、是否允许部分成交等;
* `asset_rules` —— 完整的资产规则目录(含费率、印花税、手数、价格步长);
* `fee_assumptions` —— 回测请求显式覆盖的费用 / 滑点;
* `benchmark_config` —— 基准选择(单标的 / 等权候选池)。

这些字段都写入数据库 `backtest_runs.matching_model / asset_rules / fee_assumptions /
benchmark_config`,历史 run 可完整复现。

### 多标的基准

issue #56 起,多标的回测的默认基准**不再只取请求中的第一个标的**,而是使用
`BenchmarkConfig`:

* `symbol` 显式指定基准(如 `000300.SH`);
* `equal_weight_universe=True`(默认)使用等权候选池基准;
* 单标的回测保持原行为(等于买入持有基准)。

### 已知局限

* 期货合约的多空 / 开平 / 保证金 / 每日盯市由 issue #64 的期货子模块实现,
  当前 `BacktestBroker` 仅消费 `AssetRule`(lot_size / T+N / 印花税 / 涨跌停)。
* 公司行为(分红 / 除息 / 拆股)目前依赖前复权(`adjust="qfq"`)数据源,
  未独立建模;`BacktestConfig.adjust` 是当前唯一可控入口。
* 因子选股(`selection`)与撮合模型正交,可独立启用 / 关闭。

## 因子快照时序

`BacktestConfig.selection` 默认 `enabled=false`,因此旧回测继续把静态
`symbols` 全部交给策略。启用后,引擎按交易日批处理:

1. T 日所有标的行情先统一更新,消除输入标的顺序造成的横截面偏差。
2. T 日上海时间 17:00 仅使用 `available_at <= decision_at` 的已发布研究数据,
   计算并冻结因子值、全局/行业排名和候选池。
3. 快照从下一实际回放交易日生效,并通过 `UniverseSelectionEvent` 交给策略。
4. 数据缺失、陈旧、来源混入或行业覆盖不完整时整次调仓标记为 `skipped`;
   不使用部分横截面,并保留上一有效候选池。

首批 `factor_version=v1` 支持总市值、PB、换手率、动量、ROE、毛利率和营收同比。
输出始终是静态 `symbols` 的子集。候选池只限制策略可见行情,不产生订单,也不访问
券商、持仓或风控组件。

`BacktestResult` 会归档 `selection_snapshots`、`dataset_versions` 和
`factor_version`,用于复现与审计。需要回滚时关闭 `selection.enabled` 即可恢复
旧行为。

## 统一 ResearchRun 编排(issue #80)

`finboard_backtest.research_run` 提供纯离线状态机和统一适配器端口。六类注册策略把
既有研究结果归一为 `DecisionBundle`;编排器依次持久化候选池、特征、信号、
约束前后目标、离散计划、研究订单、研究成交和账本，并强制成交驱动持仓及现金/
市值/权益恒等式。

核心入口:

- `ResearchRunManifest`:冻结策略、数据/因子、参数、成本、基准和代码版本。
- `DecisionSequenceAdapter`:#29/#60-#64 的统一规范化入口。
- `ResearchRunCoordinator`:执行、取消、重启恢复、幂等 checkpoint、重放和血缘。
- `InMemoryResearchRunStore`:单元测试/纯离线任务。
- `SqlAlchemyResearchRunStore`(`finboard_app`):PostgreSQL 持久化实现。

该包不依赖 Broker 或实盘 Repository。研究订单/成交使用 `RR-` ID，任何未成交目标
都不能进入持仓。详见
[`docs/research_run_lifecycle.md`](../../docs/research_run_lifecycle.md)。

## 策略层 Bar 规则选股 (`bar_universe`)

`BarUniverseSelector` 是不依赖因子系统的研究级选股器,只消费策略已经收到的
当前/历史 Bar,不需要任何 I/O 或研究数据。**默认 `mode="all"` 不改变现有策略
行为**;`mode="liquidity_momentum"` 启用后,会在指定回溯窗口内按以下规则筛选:

* **预热**:`lookback` 根 Bar 填满前标的恒不入选,避免读取未来数据;
* **平均成交额**:窗口内平均成交额 ≥ `min_avg_amount`(留空表示不校验);
  成交额缺失或为 0 时回退为 `close × volume`;
* **区间动量**:`close[-1] / close[0] - 1 ≥ min_momentum`(留空表示不校验);
* **退出清仓**:策略可在 `universe_exit_clear=True` 时通过 `ctx.submit_order`
  对已有内部持仓发出卖出意图,卖出仍走完整 Risk → OrderManager → Broker 链路。

约束(issue #43):

* 只能缩小回测请求的静态候选池,不能添加新标的或触发数据下载;
* 多标的状态相互隔离,异常 Bar(非正价格、负成交量/成交额)安全不入选;
* 不实现行业、市值、基本面和跨截面排名 —— 这些能力由 `selection` 模块的
  因子快照提供,需要独立的数据契约。

参数通过策略 `MaCrossParams` 暴露,前端表单由 `GET /api/backtest/strategies`
返回的 schema 自动生成,预设保存走 `strategy_presets`。扩展新规则时:

1. 在 `BarUniverseMode` 添加新模式;
2. 在 `BarUniverseSelector.update` 增加分支判定;
3. 在策略 schema(`MaCrossParams` 或新策略)以 Pydantic Field 暴露参数;
4. 不需要修改前端 —— schema-driven 表单会自动渲染新字段。


## 组合构建层(issue #59/#81)

`finboard_backtest.portfolio` 子包提供独立的组合构建层:策略输出标准化
`Signal` / `TargetWeight`,组合层统一生成调仓订单意图。

### 分配方法

| 方法               | 说明                        | 需要协方差 |
|--------------------|-----------------------------|-----------|
| `equal_weight`     | 等权分配                     | 否         |
| `inverse_volatility` | 逆波动率(1/sigma 加权)     | 是         |
| `erc`              | 等风险贡献(每标的 RC 相等)  | 是         |

所有方法支持以下硬约束(通过 `PortfolioConstraints` 配置):

* `max_weight_per_asset`(默认 0.25):单标的权重上限。
* `max_weight_per_sleeve`(默认 0.40):单资产类别权重上限。
* `min_cash_buffer`(默认 0.05):最小现金比例。
* `max_leverage`(默认 1.0):杠杆上限;**默认无杠杆**。
* `target_volatility` / `max_volatility`:年化波动率目标 / 上限。
* `rebalance_threshold`(默认 0.05):再平衡带(权重偏离 < 5% 不调仓)。

issue #81 的 v2 契约已将 `max_leverage` 配置上限与实际
`gross_exposure` / `net_exposure` 分开，并让 sleeve 映射、禁用标的、信号冲突、
目标/最大波动率、当前持仓再平衡带真正进入统一 `build_portfolio()` 入口。每项
约束都会输出 before/after/limit/reason，可直接转换成 ResearchRun artifact。

### 协方差估计

`estimate_covariance()` 使用 Ledoit-Wolf 收缩估计,避免样本协方差矩阵
不稳定导致的极端权重。缺数标的有效观测不足时被**排除**(不静默放大
其他标的权重);窗口内样本不足时收缩强度自动增加。

### 离散手数求解

`solve_sizing()` 把目标权重转换为可成交手数,支持:

* 10万 / 20万 / 50万元三档资金(`evaluate_capital_tiers`)。
* A 股 100 股 / 手、ETF 100 或 10 份 / 手、可转债 10 张 / 手、期货乘数。
* 期货保证金、最低佣金、印花税、滑点、参与率和剩余现金非负约束。
* 负现金时贪心减手(从偏离最大的标的开始减)。

退出策略执行器消费无代码 `RiskExitPolicy`，支持价格止损、波动率/ATR、止盈、
最大持有期、组合回撤降风险/暂停和冷却期。它只调整目标仓位，实际持仓仍只能由
研究成交驱动。完整接口、失败关闭语义和安全边界见
[`docs/portfolio_risk_capital.md`](../../docs/portfolio_risk_capital.md)。

### 绩效归因

`compute_attribution()` 按资产 / sleeve 分解:

* 收益贡献(w_i * r_i)
* 风险贡献(边际风险贡献 RC_i)
* 换手贡献(|delta_w| / 2)
* 回撤贡献(近似:权重 * 标的最大回撤)
* 现金利用率和杠杆比率

### 已知局限

* ERC 迭代法在极端协方差(完全正相关 + 零波动率混合)下可能不收敛;
  此时 raise `AllocationError`,不返回不稳定权重。
* 10万元档位的离散误差约 1%~3%,50万元降到 0.2%~0.5%。
* 绩效归因中的回撤贡献是近似分解(不考虑标的间相关性对回撤的影响)。
* 本子包仅用于离线研究 / 回测,**不**修改实盘 Risk Manager / Position
  Manager / 下单链路。
* 实盘 `RiskConfig.max_symbol_position_value` 尚未由 `PreTradeChecker` 执行；
  这是独立的真实资金安全事项，本离线组合约束不能替代它。


## 多因子研究框架(issue #60)

`finboard_backtest.factors` 子包提供 A 股复合因子研究管线:
因子目录 → 横截面标准化 → 中性化 → 复合评分 → 组合选择 → 因子分析。

### 因子目录

13 个已有计算实现的因子覆盖 6 个类别,每个声明经济假设、方向、极值处理和
缺失策略。目录采用 fail-closed 语义,没有可复现计算实现的因子不会暴露:

| 类别   | 因子                                                         |
|--------|--------------------------------------------------------------|
| 估值   | `pb`, `earnings_yield`(1/PE_TTM), `dividend_yield`         |
| 质量   | `roe`, `gross_profit_margin`, `debt_to_assets`              |
| 低风险 | `volatility_20d`, `volatility_60d`, `volatility_120d`, `downside_volatility` |
| 流动性 | `turnover_rate`                                              |
| 动量   | `momentum`                                                  |
| 增长   | `revenue_yoy`                                                |

A股传统价格动量证据分歧大,因此动量作为**待检验补充**,而非预设有效。
核心配置以价值×质量×低波为主。

### 标准化管线

```
原始值 → winsorize(1%/99%截尾) → zscore 或 rank_normalize
       → (可选)行业中性化(减行业中位数)
       → (可选)市值中性化(OLS残差)
       → 方向调整(SHORT因子乘-1使高分=好)
```

### 复合评分

* **加权平均** (`weighted_average`):权重在实验前声明,禁止事后调参。
* **排名平均** (`rank_average`):对各因子横截面排名取加权平均,对极端值更鲁棒。

权重之和必须接近 1.0(±0.02 容差),否则拒绝。

### 组合选择

支持进入/退出缓冲(hysteresis):
* 新持仓需进入 `top_n - entry_buffer` 才入场。
* 旧持仓需跌出 `top_n + exit_buffer` 才退出。
* 行业上限(`max_per_industry`)防止集中。

### 因子分析

`compute_factor_analysis()` 输出:
* 各因子 Rank IC 和 IC 信息比率。
* 因子间平均相关性矩阵。
* 组合在各因子上的平均暴露。
* 平均换手率、平均持仓数。
* 资金档位可行性(10万/20万/50万元)。

### 已知局限

* 残差动量 `residual_momentum` 尚无可复现的残差回归实现,因此不在公开目录中;
  实现计算、PIT 数据依赖和测试后才能登记。
* `downside_volatility` 在牛市中系统性偏低,需要结合全样本波动率使用。
* 因子分析中的 IC 计算需要前瞻收益,仅用于回测后分析,不能用于实盘信号。
* 本子包仅用于离线研究 / 回测。

## 因子实验室、风险模型与跨市场特征(issue #78)

`finboard_data.factor_lab` 和 `finboard_backtest.factor_lab` 把研究链路拆成三个不可
混淆的产物:

```text
冻结数据发布 → FeatureSnapshot → alpha 分析 → FactorSignal
                              ↘ 风险暴露 / 市场输入(不产生信号)
```

- `FeatureSnapshot` 是某一决策时点的不可变输入切片,记录发布版本、窗口、
  `available_at`、转换、中性化、缺失处理、代码版本和内容校验和。
- `FactorSignal` 是 alpha 因子的横截面研究输出,逐标的记录方向、分数、置信度、
  有效期、候选池版本和来源快照。它不是目标仓位、订单或成交指令。
- 风险因子和其他市场输入只能用于解释、约束或状态分层,不能伪装成 alpha 信号。

### 版本化目录与数据字典

目录 `FACTOR_LAB_CATALOG` 按角色明确分组:

| 角色 | 已登记输入 | 使用边界 |
|---|---|---|
| alpha | 估值、质量、增长、20/60/120 日波动率、下行波动率、换手率、动量 | 经过样本外验证后才可发布为 validated signal |
| risk | 市场 beta、行业、资产类别、规模、波动率、流动性 | 只解释组合风险与集中度 |
| market_input | 无风险利率、国债收益、美元兑人民币、黄金、市场宽度、波动状态 | 只做状态分层和条件分析 |

每个定义固定 `name/version/role/frequency/window/direction/preference/unit`、
输入字段、转换、中性化、缺失策略、经济假设、发布时间规则、代码版本和校验和。
调用方应持久化目录版本与快照 checksum,不能用同名的新实现静默覆盖历史实验。

### Alpha 分析方法

`analyze_alpha_factor()` 在冻结的周期和候选池上计算:

- Spearman Rank IC、Pearson IC、ICIR、t 统计量和近似双侧显著性;
- 分位数组合、毛/净多空收益、逐期换手和交易成本;
- 多持有期衰减、参数邻域稳定性、牛熊/波动状态分层。

前瞻收益只允许出现在离线标签与评估阶段。报告不声称统计相关必然可交易;多重
检验、幸存者偏差、容量、滑点、税费和样本选择仍须由 #57 的实验协议覆盖。

### 风险模型

`estimate_basic_risk_model()` 独立估计市场 beta、行业/资产类别哑变量、对数规模、
年化波动率和流动性暴露,并使用 Ledoit-Wolf 收缩协方差计算组合波动率、边际和
总风险贡献。标的元数据缺失、基准无效或协方差不可用时 fail closed,不会把风险
因子分数混入 alpha 排名。该模型是研究级基础模型,不是实盘 Risk Manager。

### 跨市场输入与 PIT 规则

`build_cross_market_snapshot()` 只让
`available_at <= decision_at` 的观察值进入快照,并逐项保留来源、数据版本、
业务日期、发布时间、缺失/陈旧状态。同一输入流中的未来发布值会被隔离;若过滤后
没有决策时点可见值则按目录策略拒绝,不做无来源的隐式填充。当前覆盖利率/债券、
汇率、黄金、市场宽度和波动状态,主要用于 regime 分层,不直接产生买卖方向。

### 实验与 API

因子实验冻结数据发布、候选池、训练/验证/测试窗口、试验预算、基准、因子版本、
代码版本和成本假设。状态包含失败与中断,可持久化比较历史;只有关联的 #57
实验确实达到 `validated_oos` 且选中 trial 含完整 OOS 统计报告时,持久化层才允许
发布 `validated_oos` signal。验证实验的 `candidate_universe_version`、数据发布和
因子版本也必须与信号一致;客户端提交 `passed_oos=true` 之类声明不会生效。

| 方法 | 路径 | 内容 |
|---|---|---|
| `GET` | `/api/research/factors/catalog` | 版本化因子、风险因子和市场输入目录 |
| `POST` | `/api/research/factors/features/jobs` | 异步启动特征快照计算,返回 job 状态 |
| `GET` | `/api/research/factors/features/jobs/{job_id}` | 查询计算进度、已耗时和预计剩余时间 |
| `GET` | `/api/research/factors/features` | 快照列表;支持 release 筛选 |
| `GET` | `/api/research/factors/features/{snapshot_id}` | 快照、观测值和完整 lineage |
| `GET` | `/api/research/factors/signals` | 信号列表;支持 factor/status 筛选 |
| `GET` | `/api/research/factors/signals/{signal_id}` | 逐标的分数、置信度、有效期与来源 |
| `POST` | `/api/research/factors/experiments` | 登记冻结实验计划 |
| `POST` | `/api/research/factors/experiments/{id}/sync-validation` | 从 #57 实验状态同步结论 |

这些端点只登记或读取研究产物,不会启动策略、生成目标仓位、访问 Broker 或执行
订单。LLM 可以解释目录和辅助形成假设,但不能声明样本外通过或把信号送入实盘。

特征快照页面默认使用异步 job 入口:服务端按
`FINBOARD_FEATURE_SNAPSHOT_PROCESS_WORKERS`(默认 8)启动独立 spawn 计算进程,每个进程
读取并校验一个冻结标的,主 API 进程只负责调度、收集结果和更新进度。进程内仍按
`FINBOARD_FEATURE_SNAPSHOT_MAX_CONCURRENCY`保留兼容的线程 worker 路径,价格特征只读取
`timestamp`/`close` 两列,并按发布中的标的顺序合并结果。任务状态为
`queued/running/succeeded/failed`,前端显示真实完成标的数、已耗时和完成速率推算的 ETA;
尚未完成首个标的时不显示伪造的预计时间。同步 `POST /api/research/factors/features`
保留用于兼容已有调用方,同样使用独立进程计算。该设计不引入 Go/C++ 或微服务,同时
避免大量 Parquet 解码和 Decimal 转换阻塞 API 事件循环,并保持研究产物的 PIT/checksum
不变性。

### 可复现实验最小流程

1. 用 #77 创建并校验不可变 `release_id`,固定候选池版本和决策时点。
2. 从冻结发布构建 `FeatureSnapshot`,核验版本和 checksum 后发布。
3. 预先登记经济假设、窗口、成本、基准、参数邻域和试验预算。
4. 运行 alpha / 风险 / regime 分析,保存失败和中断结果,不要只保留胜者。
5. 通过 #57 完成样本内、样本外、稳健性与最终测试,再同步实验状态并发布信号。

当前实现不提供在线 Python 编辑器;策略规格可以声明信号到目标仓位的映射,但不会
执行目标仓位或生成订单。收益、Sharpe、IC 或显著性均为历史研究指标,不是收益
承诺或投资建议。

## 无代码研究策略规格(issue #79)

`finboard_backtest.strategy_spec` 使用固定结构表达
`候选池 → 特征图 → 信号 → 目标权重 → 退出风控 → 成交假设 → 验证计划`。
统一注册表为 #60-#64 和 `ma_cross` 提供兼容模板;API 与 runner 复用同一个
`compile_registered_strategy_spec()` 解析器。

解析结果 `can_execute=false`,保存/发布不会触发回测或交易。schema 使用白名单枚举
和强类型参数,拒绝 Python 源码、模块路径、通用表达式、循环依赖、停用因子和缺失
数据发布。版本持久化支持 draft/publish/supersede/rollback、乐观并发保护和结构化
diff。完整接口、迁移规则和示例见
[`docs/research_strategy_spec.md`](../../docs/research_strategy_spec.md)。


## ETF 绝对趋势 x 相对动量轮动策略(issue #61)

`finboard_backtest.etf_rotation` 子包提供 long-only ETF tactical allocation
研究框架:候选池 → 绝对趋势过滤 → 相对动量排名 → 风险分配 → 避险切换 → 绩效分析。

### 候选池

默认池 `DEFAULT_ETF_UNIVERSE` 包含 18 只 A 股主流 ETF:

| Sector | 代表标的 |
|--------|----------|
| 宽基指数 | 沪深300 / 上证50 / 中证500 / 创业板 / 科创50 / 中证1000 |
| 行业主题 | 医药 / 酒 / 半导体 / 光伏 / 军工 |
| 红利 | 红利ETF |
| 黄金 | 黄金ETF |
| 跨境 | 纳指 / 标普500 / 恒生 |
| 国债 | 国债ETF / 十年国债ETF |
| 货币 | 华宝添益 |

时点化(PIT)过滤:`filter_pit(as_of)` 排除未上市 / 已退市 / 上市不足 N 天的 ETF。

### 信号管线

```
closes → compute_absolute_trend (SMA200 or lookback return > 0)
       → compute_relative_momentum (3/6/12 month weighted score)
       → generate_signals → top_n selection
```

* 绝对趋势:预声明方法(SMA / lookback_return),**不得在看结果后切换**。
* 相对动量:通过趋势的 ETF 按 3/6/12 月组合得分排名,同分按代码字母序排列。
* Top-N:取动量得分最高的 N 只 ETF。

### 风险分配

* **等权**:selected ETF 等权分配。
* **逆波动率**:vol 越低权重越高(vol_lookback 日窗口)。
* 单 ETF 上限(`max_weight_per_etf`)和资产大类上限(`max_weight_per_asset_class`)强制执行。
* **避险切换**(flight-to-safety):无风险 ETF 通过趋势时,分配到避险国债 ETF;
  国债 ETF 自身也必须通过独立趋势检验。

> **重要**:国债 ETF 是**风险资产**而非保本现金等价物。
> 利率上行时国债 ETF 会下跌。避险切换仅在趋势信号全面转负时作为防御措施。

### 调仓

* 月度(21 个交易日)或双周(10 个交易日)调仓。
* 再平衡带(`rebalance_threshold`):权重偏离不超过阈值时不调仓。

### 绩效分析

`analyze_etf_rotation()` 输出:
* 资产类别暴露(时间序列均值)
* 风险贡献(按资产大类分解)
* 平均换手率、估算总成本
* 最大回撤持续期
* 资金档位可行性(10万/20万/50万元)
* 平均持仓数、调仓次数、避险触发次数

### 已知局限

* 趋势/动量在 A 股 ETF 上的有效性需要样本外验证,学术证据不能替代实证。
* 跨境 ETF 存在 QDII 额度限制和 T+0/T+1 差异,回测中需要考虑溢价和暂停申赎风险。
* 默认候选池的上市日期为近似值,如需精确可替换成员。
* 本子包仅用于离线研究 / 回测。


## 高流动性 ETF 短周期均值回归策略(issue #62)

`finboard_backtest.mean_reversion` 子包提供 long-only、严格成本约束的短周期
均值回归研究基线。该策略优先级**低于** ETF 趋势/组合构建,因为均值回归
高度依赖高换手和收盘可成交假设,在实盘中冲击成本难以预估。

### 为什么优先级低

* 均值回归在趋势行情中持续失效(单边运动导致"抄底"不断亏损)。
* 高换手率放大佣金/滑点/冲击成本的影响,成本 x2 后可能直接失效。
* 负偏度分布:高胜率但尾部损失集中,不能以胜率替代回撤/尾部风险评估。
* 仅作为严格成本约束的研究 sleeve,不能与趋势策略共用未经验证的撮合结论。

### 信号族(预注册)

| 族 | 计算 | 入场 | 出场 |
|----|------|------|------|
| Z_SCORE | (close - SMA) / std | z < -entry | z > -exit |
| BOLLINGER | %B = (close - lower) / (upper - lower) | %B < entry | %B > exit |
| RSI | Wilder RSI(period) | RSI < entry | RSI > exit |
| REVERSAL | N 日收益率 | ret < -entry | ret > -exit |

`ParameterGrid` 在实验前冻结所有信号族和参数组合,`total_candidates` 是
试验预算的硬上限。

### 执行模型

* **T 日收盘信号 -> T+1 开盘成交**,禁止同 Bar 成交。
* 成本拆分:佣金(万三) + 印花税(卖出千一) + 滑点(bps)。
* 成交参与率约束:单标的成交量 / 当日成交量 <= `max_participation`。

### 状态过滤

* SMA(N) 趋势过滤:close > SMA -> BULL/NEUTRAL(允许入场),close < SMA -> BEAR(禁止)。
* 波动率过滤:当前波动 / 历史波动 > `regime_vol_max_ratio` -> HIGH_VOL(禁止入场)。
* **未知状态不交易**:数据不足以计算时标记 UNKNOWN -> 禁止入场。

### 硬约束(不可绕过)

| 约束 | 说明 |
|------|------|
| `max_positions` | 最大同时持仓数 |
| `max_weight_per_position` | 单标的权重上限 |
| `max_holding_days` | 最长持有期,到期强制平仓 |
| `cooldown_days` | 平仓后冷却期,不允许重新入场 |
| `max_daily_turnover` | 单日换手率上限 |
| `max_participation` | 单标的成交量参与率上限 |
| `no_averaging_down` | 禁止对浮亏头寸加仓 |

### 绩效分析

`analyze_mean_reversion()` 输出:
* 毛/净收益及成本归因(佣金/印花税/滑点分解)。
* 成本 x2 敏感性:翻倍成本后净收益仍为正 -> survives。
* 趋势 sleeve 相关性:与 ETF 轮动收益序列的相关系数。
* 危机期表现:最大回撤区间的交易数和入场数。
* 资金档位可行性(10万/20万/50万元)。
* rejected 判定:成本 x2 失效 或 Sharpe < 0。

### 已知局限

* 均值回归在 A 股 ETF 上的有效性需要样本外验证,学术证据不能替代实证。
* 高换手率使该策略对成本假设极度敏感;成本 x2 失效应直接拒绝。
* 负偏度分布意味着尾部风险被均值/Sharpe 低估。
* 本子包仅用于离线研究 / 回测。


## 可转债双低 x 质量 x 事件风险策略(issue #63)

`finboard_backtest.convertible_double_low` 子包提供 long-only、无对冲的
可转债研究策略基线。

### 可转债撮合规则

issue #63 修正了 `BacktestBroker` 统一使用股票规则的错误。`asset_rules.py`
新增 `CONVERTIBLE_BOND` 规则并注册到 `DEFAULT_TABLE`(版本升至 v2):

| 规则 | 值 |
|------|---|
| 手数 | 10 张/手 |
| T+N | T+0(当日回转) |
| 印花税 | 免征 |
| 佣金 | 万 2(最低 1 元) |
| 涨跌停 | ±20% |
| 价格步长 | 0.001 元 |

### 双低公式与因子

* 经典双低:price + premium * 100,取最小的 `top_n` 只。
* 扩展因子(预注册权重):YTM / 剩余期限 / 流动性;权重在 Config 中冻结。

### 事件风险过滤(严格 PIT)

| 事件 | 行为 |
|------|------|
| 强赎公告 | 禁止新开仓 |
| 退市公告 | 禁止开仓 |
| 停牌 | 禁止开仓 |
| 回售期 | 禁止新开仓 |
| 下修/转股价调整 | 跟踪但不禁止(中性/利好) |
| 临近到期 | 禁止新开仓 |

所有事件按 `available_at` 生效:未来公告(`available_at > as_of`)不参与决策。

### 执行模型

* T 日收盘信号 -> T+1 开盘成交(禁止同 Bar 成交)。
* 发行人集中度:单发行人权重 <= `max_weight_per_issuer`。
* 进入/退出缓冲:减少因排名微小变动导致的频繁换手。
* 成交参与率:单标的成交量 / 当日成交量 <= `max_participation`。

### 归因分析

`analyze_convertible_double_low()` 输出:
* 收益归因(价格变动 / 票息 / 成本拖累)。
* 成本拆分(佣金 / 滑点;可转债免印花税)。
* 资金档位可行性(10万/20万/50万元)。
* rejected 判定:负收益或无可行资金档位。

### 已知局限

* 可转债存在信用、强赎、流动性和规则变化风险;历史"双低"不能视为保证。
* 10 万元档位因离散单位约束可能不可行(10张/手 x 面值100元 = 1000元/手)。
* 仅用于离线研究 / 回测,不连接实盘。


## 股指 / 国债期货时间序列动量(TSMOM)策略(issue #64)

`finboard_backtest.futures_tsmom` 子包提供支持多空、保证金、每日盯市和
换月的研究级期货趋势基线。

### 架构选择

本子包采用**独立模拟器**模式(与 `mean_reversion` 相同),不走
`BacktestBroker`。原因:`BacktestBroker` 是 long-only、cash-account、
equity-oriented 的纸面撮合引擎,不支持做空、保证金、乘数和每日结算。

### 合约规格

| 品种 | 乘数 | 保证金率 | tick | 手续费 | 涨跌停 |
|------|------|---------|------|--------|--------|
| IF (沪深300) | 300 | 12% | 0.2 | 万0.023 | ±10% |
| IC (中证500) | 200 | 14% | 0.2 | 万0.023 | ±10% |
| IH (上证50) | 300 | 12% | 0.2 | 万0.023 | ±10% |
| T (10年国债) | 10000 | 2% | 0.005 | 3元/手 | ±2% |
| TF (5年国债) | 10000 | 1.2% | 0.005 | 3元/手 | ±1.2% |
| TS (2年国债) | 20000 | 0.5% | 0.005 | 3元/手 | ±0.5% |

合约规格取自中金所(CFFEX)现行规则(2024年核实)。实盘交易前必须从
交易所获取最新规则。

### 信号生成

* **多 lookback TSMOM**:对 1/3/6/12 月(21/63/126/252 天)的过去收益率
  取 sign 并平均,范围 [-1, 1]。
* **波动率缩放**:仓位 = vol_target / max(realized_vol, vol_floor),
  使每个品种贡献等量风险。
* **PIT 安全**:信号在 T 日收盘后生成,成交在 T+1 开盘。

### 换月与展期

* **未拼接原合约**:信号和成交使用原始合约价格,换月处产生跳空。
* **连续序列**:通过 RATIO/DIFFERENCE 调整消除换月跳空,但**仅供展示**,
  调整后的收益不能当成可交易利润。
* **展期收益**:记录每次换月的展期价差 (front - next) / next,
  正值 = backwardation(多头展期增益),负值 = contango。

### 每日盯市

* 持仓按当日 close 结算 P&L,cash 随之变动。
* 保证金 = 名义价值 x margin_rate,不减少 cash(保证金是存款)。
* equity = cash(所有已实现 P&L 已在 cash 中)。

### 风险约束

| 约束 | 默认值 | 说明 |
|------|--------|------|
| 最大名义杠杆 | 2.0x | 总名义 / 权益 |
| 最大保证金占用 | 80% | 保证金 / 权益 |
| 单市场集中度 | 40% | 股指或国债 / 权益 |
| 单合约集中度 | 30% | 单品种 / 权益 |

所有约束按**整数手**裁剪,不可用小数合约美化。

### 归因分析

`analyze_tsmom()` 输出:
* 收益归因(方向收益 / 展期收益 / 手续费 / 滑点 / 保证金利息)。
* 成本拆分(佣金 / 滑点 / 保证金资金成本)。
* 资金档位可行性(10/20/50 万元,按整数手判断)。
* 危机期表现(最大回撤 / 最差日收益 / 年化波动率)。
* 压力测试(成本 x2 / 保证金 x2)。
* rejected 判定:负收益、Sharpe < 0、无可行档位或压力测试失败。

### 已知局限

* 多空与杠杆可能导致超过本金的损失;低回撤目标不能由历史趋势证据保证。
* 10 万元档位因合约乘数(IF 1 手 ~ 10.5 万元名义)可能不可行。
* CTP 适配器是 stub,不连接实盘;任何实盘化需用户另行确认。
* 仅用于离线研究 / 回测。


## LLM 辅助因子假设登记与离线审阅流程(issue #65 / #84)

`finboard_backtest.factor_research` 子包提供受控、可审计的 LLM 辅助研究循环。
issue #84 扩展为完整的 AI 研究助手:真实 OpenAI 兼容 HTTP provider、PostgreSQL
持久化、上下文式金融问答、策略组件/diff 草案、权限矩阵与重启恢复。

> **免责声明**:AI 研究助手只服务研究与教育,**不是投资顾问,不保证收益,
> 不能下单**。AI 的建议必须经过人工审批和正式研究流水线(研究→回测→样本外→
> 模拟→小资金实盘)验证后才可使用。AI 永远不位于订单执行链路中。

### LLM 能做 / 不能做

| 能做 | 不能做 |
|------|--------|
| 输出结构化 `FactorHypothesis` / 策略草案 / diff / 问答 | 生成可执行 Python 代码 / 模块路径 |
| 引用白名单字段与算子(#79 schema 约束) | 调用 `exec` / `eval` / `import` / `open` |
| 提出有限参数预算 | 连接 Broker / OrderManager / 实盘配置 |
| 记录参考文献与失效场景 | 把单次高收益描述成有效策略 |
| 标记预期方向(long_high / long_low) | 绕过风控 / Kill Switch |
| 提示词经 `sanitize_prompt` 脱敏 | 在提示词中包含 API key / 密码 / 账户 |
| 金融问答引用项目来源,数据不足时声明 | 编造市场数据 / 收益 / 监管结论 |
| 以 `DraftArtifact` 持久化草案(含来源/审批状态) | 自动审批 / 自动采纳 / 直接启动回测 |

### 状态机

```
proposed -> approved_for_research -> in_sample -> validated_oos / rejected
```

- `proposed`: LLM 提出, 等待人工审阅
- `approved_for_research`: 人工批准, 可以进入实验
- `in_sample`: 实验进行中
- `validated_oos`: 样本外验证通过(只能由 #57 持久化机器验证产生)
- `rejected`: 被拒绝(验证失败 / 人工拒绝 / OOS 未通过)
- `superseded`: 被新版本取代

### 人工审批 gate

```python
wf = ResearchWorkflow()
h = wf.submit(hypothesis, actor="llm@gpt-4")  # 自动验证
wf.approve(h.hypothesis_id, approver="alice")  # 显式人工批准
reg = wf.register_experiment(
    h.hypothesis_id,
    model_version="gpt-4-0613",
    prompt_version="v3",
    dataset_version="akshare-2024-01",
    code_version="abc1234",
    registered_by="alice",
)
# issue #84: complete_experiment 不再接受 passed_oos: bool,
# 必须绑定持久化的 #57 机器验证终态(MachineValidationOutcome)
wf.complete_experiment(
    reg.experiment_id,
    validation=MachineValidationOutcome(
        validation_experiment_id="exp-57-xxxx",  # 来自持久化的 #57 实验
        status="validated_oos",
        trials_used=3,
    ),
    completed_by="alice",
)
```

### 白名单

输入字段: `close`, `open`, `high`, `low`, `volume`, `vwap`, `turnover`,
`market_cap`, `pe_ratio`, `pb_ratio`, `returns_1d`, `adv20` 等。

算子: `rank`, `zscore`, `ts_mean`, `ts_std`, `ts_rank`, `ts_corr`,
`delta`, `delay`, `sigmoid`, `log`, `add`, `multiply` 等。

禁止: `import`, `exec`, `eval`, `open`, `subprocess`, `socket`, `requests`,
`SELECT`/`INSERT`/`DROP`/`DELETE`(SQL), `broker`, `order_manager`, `kill_switch`。

### 试验预算

每个假设的参数搜索空间 = 所有 `ParameterSpec.grid_size` 的乘积。
默认上限 1000 组合。超过上限自动拒绝。
试验次数达到预算上限后不能再登记新实验。

### 审计追踪

所有状态变更(提交/验证/审批/拒绝/实验登记/完成/取代/中断)都记录到
`AuditTrail`(内存)与 `ai_audit_events` 表(持久化), 包含时间戳、事件类型、
操作人和详情。失败实验和反例同样保留,用于审计。

### 敏感数据脱敏

`sanitize_prompt()` 自动替换提示词中的:
- API key (OpenAI / Anthropic / AWS)
- 密码 / token / credential 赋值
- Bearer token
- 手机号 / 邮箱 / 银行卡号 / 身份证号

### LLM Provider(issue #84)

| Provider | 用途 |
|----------|------|
| `FakeLLMProvider` | 测试 stub,不调用公网,按队列返回预设输出(默认) |
| `OpenAICompatibleLLMProvider` | 真实 HTTP provider,兼容 OpenAI / DeepSeek / 通义 / 智谱 GLM / 本地 vLLM |

`OpenAICompatibleLLMProvider` 通过 `httpx` 调用 `/v1/chat/completions`,
强制 `response_format=json_object` 结构化输出。超时不重试(同源原则),
429/5xx 有限指数退避后降级为 `LLMUnavailableError`。api_key 不进入
日志 / 审计 / `Provenance`。配置见 `Settings.llm_*` 字段与
`finboard_app.llm_factory.build_llm_provider`。

### 权限矩阵(issue #84)

`ResearchAssistant`(`factor_research/assistant.py`)是 AI 助手唯一入口,
`assert_research_only_request` 在调用 provider 前拒绝越权请求与提示词注入:

| 能力 | AI 可读 | AI 可写 | 审批门 |
|------|---------|---------|--------|
| 研究上下文 | 是 | 否 | - |
| 因子假设 | - | 草案 | 人工 |
| 策略规格 | - | 草案/diff | 人工(#79 API) |
| 金融问答 | - | 解释 | - |
| 实盘账户 / 订单 / 持仓 / Kill Switch | 否 | 否 | 完全禁止 |
| 启动回测 / 模拟 | 否 | 否 | 完全禁止 |

### 持久化与重启恢复(issue #84)

假设 / 实验 / AI 草案 / 审计事件持久化在独立表(`factor_hypotheses` /
`factor_hypothesis_experiments` / `ai_drafts` / `ai_audit_events`),
不写入实盘 `orders`/`fills`/`positions`/`audit_logs`。`HypothesisWorkflowService`
以 PostgreSQL 为真实来源,每次操作从 DB 重建内存 `ResearchWorkflow`,
进程重启后状态自动恢复,失败/被拒绝记录不丢失。

### 已知局限

* `FakeLLMProvider` 是默认 provider,不调用公网 LLM。
* 真实 LLM 需配置 `FINBOARD_LLM_PROVIDER=openai_compatible` + base_url + api_key。
* DeepSeek thinking 问答通过 `/api/research/ai/ask/stream` 使用 SSE；
  `FINBOARD_LLM_TIMEOUT_SECONDS` 表示连续没有真实 token 的空闲超时(默认 30 秒),
  keep-alive 不会重置计时器；思考 token 仅在当前页面展示,不写入普通历史消息或审计。
* thinking 可由 `FINBOARD_LLM_THINKING_ENABLED` 与
  `FINBOARD_LLM_REASONING_EFFORT` 控制；总请求时限和连接时限分别由
  `FINBOARD_LLM_TOTAL_TIMEOUT_SECONDS` 与 `FINBOARD_LLM_CONNECT_TIMEOUT_SECONDS` 控制。
* 不连接 Broker / OrderManager / 实盘策略配置。
* validated_oos 只能由 #57 持久化的机器验证终态决定,不能由 LLM 或人工主观判断。
* AI 草案需人工审批后才可采纳到正式研究流水线,不自动晋级。
