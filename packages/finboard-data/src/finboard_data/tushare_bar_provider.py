"""Tushare A 股日线行情 Provider。

2000 积分覆盖 ``daily`` 与 ``adj_factor``,但不覆盖要求 5000 积分的
``fund_daily``。本 Provider 因此只承诺 A 股股票日线;ETF 由上层明确降级。
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, cast

import structlog

from finboard_data.akshare_provider import AkShareProvider
from finboard_data.tushare_budget import TushareBudget, shared_tushare_budget
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)

_VALID_ADJUSTMENTS = frozenset({"qfq", "hqfq", "none"})
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
_ADJ_FIELDS = "ts_code,trade_date,adj_factor"
_SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
_MAX_CHUNK_DAYS = 15 * 366


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
        max_concurrency: int = 4,
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

    @staticmethod
    def _is_cache_complete(cached: list[Bar], start: date, end: date) -> bool:
        """旧来源缓存不能冒充 Tushare 命中。"""
        return bool(cached) and {bar.source for bar in cached} == {"tushare"} and AkShareProvider._is_cache_complete(cached, start, end)

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
        """来源切换时全量替换,避免与旧 akshare/yfinance 缓存混合。"""
        if self._cache is not None:
            metadata = await self._cache.metadata_for(symbol, period, adjust)
            if metadata is not None and metadata.source != "tushare":
                if on_status is not None:
                    on_status("fetching")
                fresh = await self._fetch_from_akshare(symbol, period, start, end, adjust)
                if not fresh:
                    return False
                if on_status is not None:
                    on_status("writing_cache")
                await self._cache.write(symbol, period, adjust, fresh)
                return True
        return await super().update_cache(
            symbol,
            period,
            start,
            end,
            adjust=adjust,
            on_status=on_status,
        )

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
                factor_rows.extend(
                    await self._call("adj_factor", fields=_ADJ_FIELDS, **params)
                )
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
            event_type: Literal[
                "suspension_day", "intraday_suspension", "resumption"
            ]
            if suspend_type == "S":
                event_type = (
                    "intraday_suspension" if suspend_timing else "suspension_day"
                )
            elif suspend_type == "R":
                event_type = "resumption"
            else:
                raise ValueError(
                    f"Tushare suspend_d {effective_date} suspend_type 无效"
                )
            key = (effective_date, event_type)
            if key in seen:
                raise ValueError(
                    f"Tushare suspend_d 返回重复事件: {effective_date} {event_type}"
                )
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
                payload = await asyncio.to_thread(method, **kwargs)
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
        raise RuntimeError(
            f"Tushare {endpoint} 调用失败({last_error_type});凭据与上游错误已脱敏"
        )

    @staticmethod
    def _create_client(explicit_token: str | None) -> TushareBarClient:
        token = explicit_token if explicit_token is not None else os.getenv("FINBOARD_TUSHARE_TOKEN")
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
) -> list[Bar]:
    factors: dict[date, Decimal] = {
        _trade_date(row, "adj_factor"): _decimal(row, "adj_factor", "adj_factor")
        for row in factor_rows
    }
    reference_factor = factors[max(factors)] if factors else Decimal("1")
    bars: list[Bar] = []
    seen: set[date] = set()
    for row in daily_rows:
        business_date = _trade_date(row, "daily")
        if business_date in seen:
            raise ValueError(f"Tushare daily 返回重复交易日: {business_date}")
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
            name: _decimal(row, name, "daily") * multiplier
            for name in ("open", "high", "low", "close")
        }
        if any(not math.isfinite(float(value)) or value <= 0 for value in prices.values()):
            raise ValueError(f"Tushare daily {business_date} 包含无效价格")
        bars.append(
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(business_date, datetime.min.time(), tzinfo=UTC),
                open=prices["open"],
                high=prices["high"],
                low=prices["low"],
                close=prices["close"],
                # Tushare vol 为手、amount 为千元;领域 Bar 使用股和元。
                volume=_decimal(row, "vol", "daily") * Decimal("100"),
                amount=_decimal(row, "amount", "daily") * Decimal("1000"),
                source="tushare",
            )
        )
    bars.sort(key=lambda item: item.timestamp)
    return bars
