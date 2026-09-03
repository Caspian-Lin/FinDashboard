"""``AkShareProvider`` —— 基于 akshare 的 A 股历史数据提供者。

akshare 免费、无需 token,覆盖 A 股股票日线 / 分钟线、指数日线
(issue #184:指数按代码规则分流到 ``index_zh_a_hist``)、ETF/LOF 基金日线
(issue #257:基金按代码规则分流到 ``fund_etf_hist_em``,避免股票接口把
沪市基金代码误拼成深市 secid)、期货主连日线(issue #267:主连分流到
``futures_main_sina``,仅研究信号 / 基准,不可当作可成交合约)与可转债
兜底接口(issue #265:东财一览 + 集思录强赎),是个人量化的首选数据源。

akshare 为同步库,所有调用通过 ``asyncio.to_thread`` 在线程池执行,
避免阻塞事件循环。

内置限流(信号量 + 请求间隔 + 重试退避),防止被 akshare 封 IP。
"""

from __future__ import annotations

import asyncio
import re
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


#: 境内期货交易所后缀(issue #267)。与 ``finboard_data.assets.registry``
#: 的 ``_PREFIX_TABLE``(.CFFEX 等 → Market.FUTURE)同口径;INE(上期能源)
#: 在 assets 侧同期补齐。
FUTURES_EXCHANGES = frozenset({"CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX"})


def is_futures_code(code: str) -> bool:
    """按期货合约代码规则判断是否为期货(issue #267)。

    规则:带期货交易所后缀(``IF0.CFFEX`` / ``CU2408.SHFE`` / ``TA409.CZCE``),
    后缀属于 :data:`FUTURES_EXCHANGES`。与 A 股 / 指数 / ETF / 转债代码段
    互不重叠(后缀空间不相交),分流顺序不影响结果。
    """
    _, _, suffix = code.partition(".")
    return suffix.upper() in FUTURES_EXCHANGES


def is_futures_main_code(code: str) -> bool:
    """判断是否为期货**主力连续(主连)**代码(issue #267)。

    主连与具体合约是两种语义,必须可判定区分(数据不混淆):

    * 主连:品种字母段 + ``0``,如 ``IF0.CFFEX``(新浪主连接口的原生代码形制);
    * 具体合约:品种字母段 + 年月数字,如 ``IF2406.CFFEX``。

    判据 ``bare 去掉末位后全为字母`` 同时排除具体合约(``IF2410`` 去掉
    末位是 ``IF241``,非字母)。**主连仅用于研究信号 / 基准数据,不可当作
    可成交合约** —— 主连价格是换月拼接产物,无单一真实合约与之对应。
    """
    if not is_futures_code(code):
        return False
    bare = code.split(".", 1)[0]
    return len(bare) >= 2 and bare.isascii() and bare.endswith("0") and bare[:-1].isalpha()


@dataclass(frozen=True, slots=True)
class FuturesSeriesEntry:
    """期货主连品种登记(issue #267)。

    ``multiplier`` / ``margin_rate`` 与 finboard-backtest
    ``asset_rules.DEFAULT_TABLE`` 的 ``FuturesRule`` 同口径(成本口径,
    非交易所最新保证金下限);跨包一致性由单测锁定
    (tests/unit/data/test_akshare_futures.py)。``continuous=True``
    是登记语义的显式标注:登记的是主连序列,不是可成交合约。
    """

    code: str  # 归一化主连代码 IF0.CFFEX
    name: str  # 沪深300股指期货主连
    product: str  # 品种代码 IF(合约 → 品种映射的品种层)
    exchange: str  # CFFEX
    multiplier: Decimal
    margin_rate: Decimal
    price_tick: Decimal
    continuous: bool = True


#: 期货主连受控登记表(issue #267):IF/IC/IM 优先(路线 C 市场中性对冲
#: 的空头腿),IH 同属中金所股指期货一并纳入。全部条目必须满足
#: :func:`is_futures_main_code` 代码规则且 ``product`` 与代码一致(模块
#: 导入期即断言,防止登记表漂移)——#256 ``BENCHMARK_INDEX_REGISTRY``
#: 的受控风格。扩展新品种直接加一行;discover 经 ``sync_with_diff``
#: 自动写入 instruments 表。主连无 list_date / 行业的结构化上游
#: (新浪主连是连续序列,不是单一上市合约),保持 null。
FUTURES_MAIN_SERIES_REGISTRY: tuple[FuturesSeriesEntry, ...] = (
    FuturesSeriesEntry(
        code="IF0.CFFEX",
        name="沪深300股指期货主连",
        product="IF",
        exchange="CFFEX",
        multiplier=Decimal("300"),
        margin_rate=Decimal("0.12"),
        price_tick=Decimal("0.2"),
    ),
    FuturesSeriesEntry(
        code="IH0.CFFEX",
        name="上证50股指期货主连",
        product="IH",
        exchange="CFFEX",
        multiplier=Decimal("300"),
        margin_rate=Decimal("0.12"),
        price_tick=Decimal("0.2"),
    ),
    FuturesSeriesEntry(
        code="IC0.CFFEX",
        name="中证500股指期货主连",
        product="IC",
        exchange="CFFEX",
        multiplier=Decimal("200"),
        margin_rate=Decimal("0.14"),
        price_tick=Decimal("0.2"),
    ),
    FuturesSeriesEntry(
        code="IM0.CFFEX",
        name="中证1000股指期货主连",
        product="IM",
        exchange="CFFEX",
        multiplier=Decimal("200"),
        margin_rate=Decimal("0.14"),
        price_tick=Decimal("0.2"),
    ),
)

