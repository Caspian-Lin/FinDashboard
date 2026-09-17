"""issue #454:next_open 执行价矩阵 opens 全链路等值与回退归零。

根因(r11 BJ-1A99444AC4B743AE 取证修正):**池路径并没有丢 opens**——
``_load_close_histories_via_pool`` 的 include_open 透传、worker 列式读取、
pickle 反序列化回传、主进程 ``SymbolCloseHistory`` 组装全链完整(本文件
池等值测试即锁定)。真正的浪费在 ``_load_execution_prices`` 的回退纪律:
矩阵已携带 opens 时 ``open_at`` 返回 None 只可能是「read_as_of 无可见
bar」(未上市 / 数据缺口),对象路径在同一 PIT 门控下必然同样为空
(#439 切片等值),旧实现却仍逐标的回退全文件读取——全市场发布下未上市
标的每期数百只 x 75 期 ≈ r11 job timing 的 4.65 万次 parquet 读 / 2.13GB
(按发布 list_date 分布复算 41,321 次标的x期,量级吻合)。

锁定六点:
* 池构建(真实 spawn,经 pickle 序列化回传)与进程内构建的
  ``SymbolCloseHistory`` 逐字段等值(含 opens);
* 矩阵 ``open_at`` / ``close_at`` 与对象路径逐标的逐时点等值;
* next_open 全流程(加载期 + 执行期)``_load_execution_prices_from_bars``
  零调用;未上市标的上市前期无执行价、上市后正常,全程零回退;
* 执行日停牌(无 bar)标的沿用最后可见 bar 的 open,零回退;
* next_close 行为零变化(零回退、取执行日 close);
* close-only 矩阵遇 open 请求仍走对象路径回退(防御分支保留)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import finboard_backtest.research_run.frozen_loader as frozen_loader_module
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    _load_close_histories,
    _load_close_histories_via_pool,
    _load_execution_prices_from_bars,
)
from finboard_backtest.research_run.signal_engine import build_decision_load_contexts
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
from tests.unit.research_run.test_issue_288_load_multiproc import (
    _build_release as _build_multiproc_release,
)
from tests.unit.research_run.test_issue_288_load_multiproc import (
    _month_end_decisions,
    _noop_snapshot_provider,
    _price_only_spec,
    _real_manifest,
)

_CST = ZoneInfo("Asia/Shanghai")
_RELEASE_ID = "exec-open-matrix-r1"
_SYMBOLS = ("600519.SH", "000001.SZ")
_LATE_SYMBOL = "601127.SH"  # SESSIONS[_LATE_LISTING_INDEX] 才上市(此前无 bar)
_ALL_SYMBOLS = (*_SYMBOLS, _LATE_SYMBOL)
_SESSION_COUNT = 12
_LATE_LISTING_INDEX = 6
_CKSUM = "c" * 64


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


def _close(code: str, day: date) -> Decimal:
    base = Decimal("50.00") if code.startswith("0") else Decimal("1200.00")
    return base * (Decimal("1") + Decimal(SESSIONS.index(day)) / Decimal("100"))


def _open(code: str, day: date) -> Decimal:
    """open 与 close 刻意不同(next_open 价基在样本上可辨)。"""
    return (_close(code, day) * Decimal("1.01")).quantize(Decimal("0.0001"))


def _bars(code: str, *, from_index: int = 0) -> list[Bar]:
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
        for day in SESSIONS[from_index:]
    ]


def _instrument(code: str, *, list_date: date | None = None) -> ReleaseInstrumentSpec:
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
        list_date=list_date or date(2001, 8, 27),
    )


async def _build_release(
    tmp_path: Path,
    *,
    skip_days: dict[str, set[date]] | None = None,
    with_late_listing: bool = False,
) -> FrozenReleaseProvider:
    """构建测试发布。

    * ``skip_days``:标的的对应交易日不写入(模拟停牌日,执行日无 bar);
    * ``with_late_listing``:附带 ``_LATE_SYMBOL``,其 bar 只从
      ``SESSIONS[_LATE_LISTING_INDEX]`` 起存在(list_date 同步)——模拟
      全市场发布里「决策期尚未上市」的标的(#454 回退读的主体)。
    """
    skip_days = skip_days or {}
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    specs: list[tuple[ReleaseInstrumentSpec, list[Bar]]] = []
    for code in _SYMBOLS:
        days = [day for day in SESSIONS if day not in skip_days.get(code, set())]
        specs.append(
            (
                _instrument(code),
                [bar for bar in _bars(code) if bar.timestamp.date() in days],
            )
        )
    if with_late_listing:
        listing = SESSIONS[_LATE_LISTING_INDEX]
        specs.append(
            (
                _instrument(_LATE_SYMBOL, list_date=listing),
                _bars(_LATE_SYMBOL, from_index=_LATE_LISTING_INDEX),
            )
        )
    cache = ParquetCache(cache_dir)
    for spec, bars in specs:
        await cache.write(
            Symbol(code=spec.code, market=spec.market),
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
            dataset_name="exec_open_matrix_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        [spec for spec, _ in specs],
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _manifest(timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="exec_open_matrix_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    spec = spec.model_copy(
        update={"execution_model": spec.execution_model.model_copy(update={"timing": timing})}
    )
    return ResearchRunManifest(
        run_id="RR-execopenmatrix001",
        idempotency_key="exec-open-matrix-0001",
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


def _candidates(symbols: tuple[str, ...] = _ALL_SYMBOLS) -> tuple[UniverseCandidate, ...]:
    return tuple(
        UniverseCandidate(
            symbol=code,
            included=True,
            reasons=("冻结发布标的且 ready=True",),
            asset_class="equity",
            market="a_share",
        )
        for code in symbols
    )


def _decision_at(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), tzinfo=_CST)


def _next_open_at(day: date) -> datetime:
    """决策日次一交易日的 09:30(next_open 成交时点)。"""
    return datetime.combine(SESSIONS[SESSIONS.index(day) + 1], time(9, 30), tzinfo=_CST)


def _end_of(day: date) -> datetime:
    return datetime.combine(day, time(23, 59), tzinfo=_CST)


def _install_fallback_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """把 ``_load_execution_prices_from_bars`` 换成计数包装,返回计数器。"""
    calls = {"fallback": 0}
    real = frozen_loader_module._load_execution_prices_from_bars

    async def counting(provider_arg, candidates_arg, read_as_of, *, want_open):
        calls["fallback"] += 1
        return await real(provider_arg, candidates_arg, read_as_of, want_open=want_open)

    monkeypatch.setattr(frozen_loader_module, "_load_execution_prices_from_bars", counting)
    return calls


@pytest.mark.asyncio
class TestPoolBuildCarriesOpens:
    """池构建(真实 spawn,经 pickle 序列化回传)与进程内构建逐字段等值。"""

    @pytest.mark.timeout(240)
    async def test_pool_build_equals_in_process_fieldwise(self, tmp_path: Path) -> None:
        from finboard_backtest.factor_lab import PriceFeatureProcessPool

        provider = await _build_release(tmp_path)
        candidates = _candidates(_SYMBOLS)
        in_process = await _load_close_histories(provider, candidates, include_open=True)
        assert in_process, "矩阵必须覆盖全部候选"

        pool = PriceFeatureProcessPool(provider=provider, worker_count=2)
        await pool.start()
        try:
            via_pool = await _load_close_histories_via_pool(
                pool, provider, candidates, include_open=True
            )
        finally:
            await pool.aclose()

        assert via_pool is not None
        assert set(via_pool) == set(in_process)
        for code, history in in_process.items():
            pooled = via_pool[code]
            assert history is not None, f"{code} 进程内矩阵缺失"
            assert pooled is not None, f"{code} 池回传矩阵缺失"
            # 经池 pickle 序列化回传后 opens 仍在(#454 头号嫌疑的反证)。
            assert history.opens is not None, f"{code} 进程内矩阵必须携带 opens"
            assert pooled.opens is not None, f"{code} 池回传矩阵必须携带 opens"
            np.testing.assert_array_equal(history.available_at_us, pooled.available_at_us)
            np.testing.assert_array_equal(history.date_days, pooled.date_days)
            np.testing.assert_array_equal(history.closes, pooled.closes)
            np.testing.assert_array_equal(history.opens, pooled.opens)
            assert history.available_tz_aware == pooled.available_tz_aware

    @pytest.mark.timeout(240)
    async def test_pool_build_without_open_request_stays_close_only(
        self, tmp_path: Path
    ) -> None:
        """include_open=False(非 next_open run)不携带 opens,行为零变化。"""
        from finboard_backtest.factor_lab import PriceFeatureProcessPool

        provider = await _build_release(tmp_path)
        candidates = _candidates(_SYMBOLS)
        pool = PriceFeatureProcessPool(provider=provider, worker_count=2)
        await pool.start()
        try:
            via_pool = await _load_close_histories_via_pool(
                pool, provider, candidates, include_open=False
            )
        finally:
            await pool.aclose()

        assert via_pool is not None
        assert all(item is not None and item.opens is None for item in via_pool.values())


@pytest.mark.asyncio
class TestMatrixMatchesObjectPath:
    """矩阵 open_at / close_at 与对象路径逐标的逐时点等值(#439 同族)。"""

    async def test_open_and_close_match_object_path_at_every_session(
        self, tmp_path: Path
    ) -> None:
        provider = await _build_release(tmp_path, with_late_listing=True)
        candidates = _candidates()
        histories = await _load_close_histories(provider, candidates, include_open=True)
        assert all(item is not None for item in histories.values())

        for day in SESSIONS:
            read_as_of = _end_of(day)
            open_prices = await _load_execution_prices_from_bars(
                provider, candidates, read_as_of, want_open=True
            )
            close_prices = await _load_execution_prices_from_bars(
                provider, candidates, read_as_of, want_open=False
            )
            for candidate in candidates:
                history = histories[candidate.symbol]
                assert history is not None
                open_at = history.open_at(read_as_of)
                if candidate.symbol in open_prices:
                    assert open_at == pytest.approx(open_prices[candidate.symbol])
                else:
                    # 未上市 / 无可见 bar:两条路径同为 None(缺席)。
                    assert open_at is None
                close_at = history.close_at(read_as_of)
                if candidate.symbol in close_prices:
                    assert close_at == pytest.approx(close_prices[candidate.symbol])
                else:
                    assert close_at is None


@pytest.mark.asyncio
class TestNextOpenZeroFallback:
    """next_open 全流程 ``_load_execution_prices_from_bars`` 零调用。"""

    async def test_load_context_direct_next_open_builds_matrix_with_opens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """直接 load_context(未经 ensure_close_histories)时,首次构建即带 opens。

        回归 #454 次要缺陷:close 查询先触发构建会把 next_open run 的矩阵
        建成 close-only,执行价只能逐期逐标的回退全历史读取。
        """
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_OPEN)
        calls = _install_fallback_counter(monkeypatch)

        context = await loader.load_context(
            manifest,
            decision_at=_decision_at(SESSIONS[2]),
            execution_at=_next_open_at(SESSIONS[2]),
        )

        assert calls["fallback"] == 0
        assert set(context.execution_prices) == set(_SYMBOLS)
        for code in _SYMBOLS:
            assert context.execution_prices[code] == pytest.approx(
                float(_open(code, SESSIONS[3]))
            )
        histories = loader.close_histories
        assert all(item is not None and item.opens is not None for item in histories.values())

    async def test_not_yet_listed_symbol_absent_without_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未上市标的:上市前期无执行价、零回退读取(r11 浪费的主体)。"""
        provider = await _build_release(tmp_path, with_late_listing=True)
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_OPEN)
        calls = _install_fallback_counter(monkeypatch)

        # 决策期在 _LATE_SYMBOL 上市之前:其执行价缺失(矩阵权威 None),
        # 但不再为它付出全文件回退读。
        context = await loader.load_context(
            manifest,
            decision_at=_decision_at(SESSIONS[2]),
            execution_at=_next_open_at(SESSIONS[2]),
        )
        assert _LATE_SYMBOL not in context.execution_prices
        assert calls["fallback"] == 0
        for code in _SYMBOLS:
            assert context.execution_prices[code] == pytest.approx(
                float(_open(code, SESSIONS[3]))
            )

        # 上市之后的期:执行价正常命中上市后执行日的 open,仍零回退。
        context_listed = await loader.load_context(
            manifest,
            decision_at=_decision_at(SESSIONS[_LATE_LISTING_INDEX]),
            execution_at=_next_open_at(SESSIONS[_LATE_LISTING_INDEX]),
        )
        assert context_listed.execution_prices[_LATE_SYMBOL] == pytest.approx(
            float(_open(_LATE_SYMBOL, SESSIONS[_LATE_LISTING_INDEX + 1]))
        )
        assert calls["fallback"] == 0

    async def test_suspended_execution_day_uses_last_visible_open_without_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """执行日无 bar(停牌):沿用最后可见 bar 的 open,零回退。"""
        gap_day = SESSIONS[4]
        provider = await _build_release(tmp_path, skip_days={_SYMBOLS[0]: {gap_day}})
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_OPEN)
        calls = _install_fallback_counter(monkeypatch)

        context = await loader.load_context(
            manifest,
            decision_at=_decision_at(SESSIONS[3]),
            execution_at=_next_open_at(SESSIONS[3]),
        )

        assert calls["fallback"] == 0
        # 执行日 SESSIONS[4] 停牌 → 最后可见 bar(SESSIONS[3])的 open。
        assert context.execution_prices[_SYMBOLS[0]] == pytest.approx(
            float(_open(_SYMBOLS[0], SESSIONS[3]))
        )
        assert context.execution_prices[_SYMBOLS[1]] == pytest.approx(
            float(_open(_SYMBOLS[1], SESSIONS[4]))
        )

    async def test_next_close_zero_fallback_and_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """next_close run 行为零变化:取执行日 close,零回退。"""
        provider = await _build_release(tmp_path, with_late_listing=True)
        loader = _loader(provider)
        manifest = _manifest(ExecutionTiming.NEXT_CLOSE)
        calls = _install_fallback_counter(monkeypatch)

        context = await loader.load_context(
            manifest,
            decision_at=_decision_at(SESSIONS[2]),
            execution_at=datetime.combine(SESSIONS[3], time(15, 0), tzinfo=_CST),
        )

        assert calls["fallback"] == 0
        assert set(context.execution_prices) == set(_SYMBOLS)
        for code in _SYMBOLS:
            assert context.execution_prices[code] == pytest.approx(
                float(_close(code, SESSIONS[3]))
            )

    @pytest.mark.timeout(240)
    async def test_build_decision_load_contexts_zero_fallback_full_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """多期全流程(真实 spawn 池):加载期 + 执行期全程零回退调用。"""
        provider, _ = await _build_multiproc_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        calls = _install_fallback_counter(monkeypatch)

        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=lambda _release_id: provider,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=2,
        )

        assert len(contexts) == len(_month_end_decisions())
        assert calls["fallback"] == 0
        # 每期执行价均由矩阵产出(next_open)且非空。
        for decision in contexts:
            assert decision.context.execution_prices


@pytest.mark.asyncio
class TestCloseOnlyMatrixStillFallsBack:
    """close-only 矩阵遇 open 请求仍走对象路径回退(防御分支保留)。"""

    async def test_close_only_matrix_open_request_falls_back_to_bars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        # 先按 next_close 语义预建矩阵(close-only)。
        await loader.ensure_close_histories(_manifest(ExecutionTiming.NEXT_CLOSE))
        assert all(
            item is not None and item.opens is None for item in loader.close_histories.values()
        )
        calls = _install_fallback_counter(monkeypatch)

        prices = await loader._load_execution_prices(
            provider,
            _candidates(_SYMBOLS),
            execution_at=_next_open_at(SESSIONS[2]),
            want_open=True,
        )

        # close-only 矩阵无法回答 open → 逐标的回退对象路径(计数 > 0)。
        assert calls["fallback"] == 1
        assert set(prices) == set(_SYMBOLS)
        for code in _SYMBOLS:
            assert prices[code] == pytest.approx(float(_open(code, SESSIONS[3])))
