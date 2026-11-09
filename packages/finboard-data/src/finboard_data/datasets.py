"""全局研究数据目录的 canonical registry(issue #509)。

同一份数据集名单当前散落在 REST schema、发布执行器、MCP help 与 Web 页签;
本模块把它收敛为唯一事实源。目录只描述"数据是什么、从哪里来、落到哪张表、
发布成什么 kind、字段/单位/PIT/日历口径",不持有任何运行状态。

新增数据集 = 新增一个 :class:`CatalogDatasetSpec` 并注册;REST/MCP/Web 从
registry 读取标题、字段与单位,禁止各自硬编码。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from finboard_data.releases import (
    BALANCE_SHEETS_FIELDS,
    CASHFLOW_STATEMENTS_FIELDS,
    CONVERTIBLE_METRICS_FIELDS,
    DAILY_METRICS_FIELDS,
    DIVIDENDS_FIELDS,
    FINANCIAL_INDICATORS_FIELDS,
    INCOME_STATEMENTS_FIELDS,
    RELEASE_FIELDS,
    ReleaseDatasetKind,
)
from finboard_shared.types import AssetClass


class CatalogDataset(StrEnum):
    """目录数据集(与 SyncSpec / ReleaseDatasetKind 对齐的稳定 key)。"""

    BARS = "bars"
    DAILY_METRICS = "daily_metrics"
    FINANCIAL_INDICATORS = "financial_indicators"
    CONVERTIBLE_METRICS = "convertible_metrics"
    INCOME_STATEMENTS = "income_statements"
    BALANCE_SHEETS = "balance_sheets"
    CASHFLOW_STATEMENTS = "cashflow_statements"
    DIVIDENDS = "dividends"
    PROFILES = "profiles"
    SUSPENSIONS = "suspensions"


class CatalogStorage(StrEnum):
    """数据物理落点(决定目录如何计算本地摄取范围)。"""

    #: data_cache parquet(bulk_download / fetch 写入)。
    BARS_CACHE = "bars_cache"
    #: dataset_sync 批次 -> research_* 表 -> 冻结发布。
    RESEARCH_TABLE = "research_table"
    #: 发布时从缓存 + 元数据派生,不落 research 表(convertible_metrics)。
    DERIVED_CACHE = "derived_cache"
    #: 标的档案主数据(instruments / research_instrument_profiles)。
    INSTRUMENT_TABLE = "instrument_table"
    #: 事件表(research_suspensions),只有批次语义,无发布。
    EVENT_TABLE = "event_table"


@dataclass(frozen=True, slots=True)
class CatalogDatasetSpec:
    """一个目录数据集的只读元数据。"""

    key: CatalogDataset
    title: str
    title_en: str
    storage: CatalogStorage
    asset_classes: tuple[AssetClass, ...]
    #: 可冻结发布时的 kind;None = 该数据集没有 release 形态。
    release_kind: ReleaseDatasetKind | None
    #: dataset_sync 注册名;None = 不走 dataset_sync。
    sync_dataset: str | None
    #: 本地落点表;bars / convertible_metrics 为 None。
    table: str | None
    publishable: bool
    #: 期望来源;bars 为 None(多源按 sidecar 记录)。
    source: str | None
    #: 研究的复权口径;None = 非价格数据(固定 none)。
    adjustment: str | None
    pit_rule: str
    calendar_market: str
    fields: tuple[str, ...]
    unit_notes: tuple[str, ...]
    #: 覆盖缺口的分母口径(响应原样返回,前端不自算)。
    gap_denominator: str

    def __post_init__(self) -> None:
        if not self.title or not self.title_en:
            raise ValueError(f"{self.key}: title 不能为空")
        if self.publishable and self.release_kind is None:
            raise ValueError(f"{self.key}: publishable 数据集必须声明 release_kind")
        if self.release_kind is None and self.sync_dataset is None:
            raise ValueError(f"{self.key}: 至少声明 release_kind 或 sync_dataset")
        if not self.fields:
            raise ValueError(f"{self.key}: fields 不能为空")
        if len(self.fields) != len(set(self.fields)):
            raise ValueError(f"{self.key}: fields 不能重复")


_STOCK: tuple[AssetClass, ...] = (AssetClass.EQUITY,)
_ALL: tuple[AssetClass, ...] = (
    AssetClass.EQUITY,
    AssetClass.FIXED_INCOME,
    AssetClass.CONVERTIBLE,
    AssetClass.COMMODITY,
    AssetClass.DERIVATIVE,
)

_DATASETS: tuple[CatalogDatasetSpec, ...] = (
    CatalogDatasetSpec(
        key=CatalogDataset.BARS,
        title="行情 bars",
        title_en="Market bars",
        storage=CatalogStorage.BARS_CACHE,
        asset_classes=_ALL,
        release_kind=ReleaseDatasetKind.BARS,
        sync_dataset=None,
        table=None,
        publishable=True,
        source=None,
        adjustment="qfq",
        pit_rule="T 日收盘后可知;研究运行按 decision_at 逐 Bar 暴露,发布末日 17:00 放行全部日线",
        calendar_market="SSE",
        fields=RELEASE_FIELDS,
        unit_notes=(
            "股票 daily amount 千元",
            "期货 fut_daily amount 万元→元 x10000,vol 手→张",
            "可转债 vol 手→张 x10",
            "指数 amount 千元↔元按源对齐",
        ),
        gap_denominator="expected_sessions(按市场交易日历;停牌不计缺口,单列 suspended_sessions)",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.DAILY_METRICS,
        title="每日指标",
        title_en="Daily metrics",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.DAILY_METRICS,
        sync_dataset="daily_metrics",
        table="research_daily_metrics",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="trade_date 17:00 Asia/Shanghai(T 日收盘后)",
        calendar_market="SSE",
        fields=DAILY_METRICS_FIELDS,
        unit_notes=("市值/股本为原始股数与元;比率为小数比例",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.FINANCIAL_INDICATORS,
        title="财务指标",
        title_en="Financial indicators",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        sync_dataset="financial_indicators",
        table="research_financial_indicators",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = ann_date + 1 日(PIT fail-closed)",
        calendar_market="SSE",
        fields=FINANCIAL_INDICATORS_FIELDS,
        unit_notes=("比率为小数比例;每股指标为元/股",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.CONVERTIBLE_METRICS,
        title="可转债指标",
        title_en="Convertible metrics",
        storage=CatalogStorage.DERIVED_CACHE,
        asset_classes=(AssetClass.CONVERTIBLE,),
        release_kind=ReleaseDatasetKind.CONVERTIBLE_METRICS,
        sync_dataset=None,
        table=None,
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="观察日收盘后;转股价按公告日 PIT,正股 bars 同发布窗口",
        calendar_market="SSE",
        fields=CONVERTIBLE_METRICS_FIELDS,
        unit_notes=("转股价值/溢价率为元与小数比例;vol 手→张 x10",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.INCOME_STATEMENTS,
        title="利润表",
        title_en="Income statements",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.INCOME_STATEMENTS,
        sync_dataset="income_statements",
        table="research_income_statements",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = ann_date + 1 日;同一报告期修订不覆盖",
        calendar_market="SSE",
        fields=INCOME_STATEMENTS_FIELDS,
        unit_notes=("金额为元;比率为小数比例",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.BALANCE_SHEETS,
        title="资产负债表",
        title_en="Balance sheets",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.BALANCE_SHEETS,
        sync_dataset="balance_sheets",
        table="research_balance_sheets",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = ann_date + 1 日;同一报告期修订不覆盖",
        calendar_market="SSE",
        fields=BALANCE_SHEETS_FIELDS,
        unit_notes=("金额为元;比率为小数比例",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.CASHFLOW_STATEMENTS,
        title="现金流量表",
        title_en="Cashflow statements",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.CASHFLOW_STATEMENTS,
        sync_dataset="cashflow_statements",
        table="research_cashflow_statements",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = ann_date + 1 日;同一报告期修订不覆盖",
        calendar_market="SSE",
        fields=CASHFLOW_STATEMENTS_FIELDS,
        unit_notes=("金额为元;比率为小数比例",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.DIVIDENDS,
        title="分红送股",
        title_en="Dividends",
        storage=CatalogStorage.RESEARCH_TABLE,
        asset_classes=_STOCK,
        release_kind=ReleaseDatasetKind.DIVIDENDS,
        sync_dataset="dividends",
        table="research_dividends",
        publishable=True,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = ann_date + 1 日;div_proc 进身份键",
        calendar_market="SSE",
        fields=DIVIDENDS_FIELDS,
        unit_notes=("每股派息为元/股;比例为小数比例",),
        gap_denominator="发布 expected_sessions x 发布标的数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.PROFILES,
        title="标的档案",
        title_en="Instrument profiles",
        storage=CatalogStorage.INSTRUMENT_TABLE,
        asset_classes=_ALL,
        release_kind=None,
        sync_dataset="profiles",
        table="research_instrument_profiles",
        publishable=False,
        source="tushare",
        adjustment=None,
        pit_rule="available_at <= decision_at;上市/退市/更名按生命周期事件 PIT",
        calendar_market="SSE",
        fields=(
            "symbol",
            "name",
            "exchange",
            "market",
            "list_status",
            "list_date",
            "delist_date",
            "industry",
            "observed_at",
            "available_at",
        ),
        unit_notes=("日期为自然日;无价格/数量单位",),
        gap_denominator="instruments 当前快照数",
    ),
    CatalogDatasetSpec(
        key=CatalogDataset.SUSPENSIONS,
        title="停复牌",
        title_en="Suspensions",
        storage=CatalogStorage.EVENT_TABLE,
        asset_classes=_ALL,
        release_kind=None,
        sync_dataset="suspensions",
        table="research_suspensions",
        publishable=False,
        source="tushare",
        adjustment=None,
        pit_rule="available_at = 交易日 09:30 Asia/Shanghai(全天停牌开盘即可见)",
        calendar_market="SSE",
        fields=(
            "symbol",
            "trade_date",
            "suspend_type",
            "suspend_timing",
            "suspend_kind",
            "observed_at",
            "available_at",
        ),
        unit_notes=("事件记录;无数量单位",),
        gap_denominator="批次 accepted_rows / received_rows",
    ),
)

CATALOG_DATASETS: dict[CatalogDataset, CatalogDatasetSpec] = {
    spec.key: spec for spec in _DATASETS
}

_BY_RELEASE_KIND: dict[ReleaseDatasetKind, CatalogDatasetSpec] = {
    spec.release_kind: spec for spec in _DATASETS if spec.release_kind is not None
}
_BY_SYNC_DATASET: dict[str, CatalogDatasetSpec] = {
    spec.sync_dataset: spec for spec in _DATASETS if spec.sync_dataset is not None
}


def catalog_spec(key: CatalogDataset | str) -> CatalogDatasetSpec:
    """按 key 取目录规格;未知 key 抛 ``KeyError``。"""

    dataset = CatalogDataset(str(key))
    return CATALOG_DATASETS[dataset]


def catalog_specs() -> tuple[CatalogDatasetSpec, ...]:
    """全部目录规格(声明序,也是 UI 默认顺序)。"""

    return _DATASETS


def catalog_spec_by_release_kind(kind: ReleaseDatasetKind | str) -> CatalogDatasetSpec:
    """发布 kind -> 目录规格;未登记的 kind 抛 ``KeyError``。"""

    return _BY_RELEASE_KIND[ReleaseDatasetKind(str(kind))]


def catalog_spec_by_sync_dataset(name: str) -> CatalogDatasetSpec:
    """SyncSpec 名 -> 目录规格;未登记抛 ``KeyError``。"""

    return _BY_SYNC_DATASET[name]


def validate_catalog_registry() -> None:
    """启动/测试期自检:key、release kind、sync 名唯一,且发布可落地。"""

    if len(CATALOG_DATASETS) != len(_DATASETS):
        raise ValueError("CatalogDataset key 重复")
    release_kinds = [spec.release_kind for spec in _DATASETS if spec.release_kind]
    if len(release_kinds) != len(set(release_kinds)):
        raise ValueError("release_kind 重复映射")
    sync_names = [spec.sync_dataset for spec in _DATASETS if spec.sync_dataset]
    if len(sync_names) != len(set(sync_names)):
        raise ValueError("sync_dataset 重复映射")
    for spec in _DATASETS:
        if spec.storage is CatalogStorage.RESEARCH_TABLE and spec.table is None:
            raise ValueError(f"{spec.key}: research_table 必须声明 table")
        if spec.release_kind is None and spec.publishable:
            raise ValueError(f"{spec.key}: 无 release_kind 却标记 publishable")


validate_catalog_registry()

__all__ = [
    "CATALOG_DATASETS",
    "CatalogDataset",
    "CatalogDatasetSpec",
    "CatalogStorage",
    "catalog_spec",
    "catalog_spec_by_release_kind",
    "catalog_spec_by_sync_dataset",
    "catalog_specs",
    "validate_catalog_registry",
]
