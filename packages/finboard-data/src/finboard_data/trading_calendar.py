"""A 股交易日历(issue #99 前置)。

优先从 akshare ``tool_trade_date_hist_sina`` 获取历史交易日,
失败时使用随包安装的 ``exchange_calendars`` XSHG 日历,
进程内缓存避免重复请求。发布预检用真实交易日替代朴素 Mon-Fri,
避免中国法定节假日被误报为 ``missing_weekdays``。

两个可靠来源都不可用时 ``trading_days`` 仍然抛异常,
而非用朴素工作日伪装正常 -- 后者会导致覆盖率计算失真。
"""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

logger = logging.getLogger(__name__)

_CACHE: set[date] | None = None


class TradingCalendarError(Exception):
    """交易日历未加载(akshare 不可用或网络失败)。"""


def _fetch_trade_dates() -> set[date]:
    """从 akshare 拉取全部 A 股交易日(同步,在线)。"""
    import akshare as ak
    import pandas as pd  # type: ignore[import-untyped]

    df: pd.DataFrame = ak.tool_trade_date_hist_sina()
    return set(pd.to_datetime(df["trade_date"]).dt.date)


def _fetch_trade_dates_from_exchange_calendars() -> set[date]:
    """从本地交易所日历包读取 XSHG 交易日,不依赖外部网络。"""
    import exchange_calendars as xcals  # type: ignore[import-untyped]

    calendar = xcals.get_calendar("XSHG")
    return {session.date() for session in calendar.sessions}


def load_trading_calendar() -> set[date]:
    """加载交易日历(进程内缓存,首次调用联网拉取)。

    加载失败时缓存空集合并记录警告。
    调用方应通过 :class:`TradingCalendarError` 处理缺失场景。
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
    """重置进程内缓存(测试用)。"""
    global _CACHE
    _CACHE = None
    _last_date_of_calendar.cache_clear()