_INVALID_FUTURES_ENTRIES = tuple(
    entry.code
    for entry in FUTURES_MAIN_SERIES_REGISTRY
    if not is_futures_main_code(entry.code)
    or entry.product != entry.code.split(".", 1)[0][:-1]
    or entry.multiplier <= 0
    or not (Decimal("0") < entry.margin_rate <= Decimal("1"))
    or entry.price_tick <= 0
    or not entry.continuous
)
if _INVALID_FUTURES_ENTRIES:
    raise RuntimeError(
        "FUTURES_MAIN_SERIES_REGISTRY 存在不满足主连代码规则 / 乘数口径的条目: "
        + ", ".join(_INVALID_FUTURES_ENTRIES)
    )

_FUTURES_REGISTRY_BY_CODE = {entry.code: entry for entry in FUTURES_MAIN_SERIES_REGISTRY}
if len(_FUTURES_REGISTRY_BY_CODE) != len(FUTURES_MAIN_SERIES_REGISTRY):
    raise RuntimeError("FUTURES_MAIN_SERIES_REGISTRY 存在重复主连代码")


def futures_series_entry(code: str) -> FuturesSeriesEntry:
    """按归一化主连代码查登记表;未登记 raise(fail-closed,不猜测乘数)。"""
    entry = _FUTURES_REGISTRY_BY_CODE.get(code.strip().upper())
    if entry is None:
        raise ValueError(
            f"期货主连 {code} 未在 FUTURES_MAIN_SERIES_REGISTRY 登记;"
            "品种乘数 / 保证金率禁止猜测,请先扩登记表(issue #267)"
        )
    return entry


@dataclass(frozen=True, slots=True)
class FuturesDailyBar:
    """期货日线单日观测(主连 / 具体合约通用字段,issue #267)。

    语义标注由上游代码决定(:func:`is_futures_main_code`):主连价格是
    换月拼接产物,仅用于研究信号 / 基准;具体合约价格才是真实成交价。
    ``settle`` 为结算价(交易所官网日线提供;新浪主连为动态结算价)。
    """

    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None  # 手(张);新浪主连无成交额列
    open_interest: Decimal | None
    settle: Decimal | None


@dataclass(frozen=True, slots=True)
class FuturesOfficialDailyRow:
    """交易所官网日线(``get_futures_daily``)归一化单行(issue #267)。

    上游是「**按日 x 全市场合约**」表(每行一份具体月份合约),
    ``symbol`` 是具体合约代码(如 ``IF2406``)、``product`` 是品种;
    与主连的「逐标的 x 区间」形态不同构 —— 这是 v1 具体合约 EOD
    不进逐标的 parquet 缓存的直接原因(取舍见 fetch_futures_official_daily)。
    """

    symbol: str  # 具体合约代码 IF2406(上游无交易所后缀)
    product: str  # 品种 IF
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    open_interest: Decimal | None
    turnover: Decimal | None
    settle: Decimal | None
    pre_settle: Decimal | None


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


def _decimal_cell(row: Mapping[str, object], column: str) -> Decimal | None:
    """容错解析数值列:占位值(None/NaN/``--``)按缺失处理,非数字 raise。"""
    text = _cell(row, column)
    if text is None:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"列 {column} 含非数字值: {text!r}") from None
    return result if result.is_finite() else None


