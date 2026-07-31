"""ETF 多维分类器单元测试(issue #97)。

覆盖:
* 159010 跨境港股通科技 ETF 验收样例;
* 各执行档位(domestic/cross_border/bond/money_market/commodity);
* 置信度与 review_status(fail-closed);
* 弱证据降级(needs_review);
* 分类器版本化。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finboard_data.assets.classifier import (
    ETF_CLASSIFIER_VERSION,
    EtfClassifier,
    EtfRawFacts,
    classify_etf,
)
from finboard_shared.types import (
    AssetClass,
    EtfCategory,
    EtfExecutionProfile,
    EtfStrategyType,
    ReviewStatus,
    UnderlyingMarket,
)


class TestClassifierDimensions:
    """多维分类维度正确性。"""

    def test_159010_cross_border_hk_index(self) -> None:
        """验收样例:159010 恒生港股通科技 ETF。

        基金类型「股票指数」但跟踪港股 → cross_border_etf / hk / index。
        """
        facts = EtfRawFacts(
            code="159010.SZ",
            name="恒生科技ETF",
            fund_type="股票指数",
            tracked_index="恒生科技指数",
        )
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.CROSS_BORDER_ETF
        assert cls.underlying_market is UnderlyingMarket.HK
        assert cls.strategy_type is EtfStrategyType.INDEX
        assert cls.underlying_asset_class is AssetClass.EQUITY
        assert cls.category is EtfCategory.CROSS_BORDER
        assert cls.confidence >= Decimal("0.8")
        assert cls.review_status is ReviewStatus.AUTO_ADOPTED
        assert cls.tracked_index == "恒生科技指数"

    def test_domestic_equity_etf(self) -> None:
        facts = EtfRawFacts(
            code="510300.SH",
            name="沪深300ETF",
            fund_type="股票指数",
            tracked_index="沪深300指数",
        )
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.DOMESTIC_EQUITY_ETF
        assert cls.underlying_market is UnderlyingMarket.DOMESTIC
        assert cls.category is EtfCategory.EQUITY

    def test_bond_etf(self) -> None:
        facts = EtfRawFacts(code="511010.SH", name="国债ETF", fund_type="债券型")
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.BOND_ETF
        assert cls.underlying_asset_class is AssetClass.FIXED_INCOME
        assert cls.category is EtfCategory.BOND

    def test_money_market_etf(self) -> None:
        facts = EtfRawFacts(code="511990.SH", name="华宝添益", fund_type="货币型")
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.MONEY_MARKET_ETF
        assert cls.underlying_asset_class is AssetClass.CASH

    def test_commodity_etf(self) -> None:
        facts = EtfRawFacts(code="518880.SH", name="黄金ETF", fund_type="商品型")
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.COMMODITY_ETF
        assert cls.underlying_asset_class is AssetClass.COMMODITY

    def test_qdii_overseas(self) -> None:
        facts = EtfRawFacts(
            code="513100.SH",
            name="纳指ETF",
            fund_type="QDII-ETF",
            tracked_index="纳斯达克100指数",
        )
        cls = classify_etf(facts)
        assert cls.execution_profile is EtfExecutionProfile.CROSS_BORDER_ETF
        assert cls.underlying_market is UnderlyingMarket.OVERSEAS


class TestConfidenceAndReview:
    """置信度与审核状态(fail-closed)。"""

    def test_high_confidence_auto_adopted(self) -> None:
        facts = EtfRawFacts(
            code="510300.SH", name="沪深300ETF", fund_type="股票指数"
        )
        cls = classify_etf(facts)
        assert cls.review_status is ReviewStatus.AUTO_ADOPTED
        assert cls.confidence >= Decimal("0.8")

    def test_weak_evidence_needs_review(self) -> None:
        """只有名称含 ETF,无基金类型 → 低置信度 → needs_review。"""
        facts = EtfRawFacts(code="159999.SZ", name="某ETF")
        cls = classify_etf(facts)
        assert cls.review_status is ReviewStatus.NEEDS_REVIEW
        assert cls.confidence < Decimal("0.8")

    def test_no_evidence_needs_review(self) -> None:
        """完全没有分类线索 → needs_review。"""
        facts = EtfRawFacts(code="599999.SH", name="神秘基金")
        cls = classify_etf(facts)
        assert cls.review_status is ReviewStatus.NEEDS_REVIEW
        assert cls.confidence < Decimal("0.5")

    def test_evidence_not_empty(self) -> None:
        facts = EtfRawFacts(code="510300.SH", name="沪深300ETF", fund_type="股票指数")
        cls = classify_etf(facts)
        assert len(cls.evidence) > 0
        assert all(isinstance(e, str) and e for e in cls.evidence)


class TestClassifierService:
    """EtfClassifier 服务外观(可注入 / 可 mock)。"""

    def test_classify_batch(self) -> None:
        clf = EtfClassifier()
        facts_list = [
            EtfRawFacts(code="510300.SH", name="沪深300ETF", fund_type="股票指数"),
            EtfRawFacts(code="511010.SH", name="国债ETF", fund_type="债券型"),
            EtfRawFacts(code="159010.SZ", name="恒生科技ETF", fund_type="股票指数"),
        ]
        results = clf.classify_batch(facts_list)
        assert len(results) == 3
        assert results[0].execution_profile is EtfExecutionProfile.DOMESTIC_EQUITY_ETF
        assert results[1].execution_profile is EtfExecutionProfile.BOND_ETF
        assert results[2].execution_profile is EtfExecutionProfile.CROSS_BORDER_ETF

    def test_version_stamped(self) -> None:
        assert EtfClassifier.version == ETF_CLASSIFIER_VERSION
        cls = classify_etf(EtfRawFacts(code="510300.SH", name="沪深300ETF"))
        assert cls.rule_version == ETF_CLASSIFIER_VERSION


class TestCategoryCompatibility:
    """execution_profile → EtfCategory 兼容派生。"""

    @pytest.mark.parametrize(
        ("profile", "expected_category"),
        [
            (EtfExecutionProfile.DOMESTIC_EQUITY_ETF, EtfCategory.EQUITY),
            (EtfExecutionProfile.CROSS_BORDER_ETF, EtfCategory.CROSS_BORDER),
            (EtfExecutionProfile.BOND_ETF, EtfCategory.BOND),
            (EtfExecutionProfile.MONEY_MARKET_ETF, EtfCategory.MONEY_MARKET),
            (EtfExecutionProfile.COMMODITY_ETF, EtfCategory.COMMODITY),
        ],
    )
    def test_derived_category(
        self,
        profile: EtfExecutionProfile,
        expected_category: EtfCategory,
    ) -> None:
        from finboard_shared.types import etf_category_from_execution_profile

        assert etf_category_from_execution_profile(profile) is expected_category
