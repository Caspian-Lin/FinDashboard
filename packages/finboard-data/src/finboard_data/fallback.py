"""主源拉不到标的时的备用源回退 provider(issue #257)。

回测引擎按注入的单个 ``HistoricalDataProvider`` 取数;当主源不覆盖某标的
(典型:tushare 2000 积分拉不到 ETF,``daily`` 返回空)且
``settings.data_fallback_provider`` 配置了备用源时,由本组合 provider 在
取数入口显式回退,让 ETF 等标的的回测不再静默空转。

边界(有意保持最小):

* 只代理 ``fetch_bars``——这是回测引擎对 provider 的唯一消费入口;
  写缓存类入口(update_cache / 批量同步)不代理,数据落盘的来源应保持
  单一,由数据同步入口的既有回退链负责;
* 回退触发条件是 primary 抛异常或返回**空列表**(主源整体不覆盖该标的);
  「部分区间缺失」不回退——同一条权益曲线内混两个源的复权口径是数据质量
  事故,异源可用部分由 ``TushareBarProvider`` 的缓存层策略(read-through /
  具名回退)处理,不在本层拼接。
"""

from __future__ import annotations

from datetime import date

import structlog

from finboard_data.base import HistoricalDataProvider
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)


class FallbackBarProvider:
    """组合主源与备用源:primary 空结果 / 异常时回退 fallback。"""

    def __init__(
        self,
        *,
        primary: HistoricalDataProvider,
        primary_name: str,
        fallback: HistoricalDataProvider,
        fallback_name: str,
    ) -> None:
        self._primary = primary
        self._primary_name = primary_name
        self._fallback = fallback
        self._fallback_name = fallback_name

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        try:
            bars = await self._primary.fetch_bars(symbol, period, start, end, adjust=adjust)
        except Exception as exc:
            logger.warning(
                "fallback.primary_failed",
                symbol=symbol.code,
                primary=self._primary_name,
                error=str(exc),
            )
            bars = []
        if bars:
            return bars
        logger.info(
            "fallback.using_fallback",
            symbol=symbol.code,
            primary=self._primary_name,
            fallback=self._fallback_name,
            period=period.value,
            start=str(start),
            end=str(end),
        )
        return await self._fallback.fetch_bars(symbol, period, start, end, adjust=adjust)
