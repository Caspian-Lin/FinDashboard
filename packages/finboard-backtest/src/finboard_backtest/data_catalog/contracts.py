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
    "CalendarView",
    "CatalogEntry",
    "CatalogError",
    "CatalogQuery",
    "CoverageReport",
    "DatasetDetail",
    "InstrumentPage",
    "PresencePage",
    "ReferencePage",
]
