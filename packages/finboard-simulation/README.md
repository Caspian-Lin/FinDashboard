# finboard-simulation

产品级模拟交易域。账户、会话、订单、成交、持仓、账本、行情事件和审计均使用
独立 `SIM-*` ID 与 `simulation_*` 表。

本包不会导入 `finboard_broker`、QMT/CTP 适配器或实盘 `OrderManager` /
`PositionManager`。策略只能提交结构化目标仓位，由模拟 runner 转换成订单并经过
模拟风控、撮合和成交驱动记账。

架构、状态机、撮合假设、恢复流程、REST/WebSocket 契约和操作步骤见
[`../../docs/simulation_trading.md`](../../docs/simulation_trading.md)。
