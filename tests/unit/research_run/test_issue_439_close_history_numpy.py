"""close 矩阵原生数组化(issue #439)。

``SymbolCloseHistory`` 内部存储从 Python 对象元组(available_at/dates/closes
每 (天, 标的) 一个 date/datetime/装箱 float 对象,≈150B)改为 numpy 原生数组
(int64 epoch 微秒 / int64 epoch 天数 / float64,≈24B),全市场 5534 标的 x
~2800 天的常驻从 ~2.5GB 量级降到 ~400MB 量级。公开四方法
(visible_index / close_at / open_at / series_until)签名与返回类型逐值不变;
``visible_index`` 用 ``searchsorted(side="right")`` 实现旧 ``bisect_right``
双键取小语义;构建期非递减校验保留在 int64 上(乱序数据返回 ``None``,
调用方逐期回退,与元组时代同一防御语义)。

本文件锁定:与「测试内 Python 元组参照实现」逐值等值(真实 builder 发布 +
构造样本;as_of 早于首根/晚于末根/恰好相等/双键取小边界、opens 未携带、
空序列)、naive/aware 混比 TypeError、tracemalloc 峰值 ≤ 参照 1/3。纯离线
研究域,不连 broker 不下单。
"""

from __future__ import annotations

import tracemalloc
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from finboard_backtest.research_run.contracts import UniverseCandidate
from finboard_backtest.research_run.frozen_loader import (
    SymbolCloseHistory,
    _history_from_points,
    _load_close_histories,
)
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_CST = ZoneInfo("Asia/Shanghai")
_RELEASE_ID = "close-numpy-r1"
_CODES = ("600519.SH", "000001.SZ")
_SESSION_COUNT = 6


def _sessions(count: int) -> list[date]:
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


SESSIONS = _sessions(_SESSION_COUNT)
_START = SESSIONS[0]
_END = SESSIONS[-1]


# ---------------------------------------------------------------------------
# 旧「Python 元组」参照实现(#439 改造前的行为原样复刻)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TupleCloseHistory:
    """#439 前的 ``SymbolCloseHistory``(元组 + bisect_right,等值参照)。"""

    available_at: tuple[datetime, ...]
    dates: tuple[date, ...]
    closes: tuple[float, ...]
    opens: tuple[float, ...] | None = None

    def visible_index(self, as_of: datetime) -> int:
        index = min(
            bisect_right(self.available_at, as_of),
            bisect_right(self.dates, as_of.date()),
        )
        return index - 1

    def close_at(self, as_of: datetime) -> float | None:
        index = self.visible_index(as_of)
        if index < 0:
            return None
        return self.closes[index]

    def open_at(self, as_of: datetime) -> float | None:
        if self.opens is None:
            return None
        index = self.visible_index(as_of)
        if index < 0:
            return None
        return self.opens[index]

    def series_until(self, as_of: datetime) -> list[float]:
        index = self.visible_index(as_of)
        if index < 0:
            return []
        return list(self.closes[: index + 1])


def _assert_equivalent(
    history: SymbolCloseHistory | None,
    reference: _TupleCloseHistory,
    as_of_values: Sequence[datetime],
    *,
    want_open: bool = False,
) -> None:
    assert history is not None
    for as_of in as_of_values:
        assert history.visible_index(as_of) == reference.visible_index(as_of)
        assert history.close_at(as_of) == reference.close_at(as_of)
        assert history.series_until(as_of) == reference.series_until(as_of)
        if want_open:
            assert history.open_at(as_of) == reference.open_at(as_of)


