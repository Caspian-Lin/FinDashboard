"""DataScope 解析器(issue #510)。

把 :class:`DataScopeSpec` 解析成:
* 交易日窗口(``research_window`` 四日期);
* 标的池(explicit 或 instruments 过滤,PIT 上市/退市);
* 每个 required/optional 数据集选中的可用发布;
* 缺口、blockers、候选替代与受影响策略;
* 稳定 ``resolution_id``(scope + release 集合 + resolver 版本)。

纯读:除 #509 逐标的读模型自愈(``ensure_instruments`` 只补
``research_dataset_release_instruments``,不改任何发布)外不写库、不触发
同步/发布。提交 run 时由入队路由重算并比对,冻结精确 release/version/checksum
与 ``research_window``。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_scope.calendar import (
    ScopeCalendarError,
    build_window,
    load_union_calendar,
    markets_for,
)
from finboard_backtest.data_scope.contracts import (
    RESOLVER_VERSION,
    AffectedStrategy,
    DataScopeSpec,
    ResolvedRelease,
    ScopeAlternative,
    ScopeBlocker,
    ScopeFilters,
    ScopeGaps,
    ScopeResolution,
    ScopeUniverseResult,
    ScopeWindow,
    scope_checksum,
)
from finboard_data.datasets import (
    CatalogDataset,
    CatalogDatasetSpec,
    catalog_spec,
)
from finboard_data.releases import ReleaseDatasetKind
from finboard_persistence.catalog_repo import CatalogRepository, ReleaseHeader
from finboard_persistence.models import (
    InstrumentModel,
    ResearchStrategySpecModel,
)
from finboard_shared.types import AssetClass, InstrumentType, ListingStatus

_UNIVERSE_LIMIT = 2000
_PREVIEW_LIMIT = 50
_MISSING_PREVIEW = 200
_ALT_LIMIT = 3


class ScopeResolutionError(ValueError):
    """解析前置条件不满足(日历/锁定参数),API 映射 422。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def resolve_data_scope(
    session: AsyncSession,
    spec: DataScopeSpec,
    *,
    as_of: datetime | None = None,
    resolver_version: str = RESOLVER_VERSION,
    strategy_id: str | None = None,
    strategy_version: int | None = None,
) -> ScopeResolution:
    """解析范围;失败条件以 ``blockers`` 返回而不是抛异常(日历除外)。"""

    resolved_at = as_of or datetime.now(UTC)
    checksum = scope_checksum(spec)
    warnings: list[str] = []
    blockers: list[ScopeBlocker] = []
    alternatives: list[ScopeAlternative] = []

    markets = markets_for(spec)
    try:
        days, exchanges = await load_union_calendar(session, markets)
        window = build_window(
            spec.decision,
            days,
            warmup_sessions=spec.warmup_sessions,
            tail_sessions=spec.tail_sessions,
            exchanges=exchanges,
        )
    except ScopeCalendarError as exc:
        raise ScopeResolutionError(exc.code, exc.message) from exc

    if window.decision_start > spec.decision.start:
        warnings.append(
            f"decision.start={spec.decision.start} 非交易日,已顺延到 "
            f"{window.decision_start}"
        )
    if window.decision_end < spec.decision.end:
        warnings.append(
            f"decision.end={spec.decision.end} 非交易日,已前移到 "
            f"{window.decision_end}"
        )
    if window.warmup_sessions < spec.warmup_sessions:
        warnings.append(
            f"预热不足:请求 {spec.warmup_sessions} 个交易日,发布/日历仅能"
            f"提供 {window.warmup_sessions} 个"
        )
    if window.tail_sessions < spec.tail_sessions:
        warnings.append(
            f"尾部不足:请求 {spec.tail_sessions} 个交易日,日历仅能提供 "
            f"{window.tail_sessions} 个"
        )
    if spec.tail_sessions == 0:
        # multi_period 的最后一个决策日要按下一交易日撮合(dev #502);
        # 零尾部会在执行期才报 research_window_execution_tail_missing。
        warnings.append(
            "tail_sessions=0:若运行按 multi_period 执行,最后一个决策日"
            "没有可撮合的下一交易日,执行期会 fail-closed"
        )

    universe, universe_blockers, resolved_symbols = await _resolve_universe(
        session, spec, window.decision_start, warnings
    )
    blockers.extend(universe_blockers)

    chosen, missing_by_dataset, release_blockers, release_alts = (
        await _select_releases(
            session,
            spec,
            window,
            resolved_symbols,
            warnings,
        )
    )
    blockers.extend(release_blockers)
    alternatives.extend(release_alts)

    bars = chosen.get(CatalogDataset.BARS)
    gaps = ScopeGaps(
        missing_in_bars=missing_by_dataset.get(CatalogDataset.BARS, ()),
        missing_in_research={
            key.value: value[:_MISSING_PREVIEW]
            for key, value in missing_by_dataset.items()
            if key is not CatalogDataset.BARS and value
        },
    )

    if spec.benchmarks:
        if bars is None:
            blockers.append(
                _make_blocker(
                    "scope_benchmark_without_bars",
                    "声明 benchmark 但 bars 主发布未解析",
                    CatalogDataset.BARS,
                )
            )
        else:
            benchmark_missing = await _repo(session).missing_symbols(
                bars.artifact_id, spec.benchmarks
            )
            if benchmark_missing:
                blockers.append(
                    _make_blocker(
                        "scope_benchmark_not_in_bars",
                        "benchmark 不在 bars 主发布: "
                        + ", ".join(benchmark_missing),
                        CatalogDataset.BARS,
                    )
                )

    if spec.release_selection == "locked":
        await _validate_locked_bars(session, spec, blockers)
        _validate_locked(spec, chosen, blockers)
    if strategy_id is not None and strategy_version is not None:
        await _validate_existing_spec(
            session, strategy_id, strategy_version, chosen, blockers
        )

    affected = await _affected_strategies(
        session, tuple(item.artifact_id for item in chosen.values())
    )

    release_ids = tuple(sorted(item.artifact_id for item in chosen.values()))
    resolution_id = _resolution_id(
        checksum, release_ids, resolver_version, window
    )
    return ScopeResolution(
        resolution_id=resolution_id,
        scope_checksum=checksum,
        as_of=resolved_at,
        selection_mode=spec.release_selection,
        window=window,
        releases=tuple(
            chosen[key] for key in sorted(chosen, key=lambda item: item.value)
        ),
        universe=universe,
        gaps=gaps,
        warnings=tuple(warnings),
        blockers=tuple(blockers),
        alternatives=tuple(alternatives),
        affected_strategies=affected,
        resolver_version=resolver_version,
    )


