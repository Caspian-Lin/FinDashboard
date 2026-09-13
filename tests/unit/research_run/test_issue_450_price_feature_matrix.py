"""issue #450 追续:逐期价格特征从 close 矩阵切片直接计算。

逐期 ``build_price_feature_snapshot`` 在进程池对每标的整文件重读 close
历史(全市场 x 多期 = 数十万次重复读盘),是矩阵化之后加载期的主导成本。
矩阵路径(:func:`build_price_feature_snapshot_from_close_matrix`)直接切片
内存 close 矩阵——特征数学只依赖序列尾部连续元素,本文件锁定:矩阵路径
与 provider 列式路径的快照观测逐值等值、矩阵缺标的回退、尾切片等值、
available_at 重建精确性。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from finboard_backtest.factor_lab import (
    build_price_feature_snapshot,
    price_observations_from_closes,
)
from finboard_backtest.research_run.contracts import UniverseCandidate
from finboard_backtest.research_run.frozen_loader import (
    SymbolCloseHistory,
    _load_close_histories,
    build_price_feature_snapshot_from_close_matrix,
)
from finboard_data.factor_lab import FeatureObservation
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_RELEASE_ID = "pf-matrix-eq-r1"
_CODES = ("600001.SH", "000002.SZ", "300750.SZ")
_START = date(2024, 1, 2)
_END = date(2024, 8, 30)


def _trade_days() -> list[date]:
    result: list[date] = []
    current = _START
    while current <= _END:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _closes(code_index: int, count: int) -> list[Decimal]:
    """确定性伪随机 close(逐标的不同,保证特征值非平凡)。"""
    return [Decimal("10") + Decimal((code_index * 7 + j * 13) % 31) * Decimal("0.1") for j in range(count)]


def _stock(code: str):
    from finboard_data.releases import (
        ReleaseInstrumentSpec,
        default_execution_metadata,
    )

    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2023, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2020, 1, 1),
    )


async def _publish_bars_release(tmp_path: Path) -> FrozenReleaseProvider:
    """真实发布构建器冻结一份多标的 bars 发布(夹具同款)。"""
    from finboard_data.cache import ParquetCache

    days = _trade_days()
    cache = ParquetCache(tmp_path / "cache")
    for i, code in enumerate(_CODES):
        symbol = Symbol(code, Market.A_SHARE)
        closes = _closes(i, len(days))
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("10000"),
                amount=Decimal("100000"),
                source="fixed_sample",
            )
            for day, close in zip(days, closes, strict=True)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    release_root = tmp_path / "releases"
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=release_root,
    )
    spec = DatasetReleaseSpec(
        release_id=_RELEASE_ID,
        dataset_name="pf_matrix_equivalence",
        source="fixed_sample",
        version="v1",
        start_date=_START,
        end_date=_END,
        code_version="test",
        required_capabilities=("stock",),
    )
    release = await builder.publish(spec, [_stock(code) for code in _CODES])
    assert release.is_usable
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _candidates(provider: FrozenReleaseProvider) -> list[UniverseCandidate]:
    return [
        UniverseCandidate(
            symbol=item.code,
            included=True,
            reasons=("x",),
            asset_class="stock",
            market=item.market.value,
        )
        for item in provider.release.instruments
    ]


def _observation_map(
    observations: Sequence[FeatureObservation],
) -> dict[tuple[str, str], tuple[float, datetime, datetime]]:
    return {
        (item.symbol, item.feature_name): (
            item.value,
            item.available_at,
            item.observed_at,
        )
        for item in observations
    }


@pytest.mark.asyncio
async def test_matrix_path_matches_provider_path(tmp_path: Path) -> None:
    provider = await _publish_bars_release(tmp_path)
    histories = await _load_close_histories(provider, _candidates(provider))
    assert all(item is not None for item in histories.values())
    decision_at = datetime(2024, 5, 15, 23, 0, tzinfo=UTC)

    reference = await build_price_feature_snapshot(
        provider=provider,
        decision_at=decision_at,
        code_version="c0",
        symbols=list(_CODES),
    )
    from_matrix = await build_price_feature_snapshot_from_close_matrix(
        histories=histories,
        provider=provider,
        decision_at=decision_at,
        code_version="c0",
        symbols=list(_CODES),
    )
    assert _observation_map(from_matrix.observations) == _observation_map(
        reference.observations
    )
    # checksum 含 published_at=now(),两次构建本就不同——不比 checksum,
    # 观测逐值等值才是消费契约(_period_feature_values 只读 observations)。


@pytest.mark.asyncio
async def test_matrix_missing_symbol_falls_back_to_provider(tmp_path: Path) -> None:
    provider = await _publish_bars_release(tmp_path)
    histories = await _load_close_histories(provider, _candidates(provider))
    partial = dict(histories)
    partial.pop(_CODES[0])  # 模拟矩阵未覆盖:该标的必须走 provider 回退
    decision_at = datetime(2024, 5, 15, 23, 0, tzinfo=UTC)

    reference = await build_price_feature_snapshot(
        provider=provider,
        decision_at=decision_at,
        code_version="c0",
        symbols=list(_CODES),
    )
    from_matrix = await build_price_feature_snapshot_from_close_matrix(
        histories=partial,
        provider=provider,
        decision_at=decision_at,
        code_version="c0",
        symbols=list(_CODES),
    )
    assert _observation_map(from_matrix.observations) == _observation_map(
        reference.observations
    )


def test_tail_slice_equals_full_series_math() -> None:
    """特征数学只依赖尾部连续元素:尾切片与全序列计算逐位等值。"""
    rng = np.random.default_rng(7)
    closes = 10.0 + np.cumsum(rng.standard_normal(500) * 0.1)

    def _obs(closes: np.ndarray) -> list[FeatureObservation]:
        return price_observations_from_closes(
            source="fixed_sample",
            source_version="v1",
            symbol="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            closes=closes,
            last_timestamp=datetime(2024, 5, 15, tzinfo=UTC),
            last_available_at=datetime(2024, 5, 15, 7, 30, tzinfo=UTC),
            momentum_lookback=20,
            volatility_windows=(20, 60, 120),
        )

    assert _observation_map(_obs(closes)) == _observation_map(_obs(closes[-130:]))


def test_available_at_at_roundtrip_exact() -> None:
    """epoch 微秒 + 保留时区的重建与原值逐值相等(含微秒与 +08:00)。"""
    aware = [
        datetime(2024, 5, 13, 15, 30, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
        datetime(2024, 5, 14, 15, 30, 0, 999999, tzinfo=timezone(timedelta(hours=8))),
    ]
    history = SymbolCloseHistory.from_sequences(
        available_at=aware,
        dates=[item.date() for item in aware],
        closes=[1.0, 2.0],
    )
    assert history is not None
    assert history.available_at_at(0) == aware[0]
    assert history.available_at_at(1) == aware[1]
    naive = SymbolCloseHistory.from_sequences(
        available_at=[datetime(2024, 5, 13, 15, 30)],
        dates=[date(2024, 5, 13)],
        closes=[1.0],
    )
    assert naive is not None
    assert naive.available_at_at(0) == datetime(2024, 5, 13, 15, 30)
    assert naive.available_at_at(0).tzinfo is None


@pytest.mark.asyncio
async def test_precompute_cancel_probe_aborts_matrix_build(tmp_path: Path) -> None:
    """预计算打点器上的取消探针:命中即中止预建(#450 追续取消空白区)。

    此前预建段只报进度不查取消,cancel_requested 要等首个分块边界才被
    看见——全市场预建 3-5 分钟,取消延迟为分钟级。
    """
    from finboard_backtest.research_run.contracts import ResearchRunInterruptedError
    from finboard_backtest.research_run.frozen_loader import (
        _load_close_histories,
        _make_precompute_ticker,
    )

    calls: list[int] = []
    reports: list[str] = []

    async def probe() -> None:
        calls.append(1)
        raise ResearchRunInterruptedError("cancel_requested")

    async def reporter(message: str) -> None:
        reports.append(message)

    tick = _make_precompute_ticker(reporter, "close", 4, probe)
    with pytest.raises(ResearchRunInterruptedError):
        await tick()
    assert reports == []  # 取消异常先于进度上报,进度帧不再写出

    # 端到端:真实发布 + raise 探针 → _load_close_histories 中止且不产出矩阵
    provider = await _publish_bars_release(tmp_path)

    async def probe2() -> None:
        raise ResearchRunInterruptedError("cancel_requested")

    with pytest.raises(ResearchRunInterruptedError):
        await _load_close_histories(provider, _candidates(provider), cancel_probe=probe2)

    # 无探针时行为不变:矩阵正常构建
    histories = await _load_close_histories(provider, _candidates(provider))
    assert histories
    assert all(item is not None for item in histories.values())


@pytest.mark.asyncio
async def test_price_precompute_matches_snapshot_path(tmp_path: Path) -> None:
    """run 级价格特征预计算与逐期快照路径逐值等值(#450 追续)。"""
    from finboard_backtest.research_run.frozen_loader import (
        build_price_feature_precompute,
    )

    provider = await _publish_bars_release(tmp_path)
    histories = await _load_close_histories(provider, _candidates(provider))
    days = [
        datetime(2024, 4, 15, 23, 0, tzinfo=UTC),
        datetime(2024, 6, 15, 23, 0, tzinfo=UTC),
        datetime(2024, 8, 20, 23, 0, tzinfo=UTC),
    ]
    pre = await build_price_feature_precompute(
        histories=histories,
        provider=provider,
        decision_ats=days,
        symbols=list(_CODES),
    )
    assert set(pre.symbols) == set(_CODES)  # 顺序 = 发布 instruments 序(与快照路径一致)
    for day in days:
        values = pre.feature_values(day, "rel-x")
        assert values is not None
        assert values
        reference = await build_price_feature_snapshot(
            provider=provider,
            decision_at=day,
            code_version="c0",
            symbols=list(_CODES),
        )
        ref_map = {
            (item.symbol, item.feature_name): (item.value, item.available_at)
            for item in reference.observations
        }
        val_map = {
            (item.symbol, item.feature_id): (item.value, item.available_at)
            for item in values
        }
        assert val_map == ref_map, day


# --------------------------------------------------------------------- #
# issue #464:价格特征预计算进程池分发 + 进度帧
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_price_precompute_pool_path_value_equal(tmp_path: Path) -> None:
    """池分发路径与进程内路径逐值等值(#464)。"""
    from finboard_backtest.factor_lab import PriceFeatureProcessPool
    from finboard_backtest.research_run.frozen_loader import (
        build_price_feature_precompute,
    )

    provider = await _publish_bars_release(tmp_path)
    histories = await _load_close_histories(provider, _candidates(provider))
    days = [
        datetime(2024, 4, 15, 23, 0, tzinfo=UTC),
        datetime(2024, 6, 15, 23, 0, tzinfo=UTC),
        datetime(2024, 8, 20, 23, 0, tzinfo=UTC),
    ]
    baseline = await build_price_feature_precompute(
        histories=histories,
        provider=provider,
        decision_ats=days,
        symbols=list(_CODES),
    )
    pool = PriceFeatureProcessPool(provider=provider, worker_count=2)
    await pool.start()
    try:
        pooled = await build_price_feature_precompute(
            histories=histories,
            provider=provider,
            decision_ats=days,
            symbols=list(_CODES),
            process_pool=pool,
        )
    finally:
        await pool.aclose()
    assert pooled.symbols == baseline.symbols
    assert pooled.by_symbol.keys() == baseline.by_symbol.keys()
    for code in baseline.symbols:
        for per_pooled, per_base in zip(
            pooled.by_symbol[code], baseline.by_symbol[code], strict=True
        ):
            assert per_pooled == per_base


@pytest.mark.asyncio
async def test_price_precompute_progress_frames(tmp_path: Path) -> None:
    """进度帧接通:首帧 1/n、末帧 n/n(#464;此前该段零帧冻住 phase)。"""
    from finboard_backtest.research_run.frozen_loader import (
        build_price_feature_precompute,
    )

    provider = await _publish_bars_release(tmp_path)
    histories = await _load_close_histories(provider, _candidates(provider))
    days = [datetime(2024, 6, 15, 23, 0, tzinfo=UTC)]
    reports: list[str] = []

    async def reporter(message: str) -> None:
        reports.append(message)

    await build_price_feature_precompute(
        histories=histories,
        provider=provider,
        decision_ats=days,
        symbols=list(_CODES),
        progress=reporter,
    )
    assert reports[0] == "research_run:decision_load precompute price 1/3"
    assert reports[-1] == "research_run:decision_load precompute price 3/3"


def test_price_precompute_process_task_matches_history_fn() -> None:
    """进程池任务函数与进程内逐标的应用同一计算(逐值一致)。"""
    import numpy as np

    from finboard_backtest.research_run.frozen_loader import (
        _period_features_for_symbol,
        _price_precompute_process_task,
        _PricePrecomputeTask,
    )

    rng = np.arange(120, dtype=np.float64)
    history = SymbolCloseHistory.from_sequences(
        available_at=[
            datetime(2024, 1, 2, 15, 30, tzinfo=UTC) + timedelta(days=i)
            for i in range(120)
        ],
        dates=[date(2024, 1, 2) + timedelta(days=i) for i in range(120)],
        closes=[float(v) for v in rng],
    )
    assert history is not None
    epochs = np.array([1_710_000_000_000_000 + k * 86_400_000_000 for k in range(4)], dtype=np.int64)
    ordinals = np.array(
        [(date(2024, 3, 10) + timedelta(days=k)).toordinal() - date(1970, 1, 1).toordinal()
         for k in range(4)],
        dtype=np.int64,
    )
    task = _PricePrecomputeTask(
        code="600001.SH",
        history=history,
        decision_epochs=epochs,
        decision_ordinals=ordinals,
        lookback=20,
        windows=(20, 60),
        tail_n=61,
    )
    code, per = _price_precompute_process_task(task)
    assert code == "600001.SH"
    expected = _period_features_for_symbol(
        history,
        epochs,
        ordinals,
        lookback=20,
        windows=(20, 60),
        tail_n=61,
    )
    assert per == expected
