"""A 股交易日历仓储(issue #396)。

``trade_cal`` 表按 exchange 存交易日;读取口径「DB 优先,缺失回源
akshare 并回写」的持久层半边。akshare ``tool_trade_date_hist_sina``
是沪深统一日历,回源按 SSE / SZSE 两行集写入同一天集;幂等 upsert,
重复回源不产生漂移。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_persistence.models import TradeCalModel

#: akshare 回源的统一日历登记交易所(A 股两所交易日历一致)。
DEFAULT_EXCHANGES: tuple[str, ...] = ("SSE", "SZSE")
#: 读取侧的权威交易所(SSE 行集 = 沪深统一交易日历)。
PRIMARY_EXCHANGE = "SSE"


class TradeCalRepository:
    """交易日历的幂等 upsert 与读取。"""

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

    async def upsert_trading_days(
        self,
        days: set[date],
        *,
        source: str,
        exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
    ) -> int:
        """幂等写入交易日(已存在的行只刷新 source/updated_at)。"""
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
    "PgTradingCalendarStore",
    "TradeCalRepository",
]