def parse_futures_main_sina_frame(frame: object) -> list[FuturesDailyBar]:
    """把 ``futures_main_sina`` DataFrame 归一化为主连日线(纯函数,可离线测试)。

    必需列:日期 / 开盘价 / 最高价 / 最低价 / 收盘价;可选列:成交量 /
    持仓量 / 动态结算价(新浪主连无成交额列)。缺必需列直接 raise
    (上游形状变化要 fail-visible,不静默出空序列)。

    **主连语义**:输入是新浪主力连续序列,价格是换月拼接产物 ——
    仅用于研究信号 / 基准,不可当作可成交合约(issue #267)。
    """
    date_col = _frame_column(frame, ("日期", "date"))
    open_col = _frame_column(frame, ("开盘价", "开盘", "open"))
    high_col = _frame_column(frame, ("最高价", "最高", "high"))
    low_col = _frame_column(frame, ("最低价", "最低", "low"))
    close_col = _frame_column(frame, ("收盘价", "收盘", "close"))
    if date_col is None or open_col is None or high_col is None or low_col is None or close_col is None:
        raise ValueError(
            "akshare futures_main_sina 返回形状不符合预期"
            "(缺少 日期/开盘价/最高价/最低价/收盘价 列);"
            f"实际列: {_frame_columns_repr(frame)}"
        )
    volume_col = _frame_column(frame, ("成交量", "volume"))
    oi_col = _frame_column(frame, ("持仓量", "open_interest"))
    settle_col = _frame_column(frame, ("动态结算价", "结算价", "settle"))

    bars: list[FuturesDailyBar] = []
    for raw_row in cast(Any, frame).to_dict(orient="records"):
        row: Mapping[str, object] = raw_row
        # 日期列不经 _cell(会把 Timestamp 字符串化成带时刻的文本,三个
        # strptime 格式都解析不了):直接取原始值交给 _parse_flex_date,
        # 它对 datetime/Timestamp 实例与常见字符串都健在。
        trade_date = _parse_flex_date(row.get(date_col))
        open_value = _decimal_cell(row, open_col)
        high_value = _decimal_cell(row, high_col)
        low_value = _decimal_cell(row, low_col)
        close_value = _decimal_cell(row, close_col)
        if (
            trade_date is None
            or open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
        ):
            # 单行缺日期 / OHLC 无法解析时跳过并计数;整表无效由调用方
            # 判空 fail-visible(与转换后的 Bar 非空校验一致)。
            continue
        bars.append(
            FuturesDailyBar(
                trade_date=trade_date,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=_decimal_cell(row, volume_col) if volume_col is not None else None,
                open_interest=_decimal_cell(row, oi_col) if oi_col is not None else None,
                settle=_decimal_cell(row, settle_col) if settle_col is not None else None,
            )
        )
    return bars


