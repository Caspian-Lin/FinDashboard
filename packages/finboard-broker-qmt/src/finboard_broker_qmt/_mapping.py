"""xtquant 数据模型 → finboard 领域模型转换(纯函数)。

所有函数接收 ``Any`` —— xtquant 的 ``XtAsset`` / ``XtPosition`` / ``XtOrder``
是 C 扩展对象,仅在 Windows + miniQMT 环境存在,不能在类型标注里直接引用。
转换逻辑通过 ``getattr`` 读字段,同时兼容官方 snake_case 与旧 ``m_`` 前缀。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Account, Order, Position, Symbol
from finboard_shared.types import (
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
)


# --------------------------------------------------------------------------- 字段兼容读取
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """读取 xtquant 对象字段,兼容 snake_case 与 ``m_`` 前缀旧写法。

    xtquant 同一对象可能同时暴露 ``cash``(新)和 ``m_dCash``(旧),
    此函数按优先级尝试:snake_case → m_d + Capitalized → m_n + Capitalized → m_。
    """
    val = getattr(obj, name, None)
    if val is not None:
        return val
    # m_dCash → cash, m_nVolume → volume 等旧前缀(首字母大写)
    capitalized = name[0].upper() + name[1:]
    for prefix in ("m_d", "m_n", "m_s", "m_"):
        val = getattr(obj, f"{prefix}{capitalized}", None)
        if val is not None:
            return val
    return default


def _to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal("0")
    return Decimal(str(val))


# --------------------------------------------------------------------------- 委托状态映射
#: xtquant order_status 枚举 → finboard OrderStatus
#: 来源:dict.thinktrader.net/nativeApi/xttrader.html
XT_ORDER_STATUS_MAP: dict[int, OrderStatus] = {
    48: OrderStatus.CREATED,          # 未报(本地已生成但尚未提交柜台)
    49: OrderStatus.SUBMITTING,       # 待报
    50: OrderStatus.ACKNOWLEDGED,     # 已报(柜台已接收)
    51: OrderStatus.CANCEL_PENDING,   # 已报待撤
    52: OrderStatus.CANCEL_PENDING,   # 部成待撤
    53: OrderStatus.CANCELLED,        # 部撤
    54: OrderStatus.CANCELLED,        # 已撤
    55: OrderStatus.PARTIALLY_FILLED, # 部成
    56: OrderStatus.FILLED,           # 已成
    57: OrderStatus.REJECTED,         # 废单
    255: OrderStatus.UNKNOWN,         # 未知(超时红线)
}


def xt_order_status_to_finboard(status_code: int) -> OrderStatus:
    """xtquant order_status → OrderStatus;未知码 → UNKNOWN。"""
    return XT_ORDER_STATUS_MAP.get(status_code, OrderStatus.UNKNOWN)


# --------------------------------------------------------------------------- 方向 / 类型映射
def _xt_order_type_to_side(order_type: int) -> Side:
    """xtconstant.STOCK_BUY(23) → BUY,STOCK_SELL(24) → SELL。"""
    # xtconstant 值在不同版本可能不同,用奇偶约定:买=23,卖=24(A股)
    if order_type in (24,):  # STOCK_SELL
        return Side.SELL
    return Side.BUY


def _is_limit_price(price_type: int) -> bool:
    """FIX_PRICE = 11(指定价/限价);其余视为非限价。"""
    return price_type == 11


def _guess_market(stock_code: str) -> Market:
    code = stock_code.upper()
    if code.endswith(".SH") or code.endswith(".BJ"):
        return Market.A_SHARE
    if code.endswith(".SZ"):
        return Market.A_SHARE
    return Market.A_SHARE


# --------------------------------------------------------------------------- 对象转换
def xt_asset_to_account(
    asset: Any, account_id: AccountId, broker_kind: BrokerKind
) -> Account:
    return Account(
        account_id=account_id,
        broker_kind=broker_kind,
        cash=_to_decimal(_field(asset, "cash")),
        frozen_cash=_to_decimal(_field(asset, "frozen_cash")),
        total_asset=_to_decimal(_field(asset, "total_asset")),
        margin_used=Decimal("0"),  # A股无保证金
    )


def xt_position_to_position(
    pos: Any, account_id: AccountId
) -> Position:
    stock_code = str(_field(pos, "stock_code", ""))
    return Position(
        account_id=account_id,
        symbol=Symbol(code=stock_code, market=_guess_market(stock_code)),
        position_side=PositionSide.LONG,
        total_quantity=_to_decimal(_field(pos, "volume")),
        available_quantity=_to_decimal(_field(pos, "can_use_volume")),
        frozen_quantity=_to_decimal(_field(pos, "frozen_volume")),
        average_price=_to_decimal(_field(pos, "avg_price")),
        market_value=_to_decimal(_field(pos, "market_value")),
    )


def xt_order_to_order(
    order: Any,
    *,
    account_id: AccountId,
    broker_kind: BrokerKind,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
) -> Order:
    """xtquant XtOrder → finboard Order。

    ``quantity`` / ``price`` 在回报转换时可传 None,由调用方在需要时补全
    (xtquant 回报里有 order_volume / price 字段)。
    """
    stock_code = str(_field(order, "stock_code", ""))
    status_code = int(_field(order, "order_status", 255))
    order_remark = str(_field(order, "order_remark", ""))
    broker_order_id = str(_field(order, "order_id", ""))

    return Order(
        client_order_id=ClientOrderId(order_remark) if order_remark else ClientOrderId(""),
        account_id=account_id,
        broker_kind=broker_kind,
        symbol=Symbol(code=stock_code, market=_guess_market(stock_code)),
        side=_xt_order_type_to_side(int(_field(order, "order_type", 23))),
        order_type=OrderType.LIMIT
        if _is_limit_price(int(_field(order, "price_type", 11)))
        else OrderType.MARKET,
        quantity=quantity or _to_decimal(_field(order, "order_volume")),
        price=_to_decimal(_field(order, "price")) if price is None else price,
        broker_order_id=broker_order_id,
        filled_quantity=_to_decimal(_field(order, "traded_volume")),
        average_fill_price=_to_decimal(_field(order, "traded_price"))
        or None,
        status=xt_order_status_to_finboard(status_code),
    )
