"""发布交易日历对单标的洞的免疫与全局洞 fail-closed(issue #334)。

RR-26e5640 事故回归:发布里第一个 ready 标的(000001.SZ)缓存缺
2020-07-27 → 2024-01-01,旧实现只读该标的的 bars 推导交易日历,
``_next_execution_at`` 在失真日历上把全部决策的成交时点静默跳过 3.5 年
空洞落到 2024-01-02 → sizing 按决策日价格、成交按终末端价格,现金为负
被组合硬约束拒绝。锁定四点:

* 单标的(尤其是首个 ready 标的)大段缺失不再偏移日历 —— 多标的采样
  并集补齐,``_next_execution_at`` 返回洞内的真实下一交易日;
* 并集采样必须**等距铺开**:前半标的集体缺洞、后半完整时仍能补齐
  (排除「只多读前 K 只」的弱实现);
* 全部采样标的共享大段缺失时 fail-closed 具名拒绝,不再静默跳洞;
* 正常长假(春节 + 周末 ≈ 11 个自然日)不触发间隙哨兵。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from finboard_backtest.research_run.signal_engine import (
    _next_execution_at,
    _release_trading_days,
)

_CST = ZoneInfo("Asia/Shanghai")


def _weekdays(start: date, end: date) -> list[date]:
    """``[start, end]`` 内的连续工作日(跳过周末),模拟发布交易日历。"""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


@dataclass(frozen=True, slots=True)
class _Bar:
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class _Instrument:
    code: str
    ready: bool = True


@dataclass
class _Release:
    release_id: str
    instruments: tuple[_Instrument, ...]
    period: str = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2022, 1, 1)
    end_date: date = date(2022, 12, 31)


@dataclass
class _Provider:
    """按标的返回全区间 bars 的最小 stub(只暴露 fetch_bars 需要的形状)。"""

    release: _Release
    sessions_by_symbol: dict[str, list[date]] = field(default_factory=dict)

    async def fetch_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[_Bar]:
        del period, adjust
        sessions = self.sessions_by_symbol.get(symbol.code, [])  # type: ignore[attr-defined]
        return [
            _Bar(timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC))
            for day in sessions
            if start <= day <= end
        ]


def _provider(
    sessions: list[date],
    *,
    holed_symbols: tuple[str, ...] = (),
    symbols: tuple[str, ...] = ("000001.SZ", "000002.SZ", "000003.SZ"),
    hole: tuple[date, date] | None = None,
    start_date: date = date(2022, 1, 1),
    end_date: date = date(2022, 12, 31),
) -> _Provider:
    """构造发布 stub;``holed_symbols`` 中的标的挖掉 ``hole`` 区间的交易日。"""
    sessions_by_symbol: dict[str, list[date]] = {}
    for code in symbols:
        if code in holed_symbols and hole is not None:
            sessions_by_symbol[code] = [day for day in sessions if not (hole[0] <= day <= hole[1])]
        else:
            sessions_by_symbol[code] = list(sessions)
    return _Provider(
        release=_Release(
            "calendar-hole-r1",
            tuple(_Instrument(code=code) for code in symbols),
            start_date=start_date,
            end_date=end_date,
        ),
        sessions_by_symbol=sessions_by_symbol,
    )


_SESSIONS = _weekdays(date(2022, 1, 3), date(2022, 12, 30))
# 洞:2022-04-15 → 2022-10-31(约 6.5 个月,135 个自然日,远超休市合理范围)。
_HOLE = (date(2022, 4, 15), date(2022, 10, 31))
_MID_HOLE_DECISION = datetime(2022, 4, 14, 15, 0, tzinfo=_CST)


@pytest.mark.asyncio
async def test_first_symbol_hole_does_not_shift_execution() -> None:
    """首个 ready 标的大段缺失:并集补洞,成交时点落在洞内真实交易日。

    旧实现(只读第一个标的)对洞后首个决策日返回 2022-11-01(跳洞),
    RR-26e5640 的 44 笔成交全部落到 2024-01-02 即此根因。
    """
    provider = _provider(
        _SESSIONS,
        holed_symbols=("000001.SZ",),
        hole=_HOLE,
    )
    calendar = await _release_trading_days(provider)  # type: ignore[arg-type]
    assert calendar == _SESSIONS  # 并集无洞
    execution = await _next_execution_at(provider, _MID_HOLE_DECISION)  # type: ignore[arg-type]
    assert execution == datetime(2022, 4, 15, 15, 0, tzinfo=_CST)


@pytest.mark.asyncio
async def test_union_sampling_spreads_beyond_prefix() -> None:
    """前半标的集体缺洞、后半完整:等距采样必须命中完整标的补齐并集。"""
    symbols = tuple(f"6000{i:02d}.SH" for i in range(12))
    provider = _provider(
        _SESSIONS,
        symbols=symbols,
        holed_symbols=symbols[:6],
        hole=_HOLE,
    )
    calendar = await _release_trading_days(provider)  # type: ignore[arg-type]
    assert calendar == _SESSIONS
    execution = await _next_execution_at(provider, _MID_HOLE_DECISION)  # type: ignore[arg-type]
    assert execution == datetime(2022, 4, 15, 15, 0, tzinfo=_CST)


@pytest.mark.asyncio
async def test_shared_hole_fails_closed() -> None:
    """全部标的共享大段缺失:日历推导具名拒绝,不再静默跳洞。"""
    provider = _provider(
        _SESSIONS,
        holed_symbols=("000001.SZ", "000002.SZ", "000003.SZ"),
        hole=_HOLE,
    )
    with pytest.raises(ValueError, match="异常空洞"):
        await _release_trading_days(provider)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="异常空洞"):
        await _next_execution_at(provider, _MID_HOLE_DECISION)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_normal_long_holiday_passes_sentinel() -> None:
    """春节 + 周末 ≈ 11 个自然日的市场级休市不触发哨兵。"""
    # 2024 年春节:A 股 2024-02-09(周五)收盘,2024-02-19(周一)恢复。
    sessions = [
        day
        for day in _weekdays(date(2024, 1, 2), date(2024, 3, 29))
        if not (date(2024, 2, 12) <= day <= date(2024, 2, 16))
    ]
    assert (date(2024, 2, 19) - date(2024, 2, 9)).days == 10
    provider = _provider(
        sessions,
        symbols=("000001.SZ", "000002.SZ"),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
    )
    calendar = await _release_trading_days(provider)  # type: ignore[arg-type]
    assert calendar == sessions
    execution = await _next_execution_at(
        provider,  # type: ignore[arg-type]
        datetime(2024, 2, 9, 15, 0, tzinfo=_CST),
    )
    assert execution == datetime(2024, 2, 19, 15, 0, tzinfo=_CST)


@pytest.mark.asyncio
async def test_unreadable_instrument_degrades_to_warning() -> None:
    """采样单标的读取失败降级跳过(具名 warning),并集取自其余标的。"""

    class _BrokenFirst(_Provider):
        async def fetch_bars(
            self,
            symbol: object,
            period: object,
            start: date,
            end: date,
            *,
            adjust: str = "qfq",
        ) -> list[_Bar]:
            if symbol.code == "000001.SZ":  # type: ignore[attr-defined]
                raise RuntimeError("disk hiccup")
            return await super().fetch_bars(symbol, period, start, end, adjust=adjust)

    base = _provider(_SESSIONS, holed_symbols=("000002.SZ",), hole=_HOLE)
    provider = _BrokenFirst(
        release=base.release,
        sessions_by_symbol=base.sessions_by_symbol,
    )
    calendar = await _release_trading_days(provider)  # type: ignore[arg-type]
    # 首标的读取失败被跳过;洞标的(000002.SZ)由完整标的(000003.SZ)补齐。
    assert calendar == _SESSIONS
