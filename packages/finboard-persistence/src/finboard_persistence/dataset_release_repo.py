"""研究数据集发布登记与发布候选元数据快照(issue #77)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.releases import (
    DatasetReleaseSpec,
    ExecutionMetadata,
    FrozenDatasetReleaseBuilder,
    ImmutableReleaseError,
    ReleaseCapabilityError,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
    ReleaseLifecycleEvent,
    ResearchDatasetRelease,
    ResearchEtfCatalogEntry,
    default_execution_metadata,
    research_etf_catalog_entry,
    symbol_set_diff,
)
from finboard_data.research import DailySecurityMetrics, FinancialIndicator
from finboard_persistence.models import (
    ConvertibleMetadataModel,
    EtfMetadataModel,
    FuturesContractModel,
    InstrumentLifecycleEventModel,
    InstrumentModel,
    InstrumentNameModel,
    ResearchDailyMetricModel,
    ResearchDatasetReleaseModel,
    ResearchFinancialIndicatorModel,
    ResearchInstrumentProfileModel,
)
from finboard_persistence.profile_metadata import ProfileMetadataLookup
from finboard_shared.types import (
    AssetClass,
    ConvertibleEventType,
    DatasetQualityStatus,
    EtfCategory,
    EtfExecutionProfile,
    FuturesEventType,
    InstrumentType,
    ListingStatus,
    Market,
    ReviewStatus,
    etf_category_from_execution_profile,
)

_CONVERTIBLE_REQUIRED_EVENTS = (
    ConvertibleEventType.FORCED_REDEMPTION.value,
    ConvertibleEventType.SELL_BACK.value,
    ConvertibleEventType.DOWNWARD_REVISION.value,
    ConvertibleEventType.CONVERSION_PRICE_ADJUST.value,
)
_FUTURES_REQUIRED_EVENTS = (
    FuturesEventType.ROLL.value,
    FuturesEventType.EXPIRATION.value,
    FuturesEventType.DELIVERY.value,
)


class ResearchDatasetReleaseRepository:
    """不可变研究发布仓储。

    相同 release/checksum 可幂等登记;任何同 ID 或同版本的内容漂移都拒绝。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(
        self,
        release: ResearchDatasetRelease,
    ) -> ResearchDatasetReleaseModel:
        existing = await self._find_identity(
            release.release_id,
            release.dataset_name,
            release.source,
            release.version,
        )
        if existing is not None:
            if existing.release_checksum != release.release_checksum:
                raise ImmutableReleaseError(f"发布身份已登记但 checksum 不同: {release.release_id}")
            return existing
        if not release.is_usable:
            raise ValueError("仅允许登记通过质量门的研究数据发布")

        row = ResearchDatasetReleaseModel(
            release_id=release.release_id,
            dataset_name=release.dataset_name,
            source=release.source,
            version=release.version,
            schema_version=release.schema_version,
            start_date=release.start_date,
            end_date=release.end_date,
            period=release.period.value,
            adjustment=release.adjustment,
            code_version=release.code_version,
            metadata_version=release.metadata_version,
            symbol_count=release.symbol_count,
            row_count=release.row_count,
            coverage_pct=release.coverage_pct,
            quality_status=release.quality_status.value,
            capabilities=[item.as_dict() for item in release.capabilities],
            quality_report=release.quality_report,
            known_limitations=list(release.known_limitations),
            storage_uri=release.storage_uri,
            release_checksum=release.release_checksum,
            manifest=release.as_dict(),
            published_at=release.published_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, release_id: str) -> ResearchDatasetRelease | None:
        stmt = select(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.release_id == release_id
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _release_from_row(row) if row is not None else None

    async def require_usable(
        self,
        release_id: str,
        *,
        capabilities: tuple[str, ...] = (),
    ) -> ResearchDatasetRelease:
        release = await self.get(release_id)
        if release is None:
            raise ReleaseCapabilityError(f"研究数据发布不存在: {release_id}")
        if not release.is_usable:
            raise ReleaseCapabilityError(
                f"研究数据发布不可用: {release_id} quality={release.quality_status.value}"
            )
        for capability in capabilities:
            release.require_capability(capability)
        return release

    async def latest_usable(
        self,
        *,
        dataset_name: str,
        source: str | None = None,
    ) -> ResearchDatasetRelease | None:
        stmt = select(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.dataset_name == dataset_name,
            ResearchDatasetReleaseModel.quality_status.in_(
                (
                    DatasetQualityStatus.PASSED.value,
                    DatasetQualityStatus.WARNINGS.value,
                )
            ),
        )
        if source is not None:
            stmt = stmt.where(ResearchDatasetReleaseModel.source == source)
        stmt = stmt.order_by(ResearchDatasetReleaseModel.published_at.desc()).limit(1)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _release_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        dataset_name: str | None = None,
        source: str | None = None,
        quality_status: str | None = None,
        limit: int = 50,
    ) -> list[ResearchDatasetRelease]:
        stmt = select(ResearchDatasetReleaseModel)
        if dataset_name is not None:
            stmt = stmt.where(ResearchDatasetReleaseModel.dataset_name == dataset_name)
        if source is not None:
            stmt = stmt.where(ResearchDatasetReleaseModel.source == source)
        if quality_status is not None:
            stmt = stmt.where(ResearchDatasetReleaseModel.quality_status == quality_status)
        stmt = stmt.order_by(ResearchDatasetReleaseModel.published_at.desc()).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_release_from_row(row) for row in rows]

    async def _find_identity(
        self,
        release_id: str,
        dataset_name: str,
        source: str,
        version: str,
    ) -> ResearchDatasetReleaseModel | None:
        stmt = select(ResearchDatasetReleaseModel).where(
            (ResearchDatasetReleaseModel.release_id == release_id)
            | (
                (ResearchDatasetReleaseModel.dataset_name == dataset_name)
                & (ResearchDatasetReleaseModel.source == source)
                & (ResearchDatasetReleaseModel.version == version)
            )
        )
        return (await self._session.execute(stmt)).scalars().first()


