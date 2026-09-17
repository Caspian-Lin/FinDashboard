"""执行价基与 timing 声明一致(issue #336)。

修复前:执行价按 ``execution_at``(T+1 15:00)做 PIT 门控读 close,而 D1
bar 的 available_at = 15:30 —— 执行日 bar 不可见,成交价退化为**决策日
收盘**,``filled_at`` 却盖着 T+1 15:00,与规格 ``timing=next_open`` 不符。

锁定四点:
* next_open:执行价 = 执行日 bar 的 **open**(open != close 的样本上可辨),
  时间戳 = 执行日 09:30;
* next_close:执行价 = 执行日 bar 的 close(日终门控可见),时间戳 = 15:00;
* 执行日无 bar(停牌)回退最后可见 bar 的 open;
* close-only 矩阵(未携带 opens)回退对象路径读取,结果与矩阵路径一致。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    SymbolCloseHistory,
    _load_close_histories,
)
from finboard_backtest.research_run.signal_engine import _next_execution_at
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import ExecutionTiming
from finboard_data.cache import ParquetCache
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
_RELEASE_ID = "exec-timing-r1"
_SYMBOLS = ("600519.SH", "000001.SZ")
_SESSION_COUNT = 8


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
_CKSUM = "b" * 64


def _close(code: str, day: date) -> Decimal:
    base = Decimal("50.00") if code.startswith("0") else Decimal("1200.00")
    return base * (Decimal("1") + Decimal(SESSIONS.index(day)) / Decimal("100"))


def _open(code: str, day: date) -> Decimal:
    """open 与 close 刻意不同(next_open 价基在样本上可辨)。"""
    return (_close(code, day) * Decimal("1.01")).quantize(Decimal("0.0001"))


def _bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=_open(code, day),
            high=_close(code, day) * Decimal("1.03"),
            low=_close(code, day) * Decimal("0.97"),
            close=_close(code, day),
            volume=Decimal(10000),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in SESSIONS
    ]


def _instrument(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
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


async def _build_release(
    tmp_path: Path, *, skip_days: dict[str, set[date]] | None = None
) -> FrozenReleaseProvider:
    """构建测试发布;``skip_days`` 中标的的对应交易日不写入(模拟停牌)。"""
    skip_days = skip_days or {}
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _SYMBOLS]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        days = [day for day in SESSIONS if day not in skip_days.get(instrument.code, set())]
        bars = [bar for bar in _bars(instrument.code) if bar.timestamp.date() in days]
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            bars,
        )
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="exec_timing_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _manifest(timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="exec_timing_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    spec = spec.model_copy(
        update={"execution_model": spec.execution_model.model_copy(update={"timing": timing})}
    )
    return ResearchRunManifest(
        run_id="RR-exectimingtest001",
        idempotency_key="exec-timing-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum=_CKSUM,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _loader(provider: FrozenReleaseProvider) -> FrozenInputLoader:
    async def _noop(snapshot_id: str) -> None:
        del snapshot_id

    return FrozenInputLoader(
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop,
    )


@pytest.mark.asyncio
class TestExecutionPriceBasis:
    async def test_next_open_uses_execution_day_open(self, tmp_path: Path) -> None:
        """next_open:执行价 = 执行日 bar 的 open(而非任何 close)。"""
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_OPEN)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = datetime.combine(SESSIONS[3], time(9, 30), tzinfo=_CST)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        for code in _SYMBOLS:
            expected = float(_open(code, SESSIONS[3]))
            actual = context.execution_prices[code]
            assert actual == pytest.approx(expected)
            # open 与 close 刻意不同:证明没有退化为任何一天的 close。
            assert actual != pytest.approx(float(_close(code, SESSIONS[3])))
            assert actual != pytest.approx(float(_close(code, SESSIONS[2])))

    async def test_next_close_uses_execution_day_close(self, tmp_path: Path) -> None:
        """next_close:执行价 = 执行日 bar 的 close(日终门控可见)。"""
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_CLOSE)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = datetime.combine(SESSIONS[3], time(15, 0), tzinfo=_CST)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        for code in _SYMBOLS:
            assert context.execution_prices[code] == pytest.approx(float(_close(code, SESSIONS[3])))

    async def test_suspended_execution_day_falls_back_to_last_open(self, tmp_path: Path) -> None:
        """执行日无 bar(停牌):回退最后可见 bar 的 open。"""
        provider = await _build_release(tmp_path, skip_days={_SYMBOLS[0]: {SESSIONS[3]}})
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_OPEN)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = datetime.combine(SESSIONS[3], time(9, 30), tzinfo=_CST)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        assert context.execution_prices[_SYMBOLS[0]] == pytest.approx(
            float(_open(_SYMBOLS[0], SESSIONS[2]))
        )
        assert context.execution_prices[_SYMBOLS[1]] == pytest.approx(
            float(_open(_SYMBOLS[1], SESSIONS[3]))
        )

    async def test_close_only_matrix_falls_back_to_bars(self, tmp_path: Path) -> None:
        """close-only 矩阵(未携带 opens):open 查询回退对象路径,取值一致。"""
        provider = await _build_release(tmp_path)
        histories = await _load_close_histories(
            provider,
            tuple(
                type("C", (), {"symbol": code, "market": "a_share", "included": True})()
                for code in _SYMBOLS
            ),
            include_open=False,
        )
        assert all(item is not None and item.opens is None for item in histories.values())
        read_as_of = datetime.combine(SESSIONS[3], time(23, 59), tzinfo=_CST)
        assert histories[_SYMBOLS[0]].open_at(read_as_of) is None  # type: ignore[union-attr]
        # 真实矩阵携带 opens 后,同一查询命中执行日 open。
        histories_open = await _load_close_histories(
            provider,
            tuple(
                type("C", (), {"symbol": code, "market": "a_share", "included": True})()
                for code in _SYMBOLS
            ),
            include_open=True,
        )
        history = histories_open[_SYMBOLS[0]]
        assert isinstance(history, SymbolCloseHistory)
        assert history.open_at(read_as_of) == pytest.approx(float(_open(_SYMBOLS[0], SESSIONS[3])))


@pytest.mark.asyncio
class TestExecutionStamp:
    async def test_next_open_stamp_is_0930(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = await _next_execution_at(
            provider,
            decision_at,
            timing=ExecutionTiming.NEXT_OPEN,
        )
        assert execution_at.date() == SESSIONS[3]
        assert execution_at.time() == time(9, 30)
        assert execution_at.tzinfo is not None

    async def test_next_close_stamp_is_1500(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = await _next_execution_at(
            provider,
            decision_at,
            timing=ExecutionTiming.NEXT_CLOSE,
        )
        assert execution_at.date() == SESSIONS[3]
        assert execution_at.time() == time(15, 0)

    async def test_default_stamp_keeps_1500(self, tmp_path: Path) -> None:
        """timing 缺省保持 15:00(兼容既有调用方)。"""
        provider = await _build_release(tmp_path)
        decision_at = datetime.combine(SESSIONS[2], time(15, 0), tzinfo=_CST)
        execution_at = await _next_execution_at(provider, decision_at)
        assert execution_at.time() == time(15, 0)
