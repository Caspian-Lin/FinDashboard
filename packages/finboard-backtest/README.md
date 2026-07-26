# finboard-backtest

事件驱动历史回放、纸面撮合、绩效分析与按日因子选股。

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
