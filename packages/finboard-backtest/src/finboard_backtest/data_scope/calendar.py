"""DataScope 的交易日历与窗口换算(issue #510)。

窗口严格按 ``trade_cal`` 交易日口径:
``warmup_start <= decision_start <= decision_end <= valuation_end``。
多市场取各交易所交易日的并集(不同市场日历不互相裁剪);缺任一市场
日历即 fail-closed,不用自然日兜底。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeDecisionWindow,
    ScopeWindow,
)
from finboard_persistence.trade_calendar_repo import TradeCalRepository
from finboard_shared.types import AssetClass, Market

MARKET_EXCHANGES: dict[Market, tuple[str, ...]] = {
    Market.A_SHARE: ("SSE",),
    Market.HK: ("HKEX",),
    Market.US: ("NYSE",),
    Market.FUTURE: ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX"),
}

_DERIVATIVE_CLASSES = {AssetClass.DERIVATIVE, AssetClass.COMMODITY}
_A_SHARE_CLASSES = {AssetClass.EQUITY, AssetClass.FIXED_INCOME, AssetClass.CONVERTIBLE}


class ScopeCalendarError(ValueError):
    """日历缺失或窗口无法落位(具名 code 供 API/MCP 映射)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def markets_for(spec: DataScopeSpec) -> tuple[Market, ...]:
    if spec.universe.markets:
        return spec.universe.markets
    classes = set(spec.universe.asset_classes)
    markets: list[Market] = []
    if classes & _A_SHARE_CLASSES or not classes:
        markets.append(Market.A_SHARE)
    if classes & _DERIVATIVE_CLASSES:
        markets.append(Market.FUTURE)
    return tuple(markets) or (Market.A_SHARE,)


def exchanges_for(markets: tuple[Market, ...]) -> tuple[str, ...]:
    """市场 → 交易所代码元组(**排序去重**)。

    顺序必须稳定:``calendar_markets`` 会进 ``resolution_id`` 指纹与 manifest,
    按调用方声明顺序拼指纹会让 ``markets=["future","a_share"]`` 与
    ``["a_share","future"]``(checksum 相同)得到两个 ``resolution_id``,
    提交期误报漂移。
    """

    exchanges: set[str] = set()
    for market in markets:
        exchanges.update(MARKET_EXCHANGES.get(market, ()))
    return tuple(sorted(exchanges))


async def load_union_calendar(
    session: AsyncSession, markets: tuple[Market, ...]
) -> tuple[list[date], tuple[str, ...]]:
    """读取各市场交易日并集;任一交易所无日历时抛错。"""

    exchanges = exchanges_for(markets)
    repo = TradeCalRepository(session)
    days: set[date] = set()
    for exchange in exchanges:
        exchange_days = await repo.list_trading_days(exchange=exchange)
        if not exchange_days:
            raise ScopeCalendarError(
                "scope_calendar_unavailable",
                f"trade_cal 缺少交易所 {exchange} 的交易日历",
            )
        days |= set(exchange_days)
    return sorted(days), exchanges


def build_window(
    decision: ScopeDecisionWindow,
    days: list[date],
    *,
    warmup_sessions: int,
    tail_sessions: int,
    exchanges: tuple[str, ...],
) -> ScopeWindow:
    """把请求的自然日区间落位到交易日窗口。"""

    if not days:
        raise ScopeCalendarError(
            "scope_calendar_unavailable", "所选市场没有可用交易日历"
        )
    start_index = bisect_left(days, decision.start)
    if start_index >= len(days):
        raise ScopeCalendarError(
            "scope_decision_window_outside_calendar",
            f"decision.start={decision.start} 晚于日历末日 {days[-1]}",
        )
    end_index = bisect_right(days, decision.end) - 1
    if end_index < start_index:
        raise ScopeCalendarError(
            "scope_decision_window_no_trading_days",
            f"decision 区间 [{decision.start}, {decision.end}] 没有交易日",
        )
    decision_start = days[start_index]
    decision_end = days[end_index]
    warmup_index = max(0, start_index - warmup_sessions)
    valuation_index = min(len(days) - 1, end_index + tail_sessions)
    return ScopeWindow(
        warmup_start=days[warmup_index],
        decision_start=decision_start,
        decision_end=decision_end,
        valuation_end=days[valuation_index],
        warmup_sessions=start_index - warmup_index,
        decision_sessions=end_index - start_index + 1,
        tail_sessions=valuation_index - end_index,
        calendar_markets=exchanges,
        calendar_source="trade_cal",
    )


__all__ = [
    "MARKET_EXCHANGES",
    "ScopeCalendarError",
    "build_window",
    "exchanges_for",
    "load_union_calendar",
    "markets_for",
]
