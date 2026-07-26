"""因子快照与按日选股引擎测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_data import (
    DailySecurityMetrics,
    FactorInputBatch,
    FactorInputRecord,
    FactorName,
    FactorSelectionConfig,
    FactorSnapshotStatus,
    FinancialIndicator,
    IndustryMembership,
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
    ) -> None:
        self.records = records
        self.issues = issues

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
        assert required_datasets >= {
            "instrument_profiles",
            "daily_metrics",
        }
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