# ---------------------------------------------------------------------------
# 标的池
# ---------------------------------------------------------------------------


async def _resolve_universe(
    session: AsyncSession,
    spec: DataScopeSpec,
    decision_start: date,
    warnings: list[str],
) -> tuple[ScopeUniverseResult, list[ScopeBlocker], list[str]]:
    """解析候选池:返回 ``(预览结果, blockers, 标的列表)``。

    一次查询同时产出「预览统计」与「用于发布缺口检查的标的清单」——
    两段各自查一遍(旧实现)会在排除统计与真实集合之间留下漂移口子。
    空池一律 ``scope_universe_empty`` 阻塞:解析出 0 只标的的范围通过门控
    只会把失败推迟到执行期。
    """

    blockers: list[ScopeBlocker] = []
    filters = spec.universe.filters
    min_listing = timedelta(days=filters.min_listing_days)
    explicit = spec.universe.explicit_symbols
    if explicit:
        rows = (
            await session.execute(
                select(
                    InstrumentModel.code,
                    InstrumentModel.list_date,
                    InstrumentModel.delist_date,
                    InstrumentModel.status,
                ).where(InstrumentModel.code.in_(explicit))
            )
        ).all()
        found = {str(row[0]): row for row in rows}
        missing = [code for code in explicit if code not in found]
        if missing:
            blockers.append(
                _make_blocker(
                    "scope_symbol_not_found",
                    "标的不存在: " + ", ".join(missing[:50]),
                )
            )
        eligible: list[str] = []
        excluded: dict[str, int] = {}
        for code, list_date, delist_date, status in found.values():
            reason = _lifecycle_exclusion(
                list_date,
                delist_date,
                status,
                decision_start,
                min_listing=min_listing,
                filters=filters,
            )
            if reason is not None:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            eligible.append(str(code))
        if excluded:
            warnings.append(
                "显式标的被 PIT 生命周期过滤: "
                + ", ".join(f"{key}={value}" for key, value in sorted(excluded.items()))
            )
        resolved = sorted(eligible)
        if not resolved:
            blockers.append(
                _make_blocker(
                    "scope_universe_empty",
                    f"显式标的在 decision_start={decision_start} 均不可用",
                )
            )
        return (
            ScopeUniverseResult(
                requested_count=len(explicit),
                resolved_count=len(resolved),
                truncated=False,
                symbols_preview=tuple(resolved[:_PREVIEW_LIMIT]),
                excluded=excluded,
            ),
            blockers,
            resolved,
        )

    allowed_types: set[str] = set()
    if spec.universe.asset_classes:
        allowed_types, unmapped = _types_for_classes(spec.universe.asset_classes)
        if unmapped:
            # 未映射大类不能退化成「不加 instrument_type 条件」:那会让商品/
            # 现金范围静默混入全部 A 股,fail-open。
            blockers.append(
                _make_blocker(
                    "scope_asset_class_unmapped",
                    "资产大类没有对应的标的类型映射: "
                    + ", ".join(sorted(unmapped))
                    + "(显式拒绝而不是退化成全市场)",
                )
            )
            return (
                ScopeUniverseResult(
                    requested_count=0,
                    resolved_count=0,
                    truncated=False,
                    symbols_preview=(),
                ),
                blockers,
                [],
            )

    conditions: list[Any] = [
        InstrumentModel.list_date <= decision_start - min_listing,
    ]
    if spec.universe.markets:
        conditions.append(
            InstrumentModel.market.in_([item.value for item in spec.universe.markets])
        )
    if allowed_types:
        conditions.append(
            InstrumentModel.instrument_type.in_(sorted(allowed_types))
        )
    if spec.universe.instrument_types:
        conditions.append(
            InstrumentModel.instrument_type.in_(
                [item.value for item in spec.universe.instrument_types]
            )
        )
    if filters.exclude_delisted:
        conditions.append(
            (InstrumentModel.delist_date.is_(None))
            | (InstrumentModel.delist_date >= decision_start)
        )
        conditions.append(InstrumentModel.status != ListingStatus.DELISTED.value)
    if filters.exclude_suspended:
        conditions.append(InstrumentModel.status != ListingStatus.SUSPENDED.value)
    if filters.exclude_benchmark_only:
        conditions.append(
            InstrumentModel.instrument_type.notin_(
                (InstrumentType.INDEX.value, InstrumentType.FUTURES.value)
            )
        )
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(InstrumentModel).where(*conditions)
            )
        ).scalar_one()
    )
    limit = spec.universe.selection_limit or _UNIVERSE_LIMIT
    truncated = total > limit
    if truncated:
        warnings.append(
            f"标的池 {total} 只超过上限 {limit},仅解析前 {limit} 只"
        )
    selected_rows = (
        await session.execute(
            select(InstrumentModel.code)
            .where(*conditions)
            .order_by(InstrumentModel.code)
            .limit(limit)
        )
    ).all()
    resolved = [str(row[0]) for row in selected_rows]
    if not resolved:
        blockers.append(
            _make_blocker(
                "scope_universe_empty",
                f"过滤条件在 decision_start={decision_start} 解析出 0 只标的",
            )
        )
    return (
        ScopeUniverseResult(
            requested_count=total,
            resolved_count=len(resolved),
            truncated=truncated,
            symbols_preview=tuple(resolved[:_PREVIEW_LIMIT]),
        ),
        blockers,
        resolved,
    )


