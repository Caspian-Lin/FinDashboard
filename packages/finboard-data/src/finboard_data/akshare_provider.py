"""``AkShareProvider`` —— 基于 akshare 的 A 股历史数据提供者。

akshare 免费、无需 token,覆盖 A 股日线 / 分钟线,是个人量化的首选数据源。

akshare 为同步库,所有调用通过 ``asyncio.to_thread`` 在线程池执行,
避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from finboard_data.cache import ParquetCache
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)

_PERIOD_MAP: dict[BarPeriod, str] = {
    BarPeriod.D1: "daily",
    BarPeriod.M1: "1",
    BarPeriod.M5: "5",
    BarPeriod.M15: "15",
    BarPeriod.M30: "30",
    BarPeriod.H1: "60",
}

_ADJUST_MAP: dict[str, str] = {
    "qfq": "qfq",
    "hqfq": "hfq",
    "none": "",
}


class AkShareProvider:
    """akshare 历史数据提供者,带本地 parquet 缓存。

    用法::

        provider = AkShareProvider(cache_dir="data_cache")
        bars = await provider.fetch_bars(symbol, BarPeriod.D1,
                                         start, end, adjust="qfq")
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
    ) -> None:
        if use_cache:
            dir_path = str(cache_dir) if cache_dir else "data_cache"
            self._cache: ParquetCache | None = ParquetCache(dir_path)
        else:
            self._cache = None

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        """拉取历史 K 线(优先读缓存,缺失时增量拉取)。"""
        cached: list[Bar] = []
        if self._cache is not None:
            cached = await self._cache.read(symbol, period, adjust)

        if self._is_cache_complete(cached, start, end):
            logger.debug(
                "akshare.cache_hit",
                symbol=symbol.code,
                count=len(cached),
            )
            return ParquetCache.filter_by_date(cached, start, end)

        logger.info(
            "akshare.fetching",
            symbol=symbol.code,
            period=period.value,
            start=str(start),
            end=str(end),
            cached=len(cached),
        )
        fresh = await self._fetch_from_akshare(
            symbol, period, start, end, adjust
        )
        if self._cache is not None and fresh:
            all_bars = await self._cache.merge(symbol, period, adjust, fresh)
        else:
            all_bars = fresh

        return ParquetCache.filter_by_date(all_bars, start, end)

    @staticmethod
    def _is_cache_complete(
        cached: list[Bar], start: date, end: date
    ) -> bool:
        """简化判断:缓存非空且最后一条 >= end 即视为完整。

        精确的交易日对齐由 TradingCalendar 负责,这里用宽松判断避免
        引入 scheduler 依赖。回测引擎会在拿到数据后做进一步处理。
        """
        if not cached:
            return False
        last = cached[-1].timestamp.date()
        return last >= end

    async def _fetch_from_akshare(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        return await asyncio.to_thread(
            self._fetch_sync, symbol, period, start, end, adjust
        )

    def _fetch_sync(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        import akshare as ak

        code = symbol.code.split(".")[0]
        ak_period = _PERIOD_MAP.get(period)
        if ak_period is None:
            raise ValueError(f"akshare 不支持周期: {period}")
        ak_adjust = _ADJUST_MAP.get(adjust, "")

        if period == BarPeriod.D1:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period=ak_period,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=ak_adjust,
            )
        else:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=ak_period,
                start_date=f"{start.strftime('%Y-%m-%d')} 09:30:00",
                end_date=f"{end.strftime('%Y-%m-%d')} 15:00:00",
                adjust=ak_adjust,
            )

        bars: list[Bar] = []
        col_map = self._column_map(period)

        for _, row in df.iterrows():
            ts = self._parse_timestamp(row[col_map["datetime"]], period)
            bars.append(
                Bar(
                    symbol=symbol,
                    period=period,
                    timestamp=ts,
                    open=Decimal(str(row[col_map["open"]])),
                    high=Decimal(str(row[col_map["high"]])),
                    low=Decimal(str(row[col_map["low"]])),
                    close=Decimal(str(row[col_map["close"]])),
                    volume=Decimal(str(row[col_map["volume"]])),
                    amount=Decimal(
                        str(row[col_map["amount"]])
                        if col_map["amount"] in df.columns
                        else 0
                    ),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        logger.info(
            "akshare.fetched",
            symbol=symbol.code,
            period=period.value,
            count=len(bars),
        )
        return bars

    @staticmethod
    def _column_map(period: BarPeriod) -> dict[str, str]:
        """akshare 返回的中文列名映射。

        日线: 日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量 / 成交额
        分钟: 时间 / 开盘 / 最高 / 最低 / 收盘 / 成交量 / 成交额
        """
        datetime_col = "日期" if period == BarPeriod.D1 else "时间"
        return {
            "datetime": datetime_col,
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
        }

    @staticmethod
    def _parse_timestamp(raw: object, period: BarPeriod) -> datetime:
        if isinstance(raw, str):
            if period == BarPeriod.D1:
                dt = datetime.strptime(raw, "%Y-%m-%d")
            else:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        elif isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.strptime(str(raw), "%Y-%m-%d")
        return dt.replace(tzinfo=UTC)
