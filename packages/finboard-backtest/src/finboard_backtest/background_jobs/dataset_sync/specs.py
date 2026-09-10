"""六个研究数据集的 SyncSpec 注册(issue #392;#171/#251/#265 迁移)。

行为等值边界(golden 对照锁定,``tests/integration/test_issue_392_golden_dataset_sync.py``):

* dataset_version 幂等键形状与旧路径逐字节一致 ——
  ``profiles:{today}`` / ``name_changes:{today}`` / ``convertible_profiles:{today}``
  (主数据快照,随执行日轮换) / ``daily:{trade_date}`` /
  ``financial:{symbol}:{start}:{end}`` / ``industry:{symbol}``;
* 批次 parameters 与旧路径一致;
* profiles 同时拉在市(L)+ 退市(D)档案合并同一批次(#251);
* name_changes 直接重建主数据 ``instrument_names``(半开区间,无批次语义);
* convertible_profiles upsert 主数据 ``convertible_metadata``(#265),顺带
  回填 ``instruments.list_date/delist_date`` 并导入集思录强赎事件;
  akshare 评级 / 事件兜底失败降级 warning 不阻断 tushare 主链路;
* 空切片(上游空响应 / 非交易日)不产生批次行。

注册即生效:import 本模块(经包 ``__init__``)后 :data:`spec.SYNC_SPECS`
即含全部六集;新数据集照抄一份 ``SyncSpec`` + ``SYNC_SPECS.register`` 即接入。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog

from finboard_backtest.background_jobs.dataset_sync.spec import (
    SYNC_SPECS,
    EnumShape,
    PersistContext,
    PersistResult,
    SliceQuery,
    SyncSpec,
)
from finboard_data.research import ResearchDataProvider
from finboard_shared.instruments import LifecycleEvent
from finboard_shared.types import LifecycleEventType

logger = structlog.get_logger(__name__)

#: 强赎事件的 dataset_version(与 job 无关的**上游接口**口径):import 幂等键
#: 含 dataset_version,若随日期变化同一强赎事件每天都会插入新行,永不安定。
_CONVERTIBLE_EVENT_DATASET_VERSION = "jsl_redeem_v1"

type _ProviderMethod = Any


def _today() -> str:
    """主数据快照类数据集的 dataset_version 日期段(旧路径同款:本地日期)。"""

    return date.today().isoformat()


# ---- profiles(instrument_profiles;FULL_PAGED)--------------------------------


async def _fetch_profiles(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    # #251:在市(L)+ 退市(D)档案合并进同一批次 —— 退市档案携带
    # delist_date,是 instruments 主数据退市日期的唯一结构化上游。
    live = await provider.fetch_instrument_profiles(
        dirty_row_policy=query.row_policy
    )
    delisted = await provider.fetch_instrument_profiles(
        list_status="D", dirty_row_policy=query.row_policy
    )
    return [*live, *delisted]


async def _persist_profiles(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    from finboard_persistence.research_sync import ResearchDataSyncService

    service = ResearchDataSyncService(context.session_maker)
    batch = await service.sync_instrument_profiles(
        source=context.source,
        dataset_version=context.dataset_version,
        code_version=context.code_version,
        parameters={"list_status": "L+D"},
        raw_payload=None,
        records=records,
    )
    return PersistResult(accepted_rows=batch.accepted_rows)


# ---- name_changes(主数据 instrument_names;FULL_PAGED)-----------------------


async def _fetch_name_changes(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    return await provider.fetch_name_changes(dirty_row_policy=query.row_policy)


async def _persist_name_changes(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    # #251:历史名称变更重建 instrument_names(主数据表,无批次语义)。
    from finboard_persistence import InstrumentRepository

    if not records:
        return PersistResult(accepted_rows=None)
    async with context.session_maker() as session:
        await InstrumentRepository(session).import_name_history(
            [
                (change.symbol, change.name, change.start_date, change.end_date)
                for change in records
            ]
        )
        await session.commit()
    return PersistResult(accepted_rows=None)


# ---- convertible_profiles(主数据 convertible_metadata;FULL_PAGED)----------


async def _fetch_convertible_profiles(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    return await provider.fetch_convertible_profiles(
        dirty_row_policy=query.row_policy
    )


async def _persist_convertible_profiles(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    # #265:cb_basic 条款快照 upsert 主数据 convertible_metadata;顺带回填
    # instruments 的上市/退市日期(只补 null)并导入集思录强赎事件。akshare
    # 评级/事件是兜底增强,失败降级为 warning 不阻断 tushare 主链路 —— 缺失
    # 经 upsert 结果的 missing_* 计数可见。
    from finboard_persistence import (
        ConvertibleMetadataRepository,
        InstrumentRepository,
    )

    ratings: dict[str, str] = {}
    redemption_events: list[LifecycleEvent] = []
    enrichment_failed = False
    try:
        ratings, redemption_events = await _fetch_convertible_enrichment()
    except Exception as exc:
        enrichment_failed = True
        logger.warning(
            "dataset_sync.convertible_enrichment_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    async with context.session_maker() as session:
        meta_result = await ConvertibleMetadataRepository(
            session
        ).upsert_many(
            records,
            ratings=ratings,
            source=context.source,
            dataset_version=context.dataset_version,
        )
        instrument_repo = InstrumentRepository(session)
        listing_result = await instrument_repo.backfill_listing_dates(
            {
                item.symbol: (item.list_date, item.delist_date)
                for item in records
            }
        )
        if redemption_events:
            await instrument_repo.import_lifecycle_events(redemption_events)
        await session.commit()
    logger.info(
        "dataset_sync.convertible_profiles",
        **meta_result.as_dict(),
        **listing_result,
        enrichment_failed=enrichment_failed,
    )
    return PersistResult(accepted_rows=None, note="main_data_upsert")


async def _fetch_convertible_enrichment(
    *,
    now: Any = None,
) -> tuple[dict[str, str], list[LifecycleEvent]]:
    """akshare 兜底增强:东财评级映射 + 集思录强赎事件(#265)。

    主数据是 tushare cb_basic;这里只提供 cb_basic 没有的评级列与事件行。
    任一接口失败整体抛出,由调用方降级为 warning(tushare 主链路不阻断,
    评级缺失经 ``ConvertibleMetadataSyncResult.missing_rating`` 可见)。
    模块级函数:测试经 monkeypatch 替换(golden 集成对照的注入点)。
    """
    from finboard_data import AkShareProvider

    # 兜底接口是全量单页快照,与行情缓存无关:use_cache=False 免建缓存目录。
    provider = AkShareProvider(use_cache=False, max_retries=2)
    overview = await provider.fetch_convertible_overview()
    ratings = {entry.code: entry.rating for entry in overview if entry.rating}
    redeem = await provider.fetch_convertible_redeem_events(now=now)
    events = [_redemption_to_lifecycle_event(item) for item in redeem]
    return ratings, events


def _redemption_to_lifecycle_event(item: Any) -> LifecycleEvent:
    """集思录强赎快照行 → 时点化强赎事件(#265)。

    PIT 语义(诚实边界):集思录无公告时间,``observed_at`` = 本次观察时间。
    领域不变量要求 ``available_at >= effective_date`` 开盘(防未来信息泄漏),
    而已公告、未来生效的赎回日晚于观察时间 —— 取两者较晚者:生效日在未来的
    事件按**生效日**可见(保守方向:只会晚看到,不会提前看到),真实观察
    时间保留在 ``details.observed_at`` 供审计。
    """
    from datetime import UTC, datetime

    effective = item.effective_date
    if effective is None:  # parse 端已过滤双缺失行;此处防御归一
        raise ValueError(f"{item.code}:强赎事件缺少生效日(赎回日/停止交易日均空)")
    effective_open = datetime.combine(effective, datetime.min.time(), tzinfo=UTC)
    available_at = max(item.observed_at, effective_open)
    return LifecycleEvent(
        symbol=item.code,
        event_type=LifecycleEventType.FORCED_REDEMPTION,
        effective_date=effective,
        available_at=available_at,
        source="akshare",
        dataset_version=_CONVERTIBLE_EVENT_DATASET_VERSION,
        details={
            "redemption_date": (
                item.redemption_date.isoformat() if item.redemption_date else None
            ),
            "stop_transfer_date": (
                item.stop_transfer_date.isoformat() if item.stop_transfer_date else None
            ),
            "redemption_price": (
                str(item.redemption_price) if item.redemption_price is not None else None
            ),
            "underlying_symbol": item.underlying_symbol,
            "observed_at": item.observed_at.isoformat(),
        },
        observed_at=item.observed_at,
    )


# ---- daily_metrics(DAILY_MARKET)----------------------------------------------


async def _fetch_daily_metrics(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    assert query.trade_date is not None
    return await provider.fetch_daily_metrics(
        query.trade_date, dirty_row_policy=query.row_policy
    )


async def _persist_daily_metrics(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    from finboard_persistence.research_sync import ResearchDataSyncService

    assert context.query.trade_date is not None
    service = ResearchDataSyncService(context.session_maker)
    batch = await service.sync_daily_metrics(
        source=context.source,
        dataset_version=context.dataset_version,
        code_version=context.code_version,
        parameters={"trade_date": context.query.trade_date.isoformat()},
        raw_payload=None,
        records=list(records),
        expected_trade_date=context.query.trade_date,
    )
    return PersistResult(accepted_rows=batch.accepted_rows)


# ---- financial_indicators(PER_SYMBOL_RANGE)----------------------------------


async def _fetch_financial_indicators(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    assert query.symbol is not None
    assert query.start_date is not None
    assert query.end_date is not None
    return await provider.fetch_financial_indicators(
        query.symbol,
        start_period=query.start_date,
        end_period=query.end_date,
        dirty_row_policy=query.row_policy,
    )


async def _persist_financial_indicators(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    from finboard_persistence.research_sync import ResearchDataSyncService

    assert context.query.symbol is not None
    assert context.query.start_date is not None
    assert context.query.end_date is not None
    service = ResearchDataSyncService(context.session_maker)
    batch = await service.sync_financial_indicators(
        source=context.source,
        dataset_version=context.dataset_version,
        code_version=context.code_version,
        parameters={
            "symbol": context.query.symbol,
            "start_period": context.query.start_date.isoformat(),
            "end_period": context.query.end_date.isoformat(),
        },
        raw_payload=None,
        records=list(records),
    )
    return PersistResult(accepted_rows=batch.accepted_rows)


# ---- industry_memberships(PER_SYMBOL_RANGE)----------------------------------


async def _fetch_industry_memberships(
    provider: ResearchDataProvider, query: SliceQuery
) -> list[Any]:
    assert query.symbol is not None
    return await provider.fetch_industry_memberships(
        symbol=query.symbol,
        current_only=True,
        dirty_row_policy=query.row_policy,
    )


async def _persist_industry_memberships(
    context: PersistContext, records: list[Any]
) -> PersistResult:
    from finboard_persistence.research_sync import ResearchDataSyncService

    assert context.query.symbol is not None
    service = ResearchDataSyncService(context.session_maker)
    batch = await service.sync_industry_memberships(
        source=context.source,
        dataset_version=context.dataset_version,
        code_version=context.code_version,
        parameters={"symbol": context.query.symbol, "current_only": True},
        raw_payload=None,
        records=list(records),
    )
    return PersistResult(accepted_rows=batch.accepted_rows)


# ---- 注册(声明序 = 默认执行序,与旧路径编排顺序一致)--------------------------

#: 切片键 → dataset_version / parameters(与旧路径逐字节一致,golden 锁定)。


def _daily_version(query: SliceQuery) -> str:
    assert query.trade_date is not None
    return f"daily:{query.trade_date.isoformat()}"


def _daily_parameters(query: SliceQuery) -> dict[str, object]:
    assert query.trade_date is not None
    return {"trade_date": query.trade_date.isoformat()}


def _financial_version(query: SliceQuery) -> str:
    assert query.symbol is not None
    assert query.start_date is not None
    assert query.end_date is not None
    return (
        f"financial:{query.symbol}:{query.start_date.isoformat()}:"
        f"{query.end_date.isoformat()}"
    )


def _financial_parameters(query: SliceQuery) -> dict[str, object]:
    assert query.symbol is not None
    assert query.start_date is not None
    assert query.end_date is not None
    return {
        "symbol": query.symbol,
        "start_period": query.start_date.isoformat(),
        "end_period": query.end_date.isoformat(),
    }


def _industry_version(query: SliceQuery) -> str:
    assert query.symbol is not None
    return f"industry:{query.symbol}"


def _industry_parameters(query: SliceQuery) -> dict[str, object]:
    assert query.symbol is not None
    return {"symbol": query.symbol, "current_only": True}


def register_default_specs() -> None:
    """注册六个内置数据集规格(幂等:重复导入不重复注册)。"""

    if SYNC_SPECS.has("profiles"):
        return
    for spec in (
        SyncSpec(
            name="profiles",
            shape=EnumShape.FULL_PAGED,
            title="标的档案快照(tushare stock_basic,在市 L + 退市 D,#251)",
            fetch=_fetch_profiles,
            persist=_persist_profiles,
            slice_version=lambda _query: f"profiles:{_today()}",
            slice_parameters=lambda _query: {"list_status": "L+D"},
        ),
        SyncSpec(
            name="name_changes",
            shape=EnumShape.FULL_PAGED,
            title="历史名称变更重建 instrument_names(#251,主数据,无批次)",
            fetch=_fetch_name_changes,
            persist=_persist_name_changes,
            slice_version=lambda _query: f"name_changes:{_today()}",
            writes_batch=False,
        ),
        SyncSpec(
            name="convertible_profiles",
            shape=EnumShape.FULL_PAGED,
            title="转债条款快照 upsert convertible_metadata(#265,主数据,无批次)",
            fetch=_fetch_convertible_profiles,
            persist=_persist_convertible_profiles,
            slice_version=lambda _query: f"convertible_profiles:{_today()}",
            writes_batch=False,
        ),
        SyncSpec(
            name="daily_metrics",
            shape=EnumShape.DAILY_MARKET,
            title="每日指标截面(tushare daily_basic,按工作日,PIT 17:00 上海)",
            fetch=_fetch_daily_metrics,
            persist=_persist_daily_metrics,
            slice_version=_daily_version,
            slice_parameters=_daily_parameters,
        ),
        SyncSpec(
            name="financial_indicators",
            shape=EnumShape.PER_SYMBOL_RANGE,
            title="财务指标修订(tushare fina_indicator,按标的 x 报告期窗口)",
            fetch=_fetch_financial_indicators,
            persist=_persist_financial_indicators,
            slice_version=_financial_version,
            slice_parameters=_financial_parameters,
        ),
        SyncSpec(
            name="industry_memberships",
            shape=EnumShape.PER_SYMBOL_RANGE,
            title="申万行业成员(tushare index_member_all,按标的,当前任职)",
            fetch=_fetch_industry_memberships,
            persist=_persist_industry_memberships,
            slice_version=_industry_version,
            slice_parameters=_industry_parameters,
        ),
    ):
        SYNC_SPECS.register(spec)


register_default_specs()

__all__ = [
    "_fetch_convertible_enrichment",
    "_redemption_to_lifecycle_event",
    "register_default_specs",
]
