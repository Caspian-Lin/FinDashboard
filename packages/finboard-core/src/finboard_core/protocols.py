"""对外依赖的 Protocol(结构化子类型)。

* ``RiskChecker`` 由 :mod:`finboard_risk` 实现,本包通过 Protocol 引用,
  避免对 ``finboard-risk`` 包的硬依赖 —— 这样 ``finboard-core`` 可以独立单测。
* ``Reconciler`` 由 :mod:`finboard_reconcile` 实现,同理。
* Protocol 仅描述"必须有什么",实现方可以自由扩展。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from finboard_shared.models import OrderRequest


@runtime_checkable
class RiskChecker(Protocol):
    """下单前风控 + Kill Switch 状态检查的契约。"""

    async def check(self, request: OrderRequest) -> None:
        """检查下单意图;未通过则抛 :class:`RiskCheckError`。

        实现必须保证:

        * **同步**返回结果(无 IO 阻塞超过 ms 量级);
        * 抛异常时 ``reason`` 必须填(用于落 ``orders.reject_reason``);
        * Kill Switch 处于 NO_NEW_ORDERS / REDUCE_ONLY / HALT 时,
          对应场景一律抛 :class:`KillSwitchActiveError`。
        """

        ...

    async def can_place_new_orders(self) -> bool:
        """Kill Switch 软探测,不下单情况下查询当前是否允许新单。"""
        ...


class ReconciliationResult(Protocol):
    """核对结果的契约(结构匹配 :class:`finboard_reconcile.ReconciliationReport`)。"""

    @property
    def ok(self) -> bool:
        """无任何差异时为 True。"""
        ...

    def summary(self) -> str:
        """人类可读的汇总。"""
        ...


@runtime_checkable
class Reconciler(Protocol):
    """核对引擎契约。kernel.start 时调用以执行启动核对(交易安全红线)。"""

    async def run(self) -> ReconciliationResult:
        """执行一次本地 ↔ 券商核对并返回结果。"""
        ...


class RecoveryResult(Protocol):
    """恢复结果的契约(结构匹配 :class:`finboard_reconcile.RecoveryReport`)。"""

    @property
    def ok(self) -> bool:
        """所有订单均可修复时为 True。"""
        ...

    def summary(self) -> str:
        """人类可读的汇总。"""
        ...


@runtime_checkable
class Recoverer(Protocol):
    """重启恢复引擎契约。kernel.start 时在 order_manager.start() 之前调用。"""

    async def run(self) -> RecoveryResult:
        """加载本地活动订单 → 查券商 → 修复状态 → 返回报告。"""
        ...
