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
        results = await provider.fetch_bars_batch(
            sym_objs,
            period_enum,
            start,
            end,
            adjust=adjust,
            on_progress=on_progress,
        )
        success = sum(1 for v in results.values() if v)
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
