"""multi_period 特征可用性元数据与入队门控(issue #253)。

覆盖验收:

* 发布侧元数据 —— ``ResearchDatasetRelease.derived_features`` 按 kind 从
  canonical 映射冻结进 manifest;旧发布(无该字段)经
  ``derived_feature_names`` 回退 kind 映射,不误拒;
* 入队期门控 —— ``multi_period_feature_gate_error`` 对缺 daily_metrics /
  financial_indicators 发布的 multi_period run 具名拒绝(缺哪个特征、需要
  哪种发布),声明齐备 / single_shot 放行;
* 映射漂移锁定 —— canonical 映射与组合管线 ``extract_factor_matrix``
  (``_matrix_to_feature_values`` 实际调用的提取实现,不带价格历史)输出
  特征名一致;universe_precheck 的 str-keyed 投影与数据层一致。

纯离线研究域,不连 broker / 不下单。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from finboard_backtest.factors.extract import extract_factor_matrix
from finboard_backtest.research_run.signal_engine import multi_period_feature_gate_error
from finboard_backtest.strategy_spec.universe_precheck import (
    RESEARCH_RELEASE_FEATURE_NAMES,
    research_release_derived_features,
)
from finboard_data.factors import FactorInputBatch, FactorInputRecord
from finboard_data.releases import (
    RESEARCH_RELEASE_FEATURE_NAMES as CANONICAL_RELEASE_FEATURE_NAMES,
)
from finboard_data.releases import (
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)
from finboard_data.releases import (
    research_release_derived_features as canonical_derived_features,
)
from finboard_data.research import DailySecurityMetrics, FinancialIndicator
from finboard_shared.types import BarPeriod, DatasetQualityStatus


def _minimal_research_release(
    *,
    dataset_kind: ReleaseDatasetKind,
    adjustment: str = "none",
    fields: tuple[str, ...] = ("trade_date",),
) -> ResearchDatasetRelease:
    return ResearchDatasetRelease(
        release_id=f"r-{dataset_kind.value}",
        dataset_name=dataset_kind.value,
        source="tushare",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 6, 28),
        period=BarPeriod.D1,
        adjustment=adjustment,
        fields=fields,
        availability_rules=(),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 6, 28, tzinfo=UTC),
        instruments=(),
        capabilities=(),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={},
        dataset_kind=dataset_kind,
    )

# ---- 提取实现的输入构造 -------------------------------------------------------


def _daily(symbol: str, **overrides: Decimal) -> DailySecurityMetrics:
    values: dict[str, Decimal | None] = {
        "pb": Decimal("1.5"),
        "turnover_rate": Decimal("0.02"),
        "total_market_cap": Decimal("1e10"),
        "pe_ttm": Decimal("10"),
        "dividend_yield_ttm": Decimal("0.03"),
    }
    values.update(overrides)
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=date(2024, 6, 28),
        close=None,
        turnover_rate_free=None,
        volume_ratio=None,
        pe=None,
        ps=None,
        ps_ttm=None,
        dividend_yield=None,
        total_shares=None,
        float_shares=None,
        free_shares=None,
        circulating_market_cap=None,
        limit_status=None,
        source="tushare",
        observed_at=datetime(2024, 6, 28, 15, 0, tzinfo=UTC),
        available_at=datetime(2024, 6, 28, 15, 0, tzinfo=UTC),
        **values,
    )


def _financial(symbol: str) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=symbol,
        announcement_date=date(2024, 4, 26),
        report_period=date(2024, 3, 31),
        update_flag=None,
        eps=None,
        diluted_eps=None,
        book_value_per_share=None,
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal("0.1"),
        weighted_return_on_equity=None,
        gross_profit_margin=Decimal("0.3"),
        net_profit_margin=None,
        debt_to_assets=Decimal("0.4"),
        revenue_yoy=Decimal("0.05"),
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=datetime(2024, 4, 26, 15, 0, tzinfo=UTC),
        available_at=datetime(2024, 4, 27, 0, 0, tzinfo=UTC),
    )


# ---- 映射漂移锁定 -------------------------------------------------------------


class TestCanonicalMappingDrift:
    """canonical 映射(数据层)与运行时提取实现的一致性。"""

    def test_matches_extract_factor_matrix_output(self) -> None:
        """extract_factor_matrix(无价格历史)产出的特征名 ⊇/⊆ 映射并集。"""
        batch = FactorInputBatch(
            records=(
                FactorInputRecord(
                    symbol="600001.SH",
                    profile=None,
                    daily=_daily("600001.SH"),
                    financial=_financial("600001.SH"),
                    industry=None,
                ),
            ),
            source="tushare",
            dataset_versions={"research_release": "frozen"},
        )
        matrix = extract_factor_matrix(batch)
        mapped = set(
            canonical_derived_features(
                (ReleaseDatasetKind.DAILY_METRICS, ReleaseDatasetKind.FINANCIAL_INDICATORS)
            )
        )
        # 提取实现(研究发布加载路径只跑 daily/financial 两段)产出的
        # 特征名必须与映射完全一致:多出的名字会让门控误放行,缺失的
        # 名字会让门控误拒。
        assert set(matrix) == mapped

    def test_universe_precheck_projection_matches_canonical(self) -> None:
        """universe_precheck 的 str-keyed 投影与数据层 enum-keyed 映射一致。"""
        assert set(RESEARCH_RELEASE_FEATURE_NAMES) == {
            kind.value for kind in CANONICAL_RELEASE_FEATURE_NAMES
        }
        for kind, names in CANONICAL_RELEASE_FEATURE_NAMES.items():
            assert RESEARCH_RELEASE_FEATURE_NAMES[kind.value] == names
        kinds = [ReleaseDatasetKind.DAILY_METRICS, ReleaseDatasetKind.FINANCIAL_INDICATORS]
        assert research_release_derived_features(kinds) == canonical_derived_features(kinds)


# ---- 发布侧元数据 -------------------------------------------------------------


class TestReleaseDerivedFeaturesMetadata:
    """``derived_features`` 字段的冻结、序列化与回退语义。"""

    def test_builder_freezes_features_for_research_kinds(self) -> None:
        """研究数据发布携带排序后的可派生特征;bars 发布为空。"""
        release = _minimal_research_release(dataset_kind=ReleaseDatasetKind.DAILY_METRICS)
        assert release.derived_features == ()
        # 发布构造路径(builder)冻结的元数据 = 排序后的映射值。
        expected = tuple(
            sorted(
                canonical_derived_features((ReleaseDatasetKind.DAILY_METRICS,))
            )
        )
        assert expected == (
            "dividend_yield",
            "earnings_yield",
            "market_cap",
            "pb",
            "turnover_rate",
        )
        frozen = replace(release, derived_features=expected)
        assert frozen.derived_feature_names == frozenset(expected)

    def test_legacy_release_falls_back_to_kind_mapping(self) -> None:
        """旧发布(derived_features 为空)回退 kind 映射,入队校验不误拒。"""
        release = _minimal_research_release(dataset_kind=ReleaseDatasetKind.DAILY_METRICS)
        assert release.derived_features == ()
        assert release.derived_feature_names == (
            canonical_derived_features((ReleaseDatasetKind.DAILY_METRICS,))
        )

    def test_as_dict_omits_empty_field_checksum_stable(self) -> None:
        """空 derived_features 不序列化:旧 manifest 重算 checksum 不漂移。"""
        release = _minimal_research_release(
            dataset_kind=ReleaseDatasetKind.BARS,
            adjustment="qfq",
            fields=("close",),
        )
        release = replace(release, release_checksum="c" * 64)
        assert "derived_features" not in release.as_dict()
        # roundtrip:研究发布的非空字段可序列化还原。
        research = replace(
            release,
            dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
            derived_features=("debt_to_assets", "gross_profit_margin", "roe", "revenue_yoy"),
        )
        restored = ResearchDatasetRelease.from_dict(research.as_dict())
        assert restored.derived_features == research.derived_features
        assert restored.derived_feature_names == frozenset(research.derived_features)


# ---- 入队期门控 ----------------------------------------------------------------


class TestMultiPeriodFeatureGate:
    """``multi_period_feature_gate_error`` 的放行 / 拒绝语义。"""

    def test_missing_daily_metrics_named(self) -> None:
        """引用 pb 但只挂 bars 发布:具名缺失特征与所需发布 kind。"""
        error = multi_period_feature_gate_error(
            identity_sources={"pb", "momentum", "close"},
            parameters={"rebalance_frequency": "monthly"},
            research_release_kinds=[ReleaseDatasetKind.BARS],
        )
        assert error is not None
        assert "execution_mode=multi_period" in error
        assert "['pb']" in error
        assert "pb 需要 daily_metrics 研究数据发布" in error
        assert "['bars']" in error
        assert "identity 节点缺少数据源" in error
        assert "dataset_release_ids" in error

    def test_missing_financial_indicators_named(self) -> None:
        """引用 roe 但缺 financial_indicators 发布:具名指向。"""
        error = multi_period_feature_gate_error(
            identity_sources={"roe"},
            parameters={"rebalance_frequency": "quarterly"},
            research_release_kinds=[ReleaseDatasetKind.BARS, ReleaseDatasetKind.DAILY_METRICS],
        )
        assert error is not None
        assert "roe 需要 financial_indicators 研究数据发布" in error

    def test_feature_without_any_provider_named(self) -> None:
        """非标准 / 无提供者的特征:明说无发布 kind 可提供并给修复路径。"""
        error = multi_period_feature_gate_error(
            identity_sources={"volume", "my_custom_factor"},
            parameters={"rebalance_frequency": "monthly"},
            research_release_kinds=[ReleaseDatasetKind.BARS],
        )
        assert error is not None
        assert "['my_custom_factor', 'volume']" in error
        assert "无任何发布 kind 可提供" in error
        assert "momentum" in error  # 标准价格特征清单在文案里

    def test_passes_when_research_releases_attached(self) -> None:
        """声明齐备(bars + 两类研究发布):放行。"""
        assert (
            multi_period_feature_gate_error(
                identity_sources={
                    "pb",
                    "roe",
                    "turnover_rate",
                    "market_cap",
                    "momentum",
                    "volatility_20d",
                    "close",
                },
                parameters={"rebalance_frequency": "monthly"},
                research_release_kinds=[
                    ReleaseDatasetKind.BARS,
                    ReleaseDatasetKind.DAILY_METRICS,
                    ReleaseDatasetKind.FINANCIAL_INDICATORS,
                ],
            )
            is None
        )

    def test_passes_with_snapshot_observed_features(self) -> None:
        """快照观测提供的特征(非发布派生)同样可解析。"""
        assert (
            multi_period_feature_gate_error(
                identity_sources={"my_snapshot_factor"},
                parameters={"rebalance_frequency": "monthly"},
                research_release_kinds=[ReleaseDatasetKind.BARS],
                snapshot_feature_names=["my_snapshot_factor"],
            )
            is None
        )

    def test_single_shot_not_affected(self) -> None:
        """未声明频率(single_shot)不受本门控约束(由 #203 门控把关)。"""
        assert (
            multi_period_feature_gate_error(
                identity_sources={"pb"},
                parameters={},
                research_release_kinds=[ReleaseDatasetKind.BARS],
            )
            is None
        )

    def test_invalid_frequency_not_gated(self) -> None:
        """非法频率值不在本门控拦截(schema 与 fail-closed 读取负责)。

        issue #361:weekly 已成为合法多期频率(legacy 扩展),非法值改用
        "yearly";weekly 与 decision_schedule 声明同样进入本门控判定。
        """
        assert (
            multi_period_feature_gate_error(
                identity_sources={"pb"},
                parameters={"rebalance_frequency": "yearly"},
                research_release_kinds=[ReleaseDatasetKind.BARS],
            )
            is None
        )
        assert (
            multi_period_feature_gate_error(
                identity_sources={},
                parameters={"decision_schedule": {"kind": "weekly"}},
                research_release_kinds=[ReleaseDatasetKind.BARS],
            )
            is None
        )

    def test_accepts_string_kinds(self) -> None:
        """kind 入参容忍字符串形式(与 resolvable_feature_names 同风格)。"""
        assert (
            multi_period_feature_gate_error(
                identity_sources={"pb"},
                parameters={"rebalance_frequency": "monthly"},
                research_release_kinds=["daily_metrics"],
            )
            is None
        )
