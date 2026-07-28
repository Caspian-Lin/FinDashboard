"""因子提取工具测试。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from finboard_backtest.factors.extract import extract_factor_matrix
from finboard_data.factors import FactorInputBatch, FactorInputRecord
from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    InstrumentProfile,
)

_NOW = datetime(2024, 6, 28, 15, 0, 0)


def _make_profile(sym: str) -> InstrumentProfile:
    return InstrumentProfile(
        symbol=sym,
        name=f"Test {sym}",
        exchange="SSE",
        market="A股",
        list_status="L",
        list_date=date(2010, 1, 1),
        delist_date=None,
        industry="Technology",
        source="test",
        observed_at=_NOW,
        available_at=_NOW,
    )


def _make_daily(sym: str, **overrides: object) -> DailySecurityMetrics:
    defaults: dict[str, object] = {
        "symbol": sym,
        "trade_date": date(2024, 6, 28),
        "close": Decimal("10.0"),
        "turnover_rate": Decimal("0.05"),
        "turnover_rate_free": Decimal("0.06"),
        "volume_ratio": Decimal("1.0"),
        "pe": Decimal("20"),
        "pe_ttm": Decimal("18"),
        "pb": Decimal("2.0"),
        "ps": Decimal("3.0"),
        "ps_ttm": Decimal("2.8"),
        "dividend_yield": Decimal("0.02"),
        "dividend_yield_ttm": Decimal("0.025"),
        "total_shares": Decimal("1e8"),
        "float_shares": Decimal("8e7"),
        "free_shares": Decimal("7e7"),
        "total_market_cap": Decimal("1e9"),
        "circulating_market_cap": Decimal("8e8"),
        "limit_status": 0,
        "source": "test",
        "observed_at": _NOW,
        "available_at": _NOW,
    }
    defaults.update(overrides)
    return DailySecurityMetrics(**defaults)  # type: ignore[arg-type]


def _make_financial(sym: str) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=sym,
        announcement_date=date(2024, 4, 30),
        report_period=date(2024, 3, 31),
        update_flag="1",
        eps=Decimal("0.5"),
        diluted_eps=Decimal("0.48"),
        book_value_per_share=Decimal("5.0"),
        operating_cash_flow_per_share=Decimal("0.3"),
        return_on_equity=Decimal("0.15"),
        weighted_return_on_equity=Decimal("0.14"),
        gross_profit_margin=Decimal("0.35"),
        net_profit_margin=Decimal("0.12"),
        debt_to_assets=Decimal("0.40"),
        revenue_yoy=Decimal("0.10"),
        net_profit_yoy=Decimal("0.08"),
        operating_cash_flow_yoy=Decimal("0.05"),
        source="test",
        observed_at=_NOW,
        available_at=_NOW,
    )


class TestExtractFactorMatrix:
    def test_basic_extraction(self) -> None:
        records = [
            FactorInputRecord(
                symbol="A",
                profile=_make_profile("A"),
                daily=_make_daily("A"),
                financial=_make_financial("A"),
                industry=None,
            ),
        ]
        batch = FactorInputBatch(records=tuple(records), source="test", dataset_versions={})
        matrix = extract_factor_matrix(batch)
        assert "pb" in matrix
        assert matrix["pb"]["A"] == 2.0
        assert "roe" in matrix
        assert matrix["roe"]["A"] == 0.15
        assert "earnings_yield" in matrix
        assert abs(matrix["earnings_yield"]["A"] - 1.0 / 18.0) < 1e-6
        assert "dividend_yield" in matrix
        assert matrix["dividend_yield"]["A"] == 0.025
        assert "debt_to_assets" in matrix
        assert matrix["debt_to_assets"]["A"] == 0.40

    def test_missing_daily(self) -> None:
        records = [
            FactorInputRecord(
                symbol="B",
                profile=_make_profile("B"),
                daily=None,
                financial=_make_financial("B"),
                industry=None,
            ),
        ]
        batch = FactorInputBatch(records=tuple(records), source="test", dataset_versions={})
        matrix = extract_factor_matrix(batch)
        assert "roe" in matrix
        assert "pb" not in matrix

    def test_missing_financial(self) -> None:
        records = [
            FactorInputRecord(
                symbol="C",
                profile=_make_profile("C"),
                daily=_make_daily("C"),
                financial=None,
                industry=None,
            ),
        ]
        batch = FactorInputBatch(records=tuple(records), source="test", dataset_versions={})
        matrix = extract_factor_matrix(batch)
        assert "pb" in matrix
        assert "roe" not in matrix

    def test_negative_pe(self) -> None:
        records = [
            FactorInputRecord(
                symbol="D",
                profile=_make_profile("D"),
                daily=_make_daily("D", pe_ttm=Decimal("-10")),
                financial=None,
                industry=None,
            ),
        ]
        batch = FactorInputBatch(records=tuple(records), source="test", dataset_versions={})
        matrix = extract_factor_matrix(batch)
        assert matrix.get("earnings_yield", {}).get("D") == 0.0

    def test_price_factors(self) -> None:
        import numpy as np
        rng = np.random.default_rng(42)
        prices = [100.0]
        for _ in range(130):
            ret = rng.standard_normal() * 0.02
            prices.append(prices[-1] * (1 + ret))
        batch = FactorInputBatch(
            records=(FactorInputRecord(
                symbol="E", profile=_make_profile("E"),
                daily=_make_daily("E"), financial=None, industry=None,
            ),),
            source="test", dataset_versions={},
        )
        matrix = extract_factor_matrix(
            batch, price_history={"E": prices}, momentum_lookback=20,
            volatility_windows=(20, 60, 120),
        )
        assert "momentum" in matrix
        assert "volatility_20d" in matrix
        assert "volatility_60d" in matrix
        assert "volatility_120d" in matrix
        assert matrix["volatility_20d"]["E"] > 0

    def test_downside_volatility(self) -> None:
        prices = [100.0 + i * 0.5 for i in range(130)]
        for i in range(1, len(prices)):
            if i % 3 == 0:
                prices[i] = prices[i - 1] * 0.98
        batch = FactorInputBatch(
            records=(FactorInputRecord(
                symbol="F", profile=_make_profile("F"),
                daily=_make_daily("F"), financial=None, industry=None,
            ),),
            source="test", dataset_versions={},
        )
        matrix = extract_factor_matrix(
            batch, price_history={"F": prices}, momentum_lookback=20,
        )
        assert "downside_volatility" in matrix
        assert matrix["downside_volatility"]["F"] > 0

    def test_short_price_history(self) -> None:
        batch = FactorInputBatch(
            records=(FactorInputRecord(
                symbol="G", profile=_make_profile("G"),
                daily=_make_daily("G"), financial=None, industry=None,
            ),),
            source="test", dataset_versions={},
        )
        matrix = extract_factor_matrix(
            batch, price_history={"G": [10.0, 11.0, 12.0]},
            momentum_lookback=20, volatility_windows=(20, 60, 120),
        )
        assert "momentum" not in matrix
        assert "volatility_20d" not in matrix

    def test_market_cap_extracted(self) -> None:
        records = [
            FactorInputRecord(
                symbol="H",
                profile=_make_profile("H"),
                daily=_make_daily("H"),
                financial=None,
                industry=None,
            ),
        ]
        batch = FactorInputBatch(records=tuple(records), source="test", dataset_versions={})
        matrix = extract_factor_matrix(batch)
        assert "market_cap" in matrix
        assert matrix["market_cap"]["H"] == 1e9
