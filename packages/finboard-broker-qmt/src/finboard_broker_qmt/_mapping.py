"""xtquant 数据模型 → finboard 领域模型转换(纯函数)。

所有函数接收 ``Any`` —— xtquant 的 ``XtAsset`` / ``XtPosition`` / ``XtOrder``
是 C 扩展对象,仅在 Windows + miniQMT 环境存在,不能在类型标注里直接引用。
转换逻辑通过 ``getattr`` 读字段,同时兼容官方 snake_case 与旧 ``m_`` 前缀。

order_remark 编码(client_order_id ↔ xtquant order_remark,24 字符限制)::

    client_order_id  F-YYYYMMDD-<16hex>   (27 字符)
    order_remark     YYYYMMDD<16hex>      (24 字符,去掉 F- 前缀和分隔符)

回报反查时用 :func:`decode_order_remark` 还原完整 client_order_id。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Account, Fill, Order, Position, Symbol
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


# --------------------------------------------------------------------------- order_remark 编解码
# client_order_id 格式: F-YYYYMMDD-<16hex>  (27 字符)
# xtquant order_remark 限制 24 字符,编码方案:
#   去掉 "F-" 前缀和日期/hex 之间的 "-",得到 YYYYMMDD<16hex> (8+16=24 字符)
# 解码时按位置切分:前 8 字符 = 日期,后 16 字符 = hex suffix

def encode_order_remark(client_order_id: str) -> str:
    """``F-YYYYMMDD-<16hex>`` → ``YYYYMMDD<16hex>`` (24 字符)。

    非标准格式(无法解析)时截断到 24 字符,保证不超 xtquant 限制。
    """
    parts = client_order_id.split("-", 2)
    if len(parts) == 3 and parts[0] == "F":
        remark = parts[1] + parts[2]
        return remark[:24]
    return client_order_id[:24]


def decode_order_remark(remark: str) -> str:
    """``YYYYMMDD<16hex>`` → ``F-YYYYMMDD-<16hex>``。

    非标准格式(无法解析)时原样返回(容错:可能是旧版或人工填写的 remark)。
    """
    remark = remark.strip()
    if len(remark) == 24 and remark[:8].isdigit():
        return f"F-{remark[:8]}-{remark[8:]}"
    return remark


# --------------------------------------------------------------------------- 正向映射(finboard → xtconstant)
#: xtconstant 值(A股固定):STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11, LATEST_PRICE=5
XT_STOCK_BUY = 23
XT_STOCK_SELL = 24
XT_FIX_PRICE = 11
XT_LATEST_PRICE = 5


def side_to_xt_order_type(side: Side) -> int:
    """finboard Side → xtconstant order_type。"""
    if side is Side.SELL:
        return XT_STOCK_SELL
    return XT_STOCK_BUY


def order_type_to_xt_price_type(order_type: OrderType) -> int:
    """finboard OrderType → xtconstant price_type。"""
    if order_type is OrderType.MARKET:
        return XT_LATEST_PRICE
    return XT_FIX_PRICE


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

    # order_remark 是编码后的 client_order_id,需要解码
    cid_str = decode_order_remark(order_remark) if order_remark else ""

    return Order(
        client_order_id=ClientOrderId(cid_str) if cid_str else ClientOrderId(""),
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


def xt_trade_to_fill(
    trade: Any,
    *,
    broker_kind: BrokerKind,
) -> Fill:
    """xtquant XtTrade(单笔成交回报) → finboard Fill。

    XtTrade 字段(兼容 snake_case + m_ 前缀):

    * ``filled_id`` / ``m_strFilledId`` — 成交编号(→ fill_id)
    * ``order_id`` / ``m_nOrderId`` — 委托号(→ broker_order_id)
    * ``traded_volume`` / ``m_dVolume`` — 成交数量
    * ``traded_price`` / ``m_dTradedPrice`` — 成交价格
    * ``order_remark`` / ``m_strOrderRemark`` — 编码后的 client_order_id
    * ``stock_code`` / ``m_strStockCode`` — 合约代码
    * ``order_type`` / ``m_nOrderType`` — 买卖方向(23/24)
    """
    stock_code = str(_field(trade, "stock_code", ""))
    order_remark = str(_field(trade, "order_remark", ""))
    cid_str = decode_order_remark(order_remark) if order_remark else ""

    return Fill(
        fill_id=str(_field(trade, "filled_id", "")),
        client_order_id=ClientOrderId(cid_str),
        symbol=Symbol(code=stock_code, market=_guess_market(stock_code)),
        side=_xt_order_type_to_side(int(_field(trade, "order_type", XT_STOCK_BUY))),
        quantity=_to_decimal(_field(trade, "traded_volume")),
        price=_to_decimal(_field(trade, "traded_price")),
        broker_order_id=str(_field(trade, "order_id", "")) or None,
    )
