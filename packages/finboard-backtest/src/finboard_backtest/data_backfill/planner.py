"""补数预检 —— 纯读的缺口计划(issue #511,方案 5.2)。

输入一份 :class:`BackfillRequestSpec`,输出 :class:`BackfillPlan`:

1. **窗口**:复用 #510 的交易日窗口(``trade_cal`` 并集 + 预热/尾部),显式
   ``start/end`` 必须落在窗口内 —— 补数不能顺手扩大课题范围;
2. **已完成覆盖**:
   * 可发布数据集以 ``research_dataset_releases`` 的**可用发布**为准(逐标的
     派生索引 + 区间,与 #509 目录同一读模型);
   * 不可发布数据集以 ``research_sync_batches`` 的已发布批次为准;
3. **在途覆盖**:``data_backfill_requests`` 中 ``queued|running`` 且
   dataset / source / adjustment 一致、区间重叠且标的是请求标的超集的 item;
4. ``missing_ranges = 请求区间 - (已完成 并集 在途)``,
   ``missing_symbols = 请求符号 - 已完成``;
5. ``action`` 判定 / ``estimated_requests`` / 配额 / 发布影响 / blockers。

**异源与不同复权不混接**:``source`` 与 ``adjustment`` 是覆盖匹配的必要条件;
不匹配的既有覆盖不参与复用,item 带独立 warning 单独成项。

**缺口口径按同步单元切**:

* 按日全市场(``DAILY_MARKET``)与全量分页(``FULL_PAGED``)的同步单元是
  「交易日 / 整市场」,缺口按窗口级发布覆盖算;标的级缺失只在
  ``missing_symbols`` 里具名(发布时按请求标的冻结);
* 逐标的(``PER_SYMBOL_RANGE``)与 bars(``bulk_download``)的同步单元是
  「标的 x 区间」,缺口按**逐标的区间并集**算 —— 某标的缺的区间不会被别的
  标的的覆盖掩盖。

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

#: 可用发布的 quality_status 白名单(与 #509 目录 ``_USABLE_STATUSES`` 同口径)。
_USABLE_RELEASE_STATUSES = ("passed", "warnings")

#: 已发布批次状态(``dataset_sync`` 质量门通过后置 ``published``)。
_PUBLISHED_BATCH_STATUS = "published"

#: 单符号下推上限:全市场请求(几千标的)不做 ``IN`` 下推,退回发布级区间覆盖
#: —— 避免超长参数列表,也避免逐标的循环把预检拖成分钟级。
_MAX_SYMBOL_FILTER = 2000

#: bars 单次上游请求的窗口上限(天)。与 ``TushareBarProvider._MAX_CHUNK_DAYS``
#: (15 x 366)同量级:provider 内部按此分页,预检按同一口径估请求数。
_BARS_CHUNK_DAYS = 15 * 366

#: 发布身份模板里的占位:预检时请求 ID 尚未生成,真正的 release_id 在执行器
#: 用 ``request_id`` 拼出(``DR-<dataset>-<BF-...>``)。
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


@dataclass(slots=True)
class _DatasetCoverage:
    """一个数据集在窗口内的已完成覆盖汇总。"""

    sources: list[BackfillCoverage] = field(default_factory=list)
    ranges: list[DateRange] = field(default_factory=list)
    by_symbol: dict[str, list[DateRange]] = field(default_factory=dict)
    release_ids: list[str] = field(default_factory=list)
    anomalies: int = 0
    duplicates: int = 0


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
    symbols = await _resolve_symbols(session, spec.scope)

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
        start, end = _item_window(dataset_spec, window)
        item_blockers: list[BackfillBlocker] = []
        item_symbols = tuple(sorted(dataset_spec.symbols)) or symbols.symbols
        item_types = await _resolve_item_types(session, item_symbols, symbols)
        source = dataset_spec.source or support.source
        adjustment = _resolve_adjustment(support, dataset_spec.adjustment)
        shape = _sync_shape(support)

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
            symbols=item_symbols,
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

        missing_after_completed = _gap_ranges(
            support=support,
            shape=shape,
            symbols=item_symbols,
            completed=completed,
            start=start,
            end=end,
        )
        missing_symbols = _missing_symbols(item_symbols, completed, start, end)
        missing_ranges = subtract_ranges(missing_after_completed, inflight_ranges)
        reuse_only = bool(missing_after_completed) and not missing_ranges

        repair = (
            support.sync_mode == "bulk_download"
            and bool(completed.release_ids)
            and (
                completed.anomalies > 0
                or completed.duplicates > 0
                or spec.quality_profile == "strict"
            )
        )
        action = _decide_action(
            support=support,
            missing_ranges=missing_ranges,
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
                symbol_count=len(item_symbols),
            )
        total_estimated += estimated

        item_blockers.extend(
            _item_blockers(
                support=support,
                capabilities=caps,
                dataset=dataset_spec.dataset,
                publish=dataset_spec.publish,
                has_gap=bool(missing_ranges) or repair,
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
                release_id=f"DR-{dataset_spec.dataset.value}-{_REQUEST_ID_PLACEHOLDER}",
                version=f"v-{_REQUEST_ID_PLACEHOLDER}",
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
            )
        ]

        items.append(
            BackfillItem(
                dataset=dataset_spec.dataset,
                source=source,
                adjustment=adjustment,
                action=action,
                start=start,
                end=end,
                publish=dataset_spec.publish and support.publishable,
                symbols=item_symbols if dataset_spec.symbols else (),
                missing_ranges=missing_ranges,
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


def _decide_action(
    *,
    support: BackfillSupport,
    missing_ranges: Sequence[DateRange],
    has_completed_gap: bool,
    reuse_only: bool,
    repair: bool,
) -> BackfillAction:
    """动作判定(无缺口 → noop/reuse;有缺口 → sync / sync_publish / repair)。"""

    if not missing_ranges and not repair:
        return (
            BackfillAction.REUSE
            if (reuse_only or has_completed_gap)
            else BackfillAction.NOOP
        )
    if repair:
        return BackfillAction.REPAIR
    if support.publishable and support.release_kind is not None:
        return BackfillAction.SYNC_PUBLISH
    return BackfillAction.SYNC


def _item_blockers(
    *,
    support: BackfillSupport,
    capabilities: BackfillCapabilities,
    dataset: CatalogDataset,
    publish: bool,
    has_gap: bool,
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
    if publish and support.publishable and not capabilities.can_publish:
        outcome.append(
            BackfillBlocker(
                code="backfill_release_root_unwritable",
                message="发布根目录不可写,无法冻结新发布",
                dataset=dataset,
            )
        )
    return outcome


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
    if per_symbol and symbols and len(symbols) <= _MAX_SYMBOL_FILTER:
        gaps: list[DateRange] = []
        for code in symbols:
            gaps.extend(subtract_ranges(((start, end),), completed.by_symbol.get(code, [])))
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
) -> tuple[tuple[DateRange, ...], set[str]]:
    """在途明细里 dataset/source/adjustment 一致且标的超集匹配的部分。

    **异源 / 不同复权 / 更窄的标的集合不参与复用** —— 只返回匹配项,调用方
    据此保留独立缺口,不做「看起来像就复用」的近似。
    """

    wanted = {item.strip().upper() for item in symbols if item.strip()}
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
        if row_symbols and wanted and not row_symbols >= wanted:
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
        bucket.ranges.append((row[3], row[4]))
        bucket.by_symbol.setdefault(str(row[2]), []).append((row[3], row[4]))
        bucket.anomalies += int(row[5] or 0)
        bucket.duplicates += int(row[6] or 0)

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


__all__ = [
    "ScopeSymbols",
    "estimate_requests",
    "intersect_ranges",
    "merge_ranges",
    "plan_backfill",
    "subtract_ranges",
]
