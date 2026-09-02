"""选股逐期诊断与整期 SKIPPED fail-visible(issue #255)。

覆盖验收:选股启用且整期全部 SKIPPED 时,run 结果/report 携带显式诊断
(skip 原因统计、zero_trading_suspected),不再有「0 交易成功」假象;
选股未启用不携带诊断;published 正常路径诊断不误报。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.config import BacktestConfig
from finboard_backtest.engine import BacktestEngine
from finboard_core import Strategy
from finboard_data import FactorSelectionConfig
from finboard_data.factors import FactorSnapshot
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL_A = "600001.SH"
SYMBOL_B = "600002.SH"


class NoopStrategy(Strategy):
    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("noop")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx


def _bar(symbol: Symbol, day: int, close: Decimal) -> Bar:
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1000000"),
        amount=Decimal("0"),
        source="test",
    )


class MemoryProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol

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
        return self.bars_by_symbol.get(symbol.code, [])


def _bars(symbol: Symbol) -> list[Bar]:
    return [_bar(symbol, day, Decimal("10")) for day in (2, 3, 4)]


def _snapshot(
    *,
    status: str,
    skip_reason: str | None,
    selected: tuple[str, ...],
    decision_at: datetime,
    effective_date: date,
) -> FactorSnapshot:
    from finboard_data import FactorSnapshotStatus

    return FactorSnapshot(
        decision_at=decision_at,
        business_date=effective_date - timedelta(days=1),
        effective_date=effective_date,
        source="test",
        dataset_versions={"instrument_profiles": "v-test"},
        factor_version="v1",
        static_universe=(SYMBOL_A, SYMBOL_B),
        selected_symbols=selected,
        values=(),
        status=FactorSnapshotStatus(status),
        skip_reason=skip_reason,
        config={"enabled": True},
        checksum=f"{effective_date:%Y%m%d}".ljust(64, "0"),
    )


class AllSkippedSelector:
    """恒 SKIPPED(research_db 缺 profiles 的整期空转形态)。"""

    async def select(self, **kwargs: object) -> FactorSnapshot:
        return _snapshot(
            status="skipped",
            skip_reason="profile_missing:600001.SH",
            selected=(),
            decision_at=kwargs["decision_at"],  # type: ignore[arg-type]
            effective_date=kwargs["effective_date"],  # type: ignore[arg-type]
        )


class PublishedSelector:
    """恒 PUBLISHED 且选出标的(正常路径)。"""

    async def select(self, **kwargs: object) -> FactorSnapshot:
        return _snapshot(
            status="published",
            skip_reason=None,
            selected=(SYMBOL_A,),
            decision_at=kwargs["decision_at"],  # type: ignore[arg-type]
            effective_date=kwargs["effective_date"],  # type: ignore[arg-type]
        )


def _engine_config() -> BacktestConfig:
    return BacktestConfig(
        symbols=[SYMBOL_A, SYMBOL_B],
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        selection=FactorSelectionConfig(enabled=True),
    )


@pytest.mark.unit
async def test_all_skipped_selection_reports_zero_trading_diagnostics() -> None:
    """整期 SKIPPED:诊断携带 skip 原因统计 + zero_trading_suspected。"""
    from unittest.mock import patch

    from finboard_backtest import engine as engine_module

    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=MemoryProvider(
            {SYMBOL_A: _bars(Symbol(SYMBOL_A, Market.A_SHARE))}
        ),
        config=_engine_config(),
        factor_selector=AllSkippedSelector(),  # type: ignore[arg-type]
    )
    with patch.object(engine_module.logger, "warning") as warn:
        result = await engine.run()
    diag = result.selection_diagnostics
    assert diag is not None
    assert diag["total_snapshots"] == 2
    assert diag["published_snapshots"] == 0
    assert diag["skipped_snapshots"] == 2
    assert diag["skip_reasons"] == {"profile_missing:600001.SH": 2}
    assert diag["selection_pool_ever_active"] is False
    assert diag["zero_trading_suspected"] is True
    assert any(
        call.args and call.args[0] == "backtest.selection_pool_never_active"
        for call in warn.call_args_list
    )
    # summary 文本显式提示,report 可见
    assert "选股整期无候选生效" in result.summary()
    assert "profile_missing:600001.SH=2" in result.summary()


@pytest.mark.unit
async def test_published_selection_diagnostics_not_flagged() -> None:
    """正常选股路径:诊断不误报 zero_trading_suspected。"""
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=MemoryProvider(
            {SYMBOL_A: _bars(Symbol(SYMBOL_A, Market.A_SHARE))}
        ),
        config=_engine_config(),
        factor_selector=PublishedSelector(),  # type: ignore[arg-type]
    )
    result = await engine.run()
    diag = result.selection_diagnostics
    assert diag is not None
    assert diag["published_snapshots"] == 2
    assert diag["skipped_snapshots"] == 0
    assert diag["selection_pool_ever_active"] is True
    assert "zero_trading_suspected" not in diag
    assert "选股整期无候选生效" not in result.summary()


@pytest.mark.unit
async def test_no_selection_no_diagnostics() -> None:
    """选股未启用:不携带诊断字段(行为不变)。"""
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=MemoryProvider(
            {SYMBOL_A: _bars(Symbol(SYMBOL_A, Market.A_SHARE))}
        ),
        config=BacktestConfig(
            symbols=[SYMBOL_A],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        ),
    )
    result = await engine.run()
    assert result.selection_diagnostics is None
