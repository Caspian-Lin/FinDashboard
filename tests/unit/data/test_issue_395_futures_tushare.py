"""期货链路 tushare 化测试(issue #395)。

四层:

* 代码翻译 —— ``to_tushare_futures_code`` / ``from_tushare_futures_code``
  (主连 ``IF0.CFFEX`` → tushare 主力连续 ``IF.CFX`` 连续直取;合约后缀
  映射,未知后缀 fail-visible);
* ``fetch_futures_contract_profiles`` —— ``fut_basic`` 合约快照解析
  (multiplier / quote_unit_desc 最小变动价位、``.CFX`` → ``.CFFEX`` 归一、
  连续合约形制脏行跳过、截断护栏);
* ``fetch_futures_trade_cal`` —— 交易日历解析(is_open 0/1、窗口/交易所
  一致性);
* ``discover_futures_contracts`` —— 登记域收窄(CFFEX 股指四品种)+ 在市
  窗口过滤 + 退市日携带;
* ``reconcile_futures_contract_profiles`` —— fut_basic vs 受控登记表
  乘数 / 最小变动价位对账;
* ``TushareBarProvider`` 期货分流 —— 主连/合约 ``fut_daily`` 路由、
  amount 万元 → 元、vol 手 → 张 1:1、无复权 no-op。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_data import (
    ResearchDataContractError,
    TushareResearchDataProvider,
)
from finboard_data.akshare_provider import (
    FUTURES_MAIN_SERIES_REGISTRY,
    from_tushare_futures_code,
    reconcile_futures_contract_profiles,
    to_tushare_futures_code,
)
from finboard_data.cache import make_symbol
from finboard_data.discovery import (
    FUTURES_CONTRACT_PRODUCTS,
    UniverseDiscovery,
)
from finboard_data.research import FuturesContractProfile
from finboard_data.tushare_bar_provider import TushareBarProvider
from finboard_shared.types import BarPeriod

OBSERVED_AT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
_TODAY = date(2026, 9, 9)


class NoopBudget:
    async def acquire(self) -> None:
        return None


# ---- 代码翻译 -----------------------------------------------------------------


@pytest.mark.unit
def test_to_tushare_futures_code_main_continuous() -> None:
    """主连 IF0.CFFEX → IF.CFX:新浪形制 ``0`` 剥去,后缀映射(连续直取)。"""
    assert to_tushare_futures_code("IF0.CFFEX") == "IF.CFX"
    assert to_tushare_futures_code("im0.cffex") == "IM.CFX"


@pytest.mark.unit
def test_to_tushare_futures_code_contract_and_unknown() -> None:
    """具体合约后缀映射;未知后缀 fail-visible;非主连不剥尾字符。"""
    assert to_tushare_futures_code("IF2601.CFFEX") == "IF2601.CFX"
    assert to_tushare_futures_code("cu2508.SHFE") == "CU2508.SHF"
    with pytest.raises(ValueError, match="未知期货交易所后缀"):
        to_tushare_futures_code("IF2601.NYSE")


@pytest.mark.unit
def test_from_tushare_futures_code_reverse() -> None:
    """后缀映射回内部形制;连续合约形制不回填主连 ``0``(调用方显式处理)。"""
    assert from_tushare_futures_code("IF2601.CFX") == "IF2601.CFFEX"
    assert from_tushare_futures_code("IF.CFX") == "IF.CFFEX"
    with pytest.raises(ValueError, match="未知 tushare 期货交易所后缀"):
        from_tushare_futures_code("IF2601.XYZ")


@pytest.mark.unit
def test_suffix_map_covers_internal_exchanges() -> None:
    """六所后缀映射与主连登记表交易所同词表(2026-09-09 实测落表,#395)。"""
    from finboard_data.akshare_provider import TUSHARE_FUTURES_SUFFIX

    assert TUSHARE_FUTURES_SUFFIX["CFFEX"] == "CFX"
    assert TUSHARE_FUTURES_SUFFIX["SHFE"] == "SHF"
    assert TUSHARE_FUTURES_SUFFIX["DCE"] == "DCE"
    assert TUSHARE_FUTURES_SUFFIX["CZCE"] == "ZCE"
    assert TUSHARE_FUTURES_SUFFIX["INE"] == "INE"
    assert TUSHARE_FUTURES_SUFFIX["GFEX"] == "GFE"


# ---- fetch_futures_contract_profiles -------------------------------------------


def _fut_basic_row(**overrides: object) -> dict[str, object]:
    """tushare fut_basic 单行(默认 IF2601,实测列形态 2026-09-09)。"""
    row: dict[str, object] = {
        "ts_code": "IF2601.CFX",
        "name": "IF2601",
        "fut_code": "IF",
        "multiplier": 300.0,
        "quote_unit_desc": "0.2指数点",
        "list_date": "20251124",
        "delist_date": "20260116",
    }
    row.update(overrides)
    return row


class FakeFuturesClient:
    """按 endpoint 返回预置行的离线 client(记录调用参数)。"""

    def __init__(
        self,
        *,
        fut_basic_pages: list[dict[str, object]] | None = None,
        trade_cal_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._fut_basic_rows = fut_basic_pages or []
        self._trade_cal_rows = trade_cal_rows or []

    def fut_basic(self, **kwargs: str) -> object:
        self.calls.append(("fut_basic", kwargs))
        return self._fut_basic_rows

    def fut_trade_cal(self, **kwargs: str) -> object:
        self.calls.append(("fut_trade_cal", kwargs))
        return self._trade_cal_rows

    def stock_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def index_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def daily_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def fina_indicator(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def index_member_all(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def namechange(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def cb_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")


def _futures_provider(client: FakeFuturesClient) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client,
        now=lambda: OBSERVED_AT,
        budget=NoopBudget(),
    )


@pytest.mark.unit
async def test_fetch_futures_contract_profiles_parses_and_normalizes() -> None:
    """fut_basic 快照:后缀归一 .CFX→.CFFEX、乘数/最小变动价位/上市退市日。"""
    client = FakeFuturesClient(
        fut_basic_pages=[
            _fut_basic_row(),
            _fut_basic_row(
                ts_code="IH2601.CFX",
                name="IH2601",
                fut_code="IH",
                multiplier=300.0,
                quote_unit_desc="0.2指数点",
            ),
            _fut_basic_row(
                ts_code="T2603.CFX",
                name="T2603",
                fut_code="T",
                multiplier=10000.0,
                quote_unit_desc="0.005人民币元",
            ),
        ]
    )
    profiles = await _futures_provider(client).fetch_futures_contract_profiles()
    assert [item.symbol for item in profiles] == [
        "IF2601.CFFEX",
        "IH2601.CFFEX",
        "T2603.CFFEX",
    ]
    assert client.calls == [
        (
            "fut_basic",
            {
                "exchange": "CFFEX",
                "fields": (
                    "ts_code,name,fut_code,multiplier,quote_unit_desc,"
                    "list_date,delist_date"
                ),
            },
        )
    ]
    ifc = profiles[0]
    assert ifc.product == "IF"
    assert ifc.exchange == "CFFEX"
    assert ifc.multiplier == Decimal("300")
    assert ifc.price_tick == Decimal("0.2")
    assert ifc.quote_unit_desc == "0.2指数点"
    assert ifc.list_date == date(2025, 11, 24)
    assert ifc.delist_date == date(2026, 1, 16)
    assert ifc.observed_at == OBSERVED_AT
    assert ifc.available_at == OBSERVED_AT
    # 国债期货乘数 / 元计价 tick 同样解析
    assert profiles[2].multiplier == Decimal("10000")
    assert profiles[2].price_tick == Decimal("0.005")


@pytest.mark.unit
async def test_fetch_futures_contract_profiles_skips_continuous_rows() -> None:
    """主力/连续合约形制(IF.CFX / IFL1.CFX)是 vendor 序列,按脏行跳过。"""
    client = FakeFuturesClient(
        fut_basic_pages=[
            _fut_basic_row(ts_code="IF.CFX", multiplier=None, list_date=None, delist_date=None),
            _fut_basic_row(ts_code="IFL1.CFX", multiplier=None, list_date=None, delist_date=None),
            _fut_basic_row(),
        ]
    )
    profiles = await _futures_provider(client).fetch_futures_contract_profiles()
    assert [item.symbol for item in profiles] == ["IF2601.CFFEX"]


@pytest.mark.unit
async def test_fetch_futures_contract_profiles_all_dirty_rejects() -> None:
    """全部行被跳过 = 上游 schema 破坏,整批拒(fail-closed 护栏)。"""
    client = FakeFuturesClient(
        fut_basic_pages=[_fut_basic_row(ts_code="IF.CFX")]
    )
    with pytest.raises(ResearchDataContractError, match="均违反契约"):
        await _futures_provider(client).fetch_futures_contract_profiles()


@pytest.mark.unit
async def test_fetch_futures_contract_profiles_truncation_guard() -> None:
    """返回行数达到护栏上限 = 疑似截断,整批拒。"""
    client = FakeFuturesClient(
        fut_basic_pages=[_fut_basic_row() for _ in range(5000)]
    )
    with pytest.raises(ResearchDataContractError, match="截断"):
        await _futures_provider(client).fetch_futures_contract_profiles()


# ---- fetch_futures_trade_cal ----------------------------------------------------


def _trade_cal_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "exchange": "CFFEX",
        "cal_date": "20250101",
        "is_open": 0,
        "pretrade_date": "20241231",
    }
    row.update(overrides)
    return row


@pytest.mark.unit
async def test_fetch_futures_trade_cal_parses_open_and_closed() -> None:
    """is_open 0/1 双值解析(休市行同样落库),窗口外/异所行整批拒。"""
    client = FakeFuturesClient(
        trade_cal_rows=[
            _trade_cal_row(cal_date="20250102", is_open=1, pretrade_date="20241231"),
            _trade_cal_row(cal_date="20250101", is_open=0),
        ]
    )
    days = await _futures_provider(client).fetch_futures_trade_cal(
        exchange="CFFEX",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )
    assert [item.cal_date for item in days] == [
        date(2025, 1, 1),
        date(2025, 1, 2),
    ]
    assert days[0].is_open is False
    assert days[0].pretrade_date == date(2024, 12, 31)
    assert days[1].is_open is True


@pytest.mark.unit
async def test_fetch_futures_trade_cal_window_and_exchange_guards() -> None:
    """start > end 入参秒拒;返回窗口外日期 / 异所记录 = 契约违规。"""
    provider = _futures_provider(
        FakeFuturesClient(trade_cal_rows=[_trade_cal_row()])
    )
    with pytest.raises(Exception, match="start_date 不能晚于"):
        await provider.fetch_futures_trade_cal(
            exchange="CFFEX",
            start_date=date(2025, 12, 31),
            end_date=date(2025, 1, 1),
        )
    outside = _futures_provider(
        FakeFuturesClient(trade_cal_rows=[_trade_cal_row(cal_date="20240101")])
    )
    with pytest.raises(ResearchDataContractError, match="窗口之外"):
        await outside.fetch_futures_trade_cal(
            exchange="CFFEX",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
    other = _futures_provider(
        FakeFuturesClient(
            trade_cal_rows=[_trade_cal_row(exchange="SHFE", cal_date="20250102")]
        )
    )
    with pytest.raises(ResearchDataContractError, match="交易所之外"):
        await other.fetch_futures_trade_cal(
            exchange="CFFEX",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )


@pytest.mark.unit
async def test_fetch_futures_trade_cal_bad_is_open_rejects() -> None:
    """is_open 非 0/1 是契约违规(精确查询形态默认整批拒)。"""
    provider = _futures_provider(
        FakeFuturesClient(trade_cal_rows=[_trade_cal_row(is_open=2)])
    )
    with pytest.raises(ResearchDataContractError, match="is_open"):
        await provider.fetch_futures_trade_cal(
            exchange="CFFEX",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )


# ---- discover_futures_contracts -------------------------------------------------


class FakeDiscoveryProvider:
    """discover_futures_contracts 的离线注入(返回预置合约快照)。"""

    def __init__(self, profiles: list[FuturesContractProfile]) -> None:
        self._profiles = profiles

    async def fetch_futures_contract_profiles(self) -> list[FuturesContractProfile]:
        return self._profiles


def _profile(
    symbol: str,
    product: str,
    *,
    list_date: date | None = date(2026, 8, 24),
    delist_date: date | None = date(2026, 12, 18),
) -> FuturesContractProfile:
    from finboard_data.akshare_provider import futures_product_entry

    try:
        entry = futures_product_entry(product)
        multiplier = entry.multiplier
        price_tick = entry.price_tick
    except ValueError:
        # 未登记品种(T/TF/TS 等国债或编外品种):登记表无口径,用占位值
        # (登记域收窄测试只关心 product 过滤,不消费乘数)。
        multiplier = Decimal("100")
        price_tick = Decimal("0.01")
    return FuturesContractProfile(
        symbol=symbol,
        name=symbol.split(".")[0],
        product=product,
        exchange=symbol.rpartition(".")[2],
        multiplier=multiplier,
        price_tick=price_tick,
        quote_unit_desc=f"{price_tick}指数点",
        list_date=list_date,
        delist_date=delist_date,
        source="tushare",
        observed_at=OBSERVED_AT,
        available_at=OBSERVED_AT,
    )


@pytest.mark.unit
async def test_discover_futures_contracts_domain_and_listing_window() -> None:
    """登记域收窄(CFFEX 股指四品种)+ 在市窗口过滤 + 退市日携带。"""
    profiles = [
        _profile("IF2612.CFFEX", "IF", list_date=date(2026, 8, 24), delist_date=date(2026, 12, 18)),
        _profile("IM2612.CFFEX", "IM", list_date=date(2026, 7, 20), delist_date=date(2026, 12, 18)),
        # 国债品种:不在登记域(FUTURES_CONTRACT_PRODUCTS)
        _profile("T2603.CFFEX", "T"),
        # 预上市:list_date 在未来
        _profile(
            "IF2703.CFFEX",
            "IF",
            list_date=date(2026, 12, 21),
            delist_date=date(2027, 3, 19),
        ),
        # 已到期:delist_date 已过
        _profile(
            "IF2606.CFFEX",
            "IF",
            list_date=date(2025, 12, 18),
            delist_date=date(2026, 6, 19),
        ),
    ]
    result = await UniverseDiscovery().discover_futures_contracts(
        FakeDiscoveryProvider(profiles),  # type: ignore[arg-type]
        today=_TODAY,
    )
    assert [item.code for item in result] == [
        "IF2612.CFFEX",
        "IM2612.CFFEX",
    ]
    first = result[0]
    assert first.market.value == "future"
    assert first.instrument_type.value == "futures"
    assert first.exchange == "CFFEX"
    assert first.listing_board.value == "unknown"
    assert first.list_date == date(2026, 8, 24)
    # #395:合约携带退市日(最后交易日),backfill_listing_dates 通道回填。
    assert first.delist_date == date(2026, 12, 18)


@pytest.mark.unit
async def test_discover_futures_contracts_empty_rejects() -> None:
    """四品种全部被过滤 = 上游 schema 变更形态,拒绝而非静默空登记。"""
    profiles = [_profile("T2603.CFFEX", "T")]
    with pytest.raises(RuntimeError, match="无可登记"):
        await UniverseDiscovery().discover_futures_contracts(
            FakeDiscoveryProvider(profiles),  # type: ignore[arg-type]
            today=_TODAY,
        )


@pytest.mark.unit
def test_futures_contract_products_match_registry() -> None:
    """合约登记域与主连受控登记表同品种(并存语义,#395)。"""
    registry_products = {entry.product for entry in FUTURES_MAIN_SERIES_REGISTRY}
    assert registry_products == FUTURES_CONTRACT_PRODUCTS


# ---- reconcile_futures_contract_profiles ----------------------------------------


@pytest.mark.unit
def test_reconcile_futures_contract_profiles_clean() -> None:
    """实测口径(fut_basic IF 乘数 300 / tick 0.2)对受控表零不一致。"""
    profiles = [
        _profile("IF2601.CFFEX", "IF"),
        _profile("IH2601.CFFEX", "IH"),
        _profile("IC2601.CFFEX", "IC"),
        _profile("IM2601.CFFEX", "IM"),
    ]
    assert reconcile_futures_contract_profiles(profiles) == []


@pytest.mark.unit
def test_reconcile_futures_contract_profiles_mismatch_and_missing() -> None:
    """乘数/最小变动价位不一致具名;登记品种在快照中无行具名;未登记品种跳过。"""
    drifted = FuturesContractProfile(
        symbol="IF2601.CFFEX",
        name="IF2601",
        product="IF",
        exchange="CFFEX",
        multiplier=Decimal("999"),
        price_tick=Decimal("0.5"),
        quote_unit_desc="0.5指数点",
        list_date=None,
        delist_date=None,
        source="tushare",
        observed_at=OBSERVED_AT,
        available_at=OBSERVED_AT,
    )
    profiles = [drifted, _profile("XX9999.CFFEX", "XX")]
    mismatches = reconcile_futures_contract_profiles(profiles)
    assert any("IF2601.CFFEX multiplier=999" in item for item in mismatches)
    assert any("IF2601.CFFEX price_tick=0.5" in item for item in mismatches)
    # 除 IF 外的三个登记品种在快照中无行 → 具名缺失;XX 未登记不对账。
    assert sum(1 for item in mismatches if item.endswith("无该品种合约行")) == 3


@pytest.mark.unit
def test_reconcile_registry_margin_rate_is_cost_semantics() -> None:
    """受控表保证金率是成本口径(非交易所下限),上游无列不参与对账(#267)。"""
    for entry in FUTURES_MAIN_SERIES_REGISTRY:
        assert Decimal("0") < entry.margin_rate <= Decimal("1")


# ---- TushareBarProvider 期货分流 --------------------------------------------------


class FakeFutBarClient:
    """只打 fut_daily 的离线 client(记录 ts_code 翻译结果)。"""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._rows = rows or [
            {
                "ts_code": "IF.CFX",
                "trade_date": "20250610",
                "open": 3870.0,
                "high": 3878.8,
                "low": 3830.0,
                "close": 3841.0,
                # 实测口径:vol 手、amount 万元(#395 对账锁定)。
                "vol": 64495.0,
                "amount": 7459193.514,
            },
            {
                "ts_code": "IF.CFX",
                "trade_date": "20250609",
                "open": 3862.0,
                "high": 3883.6,
                "low": 3855.2,
                "close": 3867.8,
                "vol": 57026.0,
                "amount": 6619361.316,
            },
        ]

    def daily(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def cb_daily(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def fut_daily(self, **kwargs: str) -> object:
        self.calls.append(("fut_daily", kwargs))
        return self._rows

    def adj_factor(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def suspend_d(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")


def _bar_provider(client: FakeFutBarClient) -> TushareBarProvider:
    return TushareBarProvider(
        client=client,
        budget=NoopBudget(),
        use_cache=False,
        max_retries=0,
    )


@pytest.mark.unit
async def test_tushare_main_continuous_routes_to_continuous_code() -> None:
    """主连 IF0.CFFEX → fut_daily ts_code=IF.CFX(连续直取,零拼接)。"""
    client = FakeFutBarClient()
    provider = _bar_provider(client)
    bars = await provider.fetch_bars(
        make_symbol("IF0.CFFEX"), BarPeriod.D1, date(2025, 6, 9), date(2025, 6, 10)
    )
    assert client.calls, "fut_daily 应被调用"
    assert all(
        kwargs["ts_code"] == "IF.CFX" for _, kwargs in client.calls
    )
    assert all(
        kwargs["fields"]
        == "ts_code,trade_date,open,high,low,close,vol,amount"
        for _, kwargs in client.calls
    )
    assert [bar.timestamp.date() for bar in bars] == [
        date(2025, 6, 9),
        date(2025, 6, 10),
    ]
    first = bars[0]
    assert first.source == "tushare"
    # vol 手 → 张 1:1;amount 万元 → 元(x10000;股票 daily 是千元 x1000)。
    assert first.volume == Decimal("57026")
    assert first.amount == Decimal("6619361.316") * Decimal("10000")
    assert first.close == Decimal("3867.8")


@pytest.mark.unit
async def test_tushare_contract_routes_with_mapped_suffix() -> None:
    """具体合约 IF2601.CFFEX → fut_daily ts_code=IF2601.CFX。"""
    client = FakeFutBarClient()
    provider = _bar_provider(client)
    await provider.fetch_bars(
        make_symbol("IF2601.CFFEX"), BarPeriod.D1, date(2025, 6, 9), date(2025, 6, 10)
    )
    assert all(
        kwargs["ts_code"] == "IF2601.CFX" for _, kwargs in client.calls
    )
