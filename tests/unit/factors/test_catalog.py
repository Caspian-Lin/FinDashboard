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


class TestLabCatalogProjection:
    """#226:语义字段自 FACTOR_LAB_CATALOG 投影,评分参数本地维护。"""

    def test_semantic_fields_follow_lab_catalog(self) -> None:
        from finboard_backtest.factors import catalog
        from finboard_data.factor_lab import FACTOR_LAB_CATALOG

        for name, meta in catalog.RESEARCH_FACTOR_CATALOG.items():
            source = FACTOR_LAB_CATALOG[name]
            assert meta.economic_hypothesis == source.economic_hypothesis
            assert meta.expected_failure == source.expected_failure
            assert meta.source_field == source.source_fields[0]

    def test_direction_frozen_signs(self) -> None:
        # 冻结 13 因子的方向语义,投影不得改变评分行为。
        expected = {
            "pb": -1.0, "earnings_yield": 1.0, "dividend_yield": 1.0,
            "roe": 1.0, "gross_profit_margin": 1.0, "debt_to_assets": -1.0,
            "volatility_20d": -1.0, "volatility_60d": -1.0,
            "volatility_120d": -1.0, "downside_volatility": -1.0,
            "turnover_rate": -1.0, "momentum": 1.0, "revenue_yoy": 1.0,
        }
        for name, sign in expected.items():
            assert get_factor_meta(name).direction_sign == sign

    def test_compiler_dataset_mapping_unchanged(self) -> None:
        from finboard_backtest.strategy_spec.compiler import _dataset_from_source

        expected = {
            "pb": "daily_metrics",
            "earnings_yield": "daily_metrics",
            "dividend_yield": "daily_metrics",
            "roe": "financial_indicators",
            "gross_profit_margin": "financial_indicators",
            "debt_to_assets": "financial_indicators",
            "volatility_20d": "bars",
            "volatility_60d": "bars",
            "volatility_120d": "bars",
            "downside_volatility": "bars",
            "turnover_rate": "daily_metrics",
            "momentum": "bars",
            "revenue_yoy": "financial_indicators",
        }
        for name, dataset in expected.items():
            assert _dataset_from_source(get_factor_meta(name).source_field) == dataset

    def test_missing_lab_definition_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_backtest.factors import catalog
        from finboard_data.factor_lab import FACTOR_LAB_CATALOG

        shrunk = dict(FACTOR_LAB_CATALOG)
        del shrunk["pb"]
        monkeypatch.setattr(catalog, "FACTOR_LAB_CATALOG", shrunk)
        with pytest.raises(RuntimeError, match="FACTOR_LAB_CATALOG"):
            catalog._project_research_catalog()

    def test_exposure_only_preference_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import replace

        from finboard_backtest.factors import catalog
        from finboard_data.factor_lab import FACTOR_LAB_CATALOG, FactorPreference

        exposed = replace(
            FACTOR_LAB_CATALOG["pb"],
            preference=FactorPreference.EXPOSURE_ONLY,
        )
        patched = dict(FACTOR_LAB_CATALOG)
        patched["pb"] = exposed
        monkeypatch.setattr(catalog, "FACTOR_LAB_CATALOG", patched)
        with pytest.raises(RuntimeError, match="无方向映射"):
            catalog._project_research_catalog()
