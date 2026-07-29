# 组合约束、退出策略与资金档位

issue #81 将 `finboard_backtest.portfolio` 升级为 v2 离线研究契约，补齐以下链路：

```text
标准化 Signal
  → score/confidence 冲突聚合与可投资标的判定
  → 约束前 TargetWeight
  → 单标的/sleeve/现金/gross/波动率/再平衡/风险贡献硬约束
  → 约束后 TargetWeight
  → 退出策略调整目标
  → 整数手、费用、滑点、参与率、保证金求解
  → ResearchRun 调仓指令
  → 研究撮合的订单/实际成交
  → 成交驱动持仓、现金与盈亏
```

所有组件均为纯计算，不连接 Broker、不修改实盘持仓，也不向实盘订单表写入数据。

## 信号到目标仓位

`build_portfolio(PortfolioBuildInput(...))` 是正式入口。输入必须冻结：

- 信号的 `symbol/score/confidence/timestamp/strategy_id/factor_snapshot_id`；
- 分配方法：`equal_weight`、`inverse_volatility` 或 `erc`；
- 资产到 sleeve 的映射、禁用标的和当前实际权重；
- 单标的、sleeve、现金、gross leverage、目标/最大波动率、再平衡带和最小交易权重；
- 协方差版本、可选 beta 以及截至决策时点的最大回撤。

同标的多个信号默认按 `score * confidence` 净额合并；也可以选择
`neutralize`，让同时存在正负信号的标的退出本期决策。默认 long-only，负信号映射
为空仓；只有显式设置 `long_only=False` 和大于 1 的 `max_leverage` 才能产生空头/
杠杆目标。

`TargetWeight.max_leverage` 是配置上限，不再表示实际权重和。实际敞口分别读取：

- `gross_exposure = sum(abs(weight))`
- `net_exposure = sum(weight)`
- `cash_buffer` 为实际保留现金比例

v2 会直接拒绝 `gross_exposure > max_leverage`。默认配置仍是 long-only、无杠杆。

## 约束顺序与审计

约束按以下顺序执行，且每一步只维持或降低绝对权重，不在截断后放大其它标的：

1. 禁用标的与 long-only；
2. 单标的绝对权重；
3. sleeve 绝对权重合计；
4. gross leverage 与无杠杆现金缓冲；
5. 目标/最大年化波动率；
6. 基于当前实际权重的再平衡带和最小交易权重；
7. 单资产风险贡献硬上限；
8. 再次核验现金。

每个 `ConstraintAdjustment` 都包含约束名、标的或 sleeve、before、after、limit、
pass/fail 和原因。`PortfolioRiskReport` 输出预计波动率、beta、资产/sleeve 风险
贡献、最大单资产风险贡献、权重集中度、截至当期最大回撤和约束影响。

协方差不可用时必须显式选择：

- `fail_closed`：拒绝逆波动率/ERC/波动率约束构建；
- `fallback_equal_weight`：分配方法降级为等权；若波动率约束也无法计算，则保持
  已执行的现金/gross 硬约束，并在审计中记录降级，不能伪造波动率结果。

协方差缺少任一候选标的、包含非有限值、不对称或奇异时同样走上述分支，不能通过
先删除缺数标的、再归一化其余权重来静默放大剩余资产。

`max_risk_contribution < 1` 会启用硬约束。求解器按确定顺序只降低超限资产权重，
不把释放的风险预算重新分给其它资产；约束后的最大风险贡献必须小于等于上限。
缺少协方差、组合方差非正/非有限、阈值低于活跃资产数对应的理论下界 `1/N`，或
迭代不收敛时一律 fail closed，禁止继续生成研究订单。HTTP 纯计算接口为兼容无
协方差的旧请求，默认值为 `1.0`（不启用）；显式配置更低上限时必须同时提供可用
收益序列。正式 `PortfolioPipelineAdapter` 的 ResearchRun 默认上限为 `0.35`，
因此其冻结输入必须包含足以覆盖目标标的的协方差估计。

## 退出策略

`execute_risk_exit_policy()` 消费已发布无代码策略规格中的 `RiskExitPolicy`，当前
执行器版本为 `v1`。支持：

- 固定价格止损；
- 实现波动率或 `ATR / price` 止损；
- 止盈；
- 最大持有天数；
- 组合回撤触发降 gross exposure 或暂停；
- 退出后的冷却期。

