"""补数预检 —— 纯读的缺口计划(issue #511,方案 5.2)。

输入一份 :class:`BackfillRequestSpec`,输出 :class:`BackfillPlan`:

1. **窗口**:复用 #510 的交易日窗口(``trade_cal`` 并集 + 预热/尾部),显式
   ``start/end`` 必须落在窗口内 —— 补数不能顺手扩大课题范围;
2. **已完成覆盖**:
   * 可发布数据集以 ``research_dataset_releases`` 的**可用发布**为准(逐标的
     派生索引 + 区间,与 #509 目录同一读模型);
   * 不可发布数据集以 ``research_sync_batches`` 的已发布批次为准;
3. **在途覆盖**:``data_backfill_requests`` 中 ``queued|running`` 且 item 仍
   ``pending|running``、dataset / source / adjustment 一致、区间重叠且标的是
   请求标的超集的项;
4. 缺口按**同步单元**切(逐标的 / 窗口级,见下),``missing_ranges`` 冻结进
   item,执行器**只补缺口区间**,不重跑整窗;
5. ``action`` 判定(含 ``missing_symbols`` 触发的补数)/ ``estimated_requests``
   / 配额 / 发布影响 / blockers。

**缺口口径**:

* ``PER_SYMBOL_RANGE``(研究表 / 主数据 / 事件)与 bars(``bulk_download``)的
  同步单元是「标的 x 区间」:缺口 = 请求标的的逐标区间并集,某标的整段缺失
  不会被别的标的覆盖掩盖;
* ``DAILY_MARKET`` / ``FULL_PAGED`` 的同步单元是「交易日 / 整市场」:区间缺口
  按窗口级发布覆盖算;但请求标的里有未覆盖标的时仍必须补(否则发布面缺标的),
  故 ``missing_symbols`` 同样触发补数。

**标的冻结**(#511 审核 B2):逐标的 / bars item 把解析后的标的域冻结进
``symbols``(<= :data:`_MAX_SYMBOL_FILTER`)或降级为大宇宙过滤器
``universe_filters``;执行器按冻结值下传与发布,**发布面不得宽于请求面**。
超过上限且过滤器无法表达(多市场 / 多类型)时具名 blocker,不静默放宽。

**异源与不同复权不混接**:``source`` 与 ``adjustment`` 是覆盖匹配的必要条件;
不匹配的既有覆盖不参与复用,item 带 ``source_conflict`` 类 warning 单独成项。
bars 缓存只写 ``qfq``(``bulk_download`` 契约无 adjust 维度),非 qfq 请求在
预检即 fail-closed。

纯读边界:本模块不改任何表(不写发布、不建批次、不登记请求);发布是提交后
执行器的事。质量异常的判定依据是**已发布读模型里逐标的质量计数**,不重新
扫描 parquet 缓存 —— 全市场预检必须能在秒级完成。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.background_jobs.dataset_sync.spec import (
    SYNC_SPECS,
    EnumShape,
    workdays,
)
from finboard_backtest.data_backfill.contracts import (
    INFLIGHT_REQUEST_STATUSES,
    SYMBOL_PREVIEW_LIMIT,
    BackfillAction,
    BackfillBlocker,
    BackfillCoverage,
    BackfillDatasetSpec,
    BackfillGapSymbols,
    BackfillItem,
    BackfillOverlap,
    BackfillPlan,
    BackfillPlanError,
    BackfillPublishImpact,
    BackfillPublishPreview,
    BackfillQuota,
    BackfillRequestSpec,
    DateRange,
    request_fingerprint,
    utcnow,
)
from finboard_backtest.data_backfill.support import (
    BackfillCapabilities,
    BackfillSupport,
    probe_capabilities,
    support_for,
)
from finboard_backtest.data_scope.calendar import (
    ScopeCalendarError,
    build_window,
    load_union_calendar,
    markets_for,
)
from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeWindow,
    scope_checksum,
)
from finboard_data.datasets import CatalogDataset
from finboard_data.releases import ReleaseDatasetKind
from finboard_persistence.catalog_repo import CatalogRepository
from finboard_persistence.data_backfill_repo import DataBackfillRepository
from finboard_persistence.models import (
    InstrumentModel,
    ResearchDatasetReleaseInstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchSyncBatchModel,
)
from finboard_shared.types import InstrumentType

#: 可用发布的 quality_status 白名单(与 #509 目录 ``_USABLE_STATUSES`` 同口径)。
_USABLE_RELEASE_STATUSES = ("passed", "warnings")

#: 已发布批次状态(``dataset_sync`` 质量门通过后置 ``published``)。
_PUBLISHED_BATCH_STATUS = "published"

#: 单符号下推上限:超过它不把符号列表塞进 SQL ``IN`` / 执行器 payload,改用
#: 冻结的 ``universe_filters`` 让子执行器从 ``instruments`` 解析同一标的域。
#: 注意:缺口 diff 本身**不做**规模降级(逐标的缺口始终逐标的算)。
_MAX_SYMBOL_FILTER = 2000

#: bars 单次上游请求的窗口上限(天)。与 ``TushareBarProvider._MAX_CHUNK_DAYS``
#: (15 x 366)同量级:provider 内部按此分页,预检按同一口径估请求数。
_BARS_CHUNK_DAYS = 15 * 366

#: bars 缓存只支持前复权(``bulk_download`` 的 ``update_cache_batch`` 无 adjust
#: 维度);其它复权口径在预检即阻塞,不在执行期静默按 qfq 下载。
_SUPPORTED_BARS_ADJUSTMENT = "qfq"

#: 发布身份模板里的占位:预检时请求 ID 尚未生成,真正的 release_id 在执行器
#: 用 ``request_id`` 拼出(``DR-<dataset>[-<adjustment>]-<BF-...>``)。
_REQUEST_ID_PLACEHOLDER = "<request_id>"

#: FULL_PAGED 数据集的请求数上界(全量分页通常 1-3 次)。
_FULL_PAGED_ESTIMATE = 2


# ---------------------------------------------------------------------------
# 区间代数(纯函数,便于单测)
# ---------------------------------------------------------------------------


def merge_ranges(ranges: Iterable[DateRange]) -> tuple[DateRange, ...]:
    """合并重叠或相邻(+1 天)的闭区间,输出升序不重叠区间。"""

    ordered = sorted((start, end) for start, end in ranges if start <= end)
    merged: list[list[date]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((item[0], item[1]) for item in merged)


def subtract_ranges(
    requested: Iterable[DateRange], covered: Iterable[DateRange]
) -> tuple[DateRange, ...]:
    """``requested - covered`` 的闭区间集合差(保留独立缺口)。"""

    remaining = [list(item) for item in merge_ranges(requested)]
    for cover_start, cover_end in merge_ranges(covered):
        next_remaining: list[list[date]] = []
        for start, end in remaining:
            if cover_end < start or cover_start > end:
                next_remaining.append([start, end])
                continue
            if start < cover_start:
                next_remaining.append([start, cover_start - timedelta(days=1)])
            if end > cover_end:
                next_remaining.append([cover_end + timedelta(days=1), end])
        remaining = next_remaining
    return tuple((item[0], item[1]) for item in remaining if item[0] <= item[1])


def intersect_ranges(
    left: Iterable[DateRange], right: Iterable[DateRange]
) -> tuple[DateRange, ...]:
    """两个区间集合的交集(判在途重叠/批次覆盖用)。"""

    result: list[DateRange] = []
    for a_start, a_end in merge_ranges(left):
        for b_start, b_end in merge_ranges(right):
            start = max(a_start, b_start)
            end = min(a_end, b_end)
            if start <= end:
                result.append((start, end))
    return merge_ranges(result)


def estimate_requests(
    support: BackfillSupport,
    *,
    shape: EnumShape | None,
    missing_ranges: Sequence[DateRange],
    missing_symbols: int,
    symbol_count: int,
) -> int:
    """估算上游请求数(与执行器实际分页口径尽量一致)。"""

    if not missing_ranges:
        return 0
    if support.sync_mode == "bulk_download":
        # 口径说明:``bulk_download`` 的 payload 只有 ``start``(结束日取
        # today),provider 用缓存 sidecar 的 ``covered_ranges`` 跳过已覆盖区间,
        # 实际请求数≈缺口区间;这里按缺口天数估算(不回退成 start→today 的
        # 虚高计数)。单次 provider 分页上限见 ``_BARS_CHUNK_DAYS``。
        days = sum((end - start).days + 1 for start, end in missing_ranges)
        chunks = max(1, math.ceil(days / _BARS_CHUNK_DAYS))
        return max(1, symbol_count) * chunks
    if support.sync_mode == "derived":
        # 派生数据集不单独请求上游(只发布),请求数记 1 次发布准备。
        return 1
    if shape is EnumShape.PER_SYMBOL_RANGE:
        return max(1, missing_symbols or symbol_count)
    if shape is EnumShape.DAILY_MARKET:
        return sum(len(workdays(start, end)) for start, end in missing_ranges)
    return _FULL_PAGED_ESTIMATE


# ---------------------------------------------------------------------------
# 覆盖汇总
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ReleaseAggregate:
    """一份发布的聚合(来源记录 + 逐标的区间 + 质量计数)。"""

    release_id: str
    status: str
    ranges: list[DateRange] = field(default_factory=list)
    by_symbol: dict[str, list[DateRange]] = field(default_factory=dict)
    anomalies: int = 0
    duplicates: int = 0
    #: 有质量异常/重复的标的(缓存修复的最小必要集合)。
    anomaly_symbols: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _DatasetCoverage:
    """一个数据集在窗口内的已完成覆盖汇总。"""

    sources: list[BackfillCoverage] = field(default_factory=list)
    ranges: list[DateRange] = field(default_factory=list)
    by_symbol: dict[str, list[DateRange]] = field(default_factory=dict)
    release_ids: list[str] = field(default_factory=list)
    anomalies: int = 0
    duplicates: int = 0
    anomaly_symbols: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ScopeSymbols:
    """scope 解析出的标的域(逐标的类型 + 类型计数)。"""

    symbols: tuple[str, ...]
    types: dict[str, str]
    type_counts: dict[str, int]

    @property
    def has_non_stock(self) -> bool:
        return any(kind != "stock" for kind in self.type_counts)

    @property
    def has_unknown(self) -> bool:
        return "unknown" in self.type_counts


_KIND_EXPR = func.coalesce(
    ResearchDatasetReleaseModel.manifest["dataset_kind"].as_string(), "bars"
)


async def plan_backfill(
    session: AsyncSession,
    spec: BackfillRequestSpec,
    *,
    catalog: CatalogRepository | None = None,
    as_of: datetime | None = None,
    capabilities: BackfillCapabilities | None = None,
    settings_factory: Any = None,
    budget: object | None = None,
) -> BackfillPlan:
    """生成补数预检(纯读)。

    失败前置(日历缺失 / 显式窗口越界)以 :class:`BackfillPlanError` 抛出
    (API 422);「补不了」以 item 级 ``blockers`` 返回(不抛),由调用方决定
    登记需求还是改范围。
    """

    resolved_at = as_of or utcnow()
    window = await _resolve_window(session, spec.scope)
    scope_symbols = await _resolve_symbols(session, spec.scope)
    universe_filters = _universe_filters(spec.scope)

    caps = capabilities
    if caps is None:
        caps = await probe_capabilities(
            settings_factory=settings_factory, budget=budget
        )

    repo = DataBackfillRepository(session)
    catalog = catalog or CatalogRepository(session)
    inflight_rows = await repo.inflight_items(
        datasets=[item.dataset.value for item in spec.datasets],
        statuses=sorted(INFLIGHT_REQUEST_STATUSES),
        window_end=window.valuation_end,
    )
    inflight_requests = {
        row.request_id: row
        for row in await repo.get_many(
            sorted({str(row.request_id) for row in inflight_rows})
        )
    }

    items: list[BackfillItem] = []
    overlaps: dict[str, BackfillOverlap] = {}
    warnings: list[str] = []
    blockers: list[BackfillBlocker] = []
    total_estimated = 0
    affected_release_ids: list[str] = []

    for dataset_spec in spec.ordered_datasets():
        support = support_for(dataset_spec.dataset)
        shape = _sync_shape(support)
        start, end = _item_window(dataset_spec, window)
        item_blockers: list[BackfillBlocker] = []
        item_symbols = tuple(sorted(dataset_spec.symbols)) or scope_symbols.symbols
        item_types = await _resolve_item_types(session, item_symbols, scope_symbols)
        source = dataset_spec.source or support.source
        adjustment = _resolve_adjustment(support, dataset_spec.adjustment)
        symbol_unit = shape is EnumShape.PER_SYMBOL_RANGE or support.sync_mode == "bulk_download"
        # 所有数据集都冻结可枚举的标的域(不只逐标的单元):发布面必须等于
        # 请求面,窗口级数据集也不能回退到「整市场」发布。
        frozen_symbols = _freeze_symbols(item_symbols)
        item_filters = dict(universe_filters)

        completed = await _completed_coverage(
            session,
            support=support,
            source=source,
            adjustment=adjustment,
            start=start,
            end=end,
            symbols=item_symbols,
        )
        inflight_ranges, inflight_ids = _inflight_coverage(
            inflight_rows,
            dataset=dataset_spec.dataset,
            source=source,
            adjustment=adjustment,
            start=start,
            end=end,
            symbols=frozen_symbols,
            universe_filters=item_filters,
        )
        for request_id in inflight_ids:
            request_row = inflight_requests.get(request_id)
            overlaps.setdefault(
                request_id,
                BackfillOverlap(
                    request_id=request_id,
                    status=request_row.status if request_row is not None else "unknown",
                    reuse="ranges",
                    datasets=(dataset_spec.dataset.value,),
                ),
            )

        missing_symbols = _missing_symbols(item_symbols, completed, start, end)
        # 标的级缺口同样必须补:发布面 = 请求标的,缺一个就冻结不出完整发布。
        symbol_gap = bool(missing_symbols) and (
            support.publishable or symbol_unit
        )
        gap_ranges = list(
            _gap_ranges(
                support=support,
                shape=shape,
                symbols=item_symbols,
                completed=completed,
                start=start,
                end=end,
            )
        )
        if symbol_gap:
            # 关键:标的缺口必须转成**可执行区间**。窗口级数据集若只有标的
            # 缺失(窗口级覆盖齐全),这里按「该标的未覆盖的区间」补,否则
            # missing_ranges 为空、执行器零同步、发布却缺标的(B5 复审)。
            gap_ranges.extend(
                _symbol_gap_ranges(missing_symbols, completed, start, end)
            )
        missing_after_completed = merge_ranges(gap_ranges)
        missing_ranges = subtract_ranges(missing_after_completed, inflight_ranges)
        reuse_only = bool(missing_after_completed) and not missing_ranges

        repair, repair_symbols, repair_skip_reason = _repair_plan(
            support=support,
            completed=completed,
            symbols=item_symbols,
            quality_profile=spec.quality_profile,
        )
        action = _decide_action(
            support=support,
            publish=dataset_spec.publish,
            missing_ranges=missing_ranges,
            missing_symbols=missing_symbols if symbol_gap else (),
            has_completed_gap=bool(missing_after_completed),
            reuse_only=reuse_only,
            repair=repair,
        )
        estimated = 0
        if action in (
            BackfillAction.SYNC,
            BackfillAction.SYNC_PUBLISH,
            BackfillAction.REPAIR,
        ):
            estimated = estimate_requests(
                support,
                shape=shape,
                missing_ranges=missing_ranges,
                missing_symbols=len(missing_symbols),
                symbol_count=len(frozen_symbols) or _filter_symbol_count(item_filters),
            )
            if action is BackfillAction.REPAIR and estimated == 0:
                # 纯质量修复:按修复标的数计请求(每个标的至少一次回源)。
                estimated = max(1, len(repair_symbols))
        total_estimated += estimated

        cross_market_bars = _cross_market_bars(
            support, item_types, scope_symbols.type_counts
        )
        # 显式标的清单超过冻结上限时无法表达成 universe_filters;发布面会退化
        # 为数据集全集(发布面 > 请求面)。按 item 实际标的判断,避免 dataset
        # spec 已收窄标的却被全局显式清单误伤。
        explicit_over_cap = (
            bool(spec.scope.universe.explicit_symbols or dataset_spec.symbols)
            and len(item_symbols) > _MAX_SYMBOL_FILTER
        )
        item_blockers.extend(
            _item_blockers(
                support=support,
                capabilities=caps,
                dataset=dataset_spec.dataset,
                publish=dataset_spec.publish,
                has_gap=bool(missing_ranges) or symbol_gap or repair,
                adjustment=adjustment,
                frozen_symbols=frozen_symbols,
                universe_filters=item_filters,
                symbol_unit=symbol_unit,
                cross_market_bars=cross_market_bars,
                explicit_over_cap=explicit_over_cap,
            )
        )
        if support.requires_a_share_stock and item_types:
            non_stock = sorted(
                code
                for code, kind in item_types.items()
                if kind not in ("stock", "unknown")
            )
            if non_stock:
                item_blockers.append(
                    BackfillBlocker(
                        code="backfill_scope_unsupported",
                        message=(
                            f"{dataset_spec.dataset.value} 的发布只接受 A 股股票,"
                            f"请求标的含非股票: {non_stock[:5]}"
                        ),
                        dataset=dataset_spec.dataset,
                    )
                )
        if dataset_spec.publish and support.publishable and item_types:
            unregistered = sorted(
                code for code, kind in item_types.items() if kind == "unknown"
            )
            if unregistered:
                # 发布执行器会以 unknown_symbols 拒绝;预检提前具名,避免
                # 跑完同步才在冻结阶段失败(上游配额已白花)。
                item_blockers.append(
                    BackfillBlocker(
                        code="backfill_symbols_unregistered",
                        message=(
                            "请求标的未登记到 instruments 元数据表,无法发布: "
                            f"{unregistered[:5]}"
                        ),
                        dataset=dataset_spec.dataset,
                    )
                )

        publish_preview = None
        if dataset_spec.publish and support.publishable and support.release_kind:
            publish_preview = BackfillPublishPreview(
                release_id=_preview_release_id(
                    dataset_spec.dataset.value,
                    adjustment,
                    source,
                    support.source,
                ),
                version=_preview_version(adjustment, source, support.source),
                release_kind=support.release_kind,
                dataset_name=dataset_spec.dataset.value,
            )
        if completed.release_ids:
            affected_release_ids.extend(completed.release_ids)
        conflict_count = await _conflicting_release_count(
            session,
            support=support,
            source=source,
            adjustment=adjustment,
            start=start,
            end=end,
        )
        item_warnings = [
            *(
                [
                    f"{conflict_count} 份既有发布使用不同的来源/复权口径,"
                    "未参与复用(异源不混接,单独成项)"
                ]
                if conflict_count
                else []
            ),
            *([repair_skip_reason] if repair_skip_reason else []),
        ]
        if frozen_symbols and len(item_symbols) > _MAX_SYMBOL_FILTER:
            item_warnings.append(
                f"标的 {len(item_symbols)} 只,已冻结为 universe_filters"
                "(执行器按同一标的域解析,不逐只序列化)"
            )
        if repair_symbols and set(repair_symbols) != set(frozen_symbols):
            item_warnings.append(
                f"缓存质量修复仅针对 {len(repair_symbols)} 个异常标的"
            )

        items.append(
            BackfillItem(
                dataset=dataset_spec.dataset,
                source=source,
                adjustment=adjustment,
                action=action,
                start=start,
                end=end,
                publish=dataset_spec.publish and support.publishable,
                symbols=frozen_symbols,
                universe_filters=item_filters,
                missing_ranges=missing_ranges,
                repair_symbols=repair_symbols,
                missing_symbols=BackfillGapSymbols(
                    count=len(missing_symbols),
                    preview=tuple(missing_symbols[:SYMBOL_PREVIEW_LIMIT]),
                ),
                covered_by=tuple(completed.sources),
                estimated_requests=estimated,
                quality_repair=repair,
                publish_preview=publish_preview,
                reused_request_ids=tuple(sorted(inflight_ids)),
                blockers=tuple(item_blockers),
                warnings=tuple(
                    item_warnings
                    + _completed_warnings(completed, item_symbols)
                    + (
                        ["in-flight 请求未覆盖全部请求标的,保留独立缺口"]
                        if inflight_ids and missing_ranges
                        else []
                    )
                ),
            )
        )
        blockers.extend(item_blockers)

    new_releases = sum(
        1
        for item in items
        if item.publish
        and item.action in (BackfillAction.SYNC_PUBLISH, BackfillAction.REPAIR)
        and not item.blockers
    )
    affected_specs = 0
    if affected_release_ids:
        refs = await catalog.strategy_refs_for_releases(
            tuple(dict.fromkeys(affected_release_ids))
        )
        affected_specs = len(refs)

    quota = _quota(caps, total_estimated)
    if quota.status == "defer":
        warnings.append(
            "上游请求配额今日不足,执行器将按预算耗尽走 retry_waiting 退避重试"
            "(不把请求标死)"
        )

    return BackfillPlan(
        request_fingerprint=request_fingerprint(spec),
        scope_checksum=scope_checksum(spec.scope),
        as_of=(
            resolved_at.astimezone(UTC)
            if resolved_at.tzinfo is not None
            else resolved_at.replace(tzinfo=UTC)
        ),
        items=tuple(items),
        quota=quota,
        overlaps=tuple(overlaps.values()),
        publish_impact=BackfillPublishImpact(
            new_releases=new_releases,
            affected_specs=affected_specs,
            immutable_existing=True,
            releases=tuple(
                item.publish_preview
                for item in items
                if item.publish_preview is not None and not item.blockers
            ),
        ),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


async def _resolve_window(session: AsyncSession, scope: DataScopeSpec) -> ScopeWindow:
    """复用 #510 的交易日窗口(缺日历 fail-closed,不用自然日兜底)。"""

    try:
        days, exchanges = await load_union_calendar(session, markets_for(scope))
        return build_window(
            scope.decision,
            days,
            warmup_sessions=scope.warmup_sessions,
            tail_sessions=scope.tail_sessions,
            exchanges=exchanges,
        )
    except ScopeCalendarError as exc:
        raise BackfillPlanError(exc.code, exc.message) from exc


