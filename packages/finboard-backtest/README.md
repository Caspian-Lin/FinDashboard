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


## 组合构建层(issue #59)

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

### 协方差估计

`estimate_covariance()` 使用 Ledoit-Wolf 收缩估计,避免样本协方差矩阵
不稳定导致的极端权重。缺数标的有效观测不足时被**排除**(不静默放大
其他标的权重);窗口内样本不足时收缩强度自动增加。

### 离散手数求解

`solve_sizing()` 把目标权重转换为可成交手数,支持:

* 10万 / 20万 / 50万元三档资金(`CAPITAL_TIERS`)。
* A 股 100 股 / 手、ETF 100 或 10 份 / 手、可转债 10 张 / 手、期货乘数。
* 最小佣金、印花税和剩余现金非负约束。
* 负现金时贪心减手(从偏离最大的标的开始减)。

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


## 多因子研究框架(issue #60)

`finboard_backtest.factors` 子包提供 A 股复合因子研究管线:
因子目录 → 横截面标准化 → 中性化 → 复合评分 → 组合选择 → 因子分析。

### 因子目录

15 个因子覆盖 6 个类别,每个声明经济假设、方向、极值处理和缺失策略:

| 类别   | 因子                                                         |
|--------|--------------------------------------------------------------|
| 估值   | `pb`, `earnings_yield`(1/PE_TTM), `dividend_yield`         |
| 质量   | `roe`, `gross_profit_margin`, `debt_to_assets`              |
| 低风险 | `volatility_20d`, `volatility_60d`, `volatility_120d`, `downside_volatility` |
| 流动性 | `turnover_rate`                                              |
| 动量   | `momentum`, `residual_momentum`                             |
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

* 残差动量 `residual_momentum` 的计算需要因子模型,当前框架仅声明因子定义,
  实际计算需外部回归。
* `downside_volatility` 在牛市中系统性偏低,需要结合全样本波动率使用。
* 因子分析中的 IC 计算需要前瞻收益,仅用于回测后分析,不能用于实盘信号。
* 本子包仅用于离线研究 / 回测。


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


## LLM 辅助因子假设登记与离线审阅流程(issue #65)

`finboard_backtest.factor_research` 子包提供受控、可审计的 LLM 辅助研究循环。

### LLM 能做 / 不能做

| 能做 | 不能做 |
|------|--------|
| 输出结构化 `FactorHypothesis` | 生成可执行 Python 代码 |
| 引用白名单字段与算子 | 调用 `exec` / `eval` / `import` / `open` |
| 提出有限参数预算 | 连接 Broker / OrderManager / 实盘配置 |
| 记录参考文献与失效场景 | 把单次高收益描述成有效策略 |
| 标记预期方向(long_high / long_low) | 绕过风控 / Kill Switch |
| 提示词经 `sanitize_prompt` 脱敏 | 在提示词中包含 API key / 密码 / 账户 |

### 状态机

```
proposed -> approved_for_research -> in_sample -> validated_oos / rejected
```

- `proposed`: LLM 提出, 等待人工审阅
- `approved_for_research`: 人工批准, 可以进入实验
- `in_sample`: 实验进行中
- `validated_oos`: 样本外验证通过(只能由 #57 walk-forward 产生)
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
    dataset_version="ak-2024-01",
    code_version="abc1234",
    registered_by="alice",
)
wf.complete_experiment(
    reg.experiment_id,
    passed_oos=True,      # 只能由 #57 walk-forward 产生
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
`AuditTrail`, 包含时间戳、事件类型、操作人和详情。
失败实验和反例同样保留,用于审计。

### 敏感数据脱敏

`sanitize_prompt()` 自动替换提示词中的:
- API key (OpenAI / Anthropic / AWS)
- 密码 / token / credential 赋值
- Bearer token
- 手机号 / 邮箱 / 银行卡号 / 身份证号

### 已知局限

* FakeLLMProvider 是测试 stub,不调用公网 LLM。
* 真实 LLM provider 的实现不在本 issue 范围内。
* 不连接 Broker / OrderManager / 实盘策略配置。
* validated_oos 只能由 #57 walk-forward 的机器验证结果产生,不能由 LLM 或人工主观判断。


