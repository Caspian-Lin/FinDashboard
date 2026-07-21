"""订单状态机。

集中维护状态迁移合法性,任何 OrderManager 的状态变更都必须经过本模块。
状态图见 ``phase1_doc.md`` §4.2 与 :class:`finboard_shared.types.OrderStatus`。

特殊说明:
* 进入 ``UNKNOWN`` 后**只能**通过查询驱动回到 ``ACKNOWLEDGED`` 或 ``REJECTED``;
* ``FILLED`` / ``CANCELLED`` / ``REJECTED`` 是终态,不可再迁移。
"""

from __future__ import annotations

from finboard_shared.types import OrderStatus


class InvalidStateTransitionError(RuntimeError):
    """订单尝试非法状态迁移。"""

    def __init__(self, from_: OrderStatus, to: OrderStatus) -> None:
        super().__init__(f"非法订单状态迁移: {from_.value} → {to.value}")
        self.from_status = from_
        self.to_status = to


#: 合法迁移表。键为当前状态,值为允许迁入的目标状态集合。
#: 同状态保持(例如 PARTIALLY_FILLED → PARTIALLY_FILLED)默认允许,不在此列出。
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.RISK_CHECKED, OrderStatus.REJECTED}
    ),
    OrderStatus.RISK_CHECKED: frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.REJECTED}
    ),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset({OrderStatus.CANCELLED}),
    # UNKNOWN 只能通过查询驱动回到正常态;本身不能直接再 SUBMIT
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    # 终态没有出口
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class OrderStateMachine:
    """无状态工具类,所有方法都是纯函数。"""

    @staticmethod
    def check_transition(from_: OrderStatus, to: OrderStatus) -> None:
        if from_ == to:
            return  # 幂等迁移始终允许
        allowed = _TRANSITIONS.get(from_, frozenset())
        if to not in allowed:
            raise InvalidStateTransitionError(from_, to)

    @staticmethod
    def can_transition(from_: OrderStatus, to: OrderStatus) -> bool:
        if from_ == to:
            return True
        return to in _TRANSITIONS.get(from_, frozenset())