def _lifecycle_exclusion(
    list_date: date | None,
    delist_date: date | None,
    status: str,
    decision_start: date,
    *,
    min_listing: timedelta,
    filters: ScopeFilters,
) -> str | None:
    if list_date is None or list_date > decision_start:
        return "not_listed"
    if list_date > decision_start - min_listing:
        return "listing_too_short"
    if filters.exclude_delisted:
        if status == ListingStatus.DELISTED.value:
            return "delisted"
        if delist_date is not None and delist_date < decision_start:
            return "delisted"
    if filters.exclude_suspended and status == ListingStatus.SUSPENDED.value:
        return "suspended"
    return None


def _types_for_classes(
    classes: tuple[object, ...],
) -> tuple[set[str], set[str]]:
    """资产大类 → 标的类型;返回 ``(允许类型, 未映射大类)``。

    未映射绝不能回退成「不加 instrument_type 条件」——那会让商品/现金
    范围静默混入全部 A 股(实测 ``asset_classes=["commodity"]`` 解析出
    6 只正股),属于 fail-open。调用方据此产出 blocker。
    """

    allowed = {str(item) for item in classes}
    types: set[str] = set()
    mapped: set[str] = set()
    if AssetClass.EQUITY.value in allowed:
        types |= {InstrumentType.STOCK.value, InstrumentType.ETF.value}
        mapped.add(AssetClass.EQUITY.value)
    if AssetClass.FIXED_INCOME.value in allowed:
        types |= {InstrumentType.BOND.value}
        mapped.add(AssetClass.FIXED_INCOME.value)
    if AssetClass.CONVERTIBLE.value in allowed:
        types |= {InstrumentType.CONVERTIBLE.value}
        mapped.add(AssetClass.CONVERTIBLE.value)
    if AssetClass.DERIVATIVE.value in allowed:
        types |= {InstrumentType.FUTURES.value}
        mapped.add(AssetClass.DERIVATIVE.value)
    if AssetClass.COMMODITY.value in allowed:
        # 商品期货与金融期货同属 futures 标的类型;交易所/日历由 markets
        # 决定(FUTURE → 6 家商品+金融交易所并集)。
        types |= {InstrumentType.FUTURES.value}
        mapped.add(AssetClass.COMMODITY.value)
    return types, allowed - mapped


