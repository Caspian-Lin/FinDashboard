"""统一异常体系。

设计原则:

* 全部继承 ``FinboardError``,上层可统一捕获;不要捕获 ``Exception`` 来"过滤本系统异常"。
* 区分"业务可恢复"(风控拒绝、订单未找到)与"系统不可恢复"(broker 连接断、库不可用)。
* ``BrokerTimeoutError`` 是**红线异常** —— 调用方必须把订单置入 ``UNKNOWN``,
  严禁直接重试(见 AGENTS.md §交易安全红线)。
"""

from __future__ import annotations

from finboard_shared.types import RejectReason


class FinboardError(Exception):
    """所有 finboard 异常的基类。"""


class RiskCheckError(FinboardError):
    """下单前风控未通过。

    ``reason`` 用于持久化到 ``orders.reject_reason``;``message`` 给人看。
    """

    def __init__(self, reason: RejectReason, message: str = "") -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason.value}: {message}" if message else reason.value)


class KillSwitchActiveError(FinboardError):
    """Kill Switch 处于暂停状态,禁止当前操作。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BrokerError(FinboardError):
    """与券商交互过程中的非超时异常(连接失败、协议错误、参数错误等)。"""


class BrokerTimeoutError(BrokerError):
    """下单/撤单请求超时(关键红线)。

    **调用方义务**:

    1. 不要重发同一条订单;
    2. 将订单状态置为 ``UNKNOWN``;
    3. 通过查询接口确认券商是否已收到、是否已成交;
    4. 确认券商侧不存在该订单后,才允许用新 ``client_order_id`` 重发。
    """


class OrderNotFoundError(FinboardError):
    """查询订单未找到(本地 / 券商皆可)。"""


class ReconcileMismatchError(FinboardError):
    """本地 ↔ 券商状态不一致。需要人工介入或自动修复后再放行交易。"""


class KernelNotReadyError(FinboardError):
    """内核未就绪 —— 核对未通过或未完成,禁止下单(交易安全红线)。"""
