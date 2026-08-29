"""因子目录收敛(#214):v1 FACTOR_CATALOG 是 FACTOR_LAB_CATALOG 的投影。

v2(FACTOR_LAB_CATALOG)是唯一事实来源;v1 目录逐字段由投影生成,
任何一侧漂移都应在测试期暴露。
"""

from __future__ import annotations

import pytest

from finboard_data.factor_lab import FACTOR_LAB_CATALOG, FeatureFrequency
from finboard_data.factors import (
    FACTOR_CATALOG,
    FACTOR_VERSION,
    FactorFrequency,
    FactorName,
    FactorSelectionConfig,
    factor_catalog,
)


class TestProjection:
    def test_v1_names_are_subset_of_lab_catalog(self) -> None:
        for name in FactorName:
            assert name.value in FACTOR_LAB_CATALOG

    def test_dependencies_and_frequency_follow_lab_catalog(self) -> None:
        for name, definition in FACTOR_CATALOG.items():
            source = FACTOR_LAB_CATALOG[name.value]
            assert definition.dependencies == source.source_fields
            expected = (
                FactorFrequency.REPORT
                if source.frequency
                in (FeatureFrequency.REPORT, FeatureFrequency.EVENT)
                else FactorFrequency.DAILY
            )
            assert definition.frequency is expected
            assert definition.description == source.economic_hypothesis
            assert definition.version == FACTOR_VERSION

    def test_projection_field_values_stable(self) -> None:
        # 冻结投影展示映射,防止无意间改变 v1 selection 契约。
        assert FACTOR_CATALOG[FactorName.MARKET_CAP].dependencies == (
            "daily_metrics.total_market_cap",
        )
        assert FACTOR_CATALOG[FactorName.ROE].frequency is FactorFrequency.REPORT
        assert FACTOR_CATALOG[FactorName.VOLATILITY_20D].dependencies == (
            "bars.close",
        )

    def test_factor_catalog_helper_sorted(self) -> None:
        entries = factor_catalog()
        assert len(entries) == len(FACTOR_CATALOG)
        assert [entry.name for entry in entries] == sorted(
            FACTOR_CATALOG, key=lambda name: name.value
        )


class TestV1SelectionCompat:
    def test_factor_version_v1_accepted(self) -> None:
        config = FactorSelectionConfig(enabled=True)
        assert config.factor_version == FACTOR_VERSION == "v1"

    def test_unsupported_factor_version_rejected_with_enum_hint(self) -> None:
        with pytest.raises(ValueError, match=r"合法值仅 \[v1\]"):
            FactorSelectionConfig(enabled=True, factor_version="v2")

    def test_v1_factor_names_unchanged(self) -> None:
        assert {name.value for name in FactorName} == {
            "market_cap",
            "pb",
            "turnover_rate",
            "momentum",
            "volatility_20d",
            "roe",
            "gross_profit_margin",
            "revenue_yoy",
        }
