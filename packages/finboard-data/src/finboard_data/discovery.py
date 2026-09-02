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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from finboard_data.akshare_provider import is_index_code
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

    async def discover_all(self) -> list[InstrumentInfo]:
        """发现全部可用标的(A 股股票 + ETF + 基准指数,issue #256)。"""
        stocks, etfs, indices = await asyncio.gather(
            self.discover_a_shares(),
            self.discover_a_etfs(),
            self.discover_indices(),
        )
        all_instruments = stocks + etfs + indices
        logger.info(
            "discovery.all",
            total=len(all_instruments),
            indices=len(indices),
        )
        return all_instruments
