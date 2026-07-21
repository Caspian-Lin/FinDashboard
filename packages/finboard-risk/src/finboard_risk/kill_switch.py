"""Kill Switch 状态机。

由 :class:`finboard_risk.checker.PreTradeChecker` 持有,
由 :class:`finboard_core.kernel.TradingKernel.activate_kill_switch` 调用 ``set``。

红线(AGENTS.md §Kill Switch 必须由交易内核执行):

* 不能只靠网页按钮 —— 即使 API/UI 也调到这里,核心拒绝逻辑在风控层;
* HALT 时一律拒绝任何下单/撤单以外的写操作。
"""

from __future__ import annotations

from finboard_shared.types import KillSwitchLevel


class KillSwitch:
    """线程不安全 —— 假定单线程 asyncio event loop。"""

    def __init__(self, initial: KillSwitchLevel = KillSwitchLevel.OFF) -> None:
        self._level = initial

    @property
    def level(self) -> KillSwitchLevel:
        return self._level

    def set(self, level: KillSwitchLevel) -> None:
        self._level = level

    def allows_new_orders(self) -> bool:
        return self._level is KillSwitchLevel.OFF

    def allows_reduce_only(self) -> bool:
        """REDUCE_ONLY 之下允许平仓 / 卖出已有持仓。"""
        return self._level in (
            KillSwitchLevel.OFF,
            KillSwitchLevel.NO_NEW_ORDERS,
            KillSwitchLevel.REDUCE_ONLY,
        )

    def allows_cancel(self) -> bool:
        """撤单在任何级别下都允许(CANCEL_ALL 本身就要求撤)。"""
        return self._level is not KillSwitchLevel.HALT