# ---------------------------------------------------------------------------
# 发布选择
# ---------------------------------------------------------------------------


async def _select_releases(
    session: AsyncSession,
    spec: DataScopeSpec,
    window: ScopeWindow,
    symbols: list[str],
    warnings: list[str],
) -> tuple[
    dict[CatalogDataset, ResolvedRelease],
    dict[CatalogDataset, tuple[str, ...]],
    list[ScopeBlocker],
    list[ScopeAlternative],
]:
    repo = _repo(session)
    chosen: dict[CatalogDataset, ResolvedRelease] = {}
    missing_by_dataset: dict[CatalogDataset, tuple[str, ...]] = {}
    blockers: list[ScopeBlocker] = []
    alternatives: list[ScopeAlternative] = []
    symbol_tuple = tuple(symbols)

    for dataset in (*spec.datasets.required, *spec.datasets.optional):
        dataset_spec = catalog_spec(dataset)
        if dataset_spec.release_kind is None:
            if dataset in spec.datasets.required:
                blockers.append(
                    _make_blocker(
                        "scope_dataset_not_publishable",
                        f"{dataset.value} 没有发布形态,不能作为 scope 数据集",
                        dataset,
                    )
                )
            continue
        headers = await _candidate_headers(
            repo, spec, dataset_spec, warnings
        )
        if not headers:
            if dataset in spec.datasets.required:
                blockers.append(
                    _make_blocker(
                        "scope_release_unavailable",
                        f"{dataset.value} 没有覆盖窗口的可用发布",
                        dataset,
                    )
                )
            else:
                warnings.append(f"可选数据集 {dataset.value} 无可用发布")
            continue

        is_bars = dataset_spec.release_kind is ReleaseDatasetKind.BARS
        expected_adjustment = spec.bars_adjustment if is_bars else None
        best: tuple[ResolvedRelease, tuple[str, ...]] | None = None
        best_bars_missing: tuple[ReleaseHeader, tuple[str, ...]] | None = None
        for header in headers:
            missing = await _check_header(
                repo,
                header,
                window,
                symbol_tuple,
                expected_adjustment=expected_adjustment,
            )
            if missing is None:
                continue
            if is_bars and missing:
                if (
                    best_bars_missing is None
                    or len(missing) < len(best_bars_missing[1])
                ):
                    best_bars_missing = (header, missing)
                continue
            if best is None or len(missing) < len(best[1]):
                best = (
                    ResolvedRelease(
                        artifact_id=header.release_id,
                        dataset=dataset,
                        role="bars_primary" if is_bars else "research",
                        version=header.version,
                        checksum=header.release_checksum,
                        published_at=header.published_at,
                        start_date=header.start_date,
                        end_date=header.end_date,
                        coverage_pct=header.coverage_pct,
                        symbol_count=header.symbol_count,
                        reason=(
                            "窗口覆盖 + 标的齐全"
                            if not missing
                            else f"窗口覆盖,缺 {len(missing)} 只标的"
                        ),
                    ),
                    missing,
                )
            if not missing:
                # 完美命中即停止探测:每份候选都要 ensure_instruments +
                # missing_symbols,50 份候选逐个自愈是纯浪费。
                break
        if best is None:
            if is_bars and best_bars_missing is not None:
                missing_by_dataset[dataset] = best_bars_missing[1][:_MISSING_PREVIEW]
            if dataset in spec.datasets.required:
                detail = (
                    f",最少缺 {len(best_bars_missing[1])} 只标的"
                    if is_bars and best_bars_missing is not None
                    else ""
                )
                blockers.append(
                    _make_blocker(
                        "scope_release_no_match",
                        f"{dataset.value} 候选发布均不满足窗口/标的/复权约束{detail}",
                        dataset,
                    )
                )
            continue
        resolved, missing = best
        if dataset in spec.datasets.optional:
            # 可选数据集只做「有没有、缺多少」的观测,不进入冻结发布集合:
            # 冻结集合必须与策略 validation_plan.dataset_release_ids 完全
            # 一致,把 optional 放进去会让每次提交都 409。
            alternatives.append(
                ScopeAlternative(
                    dataset=dataset,
                    release_id=resolved.artifact_id,
                    version=resolved.version,
                    reason=(
                        "可选数据集可用(不进入冻结集合,需要时加入 required)"
                        if not missing
                        else f"可选数据集可用但缺 {len(missing)} 只标的(容忍)"
                    ),
                )
            )
            if missing:
                warnings.append(
                    f"可选研究发布 {dataset.value} 缺 {len(missing)} 只标的(容忍)"
                )
            continue
        chosen[dataset] = resolved
        missing_by_dataset[dataset] = missing
        if missing and not is_bars:
            # 研究数据发布只提供因子观测:缺标的是容忍项,但必须具名可见,
            # 否则「覆盖不全」会在执行期表现为因子值缺失。
            warnings.append(
                f"研究数据发布 {dataset.value} 缺 {len(missing)} 只标的(容忍,"
                "相关标的该数据集观测为空)"
            )
        for header in headers[:_ALT_LIMIT]:
            if header.release_id == resolved.artifact_id:
                continue
            alternatives.append(
                ScopeAlternative(
                    dataset=dataset,
                    release_id=header.release_id,
                    version=header.version,
                    reason="较旧/覆盖较弱候选(未选中)",
                )
            )

    return chosen, missing_by_dataset, blockers, alternatives


