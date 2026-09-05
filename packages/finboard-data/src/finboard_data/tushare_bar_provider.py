"""Tushare A 股日线行情 Provider。

2000 积分覆盖 ``daily`` 与 ``adj_factor``;2026-09-06 实测(issue #341)
``index_daily`` / ``fund_daily`` 2000 积分档亦可调(fund_daily 官方文档
仍标 5000,与实测不符)。本 Provider 的 scope 仍只承诺 A 股股票日线,
ETF / 指数由上层路由 akshare——scope 设计决定,非积分硬约束。
可转债日线(issue #265)走 2000 积分档的 ``cb_daily`` 专属接口:按代码
规则(11xxxx.SH / 12xxxx.SZ)分流,无复权,原始价落盘。
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, TypeGuard, cast

import structlog

from finboard_data.akshare_provider import AkShareProvider, is_convertible_code, is_futures_code
from finboard_data.cache import CacheMetadata, ParquetCache, expected_last_bar_date
from finboard_data.tushare_budget import TushareBudget, shared_tushare_budget
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)

_VALID_ADJUSTMENTS = frozenset({"qfq", "hqfq", "none"})
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
#: 可转债日线字段白名单(issue #265,doc_id=187,2000 积分档)。
_CB_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
_ADJ_FIELDS = "ts_code,trade_date,adj_factor"
_SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
_MAX_CHUNK_DAYS = 15 * 366
_REQUEST_EXECUTOR = ThreadPoolExecutor(
    max_workers=32,
    thread_name_prefix="finboard-tushare",
)


@dataclass(frozen=True, slots=True)
class TushareLifecycleEvent:
    """由 ``suspend_d`` 规范化的单标的停复牌事件。"""

    symbol: str
    event_type: Literal["suspension_day", "intraday_suspension", "resumption"]
    effective_date: date
    suspend_timing: str | None = None


class TushareBarClient(Protocol):
    def daily(self, **kwargs: str) -> object:
        """调用 A 股日线接口。"""
        ...

    def cb_daily(self, **kwargs: str) -> object:
        """调用可转债日线接口(issue #265)。"""
        ...

    def adj_factor(self, **kwargs: str) -> object:
        """调用 A 股复权因子接口。"""
        ...

    def suspend_d(self, **kwargs: str) -> object:
        """调用每日停复牌信息接口。"""
        ...


class TushareBarProvider(AkShareProvider):
    """复用现有缓存/批处理骨架的 Tushare A 股日线实现。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: TushareBarClient | None = None,
        budget: TushareBudget | None = None,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        max_concurrency: int = 16,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        requests_per_minute: int = 200,
        daily_request_limit: int = 100_000,
        usage_file: str | Path = "data_cache/tushare_usage.json",
        max_cache_io_concurrency: int = 1,
    ) -> None:
        super().__init__(
            cache_dir=cache_dir,
            use_cache=use_cache,
            max_concurrency=max_concurrency,
            request_interval=0.0,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            max_cache_io_concurrency=max_cache_io_concurrency,
        )
        self._client = client if client is not None else self._create_client(token)
        self._budget = budget or shared_tushare_budget(
            requests_per_minute=requests_per_minute,
            daily_request_limit=daily_request_limit,
            usage_file=usage_file,
        )

    def __repr__(self) -> str:
        return "TushareBarProvider(source='tushare', asset='stock', period='1d')"

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        """读取同源缓存,仅请求尚未成功查询过的覆盖区间。"""
        if self._cache is None:
            return await super().fetch_bars(
                symbol,
                period,
                start,
                end,
                adjust=adjust,
            )

        effective_end = expected_last_bar_date(end)
        metadata = await self._cache.metadata_for(symbol, period, adjust)
        if _is_foreign_cache(metadata) and metadata.covers(start, effective_end):
            # issue #257:异源缓存(如 akshare 同步的 ETF/指数)已完整覆盖
            # 请求区间时直接读出返回,不再视为全量缺口,也不浪费 tushare 预算。
            logger.info(
                "tushare.foreign_cache_hit",
                symbol=symbol.code,
                foreign_source=metadata.source,
                period=period.value,
            )
        ranges = self._cache_fetch_ranges(metadata, start, effective_end)
        cached = await self._cache.read(symbol, period, adjust) if metadata is not None else []
        if not ranges:
            logger.debug("tushare.cache_hit", symbol=symbol.code, count=len(cached))
            return ParquetCache.filter_by_date(cached, start, end)

        all_bars = await self._fetch_and_persist_ranges(symbol, period, adjust, ranges, cached)
        return ParquetCache.filter_by_date(all_bars, start, end)

    async def update_cache(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        on_status: Callable[[str], None] | None = None,
    ) -> bool:
        """按已查询覆盖区间判定缺口,逐段拉取并即时增量落盘。

        断点安全:每个 range 拉完立即 merge 进 parquet,中断后重跑时已落盘
        的查询区间会被 :meth:`_cache_fetch_ranges` 判定为已覆盖而跳过。
        覆盖区间与 Bar 日期分开记录,因此停牌日和上市前日期不会永久 miss。
        """
        if self._cache is None:
            return await super().update_cache(
                symbol,
                period,
                start,
                end,
                adjust=adjust,
                on_status=on_status,
            )

        if on_status is not None:
            on_status("checking_cache")
        effective_end = expected_last_bar_date(end)
        metadata = await self._cache.metadata_for(symbol, period, adjust)
        if _is_foreign_cache(metadata) and metadata.covers(start, effective_end):
            logger.info(
                "tushare.foreign_cache_hit",
                symbol=symbol.code,
                foreign_source=metadata.source,
                period=period.value,
            )
        ranges = self._cache_fetch_ranges(metadata, start, effective_end)
        if not ranges:
            if on_status is not None:
                on_status("cache_hit")
            return True

        if on_status is not None:
            on_status("fetching")
        cached = await self._cache.read(symbol, period, adjust) if metadata is not None else []
        current = await self._fetch_and_persist_ranges(symbol, period, adjust, ranges, cached)
        if on_status is not None:
            on_status("done")
        return bool(current)

    async def _fetch_and_persist_ranges(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        ranges: tuple[tuple[date, date], ...],
        cached: list[Bar],
    ) -> list[Bar]:
        """逐段拉取,每段拉完立即增量 merge 进 parquet(断点安全)。

        来源切换时丢弃旧源历史、从空开始合并,避免 akshare/yfinance 与
        tushare 混写(adjust 基准日不同,混源会产生价格跳变);唯一例外是
        tushare 对全部缺口区间都拉不到 bars(指数 / ETF 等不在本 provider
        scope 的标的——scope 由上层路由决定,非积分硬约束,#341)——此时
        不再静默丢弃异源缓存,而是具名回退返回异源已缓存的 bars
        (issue #257),回测引擎才能消费 akshare 同步的 ETF 行情。
        返回合并后的完整 Bar 列表(升序)。
        """
        sources = {bar.source for bar in cached}
        foreign = bool(sources) and sources != {"tushare"}
        current: list[Bar] = [] if foreign else list(cached)
        for range_start, range_end in ranges:
            bars = await self._fetch_from_akshare(symbol, period, range_start, range_end, adjust)
            if bars and self._cache is not None:
                current = await self._cache.merge(
                    symbol,
                    period,
                    adjust,
                    bars,
                    existing_bars=current,
                    covered_ranges=((range_start, range_end),),
                )
            elif current and self._cache is not None:
                await self._cache.mark_covered(
                    symbol,
                    period,
                    adjust,
                    range_start,
                    range_end,
                    source="tushare",
                )
        if not current and foreign:
            logger.warning(
                "tushare.foreign_cache_fallback",
                symbol=symbol.code,
                foreign_sources=sorted(source for source in sources if source),
                requested_ranges=len(ranges),
                cached_bars=len(cached),
            )
            return cached
        return current

    @staticmethod
    def _cache_fetch_ranges(
        metadata: CacheMetadata | None,
        start: date,
        effective_end: date,
    ) -> tuple[tuple[date, date], ...]:
        """返回尚未查询过的日期段;不把合法无 Bar 日期误判为缺口。"""
        if start > effective_end:
            return ()
        if metadata is None:
            return ((start, effective_end),)
        if metadata.source != "tushare":
            # issue #257:异源缓存完整覆盖时按零缺口处理(read-through,
            # 由调用方具名记 log);不完整覆盖仍视为全量缺口——对 tushare
            # 可服务的股票,重建纯 tushare 缓存的既有语义保持不变。
            if _is_foreign_cache(metadata) and metadata.covers(start, effective_end):
                return ()
            return ((start, effective_end),)
        if metadata.covers(start, effective_end):
            return ()

        covered = list(metadata.covered_ranges)
        if metadata.first_date is not None and metadata.last_date is not None:
            covered.append((metadata.first_date, metadata.last_date))
        covered.sort()

        missing: list[tuple[date, date]] = []
        cursor = start
        for range_start, range_end in covered:
            if range_end < cursor:
                continue
            if range_start > effective_end:
                break
            if range_start > cursor:
                missing.append((cursor, min(effective_end, range_start - timedelta(days=1))))
            cursor = max(cursor, range_end + timedelta(days=1))
            if cursor > effective_end:
                break
        if cursor <= effective_end:
            missing.append((cursor, effective_end))
        return tuple(item for item in missing if item[0] <= item[1])

    async def _fetch_from_akshare(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        """覆盖父类远端入口;缓存与批量逻辑仍复用父类。"""
        if period is not BarPeriod.D1:
            raise ValueError("Tushare 2000 积分行情源当前只支持 A 股日线")
        if adjust not in _VALID_ADJUSTMENTS:
            raise ValueError(f"不支持的复权方式: {adjust}")
        if start > end:
            raise ValueError("start 不能晚于 end")
        if is_futures_code(symbol.code):
            # 期货(issue #267):tushare 侧本 issue 不接线(fut_daily 属另
            # 档积分),fail-visible 指路 akshare 源,不静默走股票 daily
            # 误路由。
            raise ValueError(
                f"tushare 2000 积分源不提供期货行情: {symbol.code};"
                "期货日线请使用 akshare 源(新浪主连,issue #267)"
            )
        async with self._semaphore:
            bars = await self._fetch_daily_chunks(symbol, start, end, adjust)
        logger.info(
            "tushare.fetched",
            symbol=symbol.code,
            period=period.value,
            count=len(bars),
            adjustment=adjust,
        )
        return bars

    async def _fetch_daily_chunks(
        self,
        symbol: Symbol,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        if is_convertible_code(symbol.code):
            # 可转债日线(issue #265):cb_daily 是 2000 积分专属接口,转债
            # 无股票式复权概念 —— 不调 adj_factor,按原始成交价落盘。缓存键
            # 沿用请求 adjust(默认 qfq,与全资产/发布默认一致,#256 指数同
            # 策略:键存在但语义为 no-op,发布 adjustment 与下载键一致)。
            cb_rows: list[Mapping[str, object]] = []
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + timedelta(days=_MAX_CHUNK_DAYS - 1))
                cb_rows.extend(
                    await self._call(
                        "cb_daily",
                        ts_code=symbol.code,
                        start_date=cursor.strftime("%Y%m%d"),
                        end_date=chunk_end.strftime("%Y%m%d"),
                        fields=_CB_DAILY_FIELDS,
                    )
                )
                cursor = chunk_end + timedelta(days=1)
            return _build_bars(
                symbol,
                cb_rows,
                [],
                "none",
                endpoint="cb_daily",
                # 转债 1 手 = 10 张(与 10 张/手最小交易单位一致)。
                volume_multiplier=Decimal("10"),
            )
        daily_rows: list[Mapping[str, object]] = []
        factor_rows: list[Mapping[str, object]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=_MAX_CHUNK_DAYS - 1))
            params = {
                "ts_code": symbol.code,
                "start_date": cursor.strftime("%Y%m%d"),
                "end_date": chunk_end.strftime("%Y%m%d"),
            }
            daily_rows.extend(await self._call("daily", fields=_DAILY_FIELDS, **params))
            if adjust != "none":
                factor_rows.extend(await self._call("adj_factor", fields=_ADJ_FIELDS, **params))
            cursor = chunk_end + timedelta(days=1)
        return _build_bars(symbol, daily_rows, factor_rows, adjust)

    async def fetch_suspension_events(
        self,
        symbol: Symbol,
        start: date,
        end: date,
    ) -> list[TushareLifecycleEvent]:
        """拉取并校验指定 A 股标的的每日停复牌事件。"""
        if start > end:
            raise ValueError("start 不能晚于 end")
        async with self._semaphore:
            rows = await self._call(
                "suspend_d",
                ts_code=symbol.code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields=_SUSPEND_FIELDS,
            )

        events: list[TushareLifecycleEvent] = []
        seen: set[tuple[date, str]] = set()
        for row in rows:
            returned_symbol = str(row.get("ts_code", "")).strip().upper()
            if returned_symbol != symbol.code.upper():
                raise ValueError(
                    f"Tushare suspend_d 返回了非请求标的: {returned_symbol or '<empty>'}"
                )
            effective_date = _trade_date(row, "suspend_d")
            suspend_type = str(row.get("suspend_type", "")).strip().upper()
            suspend_timing = _optional_text(row.get("suspend_timing"))
            event_type: Literal["suspension_day", "intraday_suspension", "resumption"]
            if suspend_type == "S":
                event_type = "intraday_suspension" if suspend_timing else "suspension_day"
            elif suspend_type == "R":
                event_type = "resumption"
            else:
                raise ValueError(f"Tushare suspend_d {effective_date} suspend_type 无效")
            key = (effective_date, event_type)
            if key in seen:
                raise ValueError(f"Tushare suspend_d 返回重复事件: {effective_date} {event_type}")
            seen.add(key)
            events.append(
                TushareLifecycleEvent(
                    symbol=symbol.code,
                    event_type=event_type,
                    effective_date=effective_date,
                    suspend_timing=suspend_timing,
                )
            )
        events.sort(key=lambda item: (item.effective_date, item.event_type))
        logger.info(
            "tushare.suspension_events_fetched",
            symbol=symbol.code,
            count=len(events),
        )
        return events

    async def _call(self, endpoint: str, **kwargs: str) -> list[Mapping[str, object]]:
        method_object = getattr(self._client, endpoint, None)
        if not callable(method_object):
            raise RuntimeError(f"Tushare SDK 不支持 {endpoint}")
        method = cast(Callable[..., object], method_object)
        last_error_type = "UnknownError"
        for attempt in range(self._max_retries + 1):
            await self._budget.acquire()
            try:
                loop = asyncio.get_running_loop()
                payload = await loop.run_in_executor(
                    _REQUEST_EXECUTOR,
                    partial(method, **kwargs),
                )
                return _records(payload, endpoint)
            except Exception as exc:
                last_error_type = type(exc).__name__
                if attempt < self._max_retries:
                    wait = self._retry_backoff**attempt
                    logger.warning(
                        "tushare.fetch_retry",
                        endpoint=endpoint,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        wait_seconds=wait,
                        error_type=last_error_type,
                    )
                    await asyncio.sleep(wait)
        raise RuntimeError(f"Tushare {endpoint} 调用失败({last_error_type});凭据与上游错误已脱敏")

    @staticmethod
    def _create_client(explicit_token: str | None) -> TushareBarClient:
        token = (
            explicit_token if explicit_token is not None else os.getenv("FINBOARD_TUSHARE_TOKEN")
        )
        if token is None or not token.strip():
            raise ValueError("未配置 Tushare token;请设置 FINBOARD_TUSHARE_TOKEN")
        try:
            module = importlib.import_module("tushare")
        except ImportError:
            raise RuntimeError("未安装 Tushare SDK;请安装 finboard-data[tushare]") from None
        factory_object = getattr(module, "pro_api", None)
        if not callable(factory_object):
            raise RuntimeError("已安装的 Tushare SDK 不提供 pro_api")
        factory = cast(Callable[[str], object], factory_object)
        try:
            return cast(TushareBarClient, factory(token.strip()))
        except Exception:
            raise RuntimeError("Tushare client 初始化失败;凭据错误已脱敏") from None


def _is_foreign_cache(metadata: CacheMetadata | None) -> TypeGuard[CacheMetadata]:
    """缓存是否由其他来源(akshare / yfinance / mixed)构建(issue #257)。

    ``source=None`` 的 legacy 文件无法证明来源,按非异源处理,保持既有
    「全量重建」行为不变。
    """
    return metadata is not None and metadata.source not in (None, "tushare")


def _records(payload: object, endpoint: str) -> list[Mapping[str, object]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        raw: object = payload
    else:
        converter = getattr(payload, "to_dict", None)
        if not callable(converter):
            raise ValueError(f"Tushare {endpoint} 返回值不是记录列表或 DataFrame")
        raw = converter(orient="records")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(f"Tushare {endpoint} 响应记录格式无效")
    records: list[Mapping[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"Tushare {endpoint} 第 {index} 行不是字段映射")
        records.append(cast(Mapping[str, object], item))
    return records


def _decimal(row: Mapping[str, object], field: str, endpoint: str) -> Decimal:
    value = row.get(field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Tushare {endpoint} 字段 {field} 不是有效数字") from None
    if not result.is_finite():
        raise ValueError(f"Tushare {endpoint} 字段 {field} 不是有限数字")
    return result


def _trade_date(row: Mapping[str, object], endpoint: str) -> date:
    try:
        return datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
    except (KeyError, ValueError):
        raise ValueError(f"Tushare {endpoint} trade_date 无效") from None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _build_bars(
    symbol: Symbol,
    daily_rows: list[Mapping[str, object]],
    factor_rows: list[Mapping[str, object]],
    adjust: str,
    *,
    endpoint: str = "daily",
    volume_multiplier: Decimal = Decimal("100"),
) -> list[Bar]:
    """把 tushare 日线行规范化为领域 Bar。

    ``volume_multiplier``:daily 的 vol 单位为手(1 手=100 股);cb_daily
    的 vol 单位亦为手,但可转债 1 手=10 张(issue #265),传 10。
    """
    factors: dict[date, Decimal] = {
        _trade_date(row, "adj_factor"): _decimal(row, "adj_factor", "adj_factor")
        for row in factor_rows
    }
    reference_factor = factors[max(factors)] if factors else Decimal("1")
    bars: list[Bar] = []
    seen: set[date] = set()
    for row in daily_rows:
        business_date = _trade_date(row, endpoint)
        if business_date in seen:
            raise ValueError(f"Tushare {endpoint} 返回重复交易日: {business_date}")
        seen.add(business_date)
        factor = Decimal("1")
        if adjust != "none":
            try:
                factor = factors[business_date]
            except KeyError:
                raise ValueError(f"Tushare 缺少 {business_date} 的复权因子") from None
        multiplier = (
            Decimal("1")
            if adjust == "none"
            else factor / reference_factor
            if adjust == "qfq"
            else factor
        )
        prices = {
            name: _decimal(row, name, endpoint) * multiplier
            for name in ("open", "high", "low", "close")
        }
        if any(not math.isfinite(float(value)) or value <= 0 for value in prices.values()):
            raise ValueError(f"Tushare {endpoint} {business_date} 包含无效价格")
        bars.append(
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(business_date, datetime.min.time(), tzinfo=UTC),
                open=prices["open"],
                high=prices["high"],
                low=prices["low"],
                close=prices["close"],
                # Tushare vol 为手、amount 为千元;领域 Bar 使用股(张)和元。
                volume=_decimal(row, "vol", endpoint) * volume_multiplier,
                amount=_decimal(row, "amount", endpoint) * Decimal("1000"),
                source="tushare",
            )
        )
    bars.sort(key=lambda item: item.timestamp)
    return bars
