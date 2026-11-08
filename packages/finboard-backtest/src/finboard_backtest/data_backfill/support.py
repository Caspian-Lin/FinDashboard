"""补数支持清单与运行时能力探测(issue #511,方案 5.1)。

预检只回答「平台能不能补这个数据集」,依据两处**唯一事实源**:

* 静态支持清单 —— 由 #509 的目录注册表 :mod:`finboard_data.datasets`
  推导(数据集 key / 落点 / 可发布 / release kind / sync 名 / 默认来源),
  本模块只补两条目录不表达的执行事实:走哪个内核(``bulk_download`` /
  ``dataset_sync`` / 派生)与标的域限制;
* 运行时能力 —— tushare token、请求预算快照、缓存目录与发布根目录可写、
  市场支持。缺一项即该数据集 ``blocked`` + 具名 ``reason``,不静默跳过。

为什么不在预检里连上游:预检要可被课题/Agent 反复调用且**纯读**;真实配额
消耗由执行器在 ``retry_waiting`` 语义里兜底(与 ``dataset_sync`` 一致)。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from finboard_data.datasets import (
    CatalogDataset,
    CatalogDatasetSpec,
    CatalogStorage,
    catalog_specs,
)
from finboard_data.releases import ReleaseDatasetKind

#: 目录落点 → 补数执行内核。
#: * ``BARS_CACHE`` —— 行情缓存,走 ``bulk_download``(+ 可选 ``quality_repair``);
#: * ``RESEARCH_TABLE`` / ``INSTRUMENT_TABLE`` / ``EVENT_TABLE`` —— 走
#:   ``dataset_sync``(SyncSpec 注册表按 ``sync_dataset`` 取规格);
#: * ``DERIVED_CACHE`` —— 派生数据集(convertible_metrics),不可单独同步,
#:   只能沿用既有 bars 与条款元数据后发布。
_SYNC_MODE_BY_STORAGE: dict[CatalogStorage, str] = {
    CatalogStorage.BARS_CACHE: "bulk_download",
    CatalogStorage.RESEARCH_TABLE: "dataset_sync",
    CatalogStorage.INSTRUMENT_TABLE: "dataset_sync",
    CatalogStorage.EVENT_TABLE: "dataset_sync",
    CatalogStorage.DERIVED_CACHE: "derived",
}

#: 研究数据发布执行器的 A 股股票 scope 门(与 ``DatasetPublishExecutor``
#: 的 ``_RESEARCH_STOCK_KINDS`` 同口径):这些 kind 的发布只接受 A 股股票。
_A_SHARE_STOCK_RELEASE_KINDS: frozenset[str] = frozenset(
    {
        ReleaseDatasetKind.DAILY_METRICS.value,
        ReleaseDatasetKind.FINANCIAL_INDICATORS.value,
        ReleaseDatasetKind.INCOME_STATEMENTS.value,
        ReleaseDatasetKind.BALANCE_SHEETS.value,
        ReleaseDatasetKind.CASHFLOW_STATEMENTS.value,
        ReleaseDatasetKind.DIVIDENDS.value,
    }
)


@dataclass(frozen=True, slots=True)
class BackfillSupport:
    """一个数据集的可补数能力(静态部分)。"""

    dataset: CatalogDataset
    title: str
    #: bulk_download / dataset_sync / derived
    sync_mode: str
    #: ``dataset_sync`` 的 SyncSpec 名;非 dataset_sync 为 None。
    sync_dataset: str | None
    #: 可发布时的 payload ``release_kind``(与发布执行器白名单一致);None=不可发布。
    release_kind: str | None
    publishable: bool
    #: 默认来源;bars 由 provider 解析(此处记 "tushare" 作为 fingerprint 默认值)。
    source: str
    #: 发布时的固定复权口径;非价格数据为 "none"。
    adjustment: str
    #: 发布只接受 A 股股票(研究类发布的 scope 门)。
    requires_a_share_stock: bool
    #: 支持的市场(``a_share`` / ``future``);研究类数据集只有 a_share。
    markets: tuple[str, ...]
    note: str

    @property
    def syncable(self) -> bool:
        """能否从上游同步(派生数据集为 False,只能发布)。"""

        return self.sync_mode != "derived"

    @property
    def has_sync_dataset(self) -> bool:
        return self.sync_mode == "dataset_sync" and self.sync_dataset is not None


def _support_from_spec(spec: CatalogDatasetSpec) -> BackfillSupport:
    mode = _SYNC_MODE_BY_STORAGE[spec.storage]
    release_kind: str | None = None
    if spec.publishable:
        if spec.key is CatalogDataset.BARS:
            # bars 主发布唯一(AGENTS 不变量):backfill 用多资产混合发布,
            # 指数/期货基准与候选池同处一份发布;DB 里 dataset_kind 仍是 bars。
            release_kind = "multi_asset_mixed"
        elif spec.release_kind is not None:
            release_kind = spec.release_kind.value
    requires_stock = release_kind in _A_SHARE_STOCK_RELEASE_KINDS
    markets = ("a_share",) if mode == "dataset_sync" else ("a_share", "future")
    return BackfillSupport(
        dataset=spec.key,
        title=spec.title,
        sync_mode=mode,
        sync_dataset=spec.sync_dataset,
        release_kind=release_kind,
        publishable=spec.publishable,
        source=spec.source or "tushare",
        adjustment=spec.adjustment or "none",
        requires_a_share_stock=requires_stock,
        markets=markets,
        note=_notes_for(spec),
    )


def _notes_for(spec: CatalogDatasetSpec) -> str:
    if spec.key is CatalogDataset.BARS:
        return "缓存同步(bulk_download);质量修复可选,质量门在发布执行器"
    if spec.key is CatalogDataset.CONVERTIBLE_METRICS:
        return "由 bars 与转债条款派生,不可单独同步;需先补齐转债与正股 bars"
    if not spec.publishable:
        return "主数据/事件数据:只补同步,不产生冻结发布"
    return f"dataset_sync 摄取后按 {spec.release_kind.value if spec.release_kind else '-'} 发布"


#: 数据集 → 支持清单(声明序 = 目录顺序,预检与执行器共用同一张表)。
BACKFILL_SUPPORT: dict[CatalogDataset, BackfillSupport] = {
    spec.key: _support_from_spec(spec) for spec in catalog_specs()
}


def support_for(dataset: CatalogDataset | str) -> BackfillSupport:
    """按目录 key 取支持清单;未知 key 抛 ``KeyError``。"""

    return BACKFILL_SUPPORT[CatalogDataset(str(dataset))]


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """配额快照(只取预检需要的字段,避免把预算实现细节泄进契约)。"""

    remaining_today: int
    requests_per_minute: int
    daily_limit: int


@dataclass(frozen=True, slots=True)
class BackfillCapabilities:
    """运行时能力探测结果(缺能力 → 对应数据集 blocked)。"""

    tushare_token: bool
    cache_dir_writable: bool
    release_root_writable: bool
    supported_markets: tuple[str, ...] = ("a_share", "future")
    budget: BudgetSnapshot | None = None
    provider: str | None = None

    @property
    def can_sync(self) -> bool:
        """能否向上游发起同步(缺 token / 缓存不可写 → 只能复用已覆盖部分)。"""

        return self.tushare_token and self.cache_dir_writable

    @property
    def can_publish(self) -> bool:
        return self.release_root_writable


def _writable(path: Path) -> bool:
    """目录可写探测(纯读:不创建目录,只逐级回退到最近的已存在目录)。"""

    current = path
    while True:
        if current.exists():
            return current.is_dir() and os.access(current, os.W_OK)
        if current.parent == current:
            return False
        current = current.parent


def _default_settings_factory() -> object | None:
    """延迟读取应用配置(backtest 不反向依赖 finboard-app,失败即视为未配置)。"""

    try:
        from finboard_app.config import load_settings
    except Exception:  # pragma: no cover - 仅 worker/api 进程装了 app 包
        return None
    try:
        return load_settings()
    except Exception:  # pragma: no cover - 配置非法时不阻塞预检
        return None


async def probe_capabilities(
    *,
    settings_factory: Callable[[], object | None] | None = None,
    cache_dir: Path | None = None,
    release_root: Path | None = None,
    budget: object | None = None,
) -> BackfillCapabilities:
    """探测运行时能力(不触网、不占用配额)。

    ``budget`` 可注入任意带 ``async snapshot() -> TushareBudgetSnapshot`` 的
    对象(测试替身);缺省按 settings 构造进程内共享预算并读一次用量文件。
    """

    settings = (settings_factory or _default_settings_factory)()
    token = getattr(settings, "tushare_token", None) if settings is not None else None
    cache_path = cache_dir or Path(
        os.getenv("FINBOARD_DATA_CACHE_DIR", "data_cache")
    )
    release_path = release_root or Path(
        os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
    )
    snapshot = await _budget_snapshot(settings, budget)
    return BackfillCapabilities(
        tushare_token=bool(token),
        cache_dir_writable=_writable(cache_path),
        release_root_writable=_writable(release_path),
        budget=snapshot,
        provider=getattr(settings, "data_provider", None) if settings is not None else None,
    )


async def _budget_snapshot(
    settings: object | None, budget: object | None
) -> BudgetSnapshot | None:
    from finboard_data.tushare_budget import TushareRequestBudget

    reader = budget
    if reader is None:
        if settings is None:
            return None
        reader = TushareRequestBudget(
            requests_per_minute=int(
                getattr(settings, "tushare_requests_per_minute", 200) or 200
            ),
            daily_request_limit=int(
                getattr(settings, "tushare_daily_request_limit", 100_000) or 100_000
            ),
            usage_file=getattr(
                settings, "tushare_usage_file", "data_cache/tushare_usage.json"
            )
            or "data_cache/tushare_usage.json",
        )
    snapshot = await reader.snapshot()  # type: ignore[attr-defined]
    return BudgetSnapshot(
        remaining_today=int(snapshot.remaining),
        requests_per_minute=int(snapshot.requests_per_minute),
        daily_limit=int(snapshot.daily_limit),
    )


__all__ = [
    "BACKFILL_SUPPORT",
    "BackfillCapabilities",
    "BackfillSupport",
    "BudgetSnapshot",
    "probe_capabilities",
    "support_for",
]
