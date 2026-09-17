"""因子快照与按日选股引擎测试。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import numpy as np
import pytest

from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_backtest.selection_snapshot import FeatureSnapshotFactorReader
from finboard_data import (
    DailySecurityMetrics,
    FactorInputBatch,
    FactorInputRecord,
    FactorName,
    FactorSelectionConfig,
    FactorSnapshotStatus,
    FeatureObservation,
    FeatureSnapshot,
    FinancialIndicator,
    IndustryMembership,
    InputsMode,
    InstrumentProfile,
    PointInTimeSafety,
    RankingScope,
    factor_catalog,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

BUSINESS_DATE = date(2024, 1, 3)
DECISION_AT = datetime(2024, 1, 3, 17, tzinfo=UTC)
EFFECTIVE_DATE = date(2024, 1, 4)


class MemoryReader:
    def __init__(
        self,
        records: tuple[FactorInputRecord, ...],
        *,
        issues: tuple[str, ...] = (),
        expected_datasets: frozenset[str] = frozenset(
            {"instrument_profiles", "daily_metrics"}
        ),
    ) -> None:
        self.records = records
        self.issues = issues
        self._expected_datasets = expected_datasets

    async def load_factor_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        source: str,
        required_datasets: frozenset[str],
        dataset_versions: dict[str, str],
    ) -> FactorInputBatch:
        assert business_date == BUSINESS_DATE
        assert decision_at == DECISION_AT
        assert required_datasets >= self._expected_datasets
        return FactorInputBatch(
            records=self.records,
            source=source,
            dataset_versions={
                "instrument_profiles": "profiles-v1",
                "daily_metrics": "daily-v1",
                "industry_memberships": "industry-v1",
            },
            issues=self.issues,
        )


def _record(
    symbol: str,
    *,
    market_cap: str,
    pb: str = "1.2",
    industry: str = "I1",
    name: str = "测试股份",
    list_date: date = date(2020, 1, 1),
    delist_date: date | None = None,
    available_at: datetime = datetime(2024, 1, 3, 16, tzinfo=UTC),
) -> FactorInputRecord:
    profile = InstrumentProfile(
        symbol=symbol,
        name=name,
        exchange=symbol[-2:],
        market="main",
        list_status="L",
        list_date=list_date,
        delist_date=delist_date,
        industry=None,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )
    daily = DailySecurityMetrics(
        symbol=symbol,
        trade_date=BUSINESS_DATE,
        close=Decimal("10"),
        turnover_rate=Decimal("0.02"),
        turnover_rate_free=None,
        volume_ratio=None,
        pe=None,
        pe_ttm=None,
        pb=Decimal(pb),
        ps=None,
        ps_ttm=None,
        dividend_yield=None,
        dividend_yield_ttm=None,
        total_shares=None,
        float_shares=None,
        free_shares=None,
        total_market_cap=Decimal(market_cap),
        circulating_market_cap=None,
        limit_status=0,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )
    financial = FinancialIndicator(
        symbol=symbol,
        announcement_date=date(2023, 10, 30),
        report_period=date(2023, 9, 30),
        update_flag="1",
        eps=None,
        diluted_eps=None,
        book_value_per_share=None,
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal("0.10"),
        weighted_return_on_equity=None,
        gross_profit_margin=Decimal("0.30"),
        net_profit_margin=None,
        debt_to_assets=None,
        revenue_yoy=Decimal("0.08"),
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )
    membership = IndustryMembership(
        symbol=symbol,
        security_name=name,
        taxonomy="SW2021",
        level1_code=industry,
        level1_name=industry,
        level2_code=f"{industry}2",
        level2_name=industry,
        level3_code=f"{industry}3",
        level3_name=industry,
        effective_from=date(2020, 1, 1),
        effective_to=None,
        is_current=True,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )
    return FactorInputRecord(
        symbol=symbol,
        profile=profile,
        daily=daily,
        financial=financial,
        industry=membership,
    )


def _bars(symbol: str, *, volume: str = "100") -> tuple[Bar, ...]:
    shared = Symbol(code=symbol, market=Market.A_SHARE)
    return tuple(
        Bar(
            symbol=shared,
            period=BarPeriod.D1,
            timestamp=datetime(2024, 1, day, tzinfo=UTC),
            open=Decimal(str(day)),
            high=Decimal(str(day)),
            low=Decimal(str(day)),
            close=Decimal(str(day)),
            volume=Decimal(volume),
        )
        for day in (1, 2, 3)
    )


@pytest.mark.unit
def test_factor_catalog_is_versioned_and_point_in_time_safe() -> None:
    definitions = factor_catalog()
    assert {item.name for item in definitions} == set(FactorName)
    assert all(item.version == "v1" for item in definitions)
    assert all(item.dependencies for item in definitions)
    assert all(item.point_in_time_safety is PointInTimeSafety.STRICT for item in definitions)


@pytest.mark.unit
async def test_selection_is_deterministic_and_bounded_by_static_universe() -> None:
    delisted = _record(
        "000005.SZ",
        market_cap="999",
        industry="I3",
        delist_date=date(2023, 12, 31),
    )
    delisted = replace(delisted, daily=None)
    records = (
        _record("000001.SZ", market_cap="400", industry="I1"),
        _record("000002.SZ", market_cap="300", industry="I1"),
        _record("000003.SZ", market_cap="350", industry="I2"),
        _record("000004.SZ", market_cap="200", industry="I2"),
        delisted,
    )
    history = {record.symbol: _bars(record.symbol) for record in records}
    config = FactorSelectionConfig(
        enabled=True,
        ranking_scope=RankingScope.INDUSTRY,
        ranking_factor=FactorName.MARKET_CAP,
        max_symbols=2,
        max_per_industry=1,
        momentum_lookback=2,
    )

    first = await PointInTimeFactorSelector(reader=MemoryReader(records)).select(
        config=config,
        static_universe=[record.symbol.lower() for record in records],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history=history,
    )
    second = await PointInTimeFactorSelector(reader=MemoryReader(tuple(reversed(records)))).select(
        config=config,
        static_universe=[record.symbol for record in reversed(records)],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history=dict(reversed(tuple(history.items()))),
    )

    assert first.status is FactorSnapshotStatus.PUBLISHED
    assert first.selected_symbols == ("000001.SZ", "000003.SZ")
    assert set(first.selected_symbols) <= set(first.static_universe)
    assert first.effective_date > first.business_date
    assert first.checksum == second.checksum
    assert first.dataset_versions["daily_metrics"] == "daily-v1"
    market_cap_values = [
        value for value in first.values if value.factor_name is FactorName.MARKET_CAP
    ]
    assert {value.global_rank for value in market_cap_values} == {
        1,
        2,
        3,
        4,
    }
    assert {value.industry_rank for value in market_cap_values} == {1, 2}


@pytest.mark.unit
async def test_quality_or_future_data_skips_whole_rebalance() -> None:
    future = _record(
        "000001.SZ",
        market_cap="400",
        available_at=datetime(2024, 1, 3, 18, tzinfo=UTC),
    )
    config = FactorSelectionConfig(enabled=True)
    snapshot = await PointInTimeFactorSelector(reader=MemoryReader((future,))).select(
        config=config,
        static_universe=["000001.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history={"000001.SZ": _bars("000001.SZ")},
    )
    assert snapshot.status is FactorSnapshotStatus.SKIPPED
    assert snapshot.selected_symbols == ()
    assert snapshot.values == ()
    assert snapshot.skip_reason == "future_data:000001.SZ"

    partial_record = replace(
        _record("000001.SZ", market_cap="400"),
        industry=None,
    )
    partial = await PointInTimeFactorSelector(reader=MemoryReader((partial_record,))).select(
        config=replace(
            config,
            ranking_scope=RankingScope.INDUSTRY,
        ),
        static_universe=["000001.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history={"000001.SZ": _bars("000001.SZ")},
    )
    assert partial.status is FactorSnapshotStatus.SKIPPED
    assert partial.skip_reason == "industry_coverage_incomplete"


def _price_bars(symbol: str, closes: Sequence[Decimal]) -> tuple[Bar, ...]:
    """构造日线收盘价序列,最后一天对齐 BUSINESS_DATE。"""
    shared = Symbol(code=symbol, market=Market.A_SHARE)
    first_day = BUSINESS_DATE - timedelta(days=len(closes) - 1)
    return tuple(
        Bar(
            symbol=shared,
            period=BarPeriod.D1,
            timestamp=datetime.combine(
                first_day + timedelta(days=index),
                time(15, tzinfo=UTC),
            ),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("100"),
        )
        for index, close in enumerate(closes)
    )


def _profile_only(symbol: str) -> FactorInputRecord:
    """bars 模式使用的 profile-only 记录(daily/financial/industry 为空)。"""
    return replace(_record(symbol, market_cap="100"), daily=None)


def _feature_snapshot(
    *,
    snapshot_id: str = "snap-1",
    observations: tuple[FeatureObservation, ...],
    decision_at: datetime = DECISION_AT,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id=snapshot_id,
        dataset_release_id="rel-1",
        dataset_release_checksum="checksum-1",
        decision_at=decision_at,
        published_at=datetime(2024, 1, 3, 17, 30, tzinfo=UTC),
        framework_version="v2",
        calculation_windows={"pb": 1},
        transformations={},
        neutralization={},
        code_version="test",
        observations=observations,
        checksum="checksum-snap",
    )


def _observation(
    symbol: str,
    feature_name: str,
    value: float,
    *,
    available_at: datetime = datetime(2024, 1, 3, 16, tzinfo=UTC),
) -> FeatureObservation:
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=value,
        observed_at=available_at,
        available_at=available_at,
        source="tushare",
        source_version="daily-v1",
    )


@pytest.mark.unit
def test_required_datasets_derived_from_required_factors() -> None:
    bars_only = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.BARS,
        ranking_factor=FactorName.MOMENTUM,
    )
    assert bars_only.required_datasets == frozenset()

    market_cap = FactorSelectionConfig(
        enabled=True,
        ranking_factor=FactorName.MARKET_CAP,
    )
    assert market_cap.required_datasets == frozenset(
        {"instrument_profiles", "daily_metrics"}
    )

    industry_ranked = FactorSelectionConfig(
        enabled=True,
        ranking_factor=FactorName.MOMENTUM,
        ranking_scope=RankingScope.INDUSTRY,
    )
    assert industry_ranked.required_datasets == frozenset(
        {"instrument_profiles", "industry_memberships"}
    )

    financial = FactorSelectionConfig(
        enabled=True,
        ranking_factor=FactorName.ROE,
    )
    assert financial.required_datasets == frozenset(
        {"instrument_profiles", "financial_indicators"}
    )


@pytest.mark.unit
def test_inputs_mode_validation() -> None:
    with pytest.raises(ValueError, match="必须指定 snapshot_ids"):
        FactorSelectionConfig(
            enabled=True,
            inputs_mode=InputsMode.SNAPSHOT,
        )
    with pytest.raises(ValueError, match="snapshot 输入模式下使用"):
        FactorSelectionConfig(
            enabled=True,
            snapshot_ids=("snap-1",),
        )
    with pytest.raises(ValueError, match="不能重复"):
        FactorSelectionConfig(
            enabled=True,
            inputs_mode=InputsMode.SNAPSHOT,
            snapshot_ids=("snap-1", "snap-1"),
        )


@pytest.mark.unit
def test_factor_version_error_lists_valid_enum() -> None:
    """issue #190:不支持的 factor_version 报错必须列出合法枚举,而不是只回显输入。

    常见误传:把特征快照的 ``framework_version``(\"v2\")当选股规则版本传进来。
    """
    with pytest.raises(ValueError, match="不支持的 factor_version: v2") as excinfo:
        FactorSelectionConfig(enabled=True, factor_version="v2")
    assert "[v1]" in str(excinfo.value)

    # 合法枚举直接构造不报错。
    FactorSelectionConfig(enabled=True, factor_version="v1")


@pytest.mark.unit
async def test_bars_mode_uses_price_factors_without_daily_metrics() -> None:
    records = tuple(_profile_only(symbol) for symbol in ("000001.SZ", "000002.SZ"))
    history = {
        symbol: _price_bars(
            symbol, [Decimal(str(day)) for day in range(1, 22)]
        )
        for symbol in ("000001.SZ", "000002.SZ")
    }
    config = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.BARS,
        ranking_factor=FactorName.MOMENTUM,
        min_momentum=Decimal("0"),
        momentum_lookback=2,
        max_symbols=2,
    )
    snapshot = await PointInTimeFactorSelector(
        reader=MemoryReader(records, expected_datasets=frozenset())
    ).select(
        config=config,
        static_universe=["000001.SZ", "000002.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history=history,
    )
    assert snapshot.status is FactorSnapshotStatus.PUBLISHED
    assert snapshot.selected_symbols == ("000001.SZ", "000002.SZ")

    momentum_values = {
        v.symbol: v.value
        for v in snapshot.values
        if v.factor_name is FactorName.MOMENTUM
    }
    volatility_values = {
        v.symbol: v.value
        for v in snapshot.values
        if v.factor_name is FactorName.VOLATILITY_20D
    }
    assert momentum_values["000001.SZ"] == Decimal("21") / Decimal("19") - Decimal(1)
    # 波动率 = 最近 20 个日收益率样本标准差(ddof=1),与 extract.py 口径一致。
    closes = np.array([float(day) for day in range(1, 22)])
    returns = np.diff(closes) / closes[:-1]
    expected_vol = float(np.std(returns[-20:], ddof=1))
    assert abs(float(volatility_values["000001.SZ"]) - expected_vol) < 1e-9


@pytest.mark.unit
async def test_bars_mode_degrades_unpublished_datasets_to_warnings() -> None:
    records = tuple(
        replace(_record(symbol, market_cap="100"), profile=None)
        for symbol in ("000001.SZ", "000002.SZ")
    )
    history = {symbol: _bars(symbol) for symbol in ("000001.SZ", "000002.SZ")}
    config = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.BARS,
        ranking_factor=FactorName.MOMENTUM,
        momentum_lookback=2,
        max_symbols=2,
    )
    snapshot = await PointInTimeFactorSelector(
        reader=MemoryReader(
            records,
            issues=("dataset_unpublished:instrument_profiles",),
            expected_datasets=frozenset(),
        )
    ).select(
        config=config,
        static_universe=["000001.SZ", "000002.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history=history,
    )
    assert snapshot.status is FactorSnapshotStatus.PUBLISHED
    assert "dataset_unpublished:instrument_profiles" in snapshot.warnings
    assert any("ST/上市天数/退市过滤降级" in w for w in snapshot.warnings)


@pytest.mark.unit
async def test_bars_mode_daily_dependent_factor_fails_closed() -> None:
    records = tuple(_profile_only(symbol) for symbol in ("000001.SZ", "000002.SZ"))
    config = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.BARS,
        ranking_factor=FactorName.MARKET_CAP,
    )
    snapshot = await PointInTimeFactorSelector(
        reader=MemoryReader(records, expected_datasets=frozenset())
    ).select(
        config=config,
        static_universe=["000001.SZ", "000002.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history={"000001.SZ": _bars("000001.SZ")},
    )
    assert snapshot.status is FactorSnapshotStatus.SKIPPED
    assert snapshot.skip_reason == "daily_metrics_missing:000001.SZ"


@pytest.mark.unit
async def test_snapshot_mode_uses_frozen_observations() -> None:
    observations = (
        _observation("000001.SZ", "pb", 1.5),
        _observation("000002.SZ", "pb", 2.5),
        _observation("000001.SZ", "turnover_rate", 0.03),
        _observation("000002.SZ", "turnover_rate", 0.05),
    )
    reader = FeatureSnapshotFactorReader(
        snapshot_provider=_async_snapshot("snap-1", observations=observations),
        snapshot_ids=("snap-1",),
    )
    config = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.SNAPSHOT,
        snapshot_ids=("snap-1",),
        ranking_factor=FactorName.PB,
        min_pb=Decimal("1.0"),
        max_symbols=2,
    )
    history = {"000001.SZ": _bars("000001.SZ"), "000002.SZ": _bars("000002.SZ")}
    snapshot = await PointInTimeFactorSelector(reader=reader).select(
        config=config,
        static_universe=["000001.SZ", "000002.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history=history,
    )
    assert snapshot.status is FactorSnapshotStatus.PUBLISHED
    # 按 PB 降序:000002.SZ(2.5)优先于 000001.SZ(1.5)。
    assert snapshot.selected_symbols == ("000002.SZ", "000001.SZ")
    assert snapshot.dataset_versions["feature_snapshots"] == "snap-1"
    pb_values = {
        value.symbol: value.value
        for value in snapshot.values
        if value.factor_name is FactorName.PB
    }
    assert pb_values == {
        "000001.SZ": Decimal("1.5"),
        "000002.SZ": Decimal("2.5"),
    }


@pytest.mark.unit
async def test_snapshot_mode_missing_snapshot_skips() -> None:
    reader = FeatureSnapshotFactorReader(
        snapshot_provider=_async_snapshot(None),
        snapshot_ids=("snap-missing",),
    )
    config = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.SNAPSHOT,
        snapshot_ids=("snap-missing",),
        ranking_factor=FactorName.PB,
    )
    snapshot = await PointInTimeFactorSelector(reader=reader).select(
        config=config,
        static_universe=["000001.SZ"],
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        effective_date=EFFECTIVE_DATE,
        price_history={"000001.SZ": _bars("000001.SZ")},
    )
    assert snapshot.status is FactorSnapshotStatus.SKIPPED
    assert snapshot.skip_reason == "snapshot_not_found:snap-missing"


@pytest.mark.unit
async def test_snapshot_reader_filters_observations_by_available_at() -> None:
    observations = (
        _observation("000001.SZ", "pb", 1.5),
        _observation(
            "000001.SZ",
            "turnover_rate",
            0.03,
            available_at=datetime(2024, 1, 3, 18, tzinfo=UTC),
        ),
        _observation("000002.SZ", "pb", 2.5),
    )
    # 快照自身 decision_at(19:00)晚于 18:00 观测,合法;适配器按
    # 传入的 decision_at(17:00)做时点过滤。
    reader = FeatureSnapshotFactorReader(
        snapshot_provider=_async_snapshot(
            "snap-1",
            observations=observations,
            decision_at=datetime(2024, 1, 3, 19, tzinfo=UTC),
        ),
        snapshot_ids=("snap-1",),
    )
    batch = await reader.load_factor_inputs(
        symbols=("000001.SZ", "000002.SZ"),
        business_date=BUSINESS_DATE,
        decision_at=DECISION_AT,
        source="tushare",
        required_datasets=frozenset(),
        dataset_versions={},
    )
    assert batch.source == "tushare"
    assert batch.issues == ()
    by_symbol = {record.symbol: record for record in batch.records}
    # 18:00 的观测晚于 decision_at(17:00),被过滤。
    assert {
        obs.feature_name for obs in by_symbol["000001.SZ"].features
    } == {"pb"}
    assert {
        obs.feature_name for obs in by_symbol["000002.SZ"].features
    } == {"pb"}


def _async_snapshot(
    snapshot_id: str | None,
    observations: tuple[FeatureObservation, ...] = (),
    decision_at: datetime = DECISION_AT,
):
    async def load(_snapshot_id: str) -> FeatureSnapshot | None:
        if _snapshot_id != snapshot_id:
            return None
        return _feature_snapshot(
            snapshot_id=snapshot_id,
            observations=observations,
            decision_at=decision_at,
        )

    return load