async def _candidate_headers(
    repo: CatalogRepository,
    spec: DataScopeSpec,
    dataset_spec: CatalogDatasetSpec,
    warnings: list[str],
) -> list[ReleaseHeader]:
    if spec.release_selection == "locked":
        headers = [
            header
            for release_id in sorted(spec.locked_release_ids)
            if (header := await repo.release_header(release_id)) is not None
            and header.dataset_kind is dataset_spec.release_kind
        ]
        if not headers:
            warnings.append(
                f"locked_release_ids 中没有 {dataset_spec.key.value} 的发布"
            )
        return headers
    headers, _ = await repo.list_releases(
        kind=dataset_spec.release_kind, limit=50
    )
    return headers


async def _check_header(
    repo: CatalogRepository,
    header: ReleaseHeader,
    window: ScopeWindow,
    symbols: tuple[str, ...],
    *,
    expected_adjustment: str | None,
) -> tuple[str, ...] | None:
    if header.quality_status not in ("passed", "warnings"):
        return None
    if header.start_date > window.warmup_start or header.end_date < window.valuation_end:
        return None
    if expected_adjustment is not None and header.adjustment != expected_adjustment:
        return None
    if not await repo.ensure_instruments(header.release_id):
        return None
    return await repo.missing_symbols(header.release_id, symbols)


def _make_blocker(
    code: str, message: str, dataset: CatalogDataset | None = None
) -> ScopeBlocker:
    return ScopeBlocker(code=code, message=message, dataset=dataset)


