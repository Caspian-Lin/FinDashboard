"""A 股交易日历(issue #99 前置)。

从 akshare ``tool_trade_date_hist_sina`` 获取历史交易日,
进程内缓存避免重复网络请求。发布预检用真实交易日替代朴素 Mon-Fri,
避免中国法定节假日被误报为 ``missing_weekdays``。
"""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

logger = logging.getLogger(__name__)

_CACHE: set[date] | None = None


def _fetch_trade_dates() -> set[date]:
    """从 akshare 拉取全部 A 股交易日(同步,在线)。"""
    import akshare as ak
    import pandas as pd  # type: ignore[import-untyped]

    df: pd.DataFrame = ak.tool_trade_date_hist_sina()
    return set(pd.to_datetime(df["trade_date"]).dt.date)


def load_trading_calendar() -> set[date]:
    """加载交易日历(进程内缓存,首次调用联网拉取)。

    如果 akshare 不可用或网络异常,回退到空集合 —— 调用方应处理此情况。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = _fetch_trade_dates()
        logger.info("trading_calendar.loaded dates=%d", len(_CACHE))
    except Exception:
        logger.warning("trading_calendar.fetch_failed fallback=empty")
        _CACHE = set()
    return _CACHE


def is_trading_day(d: date) -> bool:
    """判断某日是否为 A 股交易日。"""
    return d in load_trading_calendar()


def trading_days(start: date, end: date) -> set[date]:
    """返回 ``[start, end]`` 区间内的所有 A 股交易日。

    如果交易日历不可用(akshare 未安装 / 网络失败),
    回退到朴素 Mon-Fri 工作日。
    """
    if start > end:
        return set()
    calendar = load_trading_calendar()
    if calendar:
        return {d for d in calendar if start <= d <= end}
    # fallback: naive weekdays
    from datetime import timedelta

    result: set[date] = set()
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.add(current)
        current += timedelta(days=1)
    return result


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
