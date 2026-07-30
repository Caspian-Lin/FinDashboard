# 持久化模拟交易环境

本文描述 issue #83 的产品模拟盘。它用于把已发布、已完成机器验证的无代码策略放入
历史行情回放或只读行情环境，持续验证订单、成交、持仓和盈亏。模拟结果不是收益
证明，也不会自动晋级到影子盘或实盘。

## 架构与硬隔离

固定链路如下：

```text
已发布 ResearchStrategySpec + completed ResearchRun
→ 正式 runner 的结构化目标仓位（含 signal trace）
→ SimulationRisk
→ SimulationOrderManager
→ next-bar matcher
→ SimulationFill
→ SimulationPosition / SimulationAccount / SimulationLedger
```

隔离不依赖前端标签：

| 边界 | 模拟域 | 实盘域 |
|---|---|---|
| ID | `SIM-A/S/O/F/L/X-*` | 既有账户、订单、成交 ID |
| 表 | `simulation_*` | `accounts/orders/fills/positions/audit_logs` |
| 路由 | `/api/simulation/*`、`/ws/simulation/*` | 既有交易路由、`/ws/events` |
| 行情 | 调用方推送的历史/只读 Bar | Broker MarketData Adapter |
| 执行 | 包内纯撮合函数 | Broker Adapter / Broker API |
| 恢复 | 模拟预占、事件和账本恢复 | 券商查询与实盘 RecoveryEngine |

`finboard-simulation` 不依赖 `finboard-broker`、QMT/CTP、实盘
`OrderManager`、`PositionManager` 或 `RiskManager`。模拟 API 没有直接创建订单
端点，也没有 Broker 类型、账户凭证、模块路径或 Python 字段。它只接受目标仓位；
订单 ID 由后端生成，持仓只能由模拟成交改变。

模拟域只读查询已发布的 `ResearchStrategySpec`、completed `ResearchRun` 及其
signal artifact。创建会话时，策略 checksum、策略 ID 和数据发布 ID 必须与
ResearchRun 冻结清单完全一致。每个决策还必须引用该 run 中真实存在的
`decision_id + signal trace`，不能用自然语言声明“已经验证”。

## 账户与会话状态

一个活动模拟账户最多有一个 `created/running/paused` 会话。

```text
created ── start ──> running <── resume ── paused
   │                    │                    │
   └──────── stop <─────┴────────────────────┘
                         │
                      stopped ── archive ──> archived
```

- `running` 才接收策略决策和行情。
- `paused` 不撮合新行情，但允许撤销活动订单；恢复后仍使用持久化时钟。
- `stop` 取消全部活动订单并释放现金/持仓预占。
- `archive` 同时把账户设为只读。
- `reset` 只允许从 stopped/archived 发起，始终创建新账户和新会话；旧订单、成交、
  账本和审计不会被清空或覆盖。

## 行情模式与市场时钟

`source_mode` 是必填且不可变的：

- `historical_replay`：由冻结历史发布按时间顺序推送 Bar。
- `readonly_market`：由延迟或实时只读行情源推送 Bar，不解析任何交易凭证。

每个事件包含调用方稳定的 `source_event_id` 和内容 checksum。同 ID 同内容重放返回
`duplicate=true`，不重复成交或记账；同 ID 不同内容失败关闭。事件时间不得早于会话
当前时钟。时钟保存 `current_at/trade_date/speed/last_source_event_id`，暂停、恢复、
倍速和每次推进均可通过会话与审计查询。

`clock_speed` 是回放编排器的节奏契约，撮合结果只由事件顺序和冻结配置决定，不读取
墙上时钟。行情断流不会生成虚构 Bar 或成交；操作者可暂停会话，恢复数据后继续。

## 撮合、资产和会计假设

所有产品模拟订单强制 next-bar，支持 `next_bar_open/close/vwap_proxy`、市价/限价、
GFD/IOC/FOK、部分成交、最大成交量参与率、最小手数、滑点、跳空、停牌和涨跌停。
撮合使用创建订单时持久化的资产规则快照；未知或市场/资产类型不匹配的规则失败关闭。

当前白名单：

- A 股股票：100 股手数、T+1、卖出印花税、佣金和涨跌停。
- 权益、跨境、债券、货币 ETF：使用各自手数、T+N、费用和涨跌停规则。
- 可转债：使用独立手数、费用与价格限制。
- 期货：整数手、合约乘数、双向开平、手续费、保证金和逐日盯市。

现金类买单先预占“参考金额 + 市价缓冲 + 费用”，卖单先预占可用持仓。未成交或撤单
释放剩余预占。期货按合约乘数和保证金率预占，结算价变化产生变动盈亏并重新计算
保证金；资金不足触发 `margin_call` 审计并暂停会话。

