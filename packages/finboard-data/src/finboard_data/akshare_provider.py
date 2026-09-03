"""``AkShareProvider`` —— 基于 akshare 的 A 股历史数据提供者。

akshare 免费、无需 token,覆盖 A 股股票日线 / 分钟线、指数日线
(issue #184:指数按代码规则分流到 ``index_zh_a_hist``)与 ETF/LOF 基金日线
(issue #257:基金按代码规则分流到 ``fund_etf_hist_em``,避免股票接口把
沪市基金代码误拼成深市 secid),是个人量化的首选数据源。

akshare 为同步库,所有调用通过 ``asyncio.to_thread`` 在线程池执行,
避免阻塞事件循环。

内置限流(信号量 + 请求间隔 + 重试退避),防止被 akshare 封 IP。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path
from typing import Any, cast

import structlog

from finboard_data.cache import (
    ParquetCache,
    expected_last_bar_date,
    incremental_fetch_start,
)
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


def is_index_code(code: str) -> bool:
    """按 A 股指数代码规则判断是否为指数(供基准行情分流)。

    规则(带交易所后缀,避免把深市股票误判为指数):

    * ``000xxx.SH`` —— SSE 指数(上证指数 / 沪深300 / 中证系列);SSE 股票
      全部以 6 开头,000 段在 SSE 只属于指数;
    * ``399xxx.SZ`` —— SZSE 指数(深证成指 / 深证100 等);深市股票为
      000/001/002/003/300/301 段;
    * ``899xxx.BJ`` —— 北交所指数。

    指数无除权复权概念,akshare 指数接口也不接受 adjust 参数。
    """
    bare, _, suffix = code.partition(".")
    exchange = suffix.upper()
    if exchange not in {"SH", "SZ", "BJ"}:
        return False
    if exchange == "SH":
        return bare.startswith("000")
    if exchange == "SZ":
        return bare.startswith("399")
    return bare.startswith("899")


def is_etf_code(code: str) -> bool:
    """按 A 股场内基金代码规则判断是否为 ETF/LOF(issue #257)。

    规则(带交易所后缀,与 :func:`is_index_code` 同风格):

    * ``5xxxxx.SH`` —— 沪市基金(51/56/58 段 ETF 与 50 段 LOF);
    * ``15xxxx.SZ`` / ``16xxxx.SZ`` —— 深市 ETF / LOF;
    * 北交所暂无场内基金。

    背景:EM 股票日线接口内部按 ``6`` 开头判定沪市,``5`` 开头的沪市基金
    会被拼成深市 secid(实测 ``stock_zh_a_hist("510300")`` 请求
    ``secid=0.510300``)导致取数错误;``fund_etf_hist_em`` 的
    ``get_market_id`` 才能正确路由(沪市 ``1.510300``)。基金与指数 /
    股票代码段互不重叠,分流顺序不影响结果。
    """
    bare, _, suffix = code.partition(".")
    exchange = suffix.upper()
    if exchange == "SH":
        return bare.startswith("5")
    if exchange == "SZ":
        return bare.startswith(("15", "16"))
    return False


def is_convertible_code(code: str) -> bool:
    """按 A 股可转债代码规则判断是否为转债(issue #265)。

    规则(带交易所后缀,与 :func:`is_index_code` / :func:`is_etf_code`
    同风格,代码段互不重叠):

    * ``11xxxx.SH`` —— 沪市转债(110/111/113/118 段);
    * ``12xxxx.SZ`` —— 深市转债(123/127/128 段);
    * 北交所暂无场内转债(117 段暂不纳入,无日线数据上游)。

    与 ``finboard_data.assets.registry`` 的 ``_A_SHARE_CODE_TYPE``
    (11/12 → CONVERTIBLE)同口径。
    """
    bare, _, suffix = code.partition(".")
    exchange = suffix.upper()
    if exchange == "SH":
        return bare.startswith("11")
    if exchange == "SZ":
        return bare.startswith("12")
    return False


@dataclass(frozen=True, slots=True)
class ConvertibleOverviewEntry:
    """东财可转债一览(``bond_zh_cov``)的归一化单行(issue #265)。

    用途:转债标的登记的发现源 + cb_basic 的交叉验证 / 评级兜底。
    ``rating`` / ``conversion_price`` 是快照字段,东财无历史版本。
    """

    code: str  # 归一化转债代码 113000.SH
    name: str
    underlying_symbol: str | None  # 正股归一化代码 600519.SH
    rating: str | None  # 债券评级(AA / AA+ ...)
    conversion_price: Decimal | None


@dataclass(frozen=True, slots=True)
class ConvertibleRedemptionEvent:
    """集思录强赎(``bond_cb_redeem_jsl``)归一化单行(issue #265)。

    PIT 语义(诚实边界):集思录只提供**当前时点**的强赎快照,没有历史
    公告时间,``available_at`` = 本次观察时间;历史回测只能对「观察时点
    之后」的决策生效,历史公告回补需 5000 积分的 tushare ``cb_call``
    (后续 issue)。``redemption_date``(赎回日)优先,缺失回退
    ``stop_transfer_date``(停止交易日/最后转股日)作生效日。
    """

    code: str
    name: str
    underlying_symbol: str | None
    redemption_date: date | None
    stop_transfer_date: date | None
    redemption_price: Decimal | None
    observed_at: datetime

    @property
    def effective_date(self) -> date | None:
        return self.redemption_date or self.stop_transfer_date


def _frame_column(frame: object, candidates: Sequence[str]) -> str | None:
    """容错取列名:akshare 不同版本列名可能为中英文 / 空格差异。"""
    columns = getattr(frame, "columns", None)
    if columns is None:
        return None
    available = {str(col).strip(): str(col) for col in columns}
    for candidate in candidates:
        exact = available.get(candidate)
        if exact is not None:
            return exact
    lowered = {key.lower(): value for key, value in available.items()}
    for candidate in candidates:
        match = lowered.get(candidate.lower())
        if match is not None:
            return match
    return None


def _frame_columns_repr(frame: object) -> list[str]:
    """容错列出 DataFrame 列名(报错信息用;pandas Index 不能参与真值运算)。"""
    columns = getattr(frame, "columns", None)
    return [str(col) for col in columns] if columns is not None else []


def _cell(row: Mapping[str, object], column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "--"}:
        return None
    return text


def parse_convertible_overview_frame(frame: object) -> list[ConvertibleOverviewEntry]:
    """把 ``bond_zh_cov`` DataFrame 归一化为一览条目(纯函数,可离线测试)。

    必需列:债券代码 / 债券简称 / 正股代码;可选列:债券评级 / 转股价。
    缺必需列直接 raise(上游形状变化要 fail-visible,不静默出空清单)。
    非转债代码段(不以 11/12 开头的行,如页面附带的已兑付归档)按行跳过并计数。
    """
    code_col = _frame_column(frame, ("债券代码", "bond_id"))
    name_col = _frame_column(frame, ("债券简称", "bond_nm"))
    stock_col = _frame_column(frame, ("正股代码", "stock_id"))
    if code_col is None or name_col is None or stock_col is None:
        raise ValueError(
            "akshare bond_zh_cov 返回形状不符合预期(缺少 债券代码/债券简称/正股代码 列);"
            f"实际列: {_frame_columns_repr(frame)}"
        )
    rating_col = _frame_column(frame, ("债券评级", "rating"))
    price_col = _frame_column(frame, ("转股价", "convert_price"))

    entries: list[ConvertibleOverviewEntry] = []
    skipped = 0
    # akshare 无类型标注:经 Any 解包 DataFrame 行(上游形状错误已由
    # 必需列检查 fail-visible)。
    for raw_row in cast(Any, frame).to_dict(orient="records"):
        row: Mapping[str, object] = raw_row
        raw_code = _cell(row, code_col)
        raw_name = _cell(row, name_col)
        if raw_code is None or raw_name is None:
            skipped += 1
            continue
        code = normalize_overview_code(raw_code)
        if code is None or not is_convertible_code(code):
            skipped += 1
            continue
        underlying: str | None = None
        raw_stock = _cell(row, stock_col)
        if raw_stock is not None:
            underlying = normalize_overview_code(str(raw_stock))
        conversion_price: Decimal | None = None
        if price_col is not None:
            raw_price = _cell(row, price_col)
            if raw_price is not None:
                try:
                    candidate = Decimal(raw_price)
                except InvalidOperation:
                    candidate = None
                if candidate is not None and candidate > 0:
                    conversion_price = candidate
        entries.append(
            ConvertibleOverviewEntry(
                code=code,
                name=str(raw_name),
                underlying_symbol=underlying,
                rating=_cell(row, rating_col) if rating_col is not None else None,
                conversion_price=conversion_price,
            )
        )
    if not entries and skipped:
        raise ValueError(
            f"akshare bond_zh_cov 返回 {skipped} 行但无可识别的转债代码段(11xxxx.SH/12xxxx.SZ)"
        )
    return entries


def parse_convertible_redeem_frame(
    frame: object,
    *,
    observed_at: datetime,
) -> list[ConvertibleRedemptionEvent]:
    """把 ``bond_cb_redeem_jsl`` DataFrame 归一化为强赎事件(纯函数)。

    赎回日(redeem_dt/赎回日)与停止交易日(put_dt/停止交易日)都缺的行
    跳过(无法确定生效日);日期列容错解析 ``%Y-%m-%d`` / ``%Y%m%d``。
    """
    code_col = _frame_column(frame, ("债券代码", "bond_id"))
    name_col = _frame_column(frame, ("债券简称", "bond_nm"))
    if code_col is None or name_col is None:
        raise ValueError(
            "akshare bond_cb_redeem_jsl 返回形状不符合预期(缺少 债券代码/债券简称 列);"
            f"实际列: {_frame_columns_repr(frame)}"
        )
    stock_col = _frame_column(frame, ("正股代码", "stock_id"))
    redeem_col = _frame_column(frame, ("赎回日", "redeem_dt"))
    stop_col = _frame_column(frame, ("停止交易日", "put_dt", "最后转股日", "stop_transfer_date"))
    price_col = _frame_column(frame, ("赎回价", "redeem_price"))

    events: list[ConvertibleRedemptionEvent] = []
    for raw_row in cast(Any, frame).to_dict(orient="records"):
        row: Mapping[str, object] = raw_row
        raw_code = _cell(row, code_col)
        raw_name = _cell(row, name_col)
        if raw_code is None or raw_name is None:
            continue
        code = normalize_overview_code(str(raw_code))
        if code is None or not is_convertible_code(code):
            continue
        redemption_date = (
            _parse_flex_date(_cell(row, redeem_col)) if redeem_col is not None else None
        )
        stop_date = _parse_flex_date(_cell(row, stop_col)) if stop_col is not None else None
        if redemption_date is None and stop_date is None:
            continue
        redemption_price: Decimal | None = None
        if price_col is not None:
            raw_price = _cell(row, price_col)
            if raw_price is not None:
                try:
                    candidate = Decimal(raw_price)
                except InvalidOperation:
                    candidate = None
                if candidate is not None and candidate > 0:
                    redemption_price = candidate
        events.append(
            ConvertibleRedemptionEvent(
                code=code,
                name=str(raw_name),
                underlying_symbol=(
                    normalize_overview_code(str(_cell(row, stock_col)))
                    if stock_col is not None and _cell(row, stock_col) is not None
                    else None
                ),
                redemption_date=redemption_date,
                stop_transfer_date=stop_date,
                redemption_price=redemption_price,
                observed_at=observed_at,
            )
        )
    return events


def normalize_overview_code(raw: str) -> str | None:
    """东财/集思录 6 位纯数字代码 → 带后缀归一代码(转债/正股通用)。

    转债段显式映射(11 开头 → .SH,12 开头 → .SZ)—— 不能直接委托
    :func:`normalize_a_share_code`:其 6 位数字规则只认 6/9(沪)、0/3(深)、
    8/4/92(北),转债的 1 开头段不在其中,委托会把每只转债都归一失败。
    其余代码段(转债的正股)委托既有规则;无法归一返回 None。
    """
    text = str(raw).strip()
    if text.endswith((".SH", ".SZ", ".BJ")):
        return text.upper()
    if len(text) == 6 and text.isdigit():
        if text.startswith("11"):
            return f"{text}.SH"
        if text.startswith("12"):
            return f"{text}.SZ"
        from finboard_data.discovery import normalize_a_share_code

        return normalize_a_share_code(text)
    return None


def _parse_flex_date(value: object) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


class AkShareProvider:
    """akshare 历史数据提供者,带本地 parquet 缓存 + 限流。

    用法::

        provider = AkShareProvider(cache_dir="data_cache")
        bars = await provider.fetch_bars(symbol, BarPeriod.D1,
                                         start, end, adjust="qfq")

    批量拉取::

        results = await provider.fetch_bars_batch(
            [sym1, sym2, sym3], BarPeriod.D1, start, end,
            on_progress=lambda code, done, total: print(f"{done}/{total} {code}"),
        )

    限流参数:

    * ``max_concurrency``: 信号量,同时最多 N 个 akshare 请求在途
    * ``request_interval``: 两次请求间的最小间隔(秒),防封 IP
    * ``max_retries`` / ``retry_backoff``: 网络错误时指数退避重试
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        max_concurrency: int = 3,
        request_interval: float = 0.5,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        max_cache_io_concurrency: int = 1,
    ) -> None:
        if use_cache:
            dir_path = str(cache_dir) if cache_dir else "data_cache"
            self._cache: ParquetCache | None = ParquetCache(
                dir_path,
                max_io_concurrency=max_cache_io_concurrency,
            )
        else:
            self._cache = None

        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._request_interval = request_interval
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._interval_lock = asyncio.Lock()
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------ 单标的
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
        fetch_start = incremental_fetch_start(cached, start)
        fresh = await self._fetch_from_akshare(symbol, period, fetch_start, end, adjust)
        if self._cache is not None and fresh:
            all_bars = await self._cache.merge(
                symbol,
                period,
                adjust,
                fresh,
                existing_bars=cached,
            )
        else:
            all_bars = cached or fresh

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
        """仅更新本地缓存;完整缓存只读 footer,不解码历史行情。"""
        if self._cache is None:
            return bool(await self.fetch_bars(symbol, period, start, end, adjust=adjust))

        if on_status is not None:
            on_status("checking_cache")
        metadata = await self._cache.metadata_for(symbol, period, adjust)
        expected_end = expected_last_bar_date(end)
        if (
            metadata is not None
            and metadata.bar_count > 0
            and metadata.last_date is not None
            and metadata.last_date >= expected_end
        ):
            if on_status is not None:
                on_status("cache_hit")
            return True

        fetch_start = start
        if metadata is not None and metadata.last_date is not None:
            fetch_start = max(start, metadata.last_date - timedelta(days=7))
        if on_status is not None:
            on_status("fetching")
        fresh = await self._fetch_from_akshare(symbol, period, fetch_start, end, adjust)
        if not fresh:
            return metadata is not None and metadata.bar_count > 0
        if (
            metadata is not None
            and metadata.last_date is not None
            and fresh[-1].timestamp.date() <= metadata.last_date
        ):
            return True

        if on_status is not None:
            on_status("reading_cache")
        existing = await self._cache.read(symbol, period, adjust) if metadata is not None else []
        if on_status is not None:
            on_status("writing_cache")
        await self._cache.merge(
            symbol,
            period,
            adjust,
            fresh,
            existing_bars=existing,
        )
        return True

    async def last_cached_date(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str = "qfq",
    ) -> date | None:
        """返回该标的缓存中最新 bar 的日期(``None`` 表示无缓存)。

        供停牌检测(issue #35)比较拉取前后是否有新数据。只读 parquet footer。
        """
        if self._cache is None:
            return None
        metadata = await self._cache.metadata_for(symbol, period, adjust)
        return metadata.last_date if metadata is not None else None

    # ------------------------------------------------------------------ 转债兜底 (#265)

    async def fetch_convertible_overview(self) -> list[ConvertibleOverviewEntry]:
        """拉取东财可转债一览(``bond_zh_cov``,免费)。

        用途:cb_basic 的交叉验证 / 评级兜底(research_data_sync
        ``convertible_profiles`` 数据集消费)。退避/间隔遵循本 provider
        既有约定(信号量 + 请求间隔 + 指数退避重试)。
        """
        return await self._call_with_retry(
            "bond_zh_cov",
            lambda ak: ak.bond_zh_cov(),
            parse_convertible_overview_frame,
        )

    async def fetch_convertible_redeem_events(
        self,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> list[ConvertibleRedemptionEvent]:
        """拉取集思录强赎快照(``bond_cb_redeem_jsl``,免费)。

        ``now`` 可注入观察时钟(缺省 UTC 当前时间),决定事件
        ``observed_at``/``available_at``(集思录无历史公告时间,PIT 边界
        见 :class:`ConvertibleRedemptionEvent`)。
        """
        observed_at = (now or (lambda: datetime.now(UTC)))()
        return await self._call_with_retry(
            "bond_cb_redeem_jsl",
            lambda ak: ak.bond_cb_redeem_jsl(),
            lambda frame: parse_convertible_redeem_frame(frame, observed_at=observed_at),
        )

    async def _call_with_retry[T](
        self,
        name: str,
        call: Callable[[Any], object],
        parse: Callable[[object], list[T]],
    ) -> list[T]:
        """转债兜底接口的统一调用:限流间隔 + 指数退避重试 + 线程池。"""

        def _invoke() -> list[T]:
            import akshare as ak

            frame = call(ak)
            return parse(frame)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    await self._enforce_interval()
                    return await asyncio.to_thread(_invoke)
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = self._retry_backoff**attempt
                    logger.warning(
                        "akshare.convertible_retry",
                        interface=name,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        wait=f"{wait:.1f}s",
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ 批量
    async def fetch_bars_batch(
        self,
        symbols: list[Symbol],
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, list[Bar]]:
        """批量拉取多标的数据,带限流 + 进度回调。

        :param on_progress: 回调 ``on_progress(symbol_code, done, total)``
        :returns: ``{symbol_code: [Bar, ...]}``;拉取失败的标的值为空列表
        """
        total = len(symbols)
        if total > 200:
            raise ValueError(
                "fetch_bars_batch 最多支持 200 个标的;全市场落盘请使用 update_cache_batch"
            )
        if total == 0:
            return {}
        results: dict[str, list[Bar]] = {}
        done_count = 0
        queue: asyncio.Queue[Symbol] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        before = self._cache.io_stats() if self._cache is not None else None

        async def _worker() -> None:
            nonlocal done_count
            while True:
                try:
                    sym = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    bars = await self.fetch_bars(sym, period, start, end, adjust=adjust)
                except Exception:
                    logger.exception("akshare.batch_failed", symbol=sym.code)
                    bars = []
                results[sym.code] = bars
                done_count += 1
                if on_progress is not None:
                    on_progress(sym.code, done_count, total)

        worker_count = min(self._max_concurrency, total)
        await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(worker_count)])

        if self._cache is not None and before is not None:
            after = self._cache.io_stats()
            logger.info(
                "akshare.batch_cache_io",
                symbols=total,
                workers=worker_count,
                read_ops=after.read_ops - before.read_ops,
                read_bytes=after.read_bytes - before.read_bytes,
                write_ops=after.write_ops - before.write_ops,
                write_bytes=after.write_bytes - before.write_bytes,
            )
        return results

    async def update_cache_batch(
        self,
        symbols: list[Symbol],
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        on_progress: Callable[[str, int, int], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ) -> dict[str, bool]:
        """有界并发批量更新缓存,不在内存中保留历史 bars。"""
        total = len(symbols)
        if total == 0:
            return {}
        results: dict[str, bool] = {}
        done_count = 0
        queue: asyncio.Queue[Symbol] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        before = self._cache.io_stats() if self._cache is not None else None

        async def _worker() -> None:
            nonlocal done_count
            while True:
                try:
                    sym = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    ok = await self.update_cache(
                        sym,
                        period,
                        start,
                        end,
                        adjust=adjust,
                        on_status=(
                            partial(on_status, sym.code)
                            if on_status is not None
                            else None
                        ),
                    )
                except Exception:
                    logger.exception("akshare.cache_update_failed", symbol=sym.code)
                    ok = False
                results[sym.code] = ok
                if on_status is not None:
                    on_status(sym.code, "completed" if ok else "failed")
                done_count += 1
                if on_progress is not None:
                    on_progress(sym.code, done_count, total)

        worker_count = min(self._max_concurrency, total)
        await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(worker_count)])

        if self._cache is not None and before is not None:
            after = self._cache.io_stats()
            logger.info(
                "akshare.cache_update_io",
                symbols=total,
                workers=worker_count,
                read_ops=after.read_ops - before.read_ops,
                read_bytes=after.read_bytes - before.read_bytes,
                write_ops=after.write_ops - before.write_ops,
                write_bytes=after.write_bytes - before.write_bytes,
            )
        return results

    @staticmethod
    def _is_cache_complete(cached: list[Bar], start: date, end: date) -> bool:
        """简化判断:缓存非空且最后一条 >= end 即视为完整。

        精确的交易日对齐由 TradingCalendar 负责,这里用宽松判断避免
        引入 scheduler 依赖。回测引擎会在拿到数据后做进一步处理。
        """
        if not cached:
            return False
        last = cached[-1].timestamp.date()
        return last >= expected_last_bar_date(end)

    async def _fetch_from_akshare(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        """带信号量限流 + 请求间隔 + 重试退避的 akshare 调用。"""
        async with self._semaphore:
            await self._enforce_interval()
            return await self._fetch_with_retry(symbol, period, start, end, adjust)

    async def _enforce_interval(self) -> None:
        """保证两次 akshare 请求之间至少间隔 ``_request_interval`` 秒。"""
        async with self._interval_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_request_time
            if elapsed < self._request_interval:
                await asyncio.sleep(self._request_interval - elapsed)
            self._last_request_time = loop.time()

    async def _fetch_with_retry(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        """指数退避重试。"""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.to_thread(self._fetch_sync, symbol, period, start, end, adjust)
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = self._retry_backoff**attempt
                    logger.warning(
                        "akshare.fetch_retry",
                        symbol=symbol.code,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        wait=f"{wait:.1f}s",
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

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

        if is_convertible_code(symbol.code):
            # 可转债日线(issue #265):akshare 无可靠的转债日线接口
            # (股票接口按 6 开头拼 secid,会把转债误路由成深市,#257 同源
            # 缺陷),fail-visible 拒绝而不是静默产出空/错数据;转债行情
            # 走 tushare cb_daily(TushareBarProvider._fetch_daily_chunks)。
            raise ValueError(
                f"akshare 不支持可转债日线: {symbol.code};"
                "转债行情请使用 tushare 源(cb_daily 专属接口,issue #265)"
            )

        if is_index_code(symbol.code):
            # 指数基准行情(issue #184):无复权概念,index_zh_a_hist 不接受
            # adjust 参数;分钟线留待有需要时接入。
            if period is not BarPeriod.D1:
                raise ValueError(f"akshare 指数行情仅支持日线,收到 {period}")
            df = ak.index_zh_a_hist(
                symbol=code,
                period=ak_period,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        elif is_etf_code(symbol.code):
            # ETF/LOF 基金行情(issue #257):fund_etf_hist_em 的 get_market_id
            # 能正确路由 5/6 开头沪市基金(股票接口会把 5 开头误判为深市);
            # 列名与股票接口一致,复用 _column_map。
            if period == BarPeriod.D1:
                df = ak.fund_etf_hist_em(
                    symbol=code,
                    period=ak_period,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust=ak_adjust,
                )
            else:
                df = ak.fund_etf_hist_min_em(
                    symbol=code,
                    period=ak_period,
                    start_date=f"{start.strftime('%Y-%m-%d')} 09:30:00",
                    end_date=f"{end.strftime('%Y-%m-%d')} 15:00:00",
                    adjust=ak_adjust,
                )
        elif period == BarPeriod.D1:
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
                        str(row[col_map["amount"]]) if col_map["amount"] in df.columns else 0
                    ),
                    source="akshare",
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
