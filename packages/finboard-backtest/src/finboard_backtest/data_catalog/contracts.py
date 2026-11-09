"""数据目录服务契约(issue #509)。

纯数据对象,不依赖 Web 框架;REST/MCP 各自映射为响应模型/信封。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from finboard_data.datasets import CatalogDataset, CatalogDatasetSpec, CatalogStorage
from finboard_persistence.catalog_repo import (
    BatchHeader,
    PresenceRow,
    ReleaseHeader,
    ReleaseInstrumentRow,
    ReleaseReferenceRow,
)
from finboard_shared.types import AssetClass

STATUS_USABLE = "usable"
STATUS_INGESTED_NOT_PUBLISHED = "ingested_not_published"
STATUS_BLOCKED = "blocked"
STATUS_NOT_INGESTED = "not_ingested"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

CATALOG_STATUSES = (
    STATUS_USABLE,
    STATUS_INGESTED_NOT_PUBLISHED,
    STATUS_BLOCKED,
    STATUS_NOT_INGESTED,
    STATUS_STALE,
    STATUS_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class CatalogQuery:
    dataset: CatalogDataset | None = None
    status: str | None = None
    q: str | None = None
    source: str | None = None
    asset_class: AssetClass | None = None
    start: date | None = None
    end: date | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """目录列表的一行:数据集身份 + 摄取 + 发布 + 状态。"""

    key: CatalogDataset
    title: str
    title_en: str
    storage: CatalogStorage
    publishable: bool
    source: str | None
    status: str
    release: ReleaseHeader | None = None
    batch: BatchHeader | None = None
    coverage_pct: Decimal | None = None
    symbol_count: int | None = None
    row_count: int | None = None
    denominator: str = ""
    local_start: date | None = None
    local_end: date | None = None
    updated_at: datetime | None = None
    cache_files: int = 0
    cache_bytes: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetDetail:
    entry: CatalogEntry
    spec: CatalogDatasetSpec
    releases: tuple[ReleaseHeader, ...] = ()
    batches: tuple[BatchHeader, ...] = ()
    failures: tuple[BatchHeader, ...] = ()
    adjustment: str | None = None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    dataset: CatalogDataset
    denominator: str
    release: ReleaseHeader | None = None
    batch: BatchHeader | None = None
    coverage_pct: Decimal | None = None
    expected_sessions: int = 0
    missing_sessions: int = 0
    symbols_with_gaps: int = 0
    missing_preview: tuple[ReleaseInstrumentRow, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstrumentPage:
    release_id: str
    items: tuple[ReleaseInstrumentRow, ...]
    total: int


@dataclass(frozen=True, slots=True)
class PresencePage:
    code: str
    items: tuple[PresenceRow, ...]
    total: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReferencePage:
    release_id: str
    items: tuple[ReleaseReferenceRow, ...]
    deep_links: dict[str, str] = field(default_factory=dict)


#: 课题数据视图的建议动作码(前端映射文案 + 跳转,服务端不返回格式化文案)。
TOPIC_NEXT_STEP_READY = "ready"
TOPIC_NEXT_STEP_FIX_GAPS = "fix_gaps"
TOPIC_NEXT_STEP_BLOCKED = "blocked"
TOPIC_NEXT_STEP_NO_REFS = "no_refs"

#: 课题缺口原因码(稳定码,前端按码展示;message 仅作人类可读补充)。
TOPIC_GAP_RELEASE_NOT_FOUND = "release_not_found"
TOPIC_GAP_RUN_NOT_FOUND = "run_not_found"
TOPIC_GAP_DATASET_NOT_PUBLISHED = "dataset_not_published"
TOPIC_GAP_DATASET_KIND_UNMAPPED = "dataset_kind_unmapped"
TOPIC_GAP_QUALITY_BLOCKED = "quality_blocked"
TOPIC_GAP_COVERAGE_GAP = "coverage_gap"


@dataclass(frozen=True, slots=True)
class TopicReleaseCoverage:
    """课题引用的一份发布 + 该发布的目录覆盖报告(复用 #509 口径)。"""

    release: ReleaseHeader
    dataset: CatalogDataset
    coverage: CoverageReport


@dataclass(frozen=True, slots=True)
class TopicCoverageSummary:
    """课题数据视图汇总;百分比与分母原样来自服务端,前端不自算。"""

    release_count: int
    dataset_count: int
    min_coverage_pct: Decimal | None
    missing_sessions: int
    symbols_with_gaps: int
    denominators: tuple[str, ...]
    #: ``data_scope``(显式范围)或 ``refs``(轮次引用推导)。
    resolved_by: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopicGap:
    """课题数据范围的具名缺口(不可解析引用 / 质量受阻 / 覆盖不足)。"""

    release_id: str
    dataset: str
    reason: str
    message: str
    coverage_pct: Decimal | None = None
    missing_sessions: int = 0
    symbols_with_gaps: int = 0
    #: 已知的目录数据集 key(仅内部使用:据此补 last_sync 的批次时刻)。
    dataset_key: CatalogDataset | None = None


@dataclass(frozen=True, slots=True)
class TopicCoverageReport:
    """课题数据视图:引用解析后的发布集合 + 覆盖 + 缺口 + 下一步。"""

    topic_id: str
    releases: tuple[TopicReleaseCoverage, ...]
    coverage: TopicCoverageSummary
    gaps: tuple[TopicGap, ...]
    last_sync: datetime | None
    next_step: str


@dataclass(frozen=True, slots=True)
class CalendarView:
    market: str
    start: date
    end: date
    days: tuple[date, ...]
    source: str


class CatalogError(LookupError):
    """目录查询的具名失败(API 映射 404/409/422)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = [
    "CATALOG_STATUSES",
    "STATUS_BLOCKED",
    "STATUS_INGESTED_NOT_PUBLISHED",
    "STATUS_NOT_INGESTED",
    "STATUS_STALE",
    "STATUS_UNKNOWN",
    "STATUS_USABLE",
    "TOPIC_GAP_COVERAGE_GAP",
    "TOPIC_GAP_DATASET_KIND_UNMAPPED",
    "TOPIC_GAP_DATASET_NOT_PUBLISHED",
    "TOPIC_GAP_QUALITY_BLOCKED",
    "TOPIC_GAP_RELEASE_NOT_FOUND",
    "TOPIC_GAP_RUN_NOT_FOUND",
    "TOPIC_NEXT_STEP_BLOCKED",
    "TOPIC_NEXT_STEP_FIX_GAPS",
    "TOPIC_NEXT_STEP_NO_REFS",
    "TOPIC_NEXT_STEP_READY",
    "CalendarView",
    "CatalogEntry",
    "CatalogError",
    "CatalogQuery",
    "CoverageReport",
    "DatasetDetail",
    "InstrumentPage",
    "PresencePage",
    "ReferencePage",
    "TopicCoverageReport",
    "TopicCoverageSummary",
    "TopicGap",
    "TopicReleaseCoverage",
]
