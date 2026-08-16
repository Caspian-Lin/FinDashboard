"""回测引擎的 T+1 候选池消费语义。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_core import Strategy, UniverseSelectionEvent
from finboard_data import (
    FactorInputBatch,
    FactorInputRecord,
    FactorName,
    FactorSelectionConfig,
    FactorSnapshot,
    FactorSnapshotStatus,
    InputsMode,
)
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market, Side


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


class BarsModeReader:
    """bars 模式的真实 reader:只返回无 profile/daily 的裸记录。"""

    def __init__(self) -> None:
        self.seen_required: frozenset[str] | None = None

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
        del business_date, decision_at, source, dataset_versions
        self.seen_required = required_datasets
        return FactorInputBatch(
            records=tuple(
                FactorInputRecord(
                    symbol=symbol,
                    profile=None,
                    daily=None,
                    financial=None,
                    industry=None,
                )
                for symbol in symbols
            ),
            source="tushare",
            dataset_versions={},
            issues=(),
        )


class BuyingStrategy(Strategy):
    """收到 published 快照后给全部入选标的等量买入。"""

    def __init__(self) -> None:
        self.selections: list[UniverseSelectionEvent] = []

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("buying")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx

    async def on_universe_selection(
        self,
        event: UniverseSelectionEvent,
        ctx: object,
    ) -> None:
        self.selections.append(event)
        if event.status != "published":
            return
        for code in event.selected_symbols:
            await ctx.submit_order(  # type: ignore[attr-defined]
                Symbol(code=code, market=Market.A_SHARE),
                Side.BUY,
                Decimal("100"),
            )


class RisingBarProvider:
    """5 个交易日递增收盘价,最后一天对齐 end。"""

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del period, start, adjust
        first = end - timedelta(days=4)
        return [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(first + timedelta(days=i), datetime.min.time(), tzinfo=UTC),
                open=Decimal(str(10 + i)),
                high=Decimal(str(10 + i)),
                low=Decimal(str(10 + i)),
                close=Decimal(str(10 + i)),
                volume=Decimal("100"),
            )
            for i in range(5)
        ]


@pytest.mark.unit
async def test_bars_mode_selection_produces_fills_without_daily_metrics() -> None:
    """bars 模式 + 无 research 数据表:选股快照 PUBLISHED 且回测产生交易。"""
    reader = BarsModeReader()
    strategy = BuyingStrategy()
    config = BacktestConfig(
        symbols=["000002.SZ", "000001.SZ"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        selection=FactorSelectionConfig(
            enabled=True,
            inputs_mode=InputsMode.BARS,
            ranking_factor=FactorName.MOMENTUM,
            momentum_lookback=2,
            max_symbols=2,
        ),
    )
    result = await BacktestEngine(
        strategy=strategy,
        data_provider=RisingBarProvider(),
        config=config,
        factor_selector=PointInTimeFactorSelector(reader=reader),
    ).run()

    # 纯价格因子配置不要求 daily_metrics 数据集。
    assert reader.seen_required == frozenset()
    published = [
        snapshot
        for snapshot in result.selection_snapshots
        if snapshot.status is FactorSnapshotStatus.PUBLISHED
    ]
    assert published, "bars 模式应产生 PUBLISHED 快照"
    assert published[0].selected_symbols == ("000001.SZ", "000002.SZ")
    assert any("ST/上市天数/退市过滤降级" in w for w in published[0].warnings)
    assert result.fills, "bars 模式回测应产生交易"