期货不做静默连续合约切换。每个期货 Bar 必须提供与 symbol 一致的具体
`contract_id`。换月由正式策略决策明确把旧合约目标降为 0、把新合约目标升至所需
手数，两腿分别经过风控、撮合、费用、保证金和审计。数据缺少新旧合约 Bar 时不会
自动补价或换月。

会计恒等式为：

```text
equity = cash + frozen_cash + margin_used + cash_asset_market_value
```

持仓增加/减少、已实现盈亏、费用和税只发生在 `SimulationFill` 处理中。账本是追加
写入的快照；WebSocket 仅用于低延迟通知，数据库才是恢复和审计真值。

## 风控与故障恢复

模拟下单前检查包括：

- 活动订单数、单笔名义金额和单标的目标金额；
- 当前持仓加全部未成交开仓订单后的组合总敞口；
- 可用现金、T+N 可用数量和重复预占；
- 期货空头开关与保证金使用率。

同一会话行使用 PostgreSQL 行锁串行化决策和行情处理，订单 intent key、决策 ID、
行情事件 ID 和 fill event key 均有唯一约束。一次行情推进中的订单、成交、持仓、
现金、账本和审计处于同一数据库事务；持久化失败时调用方必须回滚，不能保留半笔
成交。

应用启动时只扫描活动模拟会话：

1. 从活动订单重算冻结现金和冻结持仓；
2. 若预占超过资金或持仓，失败关闭并暂停会话；
3. 把崩溃时遗留的 `processing` 行情标为可重试的 `failed`；
4. 从持仓重算保证金和权益；
5. 增加 `recovery_count` 并写入独立模拟审计。

恢复过程不查询 Broker，也不会重发模拟订单。相同失败事件必须以原 ID 和原 checksum
重放，成功后才变为 processed。

## REST 与 WebSocket

主要 REST 契约：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST/GET | `/api/simulation/accounts` | 创建/列出模拟账户 |
| GET | `/api/simulation/accounts/{account_id}` | 查询账户资金和权益 |
| POST/GET | `/api/simulation/sessions` | 创建/列出会话 |
| GET | `/api/simulation/sessions/{session_id}` | 查询配置、时钟和状态 |
| POST | `.../{session_id}/start|pause|stop|archive|reset` | 状态迁移 |
| POST/GET | `.../{session_id}/decisions` | runner 提交目标/查询决策血缘 |
| POST | `.../{session_id}/market-events` | 幂等推进一条 Bar |
| GET | `.../{session_id}/orders|fills|positions|ledger|audit` | 查询完整生命周期 |
| POST | `.../{session_id}/orders/{order_id}/cancel` | 撤销活动模拟订单 |
| GET | `.../{session_id}/report` | 与 ResearchRun 结果比较偏差 |
| POST | `.../{session_id}/evaluate` | 记录模拟晋级评估，不触发其它环境 |

`/ws/simulation/{SIM-S-id}` 发送 `session_created/session_transition/
strategy_decision/market_event_processed/order_cancelled/promotion_evaluated`
等通知。断线后应通过 REST 和审计补查，不能把 WebSocket 当成事件真值。

当前应用没有面向公网的身份认证；模拟 runner 端点是本地模块化单体的内部契约，
不应直接暴露到公网。即使调用该端点，结构化 schema 和后端隔离仍保证它不能创建
实盘订单或读取 QMT 配置。

## 操作步骤

1. 执行 `uv run alembic upgrade head`。
2. 发布无代码策略，并完成绑定同一数据发布的 `ResearchRun`。
3. 创建 10–50 万元模拟账户和会话。
4. 启动会话，先推送当前 Bar 建立可审计参考价。
5. 正式 runner 提交带 ResearchRun decision/signal trace 的目标仓位。
6. 继续按序推送 Bar，查询订单、成交、持仓、账本和审计。
7. 必要时暂停/恢复；结束后 stop 并调用 evaluate。
8. report 只用于偏差和执行质量分析。`eligible` 不是实盘许可。

测试命令：

```bash
uv run pytest tests/unit/simulation -v
uv run pytest tests/integration/test_persistent_simulation.py -v
```

## 与 `phase1_doc.md` §3.4 的映射

§3.4 的账户查询、创建/查询订单、撤单、部分/全部成交、持仓变化、重启恢复和审计，
均可在 simulation 命名空间验证。连接交易前置、券商委托查询与券商持仓核对不适用：
模拟域明确禁止 Broker 连接，其真值是 PostgreSQL 模拟账本。任何影子盘、小资金
实盘晋级必须另开 issue 并由用户确认。