class ReleaseInstrumentCatalogRepository:
    """把 #35/#58 元数据表冻结为 ``ReleaseInstrumentSpec``。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_candidates(
        self,
        symbols: list[str],
    ) -> list[ReleaseInstrumentSpec]:
        normalized = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not normalized:
            raise ValueError("symbols 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols 包含重复代码")

        instrument_rows = await self._instrument_map(normalized)
        etf_rows = await self._etf_map(normalized)
        convertible_rows = await self._convertible_map(normalized)
        futures_rows = await self._futures_map(normalized)
        names = await self._name_history(normalized)
        events = await self._events(normalized)
        # 兜底源(issue #185):instruments 的 list_date/industry 为 null 时,
        # 用最近一次已发布档案(research_instrument_profiles)补齐,不新建读路径。
        profiles = await ProfileMetadataLookup(self._session).profiles(normalized)

        result: list[ReleaseInstrumentSpec] = []
        missing: list[str] = []
        for code in normalized:
            row = instrument_rows.get(code)
            future = futures_rows.get(code)
            if future is not None:
                lifecycle_events = events.get(code, ())
                result.append(
                    _future_candidate(
                        future,
                        lifecycle_events=lifecycle_events,
                    )
                )
                continue
            catalog = research_etf_catalog_entry(code)
            if row is None and catalog is None:
                missing.append(code)
                continue
            if row is None:
                assert catalog is not None
                result.append(_catalog_etf_candidate(catalog))
                continue

            instrument_type = InstrumentType(row.instrument_type)
            if instrument_type is InstrumentType.ETF:
                bare = code.split(".", 1)[0] if "." in code else code
                result.append(
                    _etf_candidate(
                        row,
                        etf_rows.get(code) or etf_rows.get(bare),
                        profile=profiles.get(code),
                        lifecycle_events=events.get(code, ()),
                        name_history=names.get(code, ()),
                    )
                )
            elif instrument_type is InstrumentType.CONVERTIBLE:
                lifecycle_events = events.get(code, ())
                result.append(
                    _convertible_candidate(
                        row,
                        convertible_rows.get(code),
                        lifecycle_events=lifecycle_events,
                        name_history=names.get(code, ()),
                    )
                )
            else:
                result.append(
                    _plain_candidate(
                        row,
                        instrument_type=instrument_type,
                        profile=profiles.get(code),
                        lifecycle_events=events.get(code, ()),
                        name_history=names.get(code, ()),
                    )
                )
        if missing:
            raise ReleaseCapabilityError(
                f"以下标的缺少 #35/#58 元数据,禁止猜测: {','.join(missing)}"
            )
        return result

    async def _instrument_map(self, symbols: list[str]) -> dict[str, InstrumentModel]:
        stmt = select(InstrumentModel).where(InstrumentModel.code.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.code: row for row in rows}

    async def _etf_map(self, symbols: list[str]) -> dict[str, EtfMetadataModel]:
        lookup = set(symbols)
        lookup.update({s.split(".", 1)[0] for s in symbols if "." in s})
        stmt = select(EtfMetadataModel).where(EtfMetadataModel.code.in_(lookup))
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, EtfMetadataModel] = {}
        for row in rows:
            result[row.code] = row
            bare = row.code.split(".", 1)[0]
            if bare != row.code:
                result[bare] = row
        return result

    async def _convertible_map(
        self,
        symbols: list[str],
    ) -> dict[str, ConvertibleMetadataModel]:
        stmt = select(ConvertibleMetadataModel).where(ConvertibleMetadataModel.code.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.code: row for row in rows}

    async def _futures_map(
        self,
        symbols: list[str],
    ) -> dict[str, FuturesContractModel]:
        stmt = select(FuturesContractModel).where(FuturesContractModel.contract_code.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.contract_code: row for row in rows}

    async def _name_history(
        self,
        symbols: list[str],
    ) -> dict[str, tuple[tuple[str, date, date | None], ...]]:
        stmt = (
            select(InstrumentNameModel)
            .where(InstrumentNameModel.instrument_code.in_(symbols))
            .order_by(
                InstrumentNameModel.instrument_code,
                InstrumentNameModel.valid_from,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, list[tuple[str, date, date | None]]] = {}
        for row in rows:
            result.setdefault(row.instrument_code, []).append(
                (row.name, row.valid_from, row.valid_to)
            )
        return {code: tuple(items) for code, items in result.items()}

    async def _events(
        self,
        symbols: list[str],
    ) -> dict[str, tuple[ReleaseLifecycleEvent, ...]]:
        stmt = (
            select(InstrumentLifecycleEventModel)
            .where(InstrumentLifecycleEventModel.symbol.in_(symbols))
            .order_by(
                InstrumentLifecycleEventModel.symbol,
                InstrumentLifecycleEventModel.effective_date,
                InstrumentLifecycleEventModel.available_at,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, list[ReleaseLifecycleEvent]] = {}
        for row in rows:
            available_at = row.available_at
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
            result.setdefault(row.symbol, []).append(
                ReleaseLifecycleEvent(
                    event_type=row.event_type,
                    effective_date=row.effective_date,
                    available_at=available_at,
                    source=row.source,
                    dataset_version=row.dataset_version,
                    details=row.details,
                )
            )
        return {code: tuple(items) for code, items in result.items()}


class ResearchDatasetReleaseService:
    """文件原子发布 + 数据库不可变登记的应用服务。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cache_dir: str | Path,
        release_root: str | Path,
    ) -> None:
        self._release_repo = ResearchDatasetReleaseRepository(session)
        self._catalog_repo = ReleaseInstrumentCatalogRepository(session)
        self._builder = FrozenDatasetReleaseBuilder(
            cache_dir=cache_dir,
            release_root=release_root,
            research_source=ResearchTableReleaseSource(session),
        )

    async def publish(
        self,
        spec: DatasetReleaseSpec,
        symbols: list[str],
    ) -> ResearchDatasetRelease:
        previous = await self._release_repo.latest_usable(
            dataset_name=spec.dataset_name,
            source=spec.source,
        )
        if spec.dataset_kind is not ReleaseDatasetKind.BARS:
            self._require_a_share_stock_scope(spec, symbols)
        candidates = await self._catalog_repo.list_candidates(symbols)
        release = await self._builder.publish(
            spec,
            candidates,
            previous_release=previous,
        )
        await self._release_repo.publish(release)
        return release

    @staticmethod
    def _require_a_share_stock_scope(
        spec: DatasetReleaseSpec,
        symbols: list[str],
    ) -> None:
        """研究数据(非 bars)只支持 A 股股票(range 内推定为 A 股发布)。"""
        if spec.source != "tushare" or spec.dataset_kind not in (
            ReleaseDatasetKind.DAILY_METRICS,
            ReleaseDatasetKind.FINANCIAL_INDICATORS,
        ):
            raise ReleaseCapabilityError(
                f"{spec.dataset_kind.value} 发布要求 source=tushare "
                f"(研究数据来自 research_data_sync 摄取)"
            )
        if not symbols:  # pragma: no cover - 调用方已校验非空
            raise ReleaseCapabilityError("发布标的不能为空")


