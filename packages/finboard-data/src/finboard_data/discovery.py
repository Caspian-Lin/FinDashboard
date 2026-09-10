"""``UniverseDiscovery`` —— 从数据源自动发现全市场标的。

替代手工维护的 ``symbols.yaml``。使用 akshare 的列表 API 拉取全市场标的清单,
归一化后写入 PostgreSQL ``instruments`` 表。

代码归一化规则:

* 纯 6 位数字 A 股代码 → 根据 prefix 判断交易所:
  - ``6x`` / ``9x`` → ``.SH`` (上交所:主板 + 科创板/CDR)
  - ``0x`` / ``3x`` → ``.SZ`` (深交所:主板 + 创业板/CDR)
  - ``92`` / ``8x`` / ``4x`` → ``.BJ`` (北交所)
* 带 prefix 的 ETF 代码(``sz159998`` / ``sh510300``)→ 去掉 prefix + 大写后缀

指数登记(issue #256):akshare 全市场列表接口(stock / fund ETF)不覆盖指数,
``discover_indices`` 从受控登记表 :data:`BENCHMARK_INDEX_REGISTRY` 产出
``instrument_type=index`` 的标的——这是「指数登记 → 同步 → 发布 →
benchmark_return」链路的唯一登记写入者。

可转债登记(issue #265):akshare 股票 / ETF / 指数列表接口同样不覆盖转债,
``discover_convertibles`` 从东财可转债一览 ``bond_zh_cov`` 产出
``instrument_type=convertible`` 标的(代码规则 11xxxx.SH / 12xxxx.SZ,
见 :func:`finboard_data.akshare_provider.is_convertible_code`),并入
``discover_all()``;data_sync 经 ``sync_with_diff`` 自动登记。
转债无 list_date 的结构化上游(东财一览无上市日列),保持 null,由
dataset_sync ``convertible_profiles`` 数据集从 tushare cb_basic 回填。

期货主连登记(issue #267):akshare 全市场列表接口不覆盖期货,
``discover_futures_main`` 从受控登记表
:data:`finboard_data.akshare_provider.FUTURES_MAIN_SERIES_REGISTRY`
(IF/IH/IC/IM 主连,CFFEX)产出 ``market=future`` /
``instrument_type=futures`` 标的 —— 期货进入 instruments 表的登记
写入者。登记的是**主连序列**(continuous 语义),不是可成交合约;
主连仅用于研究信号 / 基准数据。合约 → 品种映射显式维护在登记表
(乘数 / 保证金率与 finboard-backtest ``FuturesRule`` 同口径,单测锁定)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from finboard_data.akshare_provider import (
    FUTURES_MAIN_SERIES_REGISTRY,
    is_convertible_code,
    is_index_code,
    normalize_overview_code,
)
from finboard_shared.types import InstrumentType, ListingBoard, Market

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


#: 常用基准指数受控登记表 ``(code, name)``(issue #256)。
#:
#: 指数无 akshare 全市场列表接口(股票 / ETF 各有列表 API,指数没有同口径
#: 的稳定列表),这里维护一张显式登记表,覆盖 A 股主要宽基 / 基准指数;
#: 全部条目必须满足 ``is_index_code`` 代码规则(模块导入期即断言,防止
#: 登记表漂移把股票代码混进来)。扩展新指数直接加一行;data_sync 经
#: ``sync_with_diff`` 自动写入 instruments 表。指数无 list_date / 行业的
#: 结构化上游,保持 null(缺失在 data_sync 统计中可见,不虚构元数据)。
BENCHMARK_INDEX_REGISTRY: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("000688.SH", "科创50"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("899050.BJ", "北证50"),
)

_INVALID_INDEX_CODES = tuple(
    code for code, _ in BENCHMARK_INDEX_REGISTRY if not is_index_code(code)
)
if _INVALID_INDEX_CODES:
    raise RuntimeError(
        "BENCHMARK_INDEX_REGISTRY 存在不满足 is_index_code 规则的代码: "
        + ", ".join(_INVALID_INDEX_CODES)
    )


def _index_exchange(code: str) -> str:
    """指数代码后缀 → 交易所主数据标识(与股票 / ETF 同一口径)。"""
    suffix = code.rpartition(".")[2]
    exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix)
    if exchange is None:
        raise ValueError(f"无法识别指数交易所后缀: {code}")
    return exchange


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    """从数据源发现的标的元数据(归一化后)。"""

    code: str               # 归一化代码: 000001.SZ / 510300.SH / AAPL
    name: str               # 中文名称
    market: Market          # a_share / hk / us
    instrument_type: InstrumentType  # stock / etf
    exchange: str | None = None      # SSE / SZSE / BSE
    listing_board: ListingBoard = ListingBoard.UNKNOWN


def infer_a_share_listing_board(code: str) -> ListingBoard:
    """由已规范化代码推导上市板块;权威主数据可覆盖该回退值。"""
    normalized = code.strip().upper()
    bare, _, suffix = normalized.partition(".")
    if suffix == "BJ" or bare.startswith(("92", "8", "4")):
        return ListingBoard.BSE
    if suffix == "SH" and bare.startswith("689"):
        return ListingBoard.CDR
    if suffix == "SZ" and (
        "001001" <= bare <= "001199" or "309800" <= bare <= "309999"
    ):
        return ListingBoard.CDR
    if suffix == "SH" and bare.startswith("688"):
        return ListingBoard.STAR
    if suffix == "SZ" and bare.startswith("30"):
        return ListingBoard.CHINEXT
    if suffix == "SH" and bare.startswith(("600", "601", "603", "605")):
        return ListingBoard.SSE_MAIN
    if suffix == "SZ" and bare.startswith(("000", "001", "002", "003")):
        return ListingBoard.SZSE_MAIN
    return ListingBoard.UNKNOWN


def normalize_a_share_code(raw: str) -> str | None:
    """把各种 A 股代码格式归一化为 ``XXXXXX.SS/SH/SZ/BJ``。

    >>> normalize_a_share_code("000001")
    '000001.SZ'
    >>> normalize_a_share_code("600000")
    '600000.SH'
    >>> normalize_a_share_code("sz159998")
    '159998.SZ'
    >>> normalize_a_share_code("sh510300")
    '510300.SH'
    """
    raw = raw.strip().lower()

    # 已经带后缀
    if raw.endswith((".sh", ".ss", ".sz", ".bj")):
        return raw.upper()

    # 带 sina prefix (sz000001 / sh600000)
    if raw.startswith(("sh", "sz", "bj")):
        prefix_len = 2
        code = raw[prefix_len:]
        suffix = raw[:prefix_len]
        if suffix == "sh":
            return f"{code}.SH"
        if suffix == "sz":
            return f"{code}.SZ"
        if suffix == "bj":
            return f"{code}.BJ"

    # 纯 6 位数字
    if len(raw) == 6 and raw.isdigit():
        if raw.startswith("92") or raw[0] in ("8", "4"):
            return f"{raw}.BJ"
        if raw[0] in ("6", "9"):
            return f"{raw}.SH"
        if raw[0] in ("0", "3"):
            return f"{raw}.SZ"

    return None


class UniverseDiscovery:
    """从 akshare 自动发现全市场标的。

    所有方法都是 async(akshare 同步调用走 ``asyncio.to_thread``)。
    """

    async def discover_a_shares(self) -> list[InstrumentInfo]:
        """A股全部股票(ak.stock_info_a_code_name)。

        ~5500 只,含沪深京。返回归一化的 InstrumentInfo 列表。
        """
        raw_list = await asyncio.to_thread(self._fetch_a_shares_sync)
        result: list[InstrumentInfo] = []
        for raw_code, name in raw_list:
            code = normalize_a_share_code(raw_code)
            if code is None:
                continue
            exchange = "SSE" if code.endswith(".SH") else ("SZSE" if code.endswith(".SZ") else "BSE")
            result.append(
                InstrumentInfo(
                    code=code,
                    name=name.strip(),
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                    exchange=exchange,
                    listing_board=infer_a_share_listing_board(code),
                )
            )
        logger.info("discovery.a_shares", count=len(result))
        return result

    async def discover_a_etfs(self) -> list[InstrumentInfo]:
        """A股全部 ETF(ak.fund_etf_category_sina)。

        ~1600 只。只取代码+名称,丢弃实时行情字段。
        """
        raw_list = await asyncio.to_thread(self._fetch_etfs_sync)
        result: list[InstrumentInfo] = []
        for raw_code, name in raw_list:
            code = normalize_a_share_code(raw_code)
            if code is None:
                continue
            exchange = "SSE" if code.endswith(".SH") else "SZSE"
            result.append(
                InstrumentInfo(
                    code=code,
                    name=name.strip(),
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.ETF,
                    exchange=exchange,
                    listing_board=ListingBoard.UNKNOWN,
                )
            )
        logger.info("discovery.etfs", count=len(result))
        return result

    def _fetch_a_shares_sync(self) -> list[tuple[str, str]]:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        return [(str(row["code"]), str(row["name"])) for _, row in df.iterrows()]

    def _fetch_etfs_sync(self) -> list[tuple[str, str]]:
        import akshare as ak

        df = ak.fund_etf_category_sina("ETF基金")
        return [(str(row["代码"]), str(row["名称"])) for _, row in df.iterrows()]

    def _fetch_convertibles_sync(self) -> list[tuple[str, str]]:
        import akshare as ak

        # 东财可转债一览:免费、全量;列名容错解析见 akshare_provider
        # 的 parse_convertible_overview_frame(此处只取代码+名称)。
        from finboard_data.akshare_provider import _frame_column

        df = ak.bond_zh_cov()
        code_col = _frame_column(df, ("债券代码", "bond_id"))
        name_col = _frame_column(df, ("债券简称", "bond_nm"))
        if code_col is None or name_col is None:
            raise RuntimeError(
                "akshare bond_zh_cov 返回形状不符合预期(缺少 债券代码/债券简称 列)"
            )
        return [(str(row[code_col]), str(row[name_col])) for _, row in df.iterrows()]

    async def discover_indices(self) -> list[InstrumentInfo]:
        """基准指数(受控登记表,issue #256,无网络调用)。

        ``instrument_type=index``;交易所按代码后缀推导,listing_board 恒为
        UNKNOWN(指数无上市板块)。同步链路对其做 (a_share, index) 作用域的
        生命周期 diff,与其他资产类型一致。
        """
        result = [
            InstrumentInfo(
                code=code,
                name=name,
                market=Market.A_SHARE,
                instrument_type=InstrumentType.INDEX,
                exchange=_index_exchange(code),
                listing_board=ListingBoard.UNKNOWN,
            )
            for code, name in BENCHMARK_INDEX_REGISTRY
        ]
        logger.info("discovery.indices", count=len(result))
        return result

    async def discover_convertibles(self) -> list[InstrumentInfo]:
        """可转债(东财一览 bond_zh_cov,issue #265)。

        ``instrument_type=convertible``;交易所按代码规则推导
        (11 开头 → SSE,12 开头 → SZSE),listing_board 恒为 UNKNOWN。
        与指数登记(#256)同型:这是转债进入 instruments 表的登记写入者。
        东财一览只覆盖当前存续转债,退市转债不在列表 —— 存续偏差是已知
        限制(data-ops 文档与 cb_basic 摘牌档案共同缓解)。
        """
        raw_list = await asyncio.to_thread(self._fetch_convertibles_sync)
        result: list[InstrumentInfo] = []
        skipped = 0
        for raw_code, name in raw_list:
            # 必须走 normalize_overview_code:normalize_a_share_code 的 6 位
            # 数字规则不认转债 1 开头段,会把每只转债都归一失败(全量 skip)。
            code = normalize_overview_code(raw_code)
            if code is None:
                skipped += 1
                continue
            if not is_convertible_code(code):
                skipped += 1
                continue
            result.append(
                InstrumentInfo(
                    code=code,
                    name=name.strip(),
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.CONVERTIBLE,
                    exchange="SSE" if code.endswith(".SH") else "SZSE",
                    listing_board=ListingBoard.UNKNOWN,
                )
            )
        if not result and skipped:
            raise RuntimeError(
                f"bond_zh_cov 返回 {skipped} 行但无可识别的可转债代码(11xxxx.SH/12xxxx.SZ)"
            )
        logger.info("discovery.convertibles", count=len(result), skipped=skipped)
        return result

    async def discover_futures_main(self) -> list[InstrumentInfo]:
        """期货主连序列(受控登记表,issue #267,无网络调用)。

        ``market=future`` / ``instrument_type=futures``;交易所取登记表
        (CFFEX),listing_board 恒为 UNKNOWN。与指数登记(#256)同型:
        这是期货进入 instruments 表的登记写入者。**登记语义是主连**
        (continuous):主连价格是换月拼接产物,仅用于研究信号 / 基准,
        不可当作可成交合约 —— 具体月份合约不经本入口登记(v1 无结构化
        上游,合约链另行立项)。主连无 list_date 上游,保持 null 可见缺失。
        """
        result = [
            InstrumentInfo(
                code=entry.code,
                name=entry.name,
                market=Market.FUTURE,
                instrument_type=InstrumentType.FUTURES,
                exchange=entry.exchange,
                listing_board=ListingBoard.UNKNOWN,
            )
            for entry in FUTURES_MAIN_SERIES_REGISTRY
        ]
        logger.info("discovery.futures_main", count=len(result))
        return result

    async def discover_all(self) -> list[InstrumentInfo]:
        """发现全部可用标的(A 股股票 + ETF + 基准指数 + 可转债 + 期货主连)。"""
        stocks, etfs, indices, convertibles, futures = await asyncio.gather(
            self.discover_a_shares(),
            self.discover_a_etfs(),
            self.discover_indices(),
            self.discover_convertibles(),
            self.discover_futures_main(),
        )
        all_instruments = stocks + etfs + indices + convertibles + futures
        logger.info(
            "discovery.all",
            total=len(all_instruments),
            indices=len(indices),
            convertibles=len(convertibles),
            futures=len(futures),
        )
        return all_instruments