def _item_window(dataset_spec: BackfillDatasetSpec, window: ScopeWindow) -> tuple[date, date]:
    """item 的补数窗口:显式声明必须落在 scope 窗口内。"""

    start = dataset_spec.start or window.warmup_start
    end = dataset_spec.end or window.valuation_end
    if start < window.warmup_start or end > window.valuation_end:
        raise BackfillPlanError(
            "backfill_window_out_of_scope",
            (
                f"{dataset_spec.dataset.value} 的补数窗口 [{start}, {end}] 超出"
                f" scope 窗口 [{window.warmup_start}, {window.valuation_end}]"
                " —— 补数不扩大课题范围"
            ),
            dataset=dataset_spec.dataset,
        )
    return start, end


def _resolve_adjustment(support: BackfillSupport, requested: str) -> str:
    """复权口径:非价格数据固定 ``none``;bars 用请求值(默认 qfq)。"""

    if support.sync_mode == "bulk_download":
        return requested if requested in ("qfq", "hqfq", "none") else "qfq"
    return "none"


def _sync_shape(support: BackfillSupport) -> EnumShape | None:
    if support.sync_dataset is None:
        return None
    return SYNC_SPECS.get(support.sync_dataset).shape


def _universe_filters(scope: DataScopeSpec) -> dict[str, object]:
    """把 scope 标的域冻结为可下传给子执行器的过滤器(唯一值才下推)."""

    universe = scope.universe
    markets = [item.value for item in universe.markets]
    types = [item.value for item in universe.instrument_types]
    filters: dict[str, object] = {}
    if len(markets) == 1:
        filters["market"] = markets[0]
    if len(types) == 1:
        filters["instrument_type"] = types[0]
    if universe.selection_limit:
        filters["selection_limit"] = universe.selection_limit
    return filters


