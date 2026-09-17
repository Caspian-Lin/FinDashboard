"""Tushare ``suspend_d`` 停复牌事件幂等落库(单一事实源,issue #393)。

REST(``/api/data/fetch``)、MCP(``finboard_data_fetch``)与批量下载执行器
(``kind=bulk_download``,#393 起随 tushare 主源切入)三处共用同一写入函数,
保证 ``instrument_lifecycle_events`` 的行形状(source / dataset_version /
details)与幂等键完全一致,不再各自维护副本。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import InstrumentLifecycleEventModel

#: 停复牌事件的固定 dataset_version(与既有消费口径一致,#393 前已是该值)。
TUSHARE_SUSPEND_DATASET_VERSION = "suspend_d-v1"


async def persist_tushare_lifecycle_events(
    session: AsyncSession,
    events: list[Any],
) -> int:
    """幂等写入 Tushare 停复牌事件,返回本次新增数量。

    ``events`` 为 :class:`finboard_data.TushareLifecycleEvent` 形状的对象
    (symbol / event_type / effective_date / suspend_timing);按数据库唯一
    约束 ``uq_instrument_lifecycle_event`` 冲突跳过。历史事件是现在从 API
    观测到的,``available_at`` 不倒填成当时已知。
    """
    if not events:
        return 0

    observed_at = datetime.now(UTC)
    values = [
        {
            "symbol": event.symbol,
            "event_type": event.event_type,
            "effective_date": event.effective_date,
            # 历史事件是现在从 API 观测到的,不能倒填成当时已知。
            "available_at": observed_at,
            "source": "tushare",
            "dataset_version": TUSHARE_SUSPEND_DATASET_VERSION,
            "details": {
                "suspend_type": "R" if event.event_type == "resumption" else "S",
                "suspend_timing": event.suspend_timing,
            },
            "observed_at": observed_at,
        }
        for event in events
    ]
    statement = (
        insert(InstrumentLifecycleEventModel)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_instrument_lifecycle_event")
        .returning(InstrumentLifecycleEventModel.id)
    )
    result = await session.execute(statement)
    return len(result.scalars().all())


__all__ = [
    "TUSHARE_SUSPEND_DATASET_VERSION",
    "persist_tushare_lifecycle_events",
]
