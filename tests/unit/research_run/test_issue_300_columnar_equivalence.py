"""列式 close 直出的消费端等值(issue #300):provider / close 矩阵 / 价格特征。

#300 P0 后三条热路径全部改为列式数据(feat 层直出 float64,不再逐行构造
``Decimal`` / ``Bar`` / ``PointInTimeBar`` 对象):

* ``FrozenReleaseProvider.fetch_close_history`` ↔ ``fetch_point_in_time_prices``
  (对象路径参照,含 PIT 门控边界);
* ``FrozenInputLoader`` close 矩阵 ↔ ``_history_from_points`` 对象路径参照
  (available_at / dates / closes 三条平行序列逐值相等);
* ``_build_price_observations`` 列式输入 ↔ 对象路径逐行 float 转换的旧算法
  (FeatureObservation 逐字段相等,含 observed_at / available_at)。

发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建(与 #287/#299
同一夹具风格),不依赖 PostgreSQL。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from finboard_backtest.factor_lab import _build_price_observations
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    SymbolCloseHistory,
    _history_from_points,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data.cache import ParquetCache
from finboard_data.factor_lab import FeatureObservation
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    _timestamp_available_at,
    d1_available_at,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_CST = ZoneInfo("Asia/Shanghai")
_RELEASE_ID = "columnar-equ-r1"
_CODES = ("600519.SH", "000001.SZ", "600036.SH")
_SESSION_COUNT = 30
_DECISION_TIME = time(15, 0, tzinfo=_CST)


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
    base = {"600519.SH": "1500.00", "000001.SZ": "10.00", "600036.SH": "30.00"}[code]
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


async def _build_release(tmp_path: Path) -> FrozenReleaseProvider:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _CODES]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            _bars(instrument.code),
        )
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="columnar_equivalence_daily_bars",
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


def _symbol(code: str) -> Symbol:
    return Symbol(code=code, market=Market.A_SHARE)


def _decision_days() -> list[tuple[datetime, datetime]]:
    """(decision_at, execution_at) 对:决策日 15:00 CST、成交日次日 16:00。"""
    pairs: list[tuple[datetime, datetime]] = []
    for index in range(0, len(SESSIONS) - 1):
        decision_at = datetime.combine(SESSIONS[index], _DECISION_TIME)
        execution_at = datetime.combine(
            SESSIONS[index + 1], time(16, 0, tzinfo=_CST)
        )
        pairs.append((decision_at, execution_at))
    return pairs


def _manifest() -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="columnar_equ_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    return ResearchRunManifest(
        run_id="RR-columnarequtest01",
        idempotency_key="columnar-equ-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _loader(provider: FrozenReleaseProvider) -> FrozenInputLoader:
    def factory(release_id: str) -> FrozenReleaseProvider:
        assert release_id == _RELEASE_ID
        return provider

    async def snapshot_provider(snapshot_id: str) -> None:
        del snapshot_id

    return FrozenInputLoader(
        release_provider_factory=factory,
        snapshot_provider=snapshot_provider,
    )


# ---- provider 层:fetch_close_history ↔ fetch_point_in_time_prices ----------


@pytest.mark.asyncio
class TestFetchCloseHistoryEquivalence:
    """AC:列式 PIT close 与对象路径逐值相等(含 PIT 边界)。"""

    async def test_full_range_and_pit_boundaries(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        code = _CODES[0]
        # 全区间、中途、首日之前(空)、末日:四个门控点逐一对照。
        decision_points = [
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            datetime.combine(SESSIONS[5], time(15, 30, tzinfo=_CST)),
            datetime.combine(SESSIONS[0], time(15, 0, tzinfo=_CST)) - timedelta(microseconds=1),
            datetime.combine(SESSIONS[-1], time(15, 30, tzinfo=_CST)),
        ]
        for decision_at in decision_points:
            columns = await provider.fetch_close_history(
                _symbol(code),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                decision_at=decision_at,
                adjust=provider.release.adjustment,
            )
            points = await provider.fetch_point_in_time_prices(
                _symbol(code),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                decision_at=decision_at,
                adjust=provider.release.adjustment,
            )
            assert columns.dates == tuple(item.timestamp.date() for item in points)
            assert columns.available_at == tuple(item.available_at for item in points)
            assert columns.closes.tolist() == [float(item.close) for item in points]
            if points:
                assert columns.last_timestamp == points[-1].timestamp
                assert columns.available_at[-1] == points[-1].available_at
            else:
                assert columns.last_timestamp is None
                assert columns.closes.size == 0

    async def test_all_symbols_all_periods(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        for decision_at, _ in _decision_days():
            for code in _CODES:
                columns = await provider.fetch_close_history(
                    _symbol(code),
                    provider.release.period,
                    provider.release.start_date,
                    decision_at.date(),
                    decision_at=decision_at,
                    adjust=provider.release.adjustment,
                )
                points = await provider.fetch_point_in_time_prices(
                    _symbol(code),
                    provider.release.period,
                    provider.release.start_date,
                    decision_at.date(),
                    decision_at=decision_at,
                    adjust=provider.release.adjustment,
                )
                assert columns.dates == tuple(item.timestamp.date() for item in points)
                assert columns.closes.tolist() == [float(item.close) for item in points]


# ---- close 矩阵:列式构建 ↔ 对象路径参照 -------------------------------------


@pytest.mark.asyncio
class TestCloseMatrixColumnarEquivalence:
    """AC:列式矩阵与 _history_from_points 对象路径逐值相等。"""

    async def test_matrix_matches_object_reference(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest()
        await loader.ensure_close_histories(manifest)
        assert set(loader.close_histories) == set(_CODES)
        for code in _CODES:
            points = await FrozenReleaseProvider.fetch_point_in_time_bars(
                provider,
                _symbol(code),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                decision_at=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
                adjust=provider.release.adjustment,
            )
            reference = _history_from_points(points)
            assert reference is not None
            built = loader.close_histories[code]
            assert isinstance(built, SymbolCloseHistory)
            assert built.available_at == reference.available_at
            assert built.dates == reference.dates
            assert built.closes == reference.closes
            # np.float64 与 float 的边界:切片取值与参照逐值相等且为 float 子类。
            assert built.close_at(
                datetime.combine(SESSIONS[5], time(15, 0, tzinfo=_CST))
            ) == reference.close_at(
                datetime.combine(SESSIONS[5], time(15, 0, tzinfo=_CST))
            )


# ---- 价格特征观测:列式输入 ↔ 对象路径旧算法 ---------------------------------


async def _observations_via_object_path(
    provider: FrozenReleaseProvider,
    code: str,
    decision_at: datetime,
    *,
    momentum_lookback: int,
    volatility_windows: tuple[int, ...],
) -> list[FeatureObservation]:
    """旧对象路径的观测构建(fetch_point_in_time_prices + 逐行 float 转换)。"""
    points = await provider.fetch_point_in_time_prices(
        _symbol(code),
        provider.release.period,
        provider.release.start_date,
        min(decision_at.date(), provider.release.end_date),
        decision_at=decision_at,
        adjust=provider.release.adjustment,
    )
    if not points:
        return []
    closes = np.asarray([float(item.close) for item in points], dtype=np.float64)
    returns = np.diff(closes) / closes[:-1]
    last = points[-1]

    def _observation(feature_name: str, value: float) -> FeatureObservation:
        return FeatureObservation(
            symbol=code,
            feature_name=feature_name,
            value=value,
            observed_at=last.timestamp,
            available_at=last.available_at,
            source=provider.release.source,
            source_version=provider.release.version,
            market=Market.A_SHARE.value,
            asset_class=AssetClass.EQUITY.value,
        )

    observations: list[FeatureObservation] = []
    if len(closes) >= momentum_lookback + 1:
        observations.append(
            _observation(
                "momentum",
                float(closes[-1] / closes[-momentum_lookback - 1] - 1.0),
            )
        )
    for window in volatility_windows:
        if len(returns) >= window:
            observations.append(
                _observation(
                    f"volatility_{window}d",
                    float(np.std(returns[-window:], ddof=1)),
                )
            )
    return observations


@pytest.mark.asyncio
class TestPriceObservationsColumnarEquivalence:
    """AC:列式输入的观测与对象路径旧算法逐字段相等。"""

    @pytest.mark.parametrize("code", _CODES)
    async def test_observations_equal_object_path(
        self, tmp_path: Path, code: str
    ) -> None:
        provider = await _build_release(tmp_path)
        decision_at = datetime.combine(SESSIONS[-1], time(15, 0, tzinfo=_CST))
        columns = await provider.fetch_close_history(
            _symbol(code),
            provider.release.period,
            provider.release.start_date,
            min(decision_at.date(), provider.release.end_date),
            decision_at=decision_at,
            adjust=provider.release.adjustment,
        )
        columnar = _build_price_observations(
            source=provider.release.source,
            source_version=provider.release.version,
            symbol=code,
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            columns=columns,
            momentum_lookback=20,
            volatility_windows=(20, 60, 120),
        )
        reference = await _observations_via_object_path(
            provider,
            code,
            decision_at,
            momentum_lookback=20,
            volatility_windows=(20, 60, 120),
        )
        assert columnar == reference
        assert {item.feature_name for item in columnar} >= {
            "momentum",
            "volatility_20d",
        }


# ---- d1_available_at 记忆化派生等值 ------------------------------------------


class TestD1AvailableAtEquivalence:
    """AC:记忆化派生与 _timestamp_available_at 的 D1 分支逐值相等。"""

    @pytest.mark.parametrize(
        "market", [Market.A_SHARE, Market.FUTURE, Market.HK, Market.US]
    )
    @pytest.mark.parametrize(
        "business_date",
        [
            date(2024, 1, 2),
            date(2026, 3, 8),  # America/New_York DST 边界(当日/次日偏移不同)
            date(2026, 3, 9),
            date(2026, 11, 1),  # US DST 结束
        ],
    )
    def test_matches_timestamp_available_at(
        self, market: Market, business_date: date
    ) -> None:
        midnight = datetime.combine(business_date, datetime.min.time(), tzinfo=UTC)
        assert d1_available_at(market, business_date) == _timestamp_available_at(
            midnight, BarPeriod.D1, market
        )

    def test_memoized_returns_identical_object(self) -> None:
        first = d1_available_at(Market.A_SHARE, SESSIONS[0])
        second = d1_available_at(Market.A_SHARE, SESSIONS[0])
        assert first is second
        assert isinstance(first, datetime)