def _freeze_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    """规模可控时冻结完整标的列表(执行器/发布按此下传).

    所有数据集一视同仁:窗口级数据集(如 daily_metrics)的发布也只允许请求
    标的,不能因为同步单元是「按日」就回退到整市场发布。
    超过 :data:`_MAX_SYMBOL_FILTER` 时不冻结(交给 ``universe_filters`` 解析),
    避免把几千个代码写进 payload / plan JSON;缺口 diff 不受此影响。
    """

    if not symbols or len(symbols) > _MAX_SYMBOL_FILTER:
        return ()
    return tuple(symbols)


def _filter_symbol_count(filters: dict[str, object]) -> int:
    """过滤器模式的请求数估算上限(未知时按 1,避免 0 乘出零估算)."""

    limit = filters.get("selection_limit")
    return int(limit) if isinstance(limit, int) and limit > 0 else 1


def _decide_action(
    *,
    support: BackfillSupport,
    publish: bool,
    missing_ranges: Sequence[DateRange],
    missing_symbols: Sequence[str],
    has_completed_gap: bool,
    reuse_only: bool,
    repair: bool,
) -> BackfillAction:
    """动作判定(无缺口 → noop/reuse;有缺口/缺标的 → sync / sync_publish / repair)。

    ``publish=False`` 的请求只能是 ``SYNC``(含修复):执行器据此不进入发布阶段,
    不能出现「plan 显示不发布、执行器照样冻结新发布」的不一致。
    """

    has_gap = bool(missing_ranges) or bool(missing_symbols)
    if not has_gap and not repair:
        return (
            BackfillAction.REUSE
            if (reuse_only or has_completed_gap)
            else BackfillAction.NOOP
        )
    wants_publish = (
        publish and support.publishable and support.release_kind is not None
    )
    if repair:
        return BackfillAction.REPAIR if wants_publish else BackfillAction.SYNC
    return BackfillAction.SYNC_PUBLISH if wants_publish else BackfillAction.SYNC