class TestTupleReferenceEquivalence:
    """构造样本上新旧表示逐值等值(含双键取小与各类边界)。"""

    def test_realistic_available_at_with_boundaries(self) -> None:
        """available_at = 当日 15:30 CST(as_of 时刻横跨 PIT 边界两侧)。"""
        available = tuple(
            datetime.combine(day, time(15, 30), tzinfo=_CST) for day in SESSIONS
        )
        dates = tuple(SESSIONS)
        closes = tuple(float(index) + 0.5 for index in range(len(SESSIONS)))
        history = SymbolCloseHistory.from_sequences(
            available_at=available, dates=dates, closes=closes
        )
        reference = _TupleCloseHistory(
            available_at=available, dates=dates, closes=closes
        )
        as_of_values = [
            datetime.combine(SESSIONS[0], time(0, 0), tzinfo=_CST),  # 早于首根
            datetime.combine(SESSIONS[0], time(15, 29, 59, 999999), tzinfo=_CST),
            datetime.combine(SESSIONS[0], time(15, 30), tzinfo=_CST),  # 恰好相等
            datetime.combine(SESSIONS[2], time(10, 0), tzinfo=_CST),
            datetime.combine(SESSIONS[-1], time(15, 30), tzinfo=_CST),
            datetime.combine(SESSIONS[-1], time(23, 59), tzinfo=_CST),  # 晚于末根
            datetime.combine(SESSIONS[-1] + timedelta(days=3), time(12, 0), tzinfo=_CST),
        ]
        _assert_equivalent(history, reference, as_of_values)

    def test_double_key_min_boundary(self) -> None:
        """双键 disagree:available_at 键与 date 键的 cut 不同,取小者。"""
        available = (
            datetime(2024, 1, 2, 15, 30, tzinfo=_CST),
            datetime(2024, 1, 3, 15, 30, tzinfo=_CST),
            datetime(2024, 1, 4, 15, 30, tzinfo=_CST),
        )
        dates = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
        closes = (1.0, 2.0, 3.0)
        history = SymbolCloseHistory.from_sequences(
            available_at=available, dates=dates, closes=closes
        )
        reference = _TupleCloseHistory(
            available_at=available, dates=dates, closes=closes
        )
        as_of_values = [
            # available_at 键更紧:当日 10:00 尚不可见(available 15:30)。
            datetime(2024, 1, 3, 10, 0, tzinfo=_CST),
            datetime(2024, 1, 3, 15, 29, 59, 999999, tzinfo=_CST),
            # 两键同 cut。
            datetime(2024, 1, 3, 16, 0, tzinfo=_CST),
            # date 键更紧:次日 00:00,available_at 已可见但 bar 日期属前一日。
            datetime(2024, 1, 4, 0, 0, tzinfo=_CST),
            datetime(2024, 1, 4, 15, 30, tzinfo=_CST),
        ]
        _assert_equivalent(history, reference, as_of_values)

    def test_opens_carried_and_absent(self) -> None:
        opens = (0.5, 1.5, 2.5)
        with_opens = SymbolCloseHistory.from_sequences(
            available_at=(
                datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                datetime(2024, 1, 4, 15, 30, tzinfo=UTC),
            ),
            dates=(date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
            closes=(1.0, 2.0, 3.0),
            opens=opens,
        )
        reference = _TupleCloseHistory(
            available_at=(
                datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                datetime(2024, 1, 4, 15, 30, tzinfo=UTC),
            ),
            dates=(date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
            closes=(1.0, 2.0, 3.0),
            opens=opens,
        )
        _assert_equivalent(
            with_opens,
            reference,
            [datetime(2024, 1, day, 16, 0, tzinfo=UTC) for day in (1, 2, 3, 4, 5)],
            want_open=True,
        )
        # opens 未携带:open_at 恒 None(close-only 矩阵),其余方法不受影响。
        close_only = SymbolCloseHistory.from_sequences(
            available_at=reference.available_at,
            dates=reference.dates,
            closes=reference.closes,
        )
        assert close_only is not None
        as_of = datetime(2024, 1, 3, 16, 0, tzinfo=UTC)
        assert close_only.open_at(as_of) is None
        assert close_only.close_at(as_of) == reference.close_at(as_of)

    def test_empty_sequence(self) -> None:
        history = SymbolCloseHistory.from_sequences(
            available_at=[], dates=[], closes=[]
        )
        reference = _TupleCloseHistory(available_at=(), dates=(), closes=())
        _assert_equivalent(
            history, reference, [datetime(2024, 1, 2, tzinfo=UTC)]
        )
        assert history is not None
        assert history.visible_index(datetime(2024, 1, 2, tzinfo=UTC)) == -1
        assert history.series_until(datetime(2024, 1, 2, tzinfo=UTC)) == []

    def test_naive_and_aware_parity(self) -> None:
        """naive/aware 混比保持旧「直接比较」的 TypeError 语义。"""
        aware = (datetime(2024, 1, 2, 15, 30, tzinfo=UTC),)
        history = SymbolCloseHistory.from_sequences(
            available_at=aware, dates=(date(2024, 1, 2),), closes=(1.0,)
        )
        assert history is not None
        reference = _TupleCloseHistory(
            available_at=aware, dates=(date(2024, 1, 2),), closes=(1.0,)
        )
        naive_as_of = datetime(2024, 1, 2, 16, 0)
        with pytest.raises(TypeError):
            history.visible_index(naive_as_of)
        with pytest.raises(TypeError):
            reference.visible_index(naive_as_of)
        # 同为 naive 序列时 naive 查询正常(挂钟比较)。
        naive_history = SymbolCloseHistory.from_sequences(
            available_at=(datetime(2024, 1, 2, 15, 30),),
            dates=(date(2024, 1, 2),),
            closes=(1.0,),
        )
        assert naive_history is not None
        assert naive_history.close_at(naive_as_of) == 1.0

    def test_non_monotone_returns_none(self) -> None:
        """available_at/dates 任一序列回退 → None(逐期回退防御语义保留)。"""
        assert (
            SymbolCloseHistory.from_sequences(
                available_at=(
                    datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                ),
                dates=(date(2024, 1, 2), date(2024, 1, 3)),
                closes=(1.0, 2.0),
            )
            is None
        )
        assert (
            SymbolCloseHistory.from_sequences(
                available_at=(
                    datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                ),
                dates=(date(2024, 1, 3), date(2024, 1, 2)),
                closes=(1.0, 2.0),
            )
            is None
        )


def _close(code: str, day: date) -> Decimal:
    base = {"600519.SH": "1500.00", "000001.SZ": "10.00"}[code]
    return Decimal(base) * (
        Decimal("1") + Decimal(SESSIONS.index(day)) / Decimal("100")
    )


def _bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=_close(code, day) * Decimal("1.01"),
            high=_close(code, day) * Decimal("1.02"),
            low=_close(code, day) * Decimal("0.98"),
            close=_close(code, day),
            volume=Decimal(10000),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in SESSIONS
    ]


class TestRealReleaseEquivalence:
    """真实 builder 发布:列式直出构建的矩阵 ↔ 元组参照(对象路径)逐值等值。"""

    async def test_matrix_methods_match_tuple_reference(self, tmp_path: Path) -> None:
        release_root = tmp_path / "releases"
        cache_dir = tmp_path / "cache"
        instruments = [
            ReleaseInstrumentSpec(
                code=code,
                name=f"样本{code}",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                asset_class=AssetClass.EQUITY,
                available_at=datetime(2001, 8, 27, tzinfo=UTC),
                execution=default_execution_metadata(
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                ),
                list_date=date(2001, 8, 27),
            )
            for code in _CODES
        ]
        from finboard_data.cache import ParquetCache

        cache = ParquetCache(cache_dir)
        for instrument in instruments:
            await cache.write(
                Symbol(code=instrument.code, market=instrument.market),
                BarPeriod.D1,
                "qfq",
                _bars(instrument.code),
            )
        await FrozenDatasetReleaseBuilder(
            cache_dir=cache_dir, release_root=release_root
        ).publish(
            DatasetReleaseSpec(
                release_id=_RELEASE_ID,
                dataset_name="close_history_numpy",
                source="fixed_sample",
                version="2024.01",
                start_date=_START,
                end_date=_END,
                code_version="deadbeef",
                required_capabilities=("stock",),
            ),
            instruments,
        )
        provider = FrozenReleaseProvider(
            release_root=release_root, release_id=_RELEASE_ID
        )
        candidates = tuple(
            UniverseCandidate(
                symbol=instrument.code,
                included=True,
                reasons=("unit-test",),
                asset_class="equity",
                market=Market.A_SHARE.value,
            )
            for instrument in instruments
        )
        as_of_values = [
            datetime.combine(day, time(hour, 0), tzinfo=_CST)
            for day in SESSIONS
            for hour in (0, 10, 15, 16, 23)
        ] + [datetime(2023, 12, 31, tzinfo=_CST), datetime(2024, 2, 1, tzinfo=_CST)]
        for include_open in (False, True):
            built = await _load_close_histories(
                provider, candidates, include_open=include_open
            )
            assert set(built) == set(_CODES)
            for code in _CODES:
                points = await FrozenReleaseProvider.fetch_point_in_time_bars(
                    provider,
                    Symbol(code=code, market=Market.A_SHARE),
                    provider.release.period,
                    provider.release.start_date,
                    provider.release.end_date,
                    decision_at=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
                    adjust=provider.release.adjustment,
                )
                reference = _TupleCloseHistory(
                    available_at=tuple(item.available_at for item in points),
                    dates=tuple(item.bar.timestamp.date() for item in points),
                    closes=tuple(float(item.bar.close) for item in points),
                    opens=None
                    if not include_open
                    else tuple(float(item.bar.open) for item in points),
                )
                _assert_equivalent(
                    built[code], reference, as_of_values, want_open=include_open
                )

    def test_history_from_points_non_monotone_fallback(self) -> None:
        """对象路径构建的乱序防御语义保留(#287 返回 None → 逐期回退)。"""

        @dataclass
        class _BarStub:
            timestamp: datetime
            close: Decimal

        @dataclass
        class _Point:
            available_at: datetime
            bar: _BarStub

        late = datetime.combine(SESSIONS[1], time(15, 30), tzinfo=_CST)
        early = datetime.combine(SESSIONS[0], time(15, 30), tzinfo=_CST)
        points = [
            _Point(
                available_at=late,
                bar=_BarStub(
                    timestamp=datetime.combine(SESSIONS[1], time(0, 0), tzinfo=UTC),
                    close=Decimal("1"),
                ),
            ),
            _Point(
                available_at=early,
                bar=_BarStub(
                    timestamp=datetime.combine(SESSIONS[0], time(0, 0), tzinfo=UTC),
                    close=Decimal("2"),
                ),
            ),
        ]
        assert _history_from_points(points) is None  # type: ignore[arg-type]
        # 单调样本正常构建(as_of 1/3 16:00 两根可见,取 available_at 更晚的 1/3 根)。
        history = _history_from_points(
            list(reversed(points))  # type: ignore[arg-type]
        )
        assert history is not None
        assert (
            history.close_at(datetime.combine(SESSIONS[1], time(16, 0), tzinfo=_CST))
            == 1.0
        )
        assert (
            history.close_at(datetime.combine(SESSIONS[0], time(16, 0), tzinfo=_CST))
            == 2.0
        )


# ---------------------------------------------------------------------------
# 内存:tracemalloc 峰值对比(200 标的 x 500 天,新 ≤ 参照 1/3)
# ---------------------------------------------------------------------------


def _build_inputs(day_count: int) -> tuple[list[datetime], list[date], list[float]]:
    """逐标的构造输入序列(真实形态:每 (天, 标的) 独立 Python 对象)。"""
    base = date(2020, 1, 1)
    return (
        [
            datetime.combine(base + timedelta(days=d), time(15, 30), tzinfo=_CST)
            for d in range(day_count)
        ],
        [base + timedelta(days=d) for d in range(day_count)],
        [float(d) + 0.5 for d in range(day_count)],
    )


def _reference_matrix(symbol_count: int, day_count: int) -> list[_TupleCloseHistory]:
    """元组表示构建(与 #439 前的矩阵构建同构,对象全部常驻)。"""
    out = []
    for _ in range(symbol_count):
        available, dates, closes = _build_inputs(day_count)
        out.append(
            _TupleCloseHistory(
                available_at=tuple(available), dates=tuple(dates), closes=tuple(closes)
            )
        )
    return out


def _numpy_matrix(symbol_count: int, day_count: int) -> list[SymbolCloseHistory]:
    """原生数组构建(转换后 Python 对象即时释放)。"""
    out = []
    for _ in range(symbol_count):
        available, dates, closes = _build_inputs(day_count)
        history = SymbolCloseHistory.from_sequences(
            available_at=available, dates=dates, closes=closes
        )
        assert history is not None
        out.append(history)
    return out


class TestMemoryBudget:
    def test_tracemalloc_peak_under_one_third_of_reference(self) -> None:
        symbol_count, day_count = 200, 500
        tracemalloc.start()
        reference = _reference_matrix(symbol_count, day_count)
        reference_peak, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del reference
        tracemalloc.start()
        numpy_built = _numpy_matrix(symbol_count, day_count)
        numpy_peak, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del numpy_built
        # 新实现峰值 ≤ 参照 1/3(参照的 datetime/date/装箱 float 全常驻)。
        assert numpy_peak * 3 <= reference_peak
