# finboard-risk

下单前风控与 Kill Switch。

## 范围(P0)

* 价格 / 数量基础合法性
* 单笔金额上限、活动订单数上限、每分钟订单数上限
* 是否允许市价单 / 卖空(配置开关)
* Kill Switch:NO_NEW_ORDERS / REDUCE_ONLY / CANCEL_ALL / HALT

## 设计

* 实现 :class:`finboard_core.protocols.RiskChecker`,通过 Protocol 注入到 OrderManager;
* **不反向依赖** finboard-core / finboard-persistence —— 需要账户/持仓/订单计数时通过
  :class:`RiskContext` Protocol 由调用方提供;
* 所有"不通过"场景统一抛 :class:`finboard_shared.exceptions.RiskCheckError` /
  :class:`KillSwitchActiveError`,``reason`` 字段对应 ``RejectReason`` 枚举。

## 后续扩展(P0 之后)

* 单标的最大仓位、当日累计买入上限(需要日累计持久化)
* 重复订单检测(对相同 strategy + symbol + side 在窗口内去重)
* 速率限制按 strategy 维度细分