def _cross_market_bars(
    support: BackfillSupport,
    item_types: dict[str, str],
    scope_type_counts: dict[str, int],
) -> bool:
    """bars(bulk_download)请求的标的域是否跨市场。

    item 级类型解析在标的数超过冻结上限时为空,此时退到 scope 解析阶段的
    类型聚合计数 —— 全市场 / 多市场过滤器场景也必须 fail-closed,不能默认
    按 a_share 下传(期货会在下载阶段被静默过滤,发布面却仍包含期货)。
    """

    known = {kind for kind in item_types.values() if kind != "unknown"}
    if not known:
        known = set(scope_type_counts)
    return (
        support.sync_mode == "bulk_download"
        and InstrumentType.FUTURES.value in known
        and any(kind != InstrumentType.FUTURES.value for kind in known)
    )


def _item_blockers(
    *,
    support: BackfillSupport,
    capabilities: BackfillCapabilities,
    dataset: CatalogDataset,
    publish: bool,
    has_gap: bool,
    adjustment: str,
    frozen_symbols: Sequence[str],
    universe_filters: dict[str, object],
    symbol_unit: bool,
    cross_market_bars: bool = False,
    explicit_over_cap: bool = False,
) -> list[BackfillBlocker]:
    """运行时能力与支持清单的阻塞项(缺一项即具名,不静默跳过)。"""

    outcome: list[BackfillBlocker] = []
    if not has_gap:
        return outcome
    if not support.syncable:
        outcome.append(
            BackfillBlocker(
                code="backfill_dataset_derived",
                message=(
                    f"{dataset.value} 不可单独同步:{support.note};"
                    "请先补齐 bars 与条款元数据"
                ),
                dataset=dataset,
            )
        )
    elif not capabilities.can_sync:
        reason = (
            "缺少 tushare token"
            if not capabilities.tushare_token
            else "本地缓存目录不可写"
        )
        outcome.append(
            BackfillBlocker(
                code="backfill_source_not_configured",
                message=f"{dataset.value} 需要上游同步但{reason}",
                dataset=dataset,
            )
        )
    if (
        support.sync_mode == "bulk_download"
        and adjustment != _SUPPORTED_BARS_ADJUSTMENT
    ):
        # bulk_download 的缓存写入只有 qfq(契约无 adjust 维度):非 qfq 请求
        # 必须 fail-closed,不能静默按 qfq 下载出一份口径不符的缓存。
        outcome.append(
            BackfillBlocker(
                code="backfill_adjustment_unsupported",
                message=(
                    f"bars 缓存补数只支持 {_SUPPORTED_BARS_ADJUSTMENT},"
                    f"请求 adjustment={adjustment};请改用 qfq 或换数据源链路"
                ),
                dataset=dataset,
            )
        )
    if (
        support.sync_mode == "dataset_sync"
        and symbol_unit
        and not frozen_symbols
        and not universe_filters.get("instrument_type")
    ):
        # ``dataset_sync`` 能表达的过滤器只有 exchange / listing_boards /
        # instrument_type / symbols(没有 market);逐标的研究数据集冻结不了
        # 标的时至少要能下推 instrument_type,否则执行期必然 empty_symbol_pool
        # —— 预检即具名阻塞,不允许执行器自行放宽到「整市场」。
        # bars(bulk_download)例外:它的契约原生支持 market,按 market + 领域
        # 类型解析与请求面一致,不需要再下推 instrument_type。
        outcome.append(
            BackfillBlocker(
                code="backfill_symbol_scope_unresolved",
                message=(
                    f"{dataset.value} 是逐标的补数,但标的域超过 "
                    f"{_MAX_SYMBOL_FILTER} 只且 scope 无法下推单一 "
                    "instrument_type(或未声明);请显式声明 symbols / "
                    "instrument_type,或收窄 scope.universe"
                ),
                dataset=dataset,
            )
        )
    if publish and support.publishable and not capabilities.can_publish:
        outcome.append(
            BackfillBlocker(
                code="backfill_release_root_unwritable",
                message="发布根目录不可写,无法冻结新发布",
                dataset=dataset,
            )
        )
    if cross_market_bars:
        # bulk_download 的 payload 只有单个 ``market``;跨市场标的会在下载
        # 阶段被静默过滤,但发布面仍按全部冻结标的写 manifest —— 发布出一份
        # 「含期货、实际只有 A 股」的残缺缓存。预检即拒绝,要求按市场拆分。
        outcome.append(
            BackfillBlocker(
                code="backfill_multiple_markets_unsupported",
                message=(
                    "bars 缓存补数一次只能覆盖单一市场(bulk_download 按 "
                    "market 过滤),请求标的跨市场;请按市场拆分补数请求"
                ),
                dataset=dataset,
            )
        )
    if explicit_over_cap and publish and support.publishable:
        # 显式标的超过冻结上限时发布面会退化为「数据集全集」(发布面 > 请求面),
        # 不能靠 universe_filters 表达一支显式清单 —— fail-closed。
        outcome.append(
            BackfillBlocker(
                code="backfill_symbol_scope_unresolved",
                message=(
                    f"显式标的超过 {_MAX_SYMBOL_FILTER} 只,无法冻结为发布标的域;"
                    "发布面会退化为全集。请收窄 explicit_symbols 或改用 "
                    "markets / instrument_types 过滤器"
                ),
                dataset=dataset,
            )
        )
    return outcome