def parse_futures_official_daily_frame(frame: object) -> list[FuturesOfficialDailyRow]:
    """把交易所官网日线 ``get_futures_daily`` DataFrame 归一化(纯函数)。

    上游列名为英文(symbol/date/open/high/low/close/volume/open_interest/
    turnover/settle/pre_settle/variety,各交易所分支已由 akshare 统一)。
    必需列:symbol/date/open/high/low/close;variety 缺失时按合约代码的
    字母前缀推导(与 akshare CFFEX 分支同规则)。
    """
    symbol_col = _frame_column(frame, ("symbol", "合约代码"))
    date_col = _frame_column(frame, ("date", "日期"))
    open_col = _frame_column(frame, ("open",))
    high_col = _frame_column(frame, ("high",))
    low_col = _frame_column(frame, ("low",))
    close_col = _frame_column(frame, ("close",))
    if (
        symbol_col is None
        or date_col is None
        or open_col is None
        or high_col is None
        or low_col is None
        or close_col is None
    ):
        raise ValueError(
            "akshare get_futures_daily 返回形状不符合预期"
            "(缺少 symbol/date/open/high/low/close 列);"
            f"实际列: {_frame_columns_repr(frame)}"
        )
    volume_col = _frame_column(frame, ("volume",))
    oi_col = _frame_column(frame, ("open_interest",))
    turnover_col = _frame_column(frame, ("turnover",))
    settle_col = _frame_column(frame, ("settle",))
    pre_settle_col = _frame_column(frame, ("pre_settle",))
    variety_col = _frame_column(frame, ("variety",))

    rows: list[FuturesOfficialDailyRow] = []
    for raw_row in cast(Any, frame).to_dict(orient="records"):
        row: Mapping[str, object] = raw_row
        raw_symbol = _cell(row, symbol_col)
        open_value = _decimal_cell(row, open_col)
        high_value = _decimal_cell(row, high_col)
        low_value = _decimal_cell(row, low_col)
        close_value = _decimal_cell(row, close_col)
        # 同 parse_futures_main_sina_frame:日期列取原始值,不经 _cell。
        trade_date = _parse_flex_date(row.get(date_col))
        if (
            raw_symbol is None
            or trade_date is None
            or open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
        ):
            continue
        product: str | None = _cell(row, variety_col) if variety_col is not None else None
        if product is None:
            letters = re.findall(r"[A-Za-z]+", raw_symbol)
            product = letters[0] if letters else raw_symbol
        rows.append(
            FuturesOfficialDailyRow(
                symbol=str(raw_symbol).upper(),
                product=str(product).upper(),
                trade_date=trade_date,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=_decimal_cell(row, volume_col) if volume_col is not None else None,
                open_interest=_decimal_cell(row, oi_col) if oi_col is not None else None,
                turnover=_decimal_cell(row, turnover_col) if turnover_col is not None else None,
                settle=_decimal_cell(row, settle_col) if settle_col is not None else None,
                pre_settle=(
                    _decimal_cell(row, pre_settle_col) if pre_settle_col is not None else None
                ),
            )
        )
    return rows


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
    # pandas 日期列常直接给出 datetime/date 实例(新浪主连“日期”列即
    # Timestamp),先按实例处理,再退回字符串格式容错。
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
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

    # ------------------------------------------------------------------ 期货 (#267)

    async def fetch_futures_official_daily(
        self,
        *,
        start_date: date,
        end_date: date,
        market: str = "CFFEX",
    ) -> list[FuturesOfficialDailyRow]:
        """拉取交易所官网日线(``get_futures_daily``,免费)。

        上游是「**按日 x 全市场具体合约**」表(SHFE/DCE/CZCE/INE/CFFEX/GFEX
        五个官网封装),每行一份具体月份合约 EOD。**取舍**:v1 不把它接进
        逐标的 parquet 缓存 —— 缓存模型是「逐标的 x 区间增量」,与「按日
        全市场」形态不同构(每合约一次全交易所重拉,浪费且易错);且主连
        与具体合约必须在缓存层就分开(数据不混淆)。本方法是原始数据入口
        (限流 / 退避走 provider 既有约定),供后续合约链 / 换月拼接 issue
        消费;研究信号 / 基准用的主连日线走 ``fetch_bars`` 缓存路径。
        """
        return await self._call_with_retry(
            f"get_futures_daily:{market}",
            lambda ak: ak.get_futures_daily(
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                market=market,
            ),
            parse_futures_official_daily_frame,
        )

    def _fetch_futures_main_sync(
        self,
        ak: Any,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
    ) -> list[Bar]:
        """新浪主连日线(``futures_main_sina``)→ 领域 Bar(issue #267)。

        * 仅主连代码(``IF0.CFFEX``)入缓存;具体合约代码 fail-visible 拒绝,
          主连 / 具体合约语义在缓存层就不混淆(具体合约 EOD 走
          :meth:`fetch_futures_official_daily`)。
        * 新浪主连无成交额列 → ``amount=0``(质量门只拒负值);成交量单位
          为手(1 手 = 1 张合约),领域 Bar 按张 1:1 落盘。
        * 期货无复权概念:缓存键沿用请求 adjust(默认 qfq,#256 指数同策略
          —— 键存在但语义为 no-op,发布 adjustment 与下载键一致)。
        * **主连仅用于研究信号 / 基准,不可当作可成交合约** —— 主连价格是
          换月拼接产物,无单一真实合约与之对应;通用回测引擎不做期货撮合。
        """
        if period is not BarPeriod.D1:
            raise ValueError(f"akshare 期货行情仅支持日线,收到 {period}")
        if not is_futures_main_code(symbol.code):
            raise ValueError(
                f"akshare 期货日线缓存只支持主连代码(品种+0,如 IF0.CFFEX): {symbol.code};"
                "具体合约 EOD 走 fetch_futures_official_daily(交易所官网按日全市场表,"
                "v1 不进逐标的缓存,主连/具体合约语义不混淆,issue #267)"
            )
        frame = ak.futures_main_sina(
            symbol=symbol.code.split(".")[0],
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        bars = [
            Bar(
                symbol=symbol,
                period=period,
                timestamp=datetime.combine(item.trade_date, datetime.min.time(), tzinfo=UTC),
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume if item.volume is not None else Decimal("0"),
                amount=Decimal("0"),
                source="akshare",
            )
            for item in parse_futures_main_sina_frame(frame)
        ]
        bars.sort(key=lambda b: b.timestamp)
        logger.info(
            "akshare.fetched_futures_main",
            symbol=symbol.code,
            period=period.value,
            count=len(bars),
            continuous=True,
        )
        return bars

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

        if is_futures_code(symbol.code):
            # 期货日线(issue #267):主连走新浪 futures_main_sina(缓存的
            # 唯一期货形态,仅研究信号 / 基准);具体合约 fail-visible
            # 指路官网日线原始入口,主连/具体合约在缓存层不混淆。
            return self._fetch_futures_main_sync(ak, symbol, period, start, end)

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
