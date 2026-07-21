# finboard-core

交易内核(Trading Kernel)。**这是 P0 的核心**。

## 组成

| 模块 | 职责 |
|------|------|
| `events.py` | 内部领域事件(`OrderCreated` / `OrderFilled` / ...) |
| `bus.py` | in-process pub-sub EventBus(asyncio) |
| `state_machine.py` | 订单状态机,集中校验状态迁移合法性 |
| `protocols.py` | 对外依赖的 Protocol(`RiskChecker`),避免反向依赖 `finboard-risk` |
| `order_manager.py` | 下单 / 撤单 / 回报处理 / 幂等校验 |
| `position_manager.py` | 本地持仓维护(由成交事件驱动,不允许外部直接改) |
| `account_manager.py` | 账户资金快照(由 broker 查询覆盖) |
| `kernel.py` | 把所有 manager 串成 trading kernel,提供 start / stop |

## 红线落地

| AGENTS.md 红线 | 本包内的落地 |
|----------------|--------------|
| `client_order_id` 本地生成 + 全局唯一 | `OrderManager.place_order` 调用 `generate_client_order_id` + DB UNIQUE 索引 |
| 下单超时不重试 | `_submit_to_broker` 捕获 `BrokerTimeoutError` 后置入 `UNKNOWN`,不重发 |
| 持仓真实来源是券商 | `PositionManager` 双源(local / broker),`overwrite_from_broker` 是唯一改 broker 源入口 |
| 重启恢复先核对 | `Kernel.start` 内的 recovery 步骤(占位,P0 后期完善) |
| Kill Switch 由内核执行 | `Kernel.activate_kill_switch` 直接拒绝新单,不依赖前端 |