def _repair_plan(
    *,
    support: BackfillSupport,
    completed: _DatasetCoverage,
    symbols: Sequence[str],
    quality_profile: str,
) -> tuple[bool, tuple[str, ...], str | None]:
    """缓存修复计划:(是否修, 修复标的, 跳过原因).

    修复对象是**已发布读模型里带异常/重复计数的标的**(不是全市场),因此
    进入修复时 ``repair_symbols`` 恒非空;``strict`` 档在无异常时对可枚举标的
    做一次复检,超过 :data:`_MAX_SYMBOL_FILTER` 则跳过并给出具名 warning
    (发布质量门仍然生效,不会变成「静默跳过」)。
    """

    if support.sync_mode != "bulk_download" or not completed.release_ids:
        return False, (), None
    if completed.anomalies or completed.duplicates:
        crossed = tuple(
            code
            for code in sorted(completed.anomaly_symbols)
            if not symbols or code in set(symbols)
        )
        if crossed:
            return True, crossed, None
        return (
            False,
            (),
            "既有异常标的不在本次请求标的域内,跳过缓存修复(质量门仍生效)",
        )
    if quality_profile == "strict":
        if symbols and len(symbols) <= _MAX_SYMBOL_FILTER:
            return True, tuple(symbols), None
        return (
            False,
            (),
            (
                f"strict 质量复检跳过:请求标的超过 {_MAX_SYMBOL_FILTER} 只"
                "(发布质量门仍生效)"
            ),
        )
    return False, (), None


