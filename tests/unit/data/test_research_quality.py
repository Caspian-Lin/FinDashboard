"""时点化研究数据质量门测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from finboard_data import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    QualityStatus,
    ResearchDataQualityValidator,
)

NOW = datetime(2026, 7, 27, 8, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 24)


def _daily(
    symbol: str = "000001.SZ",
    *,
    source: str = "tushare",
) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=TRADE_DATE,
        close=Decimal("12.34"),
        turnover_rate=Decimal("0.025"),
        turnover_rate_free=Decimal("0.031"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("8.5"),
        pe_ttm=Decimal("9.1"),
        pb=Decimal("0.92"),
        ps=Decimal("1.1"),
        ps_ttm=Decimal("1.2"),
        dividend_yield=Decimal("0.03"),
        dividend_yield_ttm=Decimal("0.032"),
        total_shares=Decimal("100000000"),
        float_shares=Decimal("80000000"),
        free_shares=Decimal("70000000"),
        total_market_cap=Decimal("1234000000"),
        circulating_market_cap=Decimal("987200000"),
        limit_status=0,
        source=source,
        observed_at=NOW,
        available_at=NOW,
    )


def _financial() -> FinancialIndicator:
    return FinancialIndicator(
        symbol="000001.SZ",
        announcement_date=date(2026, 4, 20),
        report_period=date(2026, 3, 31),
        update_flag="0",
        eps=Decimal("0.45"),
        diluted_eps=Decimal("0.44"),
        book_value_per_share=Decimal("12"),
        operating_cash_flow_per_share=Decimal("0.8"),
        return_on_equity=Decimal("0.08"),
        weighted_return_on_equity=Decimal("0.079"),
        gross_profit_margin=Decimal("0.4"),
        net_profit_margin=Decimal("0.15"),
        debt_to_assets=Decimal("0.6"),
        revenue_yoy=Decimal("0.1"),
        net_profit_yoy=Decimal("0.12"),
        operating_cash_flow_yoy=Decimal("0.05"),
        source="tushare",
        observed_at=NOW,
        available_at=datetime(2026, 4, 21, tzinfo=UTC),
    )


def _industry() -> IndustryMembership:
    return IndustryMembership(
        symbol="000001.SZ",
        security_name="平安银行",
        taxonomy="SW2021",
        level1_code="460000",
        level1_name="银行",
        level2_code="461100",
        level2_name="股份制银行Ⅱ",
        level3_code="461101",
        level3_name="股份制银行Ⅲ",
        effective_from=date(2021, 12, 13),
        effective_to=None,
        is_current=True,
        source="tushare",
        observed_at=NOW,
        available_at=NOW,
    )


def _validator() -> ResearchDataQualityValidator:
    return ResearchDataQualityValidator(now=NOW)


def test_daily_quality_passes_valid_complete_cross_section() -> None:
    records = [_daily(), _daily("600000.SH")]

    report = _validator().validate_daily_metrics(
        records,
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
        expected_symbols={"000001.SZ", "600000.SH"},
    )

    assert report.status is QualityStatus.PASSED
    assert report.publishable is True
    assert report.issues == ()


def test_coverage_gap_is_partial_and_not_publishable() -> None:
    report = _validator().validate_daily_metrics(
        [_daily()],
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
        expected_symbols={"000001.SZ", "600000.SH"},
    )

    assert report.status is QualityStatus.PARTIAL
    assert report.publishable is False
    assert report.issues[0].code == "coverage_missing"


def test_duplicate_or_source_mixing_is_failed_not_partial() -> None:
    report = _validator().validate_daily_metrics(
        [_daily(), _daily(), _daily("600000.SH", source="akshare")],
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
        expected_symbols={"000001.SZ", "600000.SH", "300001.SZ"},
    )

    assert report.status is QualityStatus.FAILED
    assert {issue.code for issue in report.issues} >= {
        "duplicate_record",
        "source_mismatch",
        "coverage_missing",
    }


def test_empty_stale_and_invalid_values_are_rejected() -> None:
    empty = _validator().validate_daily_metrics(
        [],
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
    )
    stale = _validator().validate_daily_metrics(
        [replace(_daily(), observed_at=NOW - timedelta(days=2))],
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
    )
    invalid = _validator().validate_daily_metrics(
        [
            replace(
                _daily(),
                turnover_rate=Decimal("-0.1"),
                free_shares=Decimal("90000000"),
            )
        ],
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
    )

    assert empty.status is QualityStatus.FAILED
    assert empty.issues[0].code == "empty_dataset"
    assert stale.issues[0].code == "stale_observation"
    assert invalid.issues[0].code == "invalid_daily_value"


def test_daily_share_inversion_between_float_and_free_is_tolerated() -> None:
    """#212:tushare 流通/自由流通口径倒挂(free 略大于 float)不算非法值。

    实测 300863.SZ 2024-01-09:float=33,467,087 < free=33,519,587(倒挂 5.25 万股),
    旧判定(要求 float>=free)会让该日全市场截面整批被拒。仍要求 free 不超过总股本。
    """
    inverted = _validator().validate_daily_metrics(
        [replace(_daily(), free_shares=Decimal("81000000"))],  # float=80,000,000
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
    )
    assert inverted.status is QualityStatus.PASSED

    exceed_total = _validator().validate_daily_metrics(
        [replace(_daily(), free_shares=Decimal("200000000"))],  # total=100,000,000
        expected_trade_date=TRADE_DATE,
        expected_source="tushare",
    )
    assert exceed_total.status is QualityStatus.FAILED
    assert exceed_total.issues[-1].code == "invalid_daily_value"


def test_financial_and_industry_time_contracts_are_checked() -> None:
    financial = _validator().validate_financial_indicators(
        [replace(_financial(), available_at=datetime(2026, 4, 20, 12, tzinfo=UTC))],
        expected_source="tushare",
    )
    industry = _validator().validate_industry_memberships(
        [
            replace(
                _industry(),
                effective_to=date(2020, 1, 1),
            )
        ],
        expected_source="tushare",
    )

    assert financial.status is QualityStatus.FAILED
    assert financial.issues[-1].code == "invalid_financial_time"
    assert industry.status is QualityStatus.FAILED
    assert industry.issues[-1].code == "invalid_industry_membership"
