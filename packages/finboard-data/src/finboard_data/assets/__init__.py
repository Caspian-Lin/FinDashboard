"""多资产研究数据契约(issue #58)。

资产类型化元数据 + Provider Protocol + fail-closed 注册表。
本子包**不**实现具体的 akshare / tushare 抓取逻辑,只定义稳定契约。
"""

from finboard_data.assets.classifier import (
    ETF_CLASSIFIER_VERSION,
    EtfClassification,
    EtfClassifier,
    EtfRawFacts,
    classify_etf,
)
from finboard_data.assets.continuous import (
    ContinuousFuturesBuildError,
    build_continuous_series,
)
from finboard_data.assets.etf_sync import (
    AkShareEtfMetadataSource,
    EtfMetadataSource,
    EtfMetadataSync,
)
from finboard_data.assets.provider import (
    InstrumentMetadataProvider,
    InstrumentMetadataProviderError,
)
from finboard_data.assets.registry import (
    InstrumentRegistry,
    InstrumentResolutionError,
    resolve_market_by_code,
)
from finboard_data.assets.report import (
    CoverageEntry,
    CoverageReport,
    DatasetAuditError,
    audit_dataset_coverage,
)

__all__ = [
    "ETF_CLASSIFIER_VERSION",
    "AkShareEtfMetadataSource",
    "ContinuousFuturesBuildError",
    "CoverageEntry",
    "CoverageReport",
    "DatasetAuditError",
    "EtfClassification",
    "EtfClassifier",
    "EtfMetadataSource",
    "EtfMetadataSync",
    "EtfRawFacts",
    "InstrumentMetadataProvider",
    "InstrumentMetadataProviderError",
    "InstrumentRegistry",
    "InstrumentResolutionError",
    "audit_dataset_coverage",
    "build_continuous_series",
    "classify_etf",
    "resolve_market_by_code",
]