def _quota(capabilities: BackfillCapabilities, estimated_total: int) -> BackfillQuota:
    """配额状态:不足标 defer(不拒绝),读不到预算记 unknown。"""

    snapshot = capabilities.budget
    if snapshot is None:
        return BackfillQuota(
            status="unknown",
            estimated_total=estimated_total,
            sufficient_today=True,
            days_needed=1,
        )
    remaining = snapshot.remaining_today
    sufficient = remaining >= estimated_total
    days_needed = 1
    if not sufficient and estimated_total > 0:
        days_needed = max(1, math.ceil(estimated_total / max(1, snapshot.daily_limit)))
    return BackfillQuota(
        status="ok" if sufficient else "defer",
        remaining_today=remaining,
        requests_per_minute=snapshot.requests_per_minute,
        estimated_total=estimated_total,
        sufficient_today=sufficient,
        days_needed=days_needed,
    )


async def _resolve_symbols(session: AsyncSession, scope: DataScopeSpec) -> ScopeSymbols:
    """解析 scope 的标的域与类型。

    优先 ``universe.explicit_symbols``;否则按 ``instruments`` 的市场 / 类型
    过滤(``instruments`` 表没有 asset_class 列,资产类由发布执行器在冻结时
    二次校验)。``selection_limit`` 截断后仍按该子集补数。
    """

    explicit = tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in scope.universe.explicit_symbols
            if item.strip()
        )
    )
    if explicit:
        types = await _instrument_rows(session, explicit)
        return ScopeSymbols(explicit, types, _type_counts(types))
    conditions: list[Any] = []
    markets = [item.value for item in scope.universe.markets]
    if markets:
        conditions.append(InstrumentModel.market.in_(markets))
    instrument_types = [item.value for item in scope.universe.instrument_types]
    if instrument_types:
        conditions.append(
            InstrumentModel.instrument_type.in_(instrument_types)
        )
    stmt = select(InstrumentModel.code).where(*conditions).order_by(InstrumentModel.code)
    if scope.universe.selection_limit:
        stmt = stmt.limit(scope.universe.selection_limit)
    codes = tuple(str(row[0]) for row in (await session.execute(stmt)).all())
    if not codes:
        return ScopeSymbols((), {}, {})
    if len(codes) <= _MAX_SYMBOL_FILTER:
        types = await _instrument_rows(session, codes)
        return ScopeSymbols(codes, types, _type_counts(types))
    count_stmt = (
        select(InstrumentModel.instrument_type, func.count())
        .where(*conditions)
        .group_by(InstrumentModel.instrument_type)
    )
    counts = {
        str(row[0]): int(row[1]) for row in (await session.execute(count_stmt)).all()
    }
    return ScopeSymbols(codes, {}, counts)