async def _validate_locked_bars(
    session: AsyncSession,
    spec: DataScopeSpec,
    blockers: list[ScopeBlocker],
) -> None:
    """locked 模式必须恰好锁定一份 bars 主发布。

    按 ``locked_release_ids`` 里 kind=BARS 的**发布数**判断:``chosen`` 按
    数据集去重后最多一份 bars,数它永远看不到「锁了两份」。
    """

    repo = _repo(session)
    bars = [
        header
        for release_id in sorted(spec.locked_release_ids)
        if (header := await repo.release_header(release_id)) is not None
        and header.dataset_kind is ReleaseDatasetKind.BARS
    ]
    if len(bars) != 1:
        blockers.append(
            _make_blocker(
                "scope_locked_bars_ambiguous",
                f"locked 模式必须恰好锁定一份 bars 主发布,实际 {len(bars)} 份",
                CatalogDataset.BARS,
            )
        )


def _validate_locked(
    spec: DataScopeSpec,
    chosen: dict[CatalogDataset, ResolvedRelease],
    blockers: list[ScopeBlocker],
) -> None:
    """locked 模式的确定性校验(发布数量与 required 覆盖)。

    bars 唯一性必须按 ``locked_release_ids`` 里 kind=BARS 的**发布数**判断:
    ``chosen`` 按数据集去重,最多一份 bars,数它永远看不到「锁了两份」。
    """

    for dataset in spec.datasets.required:
        if dataset not in chosen:
            blockers.append(
                _make_blocker(
                    "scope_locked_release_missing",
                    f"locked 模式缺少 required 数据集 {dataset.value}",
                    dataset,
                )
            )


async def _validate_existing_spec(
    session: AsyncSession,
    strategy_id: str,
    strategy_version: int,
    chosen: dict[CatalogDataset, ResolvedRelease],
    blockers: list[ScopeBlocker],
) -> None:
    row = (
        await session.execute(
            select(ResearchStrategySpecModel.payload).where(
                ResearchStrategySpecModel.strategy_id == strategy_id,
                ResearchStrategySpecModel.version == strategy_version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        blockers.append(
            _make_blocker(
                "scope_strategy_spec_not_found",
                f"策略规格 {strategy_id}@{strategy_version} 不存在",
            )
        )
        return
    plan = (row or {}).get("validation_plan") if isinstance(row, dict) else None
    frozen = set(plan.get("dataset_release_ids", [])) if isinstance(plan, dict) else set()
    resolved = {item.artifact_id for item in chosen.values()}
    if frozen != resolved:
        blockers.append(
            _make_blocker(
                "spec_release_mismatch",
                "scope 解析的发布集合与策略 validation_plan 不一致: "
                f"spec={sorted(frozen)}, scope={sorted(resolved)}",
            )
        )


async def _affected_strategies(
    session: AsyncSession, release_ids: tuple[str, ...]
) -> tuple[AffectedStrategy, ...]:
    rows = await _repo(session).strategy_refs_for_releases(release_ids)
    return tuple(
        AffectedStrategy(
            strategy_id=strategy_id,
            version=version,
            name=name,
            reason="validation_plan 引用了本次解析的发布",
        )
        for strategy_id, version, name, _status in rows
    )


def _resolution_id(
    checksum: str,
    release_ids: tuple[str, ...],
    resolver_version: str,
    window: ScopeWindow,
) -> str:
    """内容指纹:scope + 发布集合 + 解析器版本 + **解析出的窗口**。

    窗口来自 ``trade_cal``,与 scope 声明无关(预热/尾部落位、非交易日
    顺延都在解析期发生)。不把窗口纳入指纹时,日历补日/修正后同一份预览
    仍能通过复验,静默冻结与预览不同的窗口。
    """

    window_key = (
        f"{window.warmup_start}|{window.decision_start}|"
        f"{window.decision_end}|{window.valuation_end}|"
        f"{window.warmup_sessions}|{window.decision_sessions}|"
        f"{window.tail_sessions}|{','.join(window.calendar_markets)}"
    )
    digest = hashlib.sha256(
        f"{checksum}|{','.join(release_ids)}|{resolver_version}|{window_key}".encode()
    ).hexdigest()[:16]
    return f"SC-{digest}"


def _repo(session: AsyncSession) -> CatalogRepository:
    return CatalogRepository(session)


__all__ = [
    "ScopeResolutionError",
    "resolve_data_scope",
]
