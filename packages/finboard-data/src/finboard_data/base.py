"""``HistoricalDataProvider`` —— 历史行情数据源统一抽象。

与 :class:`MarketDataAdapter` 的区别:

* ``MarketDataAdapter`` 面向**实时**行情(订阅推送 + 事件流);
* ``HistoricalDataProvider`` 面向**回测**(批量拉取历史 Bar 列表)。

所有实现必须 ``async``;底层同步 SDK(akshare / tushare)用 ``run_in_executor`` 桥接。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod


@runtime_checkable
class HistoricalDataProvider(Protocol):
    """历史 K 线数据源。

    实现方需保证:

    * 返回的 Bar 列表按 ``timestamp`` **升序**排列;
    * 时间范围左闭右闭(包含 ``start`` 和 ``end`` 当天);
    * ``adjust`` 取值:``"qfq"``(前复权)/ ``"hqfq"``(后复权)/ ``"none"``(不复权)。
    """

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        """拉取历史 K 线。

        :param symbol:   标的(如 ``510300.SH``)
        :param period:   K 线周期(D1 / M1 / M5 / M15 / M30 / H1)
        :param start:    起始日期(含)
        :param end:      结束日期(含)
        :param adjust:   复权方式
        :returns:        按 timestamp 升序排列的 Bar 列表
        """
        ...
