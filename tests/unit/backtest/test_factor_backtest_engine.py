"""回测引擎的 T+1 候选池消费语义。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_core import Strategy, UniverseSelectionEvent
from finboard_data import FactorSelectionConfig, FactorSnapshot, FactorSnapshotStatus
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod


class RecordingStrategy(Strategy):
    def __init__(self) -> None:
        self.market_symbols: list[tuple[date, str]] = []
        self.selections: list[UniverseSelectionEvent] = []

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("recording")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del ctx
        bar = event.bar  # type: ignore[attr-defined]
        self.market_symbols.append((bar.timestamp.date(), bar.symbol.code))

    async def on_universe_selection(
        self,
        event: UniverseSelectionEvent,
        ctx: object,
    ) -> None:
        del ctx
        self.selections.append(event)


class MemoryProvider:
    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del period, start, end, adjust
        return [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime(2024, 1, day, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                volume=Decimal("100"),
            )
            for day in (1, 2, 3)
        ]


class SequencedSelector:
    async def select(self, **kwargs: object) -> FactorSnapshot:
        business_date = kwargs["business_date"]
        decision_at = kwargs["decision_at"]
        effective_date = kwargs["effective_date"]
        assert isinstance(business_date, date)
        assert isinstance(decision_at, datetime)
        assert isinstance(effective_date, date)
        published = business_date == date(2024, 1, 1)
        return FactorSnapshot(
            decision_at=decision_at,
            business_date=business_date,
            effective_date=effective_date,
            source="tushare",
            dataset_versions={"daily_metrics": f"daily-{business_date}"},
            factor_version="v1",
            static_universe=("000001.SZ", "000002.SZ"),
            selected_symbols=("000001.SZ",) if published else (),
            values=(),
            status=(FactorSnapshotStatus.PUBLISHED if published else FactorSnapshotStatus.SKIPPED),
            skip_reason=None if published else "daily_metrics_missing",
            config={"enabled": True},
            checksum=f"{business_date:%Y%m%d}".ljust(64, "0"),
        )


@pytest.mark.unit
async def test_published_snapshot_applies_t_plus_one_and_skip_keeps_previous() -> None:
    strategy = RecordingStrategy()
    config = BacktestConfig(
        symbols=["000002.SZ", "000001.SZ"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        selection=FactorSelectionConfig(enabled=True),
    )
    result = await BacktestEngine(
        strategy=strategy,
        data_provider=MemoryProvider(),
        config=config,
        factor_selector=SequencedSelector(),  # type: ignore[arg-type]
    ).run()

    assert strategy.market_symbols == [
        (date(2024, 1, 2), "000001.SZ"),
        (date(2024, 1, 3), "000001.SZ"),
    ]
    assert [event.status for event in strategy.selections] == [
        "published",
        "skipped",
    ]
    assert strategy.selections[-1].selected_symbols == ("000001.SZ",)
    assert len(result.selection_snapshots) == 2
    assert result.dataset_versions == {"daily_metrics": ["daily-2024-01-01", "daily-2024-01-02"]}
