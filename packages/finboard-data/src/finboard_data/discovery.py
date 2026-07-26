"""``UniverseDiscovery`` —— 从数据源自动发现全市场标的。

替代手工维护的 ``symbols.yaml``。使用 akshare 的列表 API 拉取全市场标的清单,
归一化后写入 PostgreSQL ``instruments`` 表。

代码归一化规则:

* 纯 6 位数字 A 股代码 → 根据 prefix 判断交易所:
  - ``6x`` / ``68`` → ``.SH`` (上交所:主板 + 科创板)
  - ``0x`` / ``30`` → ``.SZ`` (深交所:主板 + 创业板)
  - ``8x`` / ``43`` / ``87`` → ``.BJ`` (北交所)
* 带 prefix 的 ETF 代码(``sz159998`` / ``sh510300``)→ 去掉 prefix + 大写后缀
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from finboard_shared.types import InstrumentType, Market

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    """从数据源发现的标的元数据(归一化后)。"""

    code: str               # 归一化代码: 000001.SZ / 510300.SH / AAPL
    name: str               # 中文名称
    market: Market          # a_share / hk / us
    instrument_type: InstrumentType  # stock / etf
    exchange: str | None = None      # SSE / SZSE / BSE


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
        if raw[0] in ("6", "9"):
            return f"{raw}.SH"
        if raw[0] in ("0", "3"):
            return f"{raw}.SZ"
        if raw[0] in ("8", "4"):
            return f"{raw}.BJ"

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

    async def discover_all(self) -> list[InstrumentInfo]:
        """发现全部可用标的(A 股 + ETF)。"""
        stocks, etfs = await asyncio.gather(
            self.discover_a_shares(),
            self.discover_a_etfs(),
        )
        all_instruments = stocks + etfs
        logger.info("discovery.all", total=len(all_instruments))
        return all_instruments
