"""交易日历仓储(issue #395 期货 + #396 A 股共用,add/add 和解超集)。

``trade_cal`` 表按 ``exchange`` 存交易日,同一张表靠主键段区分市场:

* #396(A 股)——``upsert_trading_days`` 写 akshare 回源的 SSE/SZSE
  交易日行集(只产 ``is_open=true`` 行);``PgTradingCalendarStore`` 是
  :mod:`finboard_data.trading_calendar`「DB 优先,缺失回源 akshare 并
  回写」读路径的持久层半边。
* #395(期货)——``upsert_calendar_days`` 写 tushare ``fut_trade_cal``
  的 CFFEX 完整日历(含 ``is_open=0`` 休市行),走 PG ``ON CONFLICT DO
  UPDATE`` 幂等刷新;``CalendarDayLike`` 让 data 层日历 day 对象免依赖
  落库。

两分支文件同名、读写语义互补:合并保留双方 API(超集),消费方各自
使用自己引入的入口,行为与合并前一致。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_persistence.models import TradeCalModel

#: akshare 回源的统一日历登记交易所(A 股两所交易日历一致)。
DEFAULT_EXCHANGES: tuple[str, ...] = ("SSE", "SZSE")
#: 读取侧的权威交易所(SSE 行集 = 沪深统一交易日历)。
PRIMARY_EXCHANGE = "SSE"


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

    两个写入入口并存:

    * :meth:`upsert_calendar_days`(通用,issue #395)——接受任意市场的
      日历日(含休市行),PG ``ON CONFLICT DO UPDATE``:已存在行只刷新
      ``is_open`` / ``source`` / ``updated_at``,重复回源零漂移。休市行
      (``is_open=False``)与交易日行共存 —— 上游 ``fut_trade_cal`` 返回
      完整日历,休市行保留可见。
    * :meth:`upsert_trading_days`(A 股便捷口,issue #396)——akshare
      回源只产交易日行,同一行集按 ``DEFAULT_EXCHANGES`` 写两所,已存在
      行跳过不重写,幂等不产生漂移。

    读取入口 :meth:`list_trading_days` 默认读权威交易所 SSE 行集(沪深
    统一交易日历),期货消费方显式传 ``exchange="CFFEX"``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_trading_days(
        self, exchange: str = PRIMARY_EXCHANGE
    ) -> set[date]:
        """读取某交易所的全部交易日(is_open=true)。"""
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

    async def upsert_trading_days(
        self,
        days: set[date],
        *,
        source: str,
        exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
    ) -> int:
        """幂等写入交易日(已存在的行跳过,不重写)。"""
        if not days:
            return 0
        written = 0
        for exchange in exchanges:
            existing = set(
                (
                    await self._session.execute(
                        select(TradeCalModel.cal_date).where(
                            TradeCalModel.exchange == exchange,
                            TradeCalModel.cal_date.in_(days),
                        )
                    )
                ).scalars()
            )
            for day in sorted(days):
                if day in existing:
                    continue
                self._session.add(
                    TradeCalModel(
                        exchange=exchange,
                        cal_date=day,
                        is_open=True,
                        source=source,
                    )
                )
                written += 1
        await self._session.flush()
        return written


class PgTradingCalendarStore:
    """:mod:`finboard_data.trading_calendar` 的 PG 存储适配器(#396)。

    实现 ``TradingCalendarStore`` 协议(DB 优先读 + akshare 回写);由
    composition root(``build_kernel_components``)安装,未安装时日历模块
    走历史同步路径,行为不变。
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def load(self) -> set[date] | None:
        async with self._session_maker() as session:
            days = await TradeCalRepository(session).list_trading_days()
            await session.commit()
        return days or None

    async def save(self, days: set[date]) -> None:
        async with self._session_maker() as session:
            await TradeCalRepository(session).upsert_trading_days(
                days, source="akshare"
            )
            await session.commit()


__all__ = [
    "DEFAULT_EXCHANGES",
    "PRIMARY_EXCHANGE",
    "CalendarDayLike",
    "PgTradingCalendarStore",
    "TradeCalRepository",
]