def _type_counts(types: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in types.values():
        counts[kind] = counts.get(kind, 0) + 1
    return counts


async def _instrument_rows(session: AsyncSession, symbols: Sequence[str]) -> dict[str, str]:
    """symbol -> instrument_type(未登记的标的记 unknown)。"""

    if not symbols:
        return {}
    stmt = select(InstrumentModel.code, InstrumentModel.instrument_type).where(
        InstrumentModel.code.in_(tuple(symbols))
    )
    found = {str(row[0]): str(row[1]) for row in (await session.execute(stmt)).all()}
    return {code: found.get(code, "unknown") for code in symbols}


async def _resolve_item_types(
    session: AsyncSession, item_symbols: Sequence[str], scope: ScopeSymbols
) -> dict[str, str]:
    """item 标的的类型映射。

    全市场请求(scope 标的 > :data:`_MAX_SYMBOL_FILTER`)只做了类型聚合,
    没有逐标的映射;此时 item 若显式收窄到小集合,按需回查 ``instruments``,
    避免把已登记的标的误判成 ``unknown`` 而误阻塞发布。
    """

    if not item_symbols:
        return {}
    if scope.types:
        return {code: scope.types.get(code, "unknown") for code in item_symbols}
    if len(item_symbols) <= _MAX_SYMBOL_FILTER:
        return await _instrument_rows(session, item_symbols)
    return {}


def _missing_symbols(
    symbols: Sequence[str], completed: _DatasetCoverage, start: date, end: date
) -> tuple[str, ...]:
    """窗口内没有「完整覆盖」的标的(逐标的看区间是否盖住整个窗口)。"""

    if not symbols:
        return ()
    missing: list[str] = []
    for code in symbols:
        ranges = completed.by_symbol.get(code, [])
        if not ranges or subtract_ranges(((start, end),), ranges):
            missing.append(code)
    return tuple(missing)


def _symbol_gap_ranges(
    missing: Sequence[str], completed: _DatasetCoverage, start: date, end: date
) -> tuple[DateRange, ...]:
    """缺失标的在窗口内未覆盖的区间(把「只缺标的」变成可执行同步区间)."""

    gaps: list[DateRange] = []
    for code in missing:
        gaps.extend(subtract_ranges(((start, end),), completed.by_symbol.get(code, [])))
    return merge_ranges(gaps)


def _gap_ranges(
    *,
    support: BackfillSupport,
    shape: EnumShape | None,
    symbols: Sequence[str],
    completed: _DatasetCoverage,
    start: date,
    end: date,
) -> tuple[DateRange, ...]:
    """按同步单元计算缺口区间(逐标的 / 窗口级两种口径)。"""

    per_symbol = shape is EnumShape.PER_SYMBOL_RANGE or support.sync_mode == "bulk_download"
    if per_symbol and symbols:
        # 逐标的单元:逐只算差值再并集(不做规模降级 —— 降级会让个别标的
        # 的缺失被其它标的的覆盖掩盖,进而判成 noop)。
        gaps: list[DateRange] = []
        for code in symbols:
            gaps.extend(
                subtract_ranges(((start, end),), completed.by_symbol.get(code, []))
            )
        return merge_ranges(gaps)
    return subtract_ranges(((start, end),), completed.ranges)


def _completed_warnings(
    completed: _DatasetCoverage, symbols: Sequence[str]
) -> list[str]:
    """完成覆盖的质量告警(异常/重复计数来自已发布读模型)。"""

    outcome: list[str] = []
    if completed.anomalies:
        outcome.append(f"既有覆盖含 {completed.anomalies} 个异常 bar 计数")
    if completed.duplicates:
        outcome.append(f"既有覆盖含 {completed.duplicates} 个重复 bar 计数")
    if not completed.release_ids and not completed.ranges and symbols:
        outcome.append("窗口内没有既有可用覆盖(将整段补数)")
    return outcome


def _inflight_coverage(
    rows: Sequence[Any],
    *,
    dataset: CatalogDataset,
    source: str,
    adjustment: str,
    start: date,
    end: date,
    symbols: Sequence[str],
    universe_filters: dict[str, object],
) -> tuple[tuple[DateRange, ...], set[str]]:
    """在途明细里 dataset/source/adjustment 一致且标的域**可证明覆盖**的部分。

    **异源 / 不同复权 / 更窄或不可比的标的域不参与复用**:

    * 双方都是显式标的 → 在途标的必须是请求标的的超集;
    * 一方显式、一方过滤器 → 无法证明超集,不复用(宁可保留缺口);
    * 双方都是过滤器 → 过滤器必须完全一致(否则覆盖域可能更窄)。

    不复用只是多跑一次同步,复用错才会掩盖缺口 —— 这里一律保守。
    """

    wanted = {item.strip().upper() for item in symbols if item.strip()}
    requested_filters = {
        key: value for key, value in universe_filters.items() if value is not None
    }
    ranges: list[DateRange] = []
    request_ids: set[str] = set()
    for row in rows:
        if row.dataset != dataset.value:
            continue
        if (row.source or "") != source or (row.adjustment or "none") != adjustment:
            continue
        if row.end_date < start or row.start_date > end:
            continue
        row_symbols = {str(item).strip().upper() for item in (row.symbols or ())}
        if row_symbols and wanted:
            if not row_symbols >= wanted:
                continue
        elif row_symbols or wanted:
            # 一边显式标的、一边过滤器:域不可比。
            continue
        elif dict(row.universe_filters or {}) != requested_filters:
            continue
        ranges.append((max(row.start_date, start), min(row.end_date, end)))
        request_ids.add(str(row.request_id))
    return merge_ranges(ranges), request_ids


async def _completed_coverage(
    session: AsyncSession,
    *,
    support: BackfillSupport,
    source: str,
    adjustment: str,
    start: date,
    end: date,
    symbols: Sequence[str],
) -> _DatasetCoverage:
    """已完成覆盖:可发布看发布读模型,不可发布看已发布批次。"""

    if support.publishable:
        return await _release_coverage(
            session,
            support=support,
            source=source,
            adjustment=adjustment,
            start=start,
            end=end,
            symbols=symbols,
        )
    return await _batch_coverage(
        session,
        dataset=support.sync_dataset,
        start=start,
        end=end,
    )


async def _release_coverage(
    session: AsyncSession,
    *,
    support: BackfillSupport,
    source: str,
    adjustment: str,
    start: date,
    end: date,
    symbols: Sequence[str],
) -> _DatasetCoverage:
    """按逐标的发布索引汇总可用覆盖(与 #509 目录同一读模型)。"""

    coverage = _DatasetCoverage()
    kind = _release_dataset_kind(support)
    if kind is None:
        return coverage
    conditions: list[Any] = [
        _KIND_EXPR.in_([kind.value]),
        ResearchDatasetReleaseModel.quality_status.in_(_USABLE_RELEASE_STATUSES),
        ResearchDatasetReleaseModel.end_date >= start,
        ResearchDatasetReleaseModel.start_date <= end,
        ResearchDatasetReleaseInstrumentModel.end_date >= start,
        ResearchDatasetReleaseInstrumentModel.start_date <= end,
    ]
    if source and source != "mixed" and support.sync_mode != "bulk_download":
        # 研究数据发布按 source 严格隔离(异源不混接);bars 发布固定是
        # ``multi_asset_mixed``(source=mixed,逐标的实际来源写质量报告),
        # 因此 bars 的覆盖只按复权口径 + 窗口 + 标的匹配。
        conditions.append(ResearchDatasetReleaseModel.source == source)
    if support.sync_mode == "bulk_download":
        conditions.append(ResearchDatasetReleaseModel.adjustment == adjustment)
    if symbols and len(symbols) <= _MAX_SYMBOL_FILTER:
        conditions.append(
            ResearchDatasetReleaseInstrumentModel.code.in_(tuple(symbols))
        )

    stmt = (
        select(
            ResearchDatasetReleaseModel.release_id,
            ResearchDatasetReleaseModel.quality_status,
            ResearchDatasetReleaseInstrumentModel.code,
            ResearchDatasetReleaseInstrumentModel.start_date,
            ResearchDatasetReleaseInstrumentModel.end_date,
            ResearchDatasetReleaseInstrumentModel.anomaly_count,
            ResearchDatasetReleaseInstrumentModel.duplicate_count,
        )
        .join(
            ResearchDatasetReleaseInstrumentModel,
            ResearchDatasetReleaseInstrumentModel.release_id
            == ResearchDatasetReleaseModel.release_id,
        )
        .where(*conditions)
        .order_by(ResearchDatasetReleaseModel.published_at.desc())
    )
    aggregated: dict[str, _ReleaseAggregate] = {}
    for row in (await session.execute(stmt)).all():
        release_id = str(row[0])
        bucket = aggregated.get(release_id)
        if bucket is None:
            bucket = _ReleaseAggregate(release_id=release_id, status=str(row[1]))
            aggregated[release_id] = bucket
        code = str(row[2])
        anomalies = int(row[5] or 0)
        duplicates = int(row[6] or 0)
        bucket.ranges.append((row[3], row[4]))
        bucket.by_symbol.setdefault(code, []).append((row[3], row[4]))
        bucket.anomalies += anomalies
        bucket.duplicates += duplicates
        if anomalies or duplicates:
            bucket.anomaly_symbols.add(code)

    for bucket in aggregated.values():
        merged = merge_ranges(bucket.ranges)
        coverage.sources.append(
            BackfillCoverage(
                kind="release",
                id=bucket.release_id,
                status=bucket.status,
                ranges=merged,
                symbols=len(bucket.by_symbol),
            )
        )
        coverage.ranges.extend(merged)
        for code, ranges in bucket.by_symbol.items():
            coverage.by_symbol.setdefault(code, []).extend(ranges)
        coverage.release_ids.append(bucket.release_id)
        coverage.anomalies += bucket.anomalies
        coverage.duplicates += bucket.duplicates
        coverage.anomaly_symbols |= bucket.anomaly_symbols
    coverage.ranges = list(merge_ranges(coverage.ranges))
    return coverage


async def _conflicting_release_count(
    session: AsyncSession,
    *,
    support: BackfillSupport,
    source: str,
    adjustment: str,
    start: date,
    end: date,
) -> int:
    """窗口内同 kind 但异源 / 不同复权的可用发布数(``source_conflict`` 告警)。

    这些发布**不参与复用**,只在 item 上给出具名 warning —— 让调用方知道
    「已有一部分数据,但口径不同,不能混接」,而不是看起来毫无覆盖。
    """

    if not support.publishable:
        return 0
    kind = _release_dataset_kind(support)
    if kind is None:
        return 0
    conditions: list[Any] = [
        _KIND_EXPR.in_([kind.value]),
        ResearchDatasetReleaseModel.quality_status.in_(_USABLE_RELEASE_STATUSES),
        ResearchDatasetReleaseModel.end_date >= start,
        ResearchDatasetReleaseModel.start_date <= end,
    ]
    if support.sync_mode == "bulk_download":
        conditions.append(ResearchDatasetReleaseModel.adjustment != adjustment)
    else:
        conditions.append(ResearchDatasetReleaseModel.source != source)
    stmt = select(
        func.count(func.distinct(ResearchDatasetReleaseModel.release_id))
    ).where(*conditions)
    return int((await session.execute(stmt)).scalar_one() or 0)


def _release_dataset_kind(support: BackfillSupport) -> ReleaseDatasetKind | None:
    """数据集 → 发布行里的 dataset_kind(bars 的两种 release kind 都归 bars)。"""

    if support.release_kind is None:
        return None
    if support.sync_mode == "bulk_download":
        return ReleaseDatasetKind.BARS
    return ReleaseDatasetKind(support.release_kind)


async def _batch_coverage(
    session: AsyncSession,
    *,
    dataset: str | None,
    start: date,
    end: date,
) -> _DatasetCoverage:
    """不可发布数据集:以已发布批次的 parameters 推断覆盖区间。"""

    coverage = _DatasetCoverage()
    if dataset is None:
        return coverage
    stmt = select(
        ResearchSyncBatchModel.dataset_version,
        ResearchSyncBatchModel.status,
        ResearchSyncBatchModel.parameters,
    ).where(
        ResearchSyncBatchModel.dataset == dataset,
        ResearchSyncBatchModel.status == _PUBLISHED_BATCH_STATUS,
    )
    for row in (await session.execute(stmt)).all():
        ranges = _batch_ranges(dict(row[2] or {}), start, end)
        if not ranges:
            continue
        coverage.sources.append(
            BackfillCoverage(
                kind="batch",
                id=str(row[0]),
                status=str(row[1]),
                ranges=merge_ranges(ranges),
            )
        )
        coverage.ranges.extend(ranges)
    coverage.ranges = list(merge_ranges(coverage.ranges))
    return coverage


def _batch_ranges(
    parameters: dict[str, Any], start: date, end: date
) -> tuple[DateRange, ...]:
    """批次参数 → 覆盖区间(全量枚举类批次视为覆盖整个窗口)。"""

    trade_date = parameters.get("trade_date")
    if isinstance(trade_date, str):
        try:
            day = date.fromisoformat(trade_date)
        except ValueError:
            return ()
        return ((day, day),) if start <= day <= end else ()
    raw_start = parameters.get("start_date")
    raw_end = parameters.get("end_date")
    if isinstance(raw_start, str) and isinstance(raw_end, str):
        try:
            batch_start = date.fromisoformat(raw_start)
            batch_end = date.fromisoformat(raw_end)
        except ValueError:
            return ()
        return intersect_ranges(((start, end),), ((batch_start, batch_end),))
    # 全量分页(如 profiles):一次发布即整市场快照,视为覆盖整个窗口。
    return ((start, end),)


def _identity_suffix(
    adjustment: str, source: str, default_source: str
) -> str:
    """发布身份后缀:非默认复权 / 非默认来源才显式带上(同请求内逐 item 唯一)."""

    parts: list[str] = []
    if adjustment != "none":
        parts.append(adjustment)
    if source and source != default_source:
        parts.append(source)
    return "".join(f"-{part}" for part in parts)


def _preview_release_id(
    dataset: str, adjustment: str, source: str, default_source: str
) -> str:
    """发布身份预告(执行器按同一规则生成真实 ID)."""

    return (
        f"DR-{dataset}{_identity_suffix(adjustment, source, default_source)}"
        f"-{_REQUEST_ID_PLACEHOLDER}"
    )


def _preview_version(
    adjustment: str, source: str, default_source: str
) -> str:
    return (
        f"v{_identity_suffix(adjustment, source, default_source)}"
        f"-{_REQUEST_ID_PLACEHOLDER}"
    )


__all__ = [
    "ScopeSymbols",
    "estimate_requests",
    "intersect_ranges",
    "merge_ranges",
    "plan_backfill",
    "subtract_ranges",
]
