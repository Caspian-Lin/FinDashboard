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


