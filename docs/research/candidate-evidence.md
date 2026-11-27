# 多资产候选策略证据台账

结论复核日期为 2026-11-28。当前候选均缺少完整的晋级证据。小盘复合 v13 的既有复核有明确负结果，其余候选的未核验字段按未知登记。资金、费用与窗口只归属于实际执行的策略和版本。 [研究范围 #66](https://github.com/Caspian-Lin/FinDashboard/issues/66) · [证据台账 #528](https://github.com/Caspian-Lin/FinDashboard/issues/528)

## 目标与版本

| 目标来源 | 适用范围 | 目标 |
| --- | --- | --- |
| [#66 执行报告](https://github.com/Caspian-Lin/FinDashboard/issues/66) | 多资产研究 | 10 万、20 万、50 万元分别验证离散手数、容量、成本和冻结 OOS。收益和风险目标由各次预注册固定。 |
| [2026-09-01 ROADMAP](ROADMAP.md) | 历史研究目标 | 年化至少 15%、最大回撤至多 20%。 |
| [2026-10-04 小盘复核的目标来源](rounds/2026-10-04-smallcap-v13/report.md#目标来源与预算) | 小盘复合 v13 的当轮门，引用 2026-09-22 冻结说明和记忆 `RM-a14a4b147ccd4afb9501980a` | 年化至少 15%、最大回撤低于 10%。 |

## 候选与证据

代码模板固定查看提交 [`6bb42b5` 的策略注册表](https://github.com/Caspian-Lin/FinDashboard/blob/6bb42b540de5126a8602127ec0ef552e84a54f06/packages/finboard-backtest/src/finboard_backtest/strategy_spec/registry.py)。兼容模板版本表示可编译规则。已发布规格版本、冻结输入和运行键只有在证据能对应时才登记。

| 候选与版本 | 冻结输入、运行及已用窗口 | 10 万 / 20 万 / 50 万元 | 实际压力、验证及判定 |
| --- | --- | --- | --- |
| A 股多因子 `multi_factor`，模板 `v1`，[#60](https://github.com/Caspian-Lin/FinDashboard/issues/60) | 已发布规格、冻结因子/行情、正式运行及已用窗口均未知。价格动量的历史证伪按 [F-1](FINDINGS.md) 约束该腿。 | 未知 / 未知 / 未知 | 成本压力、冻结 OOS、回放和连续模拟均未知。未达到晋级证据门。 |
| ETF 轮动 `etf_rotation`，模板 `v1`，[#61](https://github.com/Caspian-Lin/FinDashboard/issues/61) | 已发布规格、时点候选池、冻结行情/因子、运行和已用窗口均未知。 | 未知 / 未知 / 未知 | 实际费用、跨境可交易约束、walk-forward 与冻结 OOS 证据未知，回放和连续模拟未知。未达到晋级证据门。 |
| ETF 均值回归 `mean_reversion`，模板 `v1`，[#62](https://github.com/Caspian-Lin/FinDashboard/issues/62) | 已发布规格、时点候选池、冻结行情/因子、运行和已用窗口均未知。 | 未知 / 未知 / 未知 | 下一可交易 Bar、成本 2×、尾部压力、参数邻域、冻结 OOS、回放和连续模拟证据均未知。未达到晋级证据门。 |
| 可转债双低 `convertible_double_low`，模板 `v1`，[#63](https://github.com/Caspian-Lin/FinDashboard/issues/63) | 已发布规格、冻结行情/条款/事件序列、运行和已用窗口均未知。2025-12 行业组合观察见 [F-3](FINDINGS.md)。 | 未知 / 未知 / 未知 | 资金整手、实际费用、事件压力、冻结 OOS、回放和连续模拟都缺该版本的证据。未达到晋级证据门。 |
| 期货时间序列动量 `futures_tsmom`，模板 `v1`，[#64](https://github.com/Caspian-Lin/FinDashboard/issues/64) | 已发布规格、原合约/展期冻结版本、因子、运行和已用窗口均未知。主连仅作基准。 | 未知 / 未知 / 未知 | 逐合约整手和保证金、实际费用/保证金压力、冻结 OOS、回放及连续模拟未知。仅离线候选。 |
| 小盘复合 `sc-mr-macd-composite-v1` v13，[#505](https://github.com/Caspian-Lin/FinDashboard/issues/505) | 主发布 `a-share-cs-20260917-v5`，观测 `a-share-daily-metrics-20260829-v3`，四腿 ret5/RSI14/bias20/MACD 使用[冻结因子序列引用和覆盖](rounds/2026-10-04-smallcap-v13/evidence-index.md#factor-coverage-json)。基线 `RR-e909662f7b2c9f7ad30e89c4`。决策 2015-02-06 至 2026-08-14，2024–2026 已使用。2024 的 30 万元重新起步属于既有窗口敏感性。 | 10 万 `RR-03fdb4fad15d18ff97bbd8c5`（2024 净收益 -19.32%） / 20 万未知 / 50 万 `RR-5f04407d3bd134a153eacf4e`（2024 净收益 -16.21%）。 | 费用 2× `RR-88cd075891b02608b9ae6a9e`、滑点 10/20 bps 对照 `RR-207902172adebe8e765c6e3c` / `RR-3fcb939bd03edcb270e7bc82` 均有实际成交证据。当前目标 `not_supported`。独立 alpha、正式新 OOS、完整执行稳健性 `insufficient_evidence`。回放与连续模拟缺证据。 |

小盘复核的[固定报告](rounds/2026-10-04-smallcap-v13/report.md)、[精简证据](rounds/2026-10-04-smallcap-v13/evidence-summary.json)和[预注册](rounds/2026-10-04-smallcap-v13/preregistration.json)已纳入仓库。证据索引中列为本机原件的文件应在对应归档环境读取。本次核对了仓库归档与工单链接，未重新查询正在运行的研究实例。

## 后续补证和晋级

1. 每个新候选先登记可复核假设、精确策略版本、主 bars/关联发布及因子 checksum、已用与未用窗口、实验预算、止损条件和事前结果阈值。资金档以整数手、实际账本及成本压力分别报告，保存失败、拒绝和中断结果。
2. 正式验证固定组合管线和 `runner.kind=research_spec`，去重已揭盲窗口。记录支持、未支持和证据不足的结论。2 Bar 延迟、订单簿冲击和参数邻域按[正式验证契约](formal-validation-contract.md)记录能力与缺口。
3. 具备相应证据后按研究、回测、样本外、行情回放、模拟、影子、小资金实盘顺序晋级。记录每版的结果 checksum、实际费用、回放与模拟的会话引用。实盘恢复条件见 [QMT 真机验证](https://github.com/Caspian-Lin/FinDashboard/issues/22)、[实盘元数据](https://github.com/Caspian-Lin/FinDashboard/issues/23)、[连续运行](https://github.com/Caspian-Lin/FinDashboard/issues/24)及[实盘风控预占](https://github.com/Caspian-Lin/FinDashboard/issues/92)。
