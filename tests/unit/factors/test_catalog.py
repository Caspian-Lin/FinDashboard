"""因子目录测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.factors.catalog import (
    FACTOR_FRAMEWORK_VERSION,
    RESEARCH_FACTOR_CATALOG,
    FactorCategory,
    FactorDirection,
    get_factor_meta,
    list_factors_by_category,
)


class TestCatalog:
    def test_version(self) -> None:
        assert FACTOR_FRAMEWORK_VERSION == "v1"

    def test_all_factors_have_required_fields(self) -> None:
        for name, meta in RESEARCH_FACTOR_CATALOG.items():
            assert meta.name == name
            assert meta.economic_hypothesis
            assert meta.expected_failure
            assert meta.source_field
            assert meta.category in FactorCategory
            assert meta.direction in FactorDirection

    def test_direction_sign(self) -> None:
        long_factors = list_factors_by_category(FactorCategory.QUALITY)
        for name in long_factors:
            meta = get_factor_meta(name)
            assert meta.direction_sign == 1.0 or meta.direction_sign == -1.0

    def test_pb_is_short_direction(self) -> None:
        meta = get_factor_meta("pb")
        assert meta.direction is FactorDirection.SHORT
        assert meta.direction_sign == -1.0

    def test_roe_is_long_direction(self) -> None:
        meta = get_factor_meta("roe")
        assert meta.direction is FactorDirection.LONG
        assert meta.direction_sign == 1.0

    def test_value_category_factors(self) -> None:
        value_factors = list_factors_by_category(FactorCategory.VALUE)
        assert "pb" in value_factors
        assert "earnings_yield" in value_factors
        assert "dividend_yield" in value_factors

    def test_low_risk_category_factors(self) -> None:
        low_risk = list_factors_by_category(FactorCategory.LOW_RISK)
        assert "volatility_20d" in low_risk
        assert "volatility_60d" in low_risk
        assert "volatility_120d" in low_risk
        assert "downside_volatility" in low_risk

    def test_quality_category_factors(self) -> None:
        quality = list_factors_by_category(FactorCategory.QUALITY)
        assert "roe" in quality
        assert "gross_profit_margin" in quality
        assert "debt_to_assets" in quality

    def test_momentum_category_factors(self) -> None:
        momentum = list_factors_by_category(FactorCategory.MOMENTUM)
        assert "momentum" in momentum
        # residual_momentum 尚无提取器/风险模型实现,不得暴露为可选因子。
        assert "residual_momentum" not in momentum

    def test_growth_category_factors(self) -> None:
        growth = list_factors_by_category(FactorCategory.GROWTH)
        assert "revenue_yoy" in growth

    def test_unknown_factor_raises(self) -> None:
        with pytest.raises(KeyError):
            get_factor_meta("nonexistent_factor")

    def test_all_low_risk_factors_short_direction(self) -> None:
        for name in list_factors_by_category(FactorCategory.LOW_RISK):
            meta = get_factor_meta(name)
            assert meta.direction is FactorDirection.SHORT

    def test_factor_count(self) -> None:
        assert len(RESEARCH_FACTOR_CATALOG) >= 13