class ResearchTableReleaseSource:
    """从 ``research_*`` 表读取研究数据作为发布冻结输入的注入实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def daily_metrics(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DailySecurityMetrics]]:
        stmt = (
            select(ResearchDailyMetricModel)
            .where(
                ResearchDailyMetricModel.symbol.in_(symbols),
                ResearchDailyMetricModel.trade_date >= start_date,
                ResearchDailyMetricModel.trade_date <= end_date,
            )
            .order_by(
                ResearchDailyMetricModel.symbol,
                ResearchDailyMetricModel.trade_date,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, list[DailySecurityMetrics]] = {}
        for row in rows:
            result.setdefault(row.symbol, []).append(_daily_metrics_from_row(row))
        return result

    async def financial_indicators(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[FinancialIndicator]]:
        stmt = (
            select(ResearchFinancialIndicatorModel)
            .where(
                ResearchFinancialIndicatorModel.symbol.in_(symbols),
                ResearchFinancialIndicatorModel.report_period >= start_date,
                ResearchFinancialIndicatorModel.report_period <= end_date,
            )
            .order_by(
                ResearchFinancialIndicatorModel.symbol,
                ResearchFinancialIndicatorModel.report_period,
                ResearchFinancialIndicatorModel.announcement_date,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, list[FinancialIndicator]] = {}
        for row in rows:
            result.setdefault(row.symbol, []).append(_financial_indicators_from_row(row))
        return result


def _daily_metrics_from_row(row: ResearchDailyMetricModel) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=row.symbol,
        trade_date=row.trade_date,
        close=row.close,
        turnover_rate=row.turnover_rate,
        turnover_rate_free=row.turnover_rate_free,
        volume_ratio=row.volume_ratio,
        pe=row.pe,
        pe_ttm=row.pe_ttm,
        pb=row.pb,
        ps=row.ps,
        ps_ttm=row.ps_ttm,
        dividend_yield=row.dividend_yield,
        dividend_yield_ttm=row.dividend_yield_ttm,
        total_shares=row.total_shares,
        float_shares=row.float_shares,
        free_shares=row.free_shares,
        total_market_cap=row.total_market_cap,
        circulating_market_cap=row.circulating_market_cap,
        limit_status=row.limit_status,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _financial_indicators_from_row(
    row: ResearchFinancialIndicatorModel,
) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=row.symbol,
        announcement_date=row.announcement_date,
        report_period=row.report_period,
        update_flag=row.update_flag,
        eps=row.eps,
        diluted_eps=row.diluted_eps,
        book_value_per_share=row.book_value_per_share,
        operating_cash_flow_per_share=row.operating_cash_flow_per_share,
        return_on_equity=row.return_on_equity,
        weighted_return_on_equity=row.weighted_return_on_equity,
        gross_profit_margin=row.gross_profit_margin,
        net_profit_margin=row.net_profit_margin,
        debt_to_assets=row.debt_to_assets,
        revenue_yoy=row.revenue_yoy,
        net_profit_yoy=row.net_profit_yoy,
        operating_cash_flow_yoy=row.operating_cash_flow_yoy,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _release_from_row(row: ResearchDatasetReleaseModel) -> ResearchDatasetRelease:
    return ResearchDatasetRelease.from_dict(row.manifest)


def _available_at(row: InstrumentModel) -> datetime:
    if row.list_date is not None:
        return datetime(row.list_date.year, row.list_date.month, row.list_date.day, tzinfo=UTC)
    if row.updated_at.tzinfo is None:
        return row.updated_at.replace(tzinfo=UTC)
    return row.updated_at


def _status(value: str) -> ListingStatus:
    try:
        return ListingStatus(value)
    except ValueError:
        return ListingStatus.UNKNOWN


def _plain_candidate(
    row: InstrumentModel,
    *,
    instrument_type: InstrumentType,
    profile: ResearchInstrumentProfileModel | None,
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...],
    name_history: tuple[tuple[str, date, date | None], ...],
) -> ReleaseInstrumentSpec:
    market = Market(row.market)
    if instrument_type is InstrumentType.STOCK:
        asset_class = AssetClass.EQUITY
    elif instrument_type is InstrumentType.BOND:
        asset_class = AssetClass.FIXED_INCOME
    elif instrument_type is InstrumentType.INDEX:
        # 指数基准资产(issue #184):只进数据/发布通道,不可撮合。
        asset_class = AssetClass.EQUITY
    else:
        raise ReleaseCapabilityError(f"{row.code}: 不支持的普通资产类型 {instrument_type.value}")
    # 兜底(issue #185):instruments 由 akshare 发现链路写入,list_date/industry
    # 可能为 null;research_instrument_profiles(tushare stock_basic)是兜底源。
    list_date = row.list_date or (profile.list_date if profile is not None else None)
    industry = row.industry or (profile.industry if profile is not None else None)
    return ReleaseInstrumentSpec(
        code=row.code,
        name=row.name,
        market=market,
        instrument_type=instrument_type,
        asset_class=asset_class,
        available_at=_available_at(row),
        execution=default_execution_metadata(
            market=market,
            instrument_type=instrument_type,
        ),
        exchange=row.exchange,
        listing_board=row.listing_board,
        list_date=list_date,
        delist_date=row.delist_date,
        industry=industry,
        status=_status(row.status),
        lifecycle_events=lifecycle_events,
        present_event_types=tuple(
            sorted({event.event_type for event in lifecycle_events})
        ),
        name_history=name_history,
    )


def _etf_candidate(
    row: InstrumentModel,
    metadata: EtfMetadataModel | None,
    *,
    profile: ResearchInstrumentProfileModel | None,
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...],
    name_history: tuple[tuple[str, date, date | None], ...],
) -> ReleaseInstrumentSpec:
    catalog = research_etf_catalog_entry(row.code)
    list_date: date | None
    if catalog is not None:
        category = catalog.category
        asset_class = catalog.asset_class
        list_date = row.list_date or catalog.list_date
    elif metadata is not None:
        category, asset_class = _resolve_etf_classification(row.code, metadata)
        list_date = row.list_date or metadata.listing_date
    else:
        raise ReleaseCapabilityError(f"{row.code}: ETF 缺少分类元数据")
    # 兜底(issue #185):profiles 是 list_date/industry 的兜底源。ETF 优先
    # catalog/metadata 的 list_date,仅在都缺时用档案;industry 只来自档案。
    if list_date is None and profile is not None:
        list_date = profile.list_date
    industry = row.industry or (profile.industry if profile is not None else None)
    return ReleaseInstrumentSpec(
        code=row.code,
        name=row.name or (catalog.name if catalog else row.code),
        market=Market(row.market),
        instrument_type=InstrumentType.ETF,
        asset_class=asset_class,
        available_at=_available_at(row),
        execution=default_execution_metadata(
            market=Market(row.market),
            instrument_type=InstrumentType.ETF,
            etf_category=category,
        ),
        exchange=row.exchange,
        etf_category=category,
        list_date=list_date,
        delist_date=row.delist_date,
        industry=industry,
        status=_status(row.status),
        lifecycle_events=lifecycle_events,
        present_event_types=tuple(
            sorted({event.event_type for event in lifecycle_events})
        ),
        name_history=name_history,
    )


def _resolve_etf_classification(
    code: str,
    metadata: EtfMetadataModel,
) -> tuple[EtfCategory, AssetClass]:
    """从 ``execution_profile``(优先)或旧 ``category`` 派生发布用分类。

    fail-closed:``review_status=needs_review`` 的记录禁止通过发布质量门,
    防止低置信度分类静默影响成交规则(issue #97)。
    """
    review = metadata.review_status or ReviewStatus.NEEDS_REVIEW.value
    if review == ReviewStatus.NEEDS_REVIEW.value:
        raise ReleaseCapabilityError(
            f"{code}: ETF 分类待复核(review_status=needs_review),"
            "请先在元数据页确认或修正后发布"
        )
    try:
        asset_class = AssetClass(metadata.underlying_asset_class)
    except ValueError as exc:
        raise ReleaseCapabilityError(f"{code}: ETF 资产类别无效") from exc
    if metadata.execution_profile:
        try:
            profile = EtfExecutionProfile(metadata.execution_profile)
        except ValueError as exc:
            raise ReleaseCapabilityError(f"{code}: ETF execution_profile 无效") from exc
        return etf_category_from_execution_profile(profile), asset_class
    try:
        return EtfCategory(metadata.category), asset_class
    except ValueError as exc:
        raise ReleaseCapabilityError(f"{code}: ETF 分类无效") from exc


def _catalog_etf_candidate(
    catalog: ResearchEtfCatalogEntry,
) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=catalog.code,
        name=catalog.name,
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        asset_class=catalog.asset_class,
        available_at=datetime(
            catalog.list_date.year,
            catalog.list_date.month,
            catalog.list_date.day,
            tzinfo=UTC,
        ),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            etf_category=catalog.category,
        ),
        exchange="SSE" if catalog.code.endswith(".SH") else "SZSE",
        etf_category=catalog.category,
        list_date=catalog.list_date,
        status=ListingStatus.ACTIVE,
    )


def _convertible_candidate(
    row: InstrumentModel,
    metadata: ConvertibleMetadataModel | None,
    *,
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...],
    name_history: tuple[tuple[str, date, date | None], ...],
) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=row.code,
        name=row.name,
        market=Market(row.market),
        instrument_type=InstrumentType.CONVERTIBLE,
        asset_class=AssetClass.CONVERTIBLE,
        available_at=_available_at(row),
        execution=default_execution_metadata(
            market=Market(row.market),
            instrument_type=InstrumentType.CONVERTIBLE,
        ),
        exchange=row.exchange,
        list_date=row.list_date,
        delist_date=row.delist_date,
        status=_status(row.status),
        metadata_complete=metadata is not None,
        lifecycle_events=lifecycle_events,
        present_event_types=tuple(sorted({event.event_type for event in lifecycle_events})),
        required_event_types=_CONVERTIBLE_REQUIRED_EVENTS,
        name_history=name_history,
    )


def _future_candidate(
    row: FuturesContractModel,
    *,
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...],
) -> ReleaseInstrumentSpec:
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return ReleaseInstrumentSpec(
        code=row.contract_code,
        name=row.contract_code,
        market=Market.FUTURE,
        instrument_type=InstrumentType.FUTURES,
        asset_class=AssetClass.DERIVATIVE,
        available_at=updated_at,
        execution=ExecutionMetadata(
            lot_size=Decimal("1"),
            price_tick=row.price_tick,
            settlement_days=0,
            multiplier=row.multiplier,
            margin_rate=row.margin_rate,
            stamp_tax_rate=Decimal("0"),
            commission_min=Decimal("0"),
            trading_calendar=row.exchange,
            allows_short=True,
        ),
        exchange=row.exchange,
        list_date=row.listing_date,
        delist_date=row.last_trade_date,
        status=ListingStatus.ACTIVE,
        lifecycle_events=lifecycle_events,
        present_event_types=tuple(sorted({event.event_type for event in lifecycle_events})),
        required_event_types=_FUTURES_REQUIRED_EVENTS,
    )


def release_symbol_check(
    release: ResearchDatasetRelease,
    codes: Sequence[str],
) -> dict[str, Any]:
    """按冻结发布的逐标的成员核对标的(issue #238)。

    ``Repository.get`` 返回的领域对象自带 ``instruments`` 元组(登记时
    冻结,与 manifest 同源),成员核对无需读 parquet、无需回放全量清单。
    matched/missing 均按请求顺序返回;matched 只含 code,详细元数据走
    instrument_list。instruments 为空(理论上不可能,发布即冻结)按空集
    处理 → 全部 missing(fail-visible)。
    """
    members: set[str] = set()
    for item in getattr(release, "instruments", None) or ():
        code = item.get("code") if isinstance(item, dict) else getattr(item, "code", None)
        if code:
            members.add(str(code))
    requested = [str(code).strip() for code in codes if str(code).strip()]
    matched = [code for code in requested if code in members]
    missing = [code for code in requested if code not in members]
    return {"requested": len(requested), "matched": matched, "missing": missing}



__all__ = [
    "ReleaseInstrumentCatalogRepository",
    "ResearchDatasetReleaseRepository",
    "ResearchDatasetReleaseService",
    "release_symbol_check",
    "symbol_set_diff",
]
