"""xtdata 行情数据 → finboard 领域模型转换(纯函数)。

xtdata ``subscribe_quote`` 回调返回 ``dict``,字段名与 xtdata 文档一致。
本模块负责把 raw dict 转换为 :class:`Tick` / :class:`Bar`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finboard_shared.models import Bar, Symbol, Tick
from finboard_shared.types import BarPeriod, Market


def _guess_market(stock_code: str) -> Market:
    code = stock_code.upper()
    if code.endswith((".SH", ".SZ", ".BJ")):
        return Market.A_SHARE
    return Market.A_SHARE


def _make_symbol(stock_code: str) -> Symbol:
    return Symbol(code=stock_code, market=_guess_market(stock_code))


def _to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal("0")
    return Decimal(str(val))


def _to_float(val: Any) -> float:
    if val is None:
        return 0.0
    return float(val)


def xtdata_tick_to_tick(
    stock_code: str, data: dict[str, Any]
) -> Tick:
    """xtdata tick 回调 dict → :class:`Tick`。

    xtdata tick dict 字段::

        last_price  最新价
        open        开盘价
        high        最高价
        low         最低价
        amount      成交额
        volume      成交量(股)
        last_volume 本笔成交量
        bid_price   买一价(list)
        bid_volume  买一量(list)
        ask_price   卖一价(list)
        ask_volume  卖一量(list)
        time        时间戳(ms)
    """
    bid_prices = data.get("bid_price") or []
    bid_volumes = data.get("bid_volume") or []
    ask_prices = data.get("ask_price") or []
    ask_volumes = data.get("ask_volume") or []

    ts_ms = data.get("time")
    if ts_ms is not None:
        timestamp = datetime.fromtimestamp(_to_float(ts_ms) / 1000.0, tz=UTC)
    else:
        timestamp = datetime.now(UTC)

    return Tick(
        symbol=_make_symbol(stock_code),
        last_price=_to_decimal(data.get("last_price")),
        open=_to_decimal(data.get("open")),
        high=_to_decimal(data.get("high")),
        low=_to_decimal(data.get("low")),
        volume=_to_decimal(data.get("volume")),
        amount=_to_decimal(data.get("amount")),
        last_volume=_to_decimal(data.get("last_volume")),
        bid_price=_to_decimal(bid_prices[0]) if bid_prices else Decimal("0"),
        bid_volume=_to_decimal(bid_volumes[0]) if bid_volumes else Decimal("0"),
        ask_price=_to_decimal(ask_prices[0]) if ask_prices else Decimal("0"),
        ask_volume=_to_decimal(ask_volumes[0]) if ask_volumes else Decimal("0"),
        timestamp=timestamp,
    )


def xtdata_bar_to_bar(
    stock_code: str, period: str, data: dict[str, Any]
) -> Bar:
    """xtdata bar dict → :class:`Bar`。

    xtdata bar dict 字段::

        time    bar 起始时间戳(ms)
        open    开盘价
        high    最高价
        low     最低价
        close   收盘价
        volume  成交量
        amount  成交额
    """
    ts_ms = data.get("time")
    if ts_ms is not None:
        timestamp = datetime.fromtimestamp(_to_float(ts_ms) / 1000.0, tz=UTC)
    else:
        timestamp = datetime.now(UTC)

    return Bar(
        symbol=_make_symbol(stock_code),
        period=BarPeriod(period),
        timestamp=timestamp,
        open=_to_decimal(data.get("open")),
        high=_to_decimal(data.get("high")),
        low=_to_decimal(data.get("low")),
        close=_to_decimal(data.get("close")),
        volume=_to_decimal(data.get("volume")),
        amount=_to_decimal(data.get("amount")),
    )


def xtdata_history_to_bars(
    stock_code: str,
    period: str,
    raw_data: Any,
) -> list[Bar]:
    """xtdata ``get_market_data_ex`` 返回值 → list[Bar]。

    ``get_market_data_ex`` 返回 ``{stock_code: (DataFrame, index)}`` 或
    ``{stock_code: ndarray}``;这里通过 duck-typing 处理:

    * 如果有 ``iterrows`` → DataFrame;
    * 如果有 ``__iter__`` → 按 row 拆分。
    """
    if raw_data is None:
        return []

    result: list[Bar] = []

    # get_market_data_ex 返回 dict: {code: xtdata.DataTable}
    if isinstance(raw_data, dict):
        table = raw_data.get(stock_code)
        if table is None:
            # 可能返回了其他 key
            for v in raw_data.values():
                if v is not None:
                    table = v
                    break
        if table is None:
            return []
    else:
        table = raw_data

    # xtdata.DataTable 支持 .to_numpy() 或直接索引
    # 常见列序: time, open, high, low, close, volume, amount
    rows: list[Any] = []
    if hasattr(table, "iterrows"):
        for _, row in table.iterrows():
            rows.append(row)
    elif hasattr(table, "to_numpy"):
        for row in table.to_numpy():
            rows.append(row)
    elif hasattr(table, "__iter__"):
        for row in table:
            rows.append(row)

    for row in rows:
        if hasattr(row, "__getitem__"):
            # 按列名取值(DataFrame row)或按索引取值(ndarray row)
            time_val = row["time"] if hasattr(row, "__contains__") and "time" in row else row[0]
            open_val = row["open"] if hasattr(row, "__contains__") and "open" in row else row[1]
            high_val = row["high"] if hasattr(row, "__contains__") and "high" in row else row[2]
            low_val = row["low"] if hasattr(row, "__contains__") and "low" in row else row[3]
            close_val = row["close"] if hasattr(row, "__contains__") and "close" in row else row[4]
            vol_val = row["volume"] if hasattr(row, "__contains__") and "volume" in row else row[5]
            amt_val = row["amount"] if hasattr(row, "__contains__") and "amount" in row else row[6] if len(row) > 6 else 0
        else:
            # SimpleNamespace or dataclass
            time_val = getattr(row, "time", 0)
            open_val = getattr(row, "open", 0)
            high_val = getattr(row, "high", 0)
            low_val = getattr(row, "low", 0)
            close_val = getattr(row, "close", 0)
            vol_val = getattr(row, "volume", 0)
            amt_val = getattr(row, "amount", 0)

        ts_ms = _to_float(time_val)
        timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms else datetime.now(UTC)

        result.append(
            Bar(
                symbol=_make_symbol(stock_code),
                period=BarPeriod(period),
                timestamp=timestamp,
                open=_to_decimal(open_val),
                high=_to_decimal(high_val),
                low=_to_decimal(low_val),
                close=_to_decimal(close_val),
                volume=_to_decimal(vol_val),
                amount=_to_decimal(amt_val),
            )
        )

    return result
