"""``UniverseDiscovery`` —— 从数据源自动发现全市场标的。

替代手工维护的 ``symbols.yaml``。使用 akshare 的列表 API 拉取全市场标的清单,
归一化后写入 PostgreSQL ``instruments`` 表。

代码归一化规则:

* 纯 6 位数字 A 股代码 → 根据 prefix 判断交易所:
  - ``6x`` / ``9x`` → ``.SH`` (上交所:主板 + 科创板/CDR)
  - ``0x`` / ``3x`` → ``.SZ`` (深交所:主板 + 创业板/CDR)
  - ``92`` / ``8x`` / ``4x`` → ``.BJ`` (北交所)
* 带 prefix 的 ETF 代码(``sz159998`` / ``sh510300``)→ 去掉 prefix + 大写后缀

指数登记(issue #256,#394 起 tushare index_basic 主源):akshare 全市场列表
接口(stock / fund ETF)不覆盖指数,``discover_indices`` 产出
``instrument_type=index`` 的标的——这是「指数登记 → 同步 → 发布 →
benchmark_return」链路的唯一登记写入者。#394 起登记源从受控登记表
(9 只)扩大为 tushare ``index_basic`` 全量(A 股三所指数按
:func:`finboard_data.akshare_provider.is_index_code` 收窄;编外市场
CSI / CIC / MSCI 无行情上游,不登记);:data:`BENCHMARK_INDEX_REGISTRY`
保留并收窄为「基准资格白名单」(:func:`is_benchmark_index` 只认白名单,
哪些指数可作 benchmark 的受控语义不变,#256 边界:指数一律不可撮合,
只做基准 / 研究数据)。index_basic 的 ``base_date`` 随登记携带
(``InstrumentInfo.list_date``),由 data_sync 后置回填
``instruments.list_date``(#185 只补 null 语义),消除 mixed 发布的
list_date 恒缺失噪音。

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
from datetime import date
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
    from finboard_data.tushare_provider import TushareResearchDataProvider

logger = structlog.get_logger(__name__)


#: 基准指数资格白名单 ``(code, name)``(issue #256 受控登记;#394 收窄语义)。
#:
#: #394 起指数**登记**源是 tushare ``index_basic`` 全量(discover_indices),
#: 本表不再承担登记职责,收窄为「哪些指数可作 benchmark」的受控白名单:
#: :func:`is_benchmark_index` 只认表内代码,白名单外的指数照常登记 /
#: 可缓存 / 可发布,但只做数据资产。全部条目必须满足 ``is_index_code``
#: 代码规则(模块导入期即断言,防止白名单漂移把股票代码混进来)。
#: 扩展新基准指数直接加一行。
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

#: 白名单代码集(模块级冻结,``is_benchmark_index`` 查表用)。
_BENCHMARK_INDEX_CODES = frozenset(code for code, _ in BENCHMARK_INDEX_REGISTRY)


def is_benchmark_index(code: str) -> bool:
    """基准资格白名单判定(issue #394,#256 受控语义保留)。

    只认 :data:`BENCHMARK_INDEX_REGISTRY` 表内代码(大小写不敏感);
    白名单外的指数(含 index_basic 登记扩大的全部 A 股三所指数)返回
    False —— 它们可登记 / 可缓存 / 可发布,但不是基准资格资产。
    """
    return code.strip().upper() in _BENCHMARK_INDEX_CODES


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
    # issue #394:指数登记携带 index_basic base_date(基日),data_sync
    # 后置回填 instruments.list_date(只补 null)。其余发现路径无结构化
    # 上游,保持 None(#185 口径:缺失可见,不虚构元数据)。
    list_date: date | None = None


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

    async def discover_indices(
        self,
        provider: TushareResearchDataProvider | None = None,
    ) -> list[InstrumentInfo]:
        """A 股指数全量登记(tushare ``index_basic``,issue #394)。

        #256 的受控登记表(9 只)从 #394 起收窄为基准资格白名单
        (:func:`is_benchmark_index`),登记写入者扩大为 ``index_basic``
        全量:provider 保留全部市场快照,本方法按 ``is_index_code``
        (000xxx.SH / 399xxx.SZ / 899xxx.BJ)收窄登记域 —— 与行情路由 /
        冻结发布 / benchmark 消费口径同域;编外市场(CSI / CIC / MSCI
        等)无行情上游,登记即噪音,不进 instruments 表。

        只登记在市指数(``list_status`` 缺省或 L);上游按市场分片动态裁列,
        ``list_status`` 实测经常整批缺失(2026-09-10 复核)—— 缺失按在市
        处理,退市交 sync_with_diff 的生命周期 diff(缺席二次确认)语义。
        ``base_date``(基日)映射 ``InstrumentInfo.list_date``,由 data_sync
        后置回填 ``instruments.list_date``(只补 null,#185)。

        :param provider: 可注入的 tushare 研究数据 provider(测试离线注入);
            缺省从环境构造(token 缺失具名拒绝 —— 登记源已切换,不静默
            回退白名单)。
        """
        if provider is None:
            from finboard_data.tushare_provider import TushareResearchDataProvider

            provider = TushareResearchDataProvider()
        profiles = await provider.fetch_index_profiles()
        result: list[InstrumentInfo] = []
        skipped_out_of_scope = 0
        skipped_not_listed = 0
        for item in profiles:
            if not is_index_code(item.symbol):
                skipped_out_of_scope += 1
                continue
            if item.list_status is not None and item.list_status.upper() not in {
                "",
                "L",
            }:
                skipped_not_listed += 1
                continue
            result.append(
                InstrumentInfo(
                    code=item.symbol,
                    name=item.name,
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.INDEX,
                    exchange=_index_exchange(item.symbol),
                    listing_board=ListingBoard.UNKNOWN,
                    # 基日是指数「自何时存在」的结构化上游;上游 list_date
                    # 大量为 null,回填口径 base_date 优先(#394)。
                    list_date=item.base_date or item.list_date,
                )
            )
        if not result:
            raise RuntimeError(
                f"tushare index_basic 返回 {len(profiles)} 行但无可登记的 "
                "A 股指数(000xxx.SH / 399xxx.SZ / 899xxx.BJ),疑似上游 schema 变更"
            )
        logger.info(
            "discovery.indices",
            count=len(result),
            source_rows=len(profiles),
            skipped_out_of_scope=skipped_out_of_scope,
            skipped_not_listed=skipped_not_listed,
            whitelist=sorted(
                item.code
                for item in result
                if is_benchmark_index(item.code)
            ),
        )
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
