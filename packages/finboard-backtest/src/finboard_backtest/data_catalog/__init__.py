"""全局数据目录(issue #509)。"""

from finboard_backtest.data_catalog.contracts import (
    CalendarView,
    CatalogEntry,
    CatalogError,
    CatalogQuery,
    CoverageReport,
    DatasetDetail,
    InstrumentPage,
    PresencePage,
    ReferencePage,
    TopicCoverageReport,
    TopicCoverageSummary,
    TopicGap,
    TopicReleaseCoverage,
)
from finboard_backtest.data_catalog.service import DataCatalogService

__all__ = [
    "CalendarView",
    "CatalogEntry",
    "CatalogError",
    "CatalogQuery",
    "CoverageReport",
    "DataCatalogService",
    "DatasetDetail",
    "InstrumentPage",
    "PresencePage",
    "ReferencePage",
    "TopicCoverageReport",
    "TopicCoverageSummary",
    "TopicGap",
    "TopicReleaseCoverage",
]
