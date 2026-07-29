"""定时行情拉取任务工厂。

返回 :class:`~finboard_scheduler.scheduler.ScheduledTask`,
用于在交易日收盘后自动批量拉取行情数据。

延迟 import ``ScheduledTask``,避免 ``finboard-data`` 硬依赖 ``finboard-scheduler``。
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import TYPE_CHECKING

import structlog

from finboard_data.symbols import SymbolPoolConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from finboard_data.akshare_provider import AkShareProvider
    from finboard_data.lifecycle import SuspendDetector
    from finboard_persistence import InstrumentRepository
    from finboard_scheduler.scheduler import ScheduledTask

logger = structlog.get_logger(__name__)


def data_fetch_task(
    *,
    provider: AkShareProvider,
    symbols: list[str],
    period: str = "D1",
    lookback_days: int = 5,
    adjust: str = "qfq",
    at: time = time(15, 35),
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ScheduledTask:
    """盘后定时批量拉取行情(默认 15:35,仅交易日)。

    :param provider:      已初始化的 AkShareProvider(带限流)
    :param symbols:       标的代码列表(如 ``["510300.SH", "510050.SH"]``)
    :param period:        K 线周期(如 ``"D1"``)
    :param lookback_days: 增量拉取天数(从今天往前)
    :param adjust:        复权方式
    :param at:            触发时间(Asia/Shanghai)
    :param on_progress:   进度回调
    """
    from finboard_scheduler.scheduler import ScheduledTask
    from finboard_shared.types import BarPeriod

    period_enum = BarPeriod(period)

    async def _run() -> None:
        from finboard_data.cache import make_symbol as _mk

        end = date.today()
        start = end - timedelta(days=lookback_days)
        sym_objs = [_mk(s) for s in symbols]
        logger.info(
            "data_fetch.start",
            symbols=len(sym_objs),
            start=str(start),
            end=str(end),
        )
        results = await provider.update_cache_batch(
            sym_objs,
            period_enum,
            start,
            end,
            adjust=adjust,
            on_progress=on_progress,
        )
        success = sum(results.values())
        logger.info(
            "data_fetch.done",
            total=len(symbols),
            success=success,
            failed=len(symbols) - success,
        )

    return ScheduledTask(
        name="data_fetch",
        func=_run,
        time=at,
        trading_days_only=True,
    )


def data_fetch_task_from_config(
    *,
    provider: AkShareProvider,
    config: SymbolPoolConfig,
    at: time = time(15, 35),
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ScheduledTask:
    """从 :class:`SymbolPoolConfig` 构造定时拉取任务。"""
    return data_fetch_task(
        provider=provider,
        symbols=[s.code for s in config.symbols],
        period=config.fetch_period,
        lookback_days=config.fetch_lookback_days,
        adjust=config.fetch_adjust,
        at=at,
        on_progress=on_progress,
    )


def suspend_detect_task(
    *,
    provider: AkShareProvider,
    db_url: str,
    symbols: list[str],
    detector: SuspendDetector | None = None,
    period: str = "D1",
    lookback_days: int = 5,
    adjust: str = "qfq",
    threshold: int = 5,
    at: time = time(16, 0),
) -> ScheduledTask:
    """盘后停牌检测(issue #35):连续 ``threshold`` 个交易日无新数据 → suspended。

    流程:对每个标的记录拉取前的缓存 ``last_date`` → ``update_cache`` → 比较
    拉取后的 ``last_date`` 是否前进 → :class:`SuspendDetector` 累计计数 →
    回写 ``instruments.status``。

    与实盘交易隔离:只读行情缓存 + 只写研究元数据 ``instruments.status``,
    不触碰订单 / 持仓 / 风控 / Kill Switch。
    """
    from finboard_data.cache import make_symbol as _mk
    from finboard_data.lifecycle import SuspendDetector, apply_suspend_decision, compute_has_new
    from finboard_scheduler.scheduler import ScheduledTask
    from finboard_shared.types import BarPeriod

    period_enum = BarPeriod(period)
    det = detector if detector is not None else SuspendDetector(threshold=threshold)

    async def _run() -> None:
        from finboard_persistence import InstrumentRepository, create_async_engine, session_factory

        end = date.today()
        start = end - timedelta(days=lookback_days)
        sym_objs = [_mk(s) for s in symbols]

        # 拉取前快照缓存 last_date
        before: dict[str, date | None] = {
            s.code: await provider.last_cached_date(s, period_enum, adjust) for s in sym_objs
        }
        await provider.update_cache_batch(sym_objs, period_enum, start, end, adjust=adjust)
        after: dict[str, date | None] = {
            s.code: await provider.last_cached_date(s, period_enum, adjust) for s in sym_objs
        }
        fetch_results = {
            code: compute_has_new(before.get(code), after.get(code)) for code in before
        }

        engine = create_async_engine(db_url)
        try:
            async with session_factory(engine)() as session:
                repo = InstrumentRepository(session)
                # 取当前 active / suspended 标的集合(限定本次观测的 code)
                observed = set(fetch_results)
                active, _, suspended = await _split_status(repo, observed)
                decision = det.process(
                    fetch_results=fetch_results,
                    active_codes=active,
                    suspended_codes=suspended,
                )
                if not decision.is_empty:
                    await apply_suspend_decision(repo, decision)
                    await session.commit()
                logger.info(
                    "suspend_detect.done",
                    observed=len(observed),
                    suspend=len(decision.to_suspend),
                    resume=len(decision.to_resume),
                )
        finally:
            await engine.dispose()

    return ScheduledTask(
        name="suspend_detect",
        func=_run,
        time=at,
        trading_days_only=True,
    )


async def _split_status(
    repo: InstrumentRepository,
    codes: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """把 ``codes`` 按 instruments.status 分成 active / delisted / suspended 三组。"""
    from finboard_shared.types import ListingStatus

    active: set[str] = set()
    delisted: set[str] = set()
    suspended: set[str] = set()
    for code, status in (await repo.get_status_map(codes)).items():
        if status == ListingStatus.ACTIVE.value:
            active.add(code)
        elif status == ListingStatus.DELISTED.value:
            delisted.add(code)
        elif status == ListingStatus.SUSPENDED.value:
            suspended.add(code)
    return active, delisted, suspended
