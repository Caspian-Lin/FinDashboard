"""数据集覆盖率审计报告(issue #58 验收)。

扫描本地缓存或一份 ``DatasetManifest``,生成 ``CoverageReport`` 区分:
* 完整历史(从期望起始日到结束日无缺口)
* 新上市短历史(起始日晚于期望起始日)
* 异常缺口(中间有缺口)
* 已退市(终止于期望结束日之前)

质量不合格的数据集 status=FAILED,禁止回测使用。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finboard_shared.instruments import DatasetManifest
from finboard_shared.types import DatasetQualityStatus


class DatasetAuditError(RuntimeError):
    """数据集审计异常。"""


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """单一标的的覆盖情况。"""

    symbol: str
    start_date: date | None
    end_date: date | None
    bar_count: int
    expected_start: date | None
    expected_end: date | None
    gap_count: int = 0
    gap_days: int = 0
    category: str = "unknown"  # full / short_history / gaps / delisted / missing

    @property
    def coverage_ratio(self) -> Decimal:
        """实际交易日 / 期望交易日(粗略,基于日历天数)。"""
        if self.expected_start is None or self.expected_end is None:
            return Decimal("0")
        expected = (self.expected_end - self.expected_start).days + 1
        if expected <= 0:
            return Decimal("0")
        if self.start_date is None or self.end_date is None:
            return Decimal("0")
        actual = (self.end_date - self.start_date).days + 1
        return Decimal(actual) / Decimal(expected)

    @property
    def is_complete(self) -> bool:
        return self.category == "full"


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """整张数据集的覆盖率审计报告。"""

    dataset_name: str
    source: str
    version: str
    entries: tuple[CoverageEntry, ...]
    total_symbols: int
    full_history_count: int
    short_history_count: int
    gaps_count: int
    delisted_count: int
    missing_count: int
    overall_coverage_pct: Decimal
    quality_status: DatasetQualityStatus
    quality_report: dict[str, object] = field(default_factory=dict)
    generated_at: date | None = None

    @property
    def is_usable(self) -> bool:
        """质量门:``passed`` / ``warnings`` 可用,其他拒绝。"""
        return self.quality_status in (
            DatasetQualityStatus.PASSED,
            DatasetQualityStatus.WARNINGS,
        )

    def entries_by_category(self, category: str) -> list[CoverageEntry]:
        return [e for e in self.entries if e.category == category]


def _classify_entry(
    actual_start: date | None,
    actual_end: date | None,
    expected_start: date | None,
    expected_end: date | None,
    gap_count: int,
) -> str:
    """根据起止 / 缺口判定分类。"""
    if actual_start is None or actual_end is None:
        return "missing"
    if gap_count > 0:
        return "gaps"
    # 期望未指定则只看是否缺失
    if expected_start is None and expected_end is None:
        return "full"
    # 起始晚于期望:新上市短历史
    if expected_start is not None and actual_start > expected_start:
        # 若结束也早于期望:可能是退市
        if expected_end is not None and actual_end < expected_end:
            return "delisted"
        return "short_history"
    # 结束早于期望:退市
    if expected_end is not None and actual_end < expected_end:
        return "delisted"
    return "full"


def audit_dataset_coverage(
    manifest: DatasetManifest,
    symbol_records: Sequence[
        tuple[str, date | None, date | None, int]
    ],
    *,
    expected_start: date | None = None,
    expected_end: date | None = None,
    gap_tolerance_days: int = 7,
) -> CoverageReport:
    """根据 manifest + 每个标的的实际起止 / bar 数生成覆盖率报告。

    Parameters
    ----------
    manifest
        数据集发布清单(决定 dataset_name / source / version / quality_status)。
    symbol_records
        每个标的一行:``(symbol, first_date, last_date, bar_count)``。
    expected_start, expected_end
        期望的起止日(用于分类完整 / 短历史 / 退市)。``None`` 表示不校验。
    gap_tolerance_days
        单个缺口允许的最大天数(超过则计入 gap_count)。

    Returns
    -------
    CoverageReport
    """
    entries: list[CoverageEntry] = []
    full_count = 0
    short_count = 0
    gaps_count = 0
    delisted_count = 0
    missing_count = 0
    total_coverage_sum = Decimal("0")

    for symbol, first, last, bar_count in symbol_records:
        # 粗略估计 gap_count:基于日历天数 vs bar_count
        gap_count = 0
        gap_days = 0
        if first is not None and last is not None and bar_count > 0:
            calendar_days = (last - first).days + 1
            # 假设一周 5 个交易日,允许 holidays
            expected_bars = max(calendar_days * 5 // 7, 1)
            missing_bars = max(expected_bars - bar_count, 0)
            if missing_bars > gap_tolerance_days:
                gap_count = 1
                gap_days = missing_bars

        category = _classify_entry(first, last, expected_start, expected_end, gap_count)

        entry = CoverageEntry(
            symbol=symbol,
            start_date=first,
            end_date=last,
            bar_count=bar_count,
            expected_start=expected_start,
            expected_end=expected_end,
            gap_count=gap_count,
            gap_days=gap_days,
            category=category,
        )
        entries.append(entry)
        total_coverage_sum += entry.coverage_ratio

        if category == "full":
            full_count += 1
        elif category == "short_history":
            short_count += 1
        elif category == "gaps":
            gaps_count += 1
        elif category == "delisted":
            delisted_count += 1
        elif category == "missing":
            missing_count += 1

    total = len(symbol_records)
    overall = total_coverage_sum / Decimal(total) if total > 0 else Decimal("0")

    # 质量判定:无缺口 + 覆盖率 >= 0.95 → passed;有缺口或覆盖率 < 0.5 → failed
    quality_status = manifest.quality_status
    if quality_status is DatasetQualityStatus.UNKNOWN:
        if gaps_count == 0 and missing_count == 0 and overall >= Decimal("0.95"):
            quality_status = DatasetQualityStatus.PASSED
        elif gaps_count > 0 or missing_count > total * 0.1:
            quality_status = DatasetQualityStatus.FAILED
        else:
            quality_status = DatasetQualityStatus.WARNINGS

    return CoverageReport(
        dataset_name=manifest.dataset_name,
        source=manifest.source,
        version=manifest.version,
        entries=tuple(entries),
        total_symbols=total,
        full_history_count=full_count,
        short_history_count=short_count,
        gaps_count=gaps_count,
        delisted_count=delisted_count,
        missing_count=missing_count,
        overall_coverage_pct=overall,
        quality_status=quality_status,
        quality_report={
            "expected_start": expected_start.isoformat() if expected_start else None,
            "expected_end": expected_end.isoformat() if expected_end else None,
            "gap_tolerance_days": gap_tolerance_days,
            "manifest_checksum": manifest.checksum,
        },
        generated_at=date.today(),
    )


__all__ = [
    "CoverageEntry",
    "CoverageReport",
    "DatasetAuditError",
    "audit_dataset_coverage",
]
