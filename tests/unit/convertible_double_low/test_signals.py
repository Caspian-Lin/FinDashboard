"""可转债双低信号排名测试(issue #63)。"""

from datetime import date
from decimal import Decimal

from finboard_backtest.convertible_double_low.config import (
    ConvertibleDoubleLowConfig,
    FactorWeight,
)
from finboard_backtest.convertible_double_low.signals import (
    compute_composite_score,
    compute_rank_threshold,
    generate_signals,
)
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_shared.instruments import ConvertibleMetadata, Instrument
from finboard_shared.types import InstrumentType, ListingStatus, Market


def _snap(
    code: str,
    close: Decimal,
    premium: Decimal,
    ytm: Decimal | None = None,
    days: int = 365,
    avg_amount: Decimal = Decimal("10000000"),
) -> ConvertibleSnapshot:
    return ConvertibleSnapshot(
        instrument=Instrument(
            code=code,
            name="测试",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
            list_date=date(2020, 1, 1),
            status=ListingStatus.ACTIVE,
        ),
        metadata=ConvertibleMetadata(
            underlying_stock_code="600000.SH",
            conversion_price=Decimal("10"),
        ),
        as_of=date(2023, 6, 1),
        close=close,
        volume=Decimal("100000"),
        amount=close * Decimal("100000"),
        conversion_premium=premium,
        conversion_price=Decimal("10"),
        conversion_value=close / (Decimal("1") + premium),
        ytm=ytm,
        days_to_maturity=days,
        remaining_size=Decimal("100000000"),
        avg_amount_20d=avg_amount,
    )


class TestGenerateSignals:
    def test_ranks_by_double_low(self) -> None:
        candidates = [
            _snap("AAA", Decimal("101"), Decimal("0.05")),
            _snap("BBB", Decimal("120"), Decimal("0.20")),
            _snap("CCC", Decimal("110"), Decimal("0.10")),
        ]
        cfg = ConvertibleDoubleLowConfig(top_n=3)
        signals = generate_signals(candidates, cfg)
        assert len(signals) == 3
        assert signals[0].code == "AAA"
        assert signals[1].code == "CCC"
        assert signals[2].code == "BBB"

    def test_top_n_limit(self) -> None:
        candidates = [_snap(f"C{i}", Decimal(f"{100 + i}"), Decimal("0.01")) for i in range(10)]
        cfg = ConvertibleDoubleLowConfig(top_n=3)
        signals = generate_signals(candidates, cfg)
        assert len(signals) == 3
        assert signals[0].rank == 1

    def test_empty_candidates(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        assert generate_signals([], cfg) == []

    def test_signal_has_double_low_value(self) -> None:
        candidates = [_snap("AAA", Decimal("105"), Decimal("0.10"))]
        cfg = ConvertibleDoubleLowConfig(top_n=1)
        signals = generate_signals(candidates, cfg)
        assert signals[0].double_low_value == Decimal("105") + Decimal("0.10") * Decimal("100")


class TestCompositeScore:
    def test_price_weight_only(self) -> None:
        candidates = [
            _snap("AAA", Decimal("101"), Decimal("0.05")),
            _snap("BBB", Decimal("120"), Decimal("0.05")),
        ]
        cfg = ConvertibleDoubleLowConfig(
            factor_weights={FactorWeight.PRICE: Decimal("1"), FactorWeight.PREMIUM: Decimal("0")}
        )
        score_a = compute_composite_score(candidates[0], candidates, cfg)
        score_b = compute_composite_score(candidates[1], candidates, cfg)
        assert score_a < score_b

    def test_ytm_weight_prefers_higher_ytm(self) -> None:
        candidates = [
            _snap("AAA", Decimal("105"), Decimal("0.10"), ytm=Decimal("0.03")),
            _snap("BBB", Decimal("105"), Decimal("0.10"), ytm=Decimal("-0.02")),
        ]
        cfg = ConvertibleDoubleLowConfig(
            factor_weights={FactorWeight.PRICE: Decimal("0"), FactorWeight.PREMIUM: Decimal("0"), FactorWeight.YTM: Decimal("1")}
        )
        score_a = compute_composite_score(candidates[0], candidates, cfg)
        score_b = compute_composite_score(candidates[1], candidates, cfg)
        assert score_a < score_b


class TestRankThreshold:
    def test_entry_threshold(self) -> None:
        cfg = ConvertibleDoubleLowConfig(top_n=20, entry_buffer_pct=Decimal("0.02"))
        threshold = compute_rank_threshold(cfg, direction="entry")
        assert threshold == 20

    def test_exit_threshold(self) -> None:
        cfg = ConvertibleDoubleLowConfig(top_n=20, exit_buffer_pct=Decimal("0.05"))
        threshold = compute_rank_threshold(cfg, direction="exit")
        assert threshold == 21

    def test_entry_threshold_with_large_buffer(self) -> None:
        cfg = ConvertibleDoubleLowConfig(top_n=10, entry_buffer_pct=Decimal("0.20"))
        threshold = compute_rank_threshold(cfg, direction="entry")
        assert threshold == 12