禁用规则不执行。每次触发都输出 `ExitDecision`，记录测量值、阈值、调整前后权重
和理由。执行器只输出新的目标仓位；它不会修改 `PositionManager` 或研究持仓。
`ExitPositionSnapshot.filled_quantity` 必须来自撮合后的真实成交，拒单数量为零不会
被当成持仓，部分成交也只按实际剩余数量退出。

## 10/20/50 万元可行性

`evaluate_capital_tiers()` 固定输出 `100k`、`200k`、`500k` 三档结果。每档使用
冻结的 `ExecutionMetadata` 或等价 `AssetLotInfo`，同时处理：

- 股票、ETF、转债的整数交易单位；
- 期货合约乘数和保证金率；
- 佣金率、最低佣金、卖出税、滑点；
- 最大成交量参与率和不可交易状态（包括停牌、涨跌停等上游判定）；
- 当前实际持仓、再平衡最小交易权重和剩余现金。

每档返回实际可持仓数量与权重、现金利用率、目标跟踪误差、不可成交标的、容量
压力、保证金占用和预计成本。参与率受限时，计划保留 requested/actual/unfilled
差异，不能把未成交部分计入持仓。

通用 `solve_sizing()` 当前明确限定 long-only；收到负权重会失败关闭。期货多空
目标继续交给已有 `futures_tsmom` 策略适配器处理，避免在单一净数量模型中错误
合并平多、开空两种不同动作。

## ResearchRun 与 API

`PortfolioPipelineAdapter` 是 long-only 研究组合进入 #80 的正式入口。调用方只
能提供冻结候选池、特征、标准化信号、价格、协方差、交易单位和成交假设；适配器
内部依次执行组合构建、硬约束、风险退出、三档资金可行性、整数手 sizing、研究
订单/成交和成交驱动记账，并生成绑定 manifest 输入与阶段输出 checksum 的证据。
`ResearchRunCoordinator` 缺少证据、证据漂移或任一硬约束失败时将 run 标记为
`rejected`，不会持久化订单 artifact。

底层转换函数包括：

- `to_research_targets()` → `targets_before_constraints` /
  `targets_after_constraints`
- `to_research_constraint_outcomes()` → `constraints`
- `to_research_rebalance_instructions()` → `rebalance_plan`
- `constraint_impact_summary()` → `ResearchRunReport.constraint_impact`

研究订单、成交、持仓和盈亏由正式适配器与研究撮合生成，ID 保持 `RR-` 命名空间。
同一冻结输入的重放使用稳定研究 ID；组合层不允许越过撮合直接写持仓。

HTTP 纯计算接口：

| 方法 | 路径 | 输出 |
|---|---|---|
| `POST` | `/api/portfolio/allocate` | 约束前后目标、逐项约束差异、gross/net、风险摘要 |
| `POST` | `/api/portfolio/sizing` | 离散计划、requested/actual/unfilled、费用、滑点、保证金 |
| `POST` | `/api/portfolio/feasibility` | 10/20/50 万三档可行性 |
| `POST` | `/api/portfolio/attribution` | 资产与 sleeve 收益/风险/换手/回撤归因 |

网页仅提交结构化字段，不接受 Python 策略代码。

## 与 `phase1_doc.md` §3.4 的映射

本模块只能提供离线等价证据：

- §3.4 3-5：信号、目标仓位、约束和离散研究指令；
- §3.4 6-9：ResearchRun 承接拒单、部分成交和成交 artifact；
- §3.4 10、13-14：成交驱动持仓、退出目标、现金/费用/保证金/盈亏恒等式。

§3.4 1-2 的券商连接和真实账户，以及 §3.4 11-12 的实盘恢复仍由交易内核负责。

## 实盘风险边界与回滚

本变更没有修改 `PreTradeChecker`、`KillSwitch`、`OrderManager`、
`PositionManager`、`RecoveryEngine` 或券商适配器。实盘资金/仓位预占、并发下单
和失败关闭缺口已单独登记为 issue #92；按用户要求本轮不实施，不能把本离线约束
当成实盘保护。

v2 没有数据库迁移。回滚只需恢复 portfolio v1 代码和旧 API response；已持久化
ResearchRun artifact 是 JSON 快照，不会被代码回滚改写。
