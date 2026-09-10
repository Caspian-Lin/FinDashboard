"""交易日历仓储(issue #395;与 #396 股票 trade_cal 同表同构)。

``trade_cal`` 表按 ``exchange`` 主键段区分市场:本模块提供通用幂等
upsert(含 ``is_open=0`` 休市行 —— tushare ``fut_trade_cal`` 上游自带
完整日历)与按交易所读取。#396 分支的 ``TradeCalRepository``(akshare
回源 SSE/SZSE 交易日行集 + ``PgTradingCalendarStore`` 读钩子)与本模块
消费同一张表;两分支文件同名、读写语义互补,合并时按 PR 说明取舍。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import TradeCalModel


class CalendarDayLike(Protocol):
    """可落库的日历日(duck type,:class:`~finboard_data.research.FuturesTradeCalendarDay` 满足)。

    成员声明为只读 property:冻结 dataclass(frozen dataclass)的属性是
    read-only,mypy 的结构化子类型才成立。
    """

    @property
    def exchange(self) -> str: ...

    @property
    def cal_date(self) -> date: ...

    @property
    def is_open(self) -> bool: ...


class TradeCalRepository:
    """交易日历的幂等 upsert 与按交易所读取。

    upsert 走 PG ``ON CONFLICT DO UPDATE``:已存在行只刷新
    ``is_open`` / ``source`` / ``updated_at``,重复回源零漂移。休市行
    (``is_open=False``)与交易日行共存 —— 上游 ``fut_trade_cal`` 返回
    完整日历,休市行保留可见(与 #396 akshare 回源只产交易日行的口径
    并存:同一张表,来源行集不同)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_trading_days(self, exchange: str) -> set[date]:
        """读取某交易所的全部交易日(``is_open=true``)。"""
        stmt = select(TradeCalModel.cal_date).where(
            TradeCalModel.exchange == exchange,
            TradeCalModel.is_open.is_(True),
        )
        return set((await self._session.execute(stmt)).scalars())

    async def upsert_calendar_days(
        self,
        days: Sequence[CalendarDayLike],
        *,
        source: str,
    ) -> dict[str, int]:
        """幂等写入日历日(交易日 + 休市行)。

        返回 ``{received, written, trading_days}``:``written`` 是 ON
        CONFLICT 后实际影响行数(新插入 + 刷新),重跑同一上游快照时
        ``written`` 仍等于输入行数(PG upsert 计数语义),``trading_days``
        是其中 ``is_open=true`` 的行数(读取消费口径)。
        """
        received = len(days)
        written = 0
        trading_days = 0
        for day in days:
            stmt = (
                pg_insert(TradeCalModel)
                .values(
                    exchange=day.exchange,
                    cal_date=day.cal_date,
                    is_open=day.is_open,
                    source=source,
                )
                .on_conflict_do_update(
                    index_elements=["exchange", "cal_date"],
                    set_={
                        "is_open": day.is_open,
                        "source": source,
                        "updated_at": func.now(),
                    },
                )
            )
            await self._session.execute(stmt)
            written += 1
            if day.is_open:
                trading_days += 1
        await self._session.flush()
        return {
            "received": received,
            "written": written,
            "trading_days": trading_days,
        }


__all__ = [
    "CalendarDayLike",
    "TradeCalRepository",
]
