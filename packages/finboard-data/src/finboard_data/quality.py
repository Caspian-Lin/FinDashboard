"""研究数据质量门。

质量检查是纯函数式边界:不访问数据库或网络,不修补坏数据。任何 ERROR 都会阻止
同步批次发布;覆盖率不足单独标记为 partial,便于调度器决定补拉范围。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any, TypeVar

from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
    IndustryMembership,
    InstrumentProfile,
    SuspensionRecord,
)
from finboard_shared.models import Bar


class QualitySeverity(StrEnum):
    """质量问题严重级别。"""

    ERROR = "error"
    WARNING = "warning"


class QualityStatus(StrEnum):
    """批次质量门结果。"""

    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """一类可聚合的质量问题。"""

    code: str
    severity: QualitySeverity
    message: str
    count: int = 1

    def as_dict(self) -> dict[str, object]:
        """转换为可写入 JSON 的结构。"""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """一次质量门判定结果。"""

    status: QualityStatus
    row_count: int
    expected_count: int | None
    issues: tuple[QualityIssue, ...]

    @property
    def publishable(self) -> bool:
        """只有完全通过的数据集才可发布。"""
        return self.status is QualityStatus.PASSED

    def as_dict(self) -> dict[str, object]:
        """转换为可写入 JSON 的结构。"""
        return {
            "status": self.status.value,
            "row_count": self.row_count,
            "expected_count": self.expected_count,
            "issues": [issue.as_dict() for issue in self.issues],
        }


_Record = TypeVar(
    "_Record",
    InstrumentProfile,
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    SuspensionRecord,
)


class ResearchDataQualityValidator:
    """覆盖率、重复、单位、时间、陈旧度和异常值检查。"""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        max_observation_age: timedelta = timedelta(days=1),
    ) -> None:
        self._now = now or datetime.now(UTC)
        if self._now.tzinfo is None or self._now.utcoffset() is None:
            raise ValueError("quality validator 的 now 必须带时区")
        if max_observation_age < timedelta(0):
            raise ValueError("max_observation_age 不能为负数")
        self._max_observation_age = max_observation_age

    def validate_instrument_profiles(
        self,
        records: list[InstrumentProfile],
        *,
        expected_source: str,
        expected_symbols: set[str] | None = None,
    ) -> QualityReport:
        """校验标的档案。"""
        issues = self._common_issues(
            records,
            keys=[(item.source, item.symbol) for item in records],
            expected_source=expected_source,
            expected_symbols=expected_symbols,
        )
        invalid_ranges = sum(
            item.delist_date is not None and item.delist_date < item.list_date for item in records
        )
        if invalid_ranges:
            issues.append(
                QualityIssue(
                    code="invalid_listing_range",
                    severity=QualitySeverity.ERROR,
                    message="退市日期早于上市日期",
                    count=invalid_ranges,
                )
            )
        return _report(records, expected_symbols, issues)

    def validate_daily_metrics(
        self,
        records: list[DailySecurityMetrics],
        *,
        expected_trade_date: date,
        expected_source: str,
        expected_symbols: set[str] | None = None,
    ) -> QualityReport:
        """校验单日全市场指标。"""
        issues = self._common_issues(
            records,
            keys=[(item.source, item.symbol, item.trade_date) for item in records],
            expected_source=expected_source,
            expected_symbols=expected_symbols,
        )
        wrong_dates = sum(item.trade_date != expected_trade_date for item in records)
        if wrong_dates:
            issues.append(
                QualityIssue(
                    code="business_date_mismatch",
                    severity=QualitySeverity.ERROR,
                    message="记录交易日与批次参数不一致",
                    count=wrong_dates,
                )
            )
        invalid_values = sum(_daily_has_invalid_value(item) for item in records)
        if invalid_values:
            issues.append(
                QualityIssue(
                    code="invalid_daily_value",
                    severity=QualitySeverity.ERROR,
                    message="价格、换手率、股本、市值或涨跌状态超出合法范围",
                    count=invalid_values,
                )
            )
        return _report(records, expected_symbols, issues)

    def validate_financial_indicators(
        self,
        records: list[FinancialIndicator],
        *,
        expected_source: str,
    ) -> QualityReport:
        """校验财务修订记录。"""
        issues = self._common_issues(
            records,
            keys=[
                (
                    item.source,
                    item.symbol,
                    item.report_period,
                    item.announcement_date,
                    item.update_flag or "",
                )
                for item in records
            ],
            expected_source=expected_source,
            expected_symbols=None,
        )
        invalid_times = sum(
            item.report_period > item.announcement_date
            or item.available_at.date() <= item.announcement_date
            for item in records
        )
        if invalid_times:
            issues.append(
                QualityIssue(
                    code="invalid_financial_time",
                    severity=QualitySeverity.ERROR,
                    message="报告期或 available_at 与公告日期矛盾",
                    count=invalid_times,
                )
            )
        return _report(records, None, issues)

    def validate_income_statements(
        self,
        records: list[IncomeStatement],
        *,
        expected_source: str,
    ) -> QualityReport:
        """校验利润表公告修订(issue #397,语义同 validate_financial_indicators)。"""
        return self._validate_announced_statements(
            records,
            expected_source=expected_source,
            keys=[
                (
                    item.source,
                    item.symbol,
                    item.report_period,
                    item.announcement_date,
                    item.update_flag or "",
                    item.report_type or "",
                    item.comp_type or "",
                )
                for item in records
            ],
        )

    def validate_balance_sheets(
        self,
        records: list[BalanceSheet],
        *,
        expected_source: str,
    ) -> QualityReport:
        """校验资产负债表公告修订(issue #397)。"""
        return self._validate_announced_statements(
            records,
            expected_source=expected_source,
            keys=[
                (
                    item.source,
                    item.symbol,
                    item.report_period,
                    item.announcement_date,
                    item.update_flag or "",
                    item.report_type or "",
                    item.comp_type or "",
                )
                for item in records
            ],
        )

    def validate_cashflow_statements(
        self,
        records: list[CashflowStatement],
        *,
        expected_source: str,
    ) -> QualityReport:
        """校验现金流量表公告修订(issue #397)。"""
        return self._validate_announced_statements(
            records,
            expected_source=expected_source,
            keys=[
                (
                    item.source,
                    item.symbol,
                    item.report_period,
                    item.announcement_date,
                    item.update_flag or "",
                    item.report_type or "",
                    item.comp_type or "",
                )
                for item in records
            ],
        )

    def validate_dividends(
        self,
        records: list[DividendRecord],
        *,
        expected_source: str,
    ) -> QualityReport:
        """校验分红送股进展记录(issue #397;身份键含 div_proc)。"""
        return self._validate_announced_statements(
            records,
            expected_source=expected_source,
            keys=[
                (
                    item.source,
                    item.symbol,
                    item.report_period,
                    item.announcement_date,
                    item.div_proc or "",
                )
                for item in records
            ],
        )

    def _validate_announced_statements(
        self,
        records: list[Any],
        *,
        expected_source: str,
        keys: list[tuple[object, ...]],
    ) -> QualityReport:
        """公告类研究数据(三表/dividend)共享的批次质量门。

        与 ``validate_financial_indicators`` 同语义:重复业务键 / 报告期晚于
        公告日 / available_at 不晚于公告日(PIT=ann_date+1 被破坏)均为
        ERROR;值字段稀疏不是质量问题(#187 all_null_fields 同精神)。
        """
        issues = self._common_issues(
            records,
            keys=keys,
            expected_source=expected_source,
            expected_symbols=None,
        )
        invalid_times = sum(
            item.report_period > item.announcement_date
            or item.available_at.date() <= item.announcement_date
            for item in records
        )
        if invalid_times:
            issues.append(
                QualityIssue(
                    code="invalid_financial_time",
                    severity=QualitySeverity.ERROR,
                    message="报告期或 available_at 与公告日期矛盾",
                    count=invalid_times,
                )
            )
        return _report(records, None, issues)

    def validate_industry_memberships(
        self,
        records: list[IndustryMembership],
        *,
        expected_source: str,
        expected_symbols: set[str] | None = None,
    ) -> QualityReport:
        """校验行业成员有效区间与层级字段。"""
        issues = self._common_issues(
            records,
            keys=[
                (
                    item.source,
                    item.taxonomy,
                    item.symbol,
                    item.level3_code,
                    item.effective_from,
                )
                for item in records
            ],
            expected_source=expected_source,
            expected_symbols=expected_symbols,
        )
        invalid_ranges = sum(
            (item.effective_to is not None and item.effective_to < item.effective_from)
            or not all(
                (
                    item.taxonomy,
                    item.level1_code,
                    item.level1_name,
                    item.level2_code,
                    item.level2_name,
                    item.level3_code,
                    item.level3_name,
                )
            )
            for item in records
        )
        if invalid_ranges:
            issues.append(
                QualityIssue(
                    code="invalid_industry_membership",
                    severity=QualitySeverity.ERROR,
                    message="行业层级为空或成员有效区间无效",
                    count=invalid_ranges,
                )
            )
        return _report(records, expected_symbols, issues)

    def validate_suspensions(
        self,
        records: list[SuspensionRecord],
        *,
        expected_trade_date: date,
        expected_source: str,
        expected_symbols: set[str] | None = None,
    ) -> QualityReport:
        """校验单日全市场停复牌枚举(issue #396)。"""
        issues = self._common_issues(
            records,
            keys=[(item.source, item.symbol, item.trade_date) for item in records],
            expected_source=expected_source,
            expected_symbols=expected_symbols,
        )
        wrong_dates = sum(item.trade_date != expected_trade_date for item in records)
        if wrong_dates:
            issues.append(
                QualityIssue(
                    code="business_date_mismatch",
                    severity=QualitySeverity.ERROR,
                    message="记录交易日与批次参数不一致",
                    count=wrong_dates,
                )
            )
        invalid_kinds = sum(
            item.suspend_kind
            not in {
                "suspension_day",
                "intraday_suspension",
                "resumption",
            }
            or (item.suspend_type == "S" and item.suspend_kind == "resumption")
            or (item.suspend_type == "R" and item.suspend_kind != "resumption")
            for item in records
        )
        if invalid_kinds:
            issues.append(
                QualityIssue(
                    code="invalid_suspend_kind",
                    severity=QualitySeverity.ERROR,
                    message="停复牌 kind 与 suspend_type 矛盾",
                    count=invalid_kinds,
                )
            )
        invalid_visibility = sum(
            item.available_at < datetime.combine(
                item.trade_date, datetime.min.time(), tzinfo=item.available_at.tzinfo
            )
            for item in records
        )
        if invalid_visibility:
            issues.append(
                QualityIssue(
                    code="invalid_available_at",
                    severity=QualitySeverity.ERROR,
                    message="available_at 早于交易日(PIT=当日语义被破坏)",
                    count=invalid_visibility,
                )
            )
        return _report(records, expected_symbols, issues)

    def _common_issues(
        self,
        records: list[_Record],
        *,
        keys: list[tuple[object, ...]],
        expected_source: str,
        expected_symbols: set[str] | None,
    ) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        if not records:
            issues.append(
                QualityIssue(
                    code="empty_dataset",
                    severity=QualitySeverity.ERROR,
                    message="批次没有可发布的记录",
                )
            )

        source_mismatches = sum(item.source != expected_source for item in records)
        if source_mismatches:
            issues.append(
                QualityIssue(
                    code="source_mismatch",
                    severity=QualitySeverity.ERROR,
                    message="记录来源与同步批次来源不一致",
                    count=source_mismatches,
                )
            )

        duplicate_count = len(keys) - len(set(keys))
        if duplicate_count:
            issues.append(
                QualityIssue(
                    code="duplicate_record",
                    severity=QualitySeverity.ERROR,
                    message="批次内存在重复业务键",
                    count=duplicate_count,
                )
            )

        naive_times = sum(
            _is_naive(item.observed_at) or _is_naive(item.available_at) for item in records
        )
        if naive_times:
            issues.append(
                QualityIssue(
                    code="naive_timestamp",
                    severity=QualitySeverity.ERROR,
                    message="observed_at/available_at 必须带时区",
                    count=naive_times,
                )
            )

        stale_count = sum(
            not _is_naive(item.observed_at)
            and item.observed_at < self._now - self._max_observation_age
            for item in records
        )
        if stale_count:
            issues.append(
                QualityIssue(
                    code="stale_observation",
                    severity=QualitySeverity.ERROR,
                    message="数据观察时间超过允许陈旧度",
                    count=stale_count,
                )
            )

        if expected_symbols is not None:
            actual_symbols = {item.symbol for item in records}
            missing = expected_symbols - actual_symbols
            extras = actual_symbols - expected_symbols
            if missing:
                issues.append(
                    QualityIssue(
                        code="coverage_missing",
                        severity=QualitySeverity.ERROR,
                        message="批次缺少预期标的",
                        count=len(missing),
                    )
                )
            if extras:
                issues.append(
                    QualityIssue(
                        code="coverage_unexpected",
                        severity=QualitySeverity.ERROR,
                        message="批次包含预期范围外标的",
                        count=len(extras),
                    )
                )
        return issues


def _daily_has_invalid_value(item: DailySecurityMetrics) -> bool:
    non_negative = (
        item.turnover_rate,
        item.turnover_rate_free,
        item.volume_ratio,
        item.dividend_yield,
        item.dividend_yield_ttm,
        item.total_shares,
        item.float_shares,
        item.free_shares,
        item.total_market_cap,
        item.circulating_market_cap,
    )
    return (
        (item.close is not None and item.close <= 0)
        or any(value is not None and value < 0 for value in non_negative)
        # tushare 口径:总/流通/自由流通股本来自不同上游表,解禁过渡期偶发小幅
        # 倒挂(issue #212 实测 300863.SZ float<free 0.16%、603882.SH 2020-09~10
        # total<float 0.35% 且持续一个月),严格链式不变式会连坐拒收整日全市场
        # 截面;保留链式检查但允许 2% 相对倒挂,大幅倒挂仍按字段装配错误拦截。
        or _descending_values_invalid(item.total_shares, item.float_shares, item.free_shares)
        or _descending_values_invalid(item.total_market_cap, item.circulating_market_cap)
        or (item.limit_status is not None and not 0 <= item.limit_status <= 6)
    )


def _descending_values_invalid(*values: Decimal | None) -> bool:
    tolerance = Decimal("0.02")
    present = [value for value in values if value is not None]
    return any(
        left < right and (right - left) > tolerance * right
        for left, right in pairwise(present)
    )


def _is_naive(value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None


def _report(
    records: Sequence[object],
    expected_symbols: set[str] | None,
    issues: list[QualityIssue],
) -> QualityReport:
    status = QualityStatus.PASSED
    non_coverage_errors = any(
        issue.severity is QualitySeverity.ERROR and not issue.code.startswith("coverage_")
        for issue in issues
    )
    if non_coverage_errors:
        status = QualityStatus.FAILED
    elif any(issue.code.startswith("coverage_") for issue in issues):
        status = QualityStatus.PARTIAL
    return QualityReport(
        status=status,
        row_count=len(records),
        expected_count=len(expected_symbols) if expected_symbols is not None else None,
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# Bar-level quality (OHLCV) — used at both ingestion and release time
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarAnomaly:
    """单根 bar 的异常详情。"""

    date: date
    source: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BarQualityResult:
    """单标的 bar 质量检查结果。"""

    symbol: str
    total_bars: int
    anomaly_dates: tuple[date, ...]
    anomalies: tuple[BarAnomaly, ...]
    duplicate_count: int
    sources: tuple[str, ...]

    @property
    def anomaly_count(self) -> int:
        """不包括重复的异常 bar 数量。"""
        return len(self.anomalies)

    @property
    def anomaly_ratio(self) -> Decimal:
        if self.total_bars == 0:
            return Decimal("0")
        return Decimal(len(self.anomalies)) / Decimal(self.total_bars)

    @property
    def passed(self) -> bool:
        """通过质量门 = 零异常 + 零重复。"""
        return self.anomaly_count == 0 and self.duplicate_count == 0


class BarQualityChecker:
    """Bar 级别质量检查器。

    在拉取后立即运行,拦截 OHLCV 异常、重复时间戳、NaN/负值等问题。
    覆盖率/生命周期相关的检查由 release 层在发布时补充。
    """

    OHLC_TOLERANCE = Decimal("0.01")

    def check(self, bars: Sequence[Bar], *, symbol: str = "") -> BarQualityResult:
        """检查 bar 列表,返回异常详情。"""
        seen: set[date] = set()
        duplicates = 0
        anomalies: list[BarAnomaly] = []
        sources: set[str] = set()

        for bar in bars:
            bar_date = bar.timestamp.date()
            if bar_date in seen:
                duplicates += 1
            else:
                seen.add(bar_date)
            if bar.source:
                sources.add(bar.source)

            reasons = self._check_bar(bar)
            if reasons:
                anomalies.append(
                    BarAnomaly(date=bar_date, source=bar.source, reasons=tuple(reasons))
                )

        return BarQualityResult(
            symbol=symbol,
            total_bars=len(bars),
            anomaly_dates=tuple(a.date for a in anomalies),
            anomalies=tuple(anomalies),
            duplicate_count=duplicates,
            sources=tuple(sorted(sources)),
        )

    @classmethod
    def _check_bar(cls, bar: Bar) -> list[str]:
        """返回单根 bar 的异常原因列表(空列表 = 正常)。"""
        if bar.volume == 0 and bar.open == bar.high == bar.low == bar.close:
            return []

        reasons: list[str] = []
        tol = cls.OHLC_TOLERANCE
        upper = Decimal("1") + tol
        lower = Decimal("1") - tol

        for name, val in (("open", bar.open), ("high", bar.high),
                          ("low", bar.low), ("close", bar.close)):
            if val.is_nan():
                reasons.append(f"{name}_nan")
            elif val <= 0:
                reasons.append(f"{name}_non_positive")

        if not any(r.endswith("_nan") for r in reasons):
            o, h, low, c = bar.open, bar.high, bar.low, bar.close
            if h < max(o, low, c) * lower:
                reasons.append("high_lt_ohlc")
            if low > min(o, h, c) * upper:
                reasons.append("low_gt_ohlc")

        if bar.volume < 0:
            reasons.append("neg_volume")
        if bar.amount < 0:
            reasons.append("neg_amount")

        return reasons
