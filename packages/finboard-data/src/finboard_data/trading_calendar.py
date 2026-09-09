"""A 股交易日历(issue #99 前置;#396 起支持 PG 落库)。

两条加载路径:

* :func:`ensure_calendar_loaded`(异步,DB 优先)—— dataset_publish /
  factor_series_build 等异步执行域入口调用:进程缓存 → PG ``trade_cal``
  (经安装的 :class:`TradingCalendarStore`)→ 缺失/过期回源 akshare 并
  幂等回写 → exchange_calendars 兜底。预热后同一进程内的同步消费
  (发布覆盖率审计等)直接命中缓存,启动期不再依赖 akshare 可用性。
* :func:`load_trading_calendar`(同步,历史路径)—— 进程缓存 → akshare →
  exchange_calendars。未预热场景(纯同步消费、测试)保持原行为,不访问 DB。

两个可靠来源都不可用时 ``trading_days`` 仍然抛异常,
而非用朴素工作日伪装正常 -- 后者会导致覆盖率计算失真。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from functools import lru_cache
from typing import Protocol

logger = logging.getLogger(__name__)

_CACHE: set[date] | None = None


class TradingCalendarError(Exception):
    """交易日历未加载(akshare 不可用或网络失败)。"""


class TradingCalendarStore(Protocol):
    """交易日历持久化存储钩子(#396,由持久层适配器安装)。

    ``load`` 返回 ``None`` 表示 DB 无日历(表不存在 / 空表 / 读取失败由
    实现自行降级为 ``None``);``save`` 对 akshare 回源结果做幂等 upsert。
    """

    async def load(self) -> set[date] | None:
        """读取已落库的交易日集;无数据返回 ``None``。"""
        ...

    async def save(self, days: set[date]) -> None:
        """幂等回写交易日集(回源后的持久化)。"""
        ...


_STORE: TradingCalendarStore | None = None


def install_calendar_store(store: TradingCalendarStore | None) -> None:
    """安装 / 卸载持久化存储钩子(composition root 调用,进程级单次)。"""
    global _STORE
    _STORE = store


def _fetch_trade_dates() -> set[date]:
    """从 akshare 拉取全部 A 股交易日(同步,在线)。"""
    import akshare as ak
    import pandas as pd

    df: pd.DataFrame = ak.tool_trade_date_hist_sina()
    return set(pd.to_datetime(df["trade_date"]).dt.date)


def _fetch_trade_dates_from_exchange_calendars() -> set[date]:
    """从本地交易所日历包读取 XSHG 交易日,不依赖外部网络。"""
    import exchange_calendars as xcals  # type: ignore[import-untyped]

    calendar = xcals.get_calendar("XSHG")
    return {session.date() for session in calendar.sessions}


def load_trading_calendar() -> set[date]:
    """加载交易日历(进程内缓存,首次调用联网拉取;历史同步路径)。

    加载失败时缓存空集合并记录警告。
    调用方应通过 :class:`TradingCalendarError` 处理缺失场景。
    异步执行域请改用 :func:`ensure_calendar_loaded`(DB 优先)。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = _fetch_trade_dates()
        logger.info("trading_calendar.loaded source=akshare dates=%d", len(_CACHE))
    except Exception:
        logger.warning("trading_calendar.akshare_failed")
        try:
            _CACHE = _fetch_trade_dates_from_exchange_calendars()
            logger.info(
                "trading_calendar.loaded source=exchange_calendars dates=%d",
                len(_CACHE),
            )
        except Exception:
            logger.warning("trading_calendar.exchange_calendars_failed")
            _CACHE = set()
    return _CACHE


async def ensure_calendar_loaded() -> set[date]:
    """DB 优先加载交易日历(#396;异步执行域入口调用)。

    顺序:进程缓存 → PG(安装的 store)→ DB 空 / 过期时回源 akshare 并
    幂等回写 → exchange_calendars 兜底。全部失败时缓存空集(与同步路径
    同语义,消费方经 :class:`TradingCalendarError` 收口)。本函数永不抛出
    网络异常 —— 日历加载失败不应阻断调用方自身的错误语义。
    """
    global _CACHE
    if _CACHE:
        return _CACHE
    store = _STORE
    if store is not None:
        try:
            db_days = await store.load()
        except Exception:
            logger.warning("trading_calendar.db_load_failed", exc_info=True)
            db_days = None
        if db_days:
            if _calendar_is_stale(db_days):
                logger.info("trading_calendar.database_stale")
                refreshed = await _backfill_from_akshare(store)
                if refreshed:
                    return refreshed
            _CACHE = db_days
            logger.info(
                "trading_calendar.loaded source=database dates=%d", len(_CACHE)
            )
            return _CACHE
    backfilled = await _backfill_from_akshare(store)
    if backfilled:
        return backfilled
    try:
        _CACHE = _fetch_trade_dates_from_exchange_calendars()
        logger.info(
            "trading_calendar.loaded source=exchange_calendars dates=%d",
            len(_CACHE),
        )
    except Exception:
        logger.warning("trading_calendar.exchange_calendars_failed")
        _CACHE = set()
    return _CACHE


async def _backfill_from_akshare(store: TradingCalendarStore | None) -> set[date] | None:
    """回源 akshare 并回写 DB(缺失区间补齐语义;失败返回 ``None``)。"""
    global _CACHE
    try:
        days = await asyncio.to_thread(_fetch_trade_dates)
    except Exception:
        logger.warning("trading_calendar.akshare_failed")
        return None
    _CACHE = days
    logger.info("trading_calendar.loaded source=akshare dates=%d", len(days))
    if store is not None:
        try:
            await store.save(days)
        except Exception:
            logger.warning("trading_calendar.db_save_failed", exc_info=True)
    return days


def _calendar_is_stale(days: set[date]) -> bool:
    """DB 日历最大日期早于今天即视为可能缺新区间(如跨年后新交易日)。

    akshare 日历含至年末的未来交易日,正常情况下 max(DB) >= today;
    只有跨年未回源时才会落后,此时重拉合并(幂等 upsert)。
    """
    return bool(days) and date.today() > max(days)


def is_trading_day(d: date) -> bool:
    """判断某日是否为 A 股交易日。"""
    return d in load_trading_calendar()


def trading_days(start: date, end: date) -> set[date]:
    """返回 ``[start, end]`` 区间内的所有 A 股交易日。

    无 fallback -- 日历未加载时抛 :class:`TradingCalendarError`。
    """
    if start > end:
        return set()
    calendar = load_trading_calendar()
    if not calendar:
        raise TradingCalendarError(
            "A 股交易日历未加载, 无法计算期望交易日"
            "(请检查 akshare 是否可用)"
        )
    return {d for d in calendar if start <= d <= end}


@lru_cache(maxsize=1)
def _last_date_of_calendar() -> date | None:
    """交易日历中的最大日期(用于判断某日是否尚未发生 vs 日历缺失)。"""
    cal = load_trading_calendar()
    return max(cal) if cal else None


def reset_cache() -> None:
    """重置进程内缓存(测试用;不影响已安装的持久化钩子)。"""
    global _CACHE
    _CACHE = None
    _last_date_of_calendar.cache_clear()
