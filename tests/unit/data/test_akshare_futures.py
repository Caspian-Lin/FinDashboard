"""akshare 期货日线接口与主连语义测试(issue #267)。

不联网:解析函数对内存 DataFrame 直接断言;fetch 方法 monkeypatch akshare
模块属性(转债链路测试同风格,见 tests/unit/data/test_akshare_convertible.py)。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from finboard_data import (
    FUTURES_MAIN_SERIES_REGISTRY,
    AkShareProvider,
    FuturesDailyBar,
    futures_series_entry,
    is_convertible_code,
    is_futures_code,
    is_futures_main_code,
    parse_futures_main_sina_frame,
    parse_futures_official_daily_frame,
)
from finboard_data.akshare_provider import is_etf_code, is_index_code
from finboard_data.cache import _FUTURE_SUFFIXES, make_symbol
from finboard_data.tushare_bar_provider import TushareBarProvider
from finboard_shared.types import BarPeriod, Market

MAIN_COLUMNS = ("日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "持仓量", "动态结算价")


def _main_frame() -> pd.DataFrame:
    # 形制对齐 akshare futures_main_sina:日期 + 中文价格列 + 持仓量,无成交额。
    return pd.DataFrame(
        {
            "日期": [datetime(2024, 1, 2), datetime(2024, 1, 3)],
            "开盘价": [3450.2, 3461.0],
            "最高价": [3488.0, 3490.5],
            "最低价": [3440.0, 3452.2],
            "收盘价": [3480.6, 3475.4],
            "成交量": [123456, 111111],
            "持仓量": [234567, 238000],
            "动态结算价": [3475.0, 3470.8],
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("IF0.CFFEX", True),
        ("IH0.CFFEX", True),
        ("CU0.SHFE", True),  # 主连不限交易所
        ("IF2406.CFFEX", False),  # 具体合约(年月数字)
        ("IF2410.CFFEX", False),  # 末位 0 的具体合约不误判(去尾非纯字母)
        ("CU2408.SHFE", False),  # 他所具体合约
        ("TA409.CZCE", False),  # 郑商所 3 位年月合约
        ("IF2406", False),  # 缺后缀不猜交易所
        ("600519.SH", False),  # 沪市股票
        ("000300.SH", False),  # 指数(#256)
        ("510300.SH", False),  # ETF(#257)
        ("113050.SH", False),  # 转债(#265)
    ],
)
def test_is_futures_main_code_segments(code: str, expected: bool) -> None:
    assert is_futures_main_code(code) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("IF0.CFFEX", True),
        ("IF2406.CFFEX", True),  # 具体合约也是期货代码(主连谓词再细分)
        ("CU2408.SHFE", True),
        ("TA409.CZCE", True),
        ("SC2409.INE", True),
        ("IF2406", False),
        ("600519.SH", False),
        ("000300.SH", False),
    ],
)
def test_is_futures_code_segments(code: str, expected: bool) -> None:
    assert is_futures_code(code) is expected


@pytest.mark.unit
def test_code_predicates_disjoint_with_existing_assets() -> None:
    """期货谓词与指数 / ETF / 转债代码空间互不重叠(分流顺序无关)。"""
    codes = ("IF0.CFFEX", "IF2406.CFFEX", "SC2409.INE")
    for code in codes:
        assert not is_index_code(code)
        assert not is_etf_code(code)
        assert not is_convertible_code(code)


class TestFuturesRegistry:
    """期货主连受控登记表(issue #267,#256 指数登记表同风格)。"""

    def test_all_entries_satisfy_main_code_rule(self) -> None:
        assert FUTURES_MAIN_SERIES_REGISTRY
        for entry in FUTURES_MAIN_SERIES_REGISTRY:
            assert is_futures_main_code(entry.code), entry.code
            assert entry.product == entry.code.split(".")[0][:-1]
            assert entry.continuous is True
            assert entry.multiplier > 0
            assert Decimal("0") < entry.margin_rate <= Decimal("1")
            assert entry.price_tick > 0

    def test_cffex_stock_index_futures_present(self) -> None:
        """IF/IC/IM 优先(路线 C 空头腿),IH 同属中金所股指一并登记。"""
        products = {entry.product: entry for entry in FUTURES_MAIN_SERIES_REGISTRY}
        assert {"IF", "IC", "IM", "IH"} <= set(products)
        for product in ("IF", "IC", "IM", "IH"):
            assert products[product].exchange == "CFFEX"

    def test_aligned_with_backtest_futures_rules(self) -> None:
        """乘数 / 保证金率与 finboard-backtest FuturesRule 同口径(跨包锁定)。

        finboard-data 不依赖 finboard-backtest(依赖方向相反),一致性靠
        测试锁定,防止两处口径漂移。
        """
        from finboard_backtest.asset_rules import DEFAULT_TABLE

        backtest_rules = dict(DEFAULT_TABLE.futures_rules)
        for entry in FUTURES_MAIN_SERIES_REGISTRY:
            rule = backtest_rules.get(entry.product)
            if rule is None:
                continue
            assert entry.multiplier == rule.multiplier, entry.code
            assert entry.margin_rate == rule.margin_rate, entry.code

    def test_lookup_unknown_product_raises(self) -> None:
        with pytest.raises(ValueError, match="FUTURES_MAIN_SERIES_REGISTRY"):
            futures_series_entry("CU0.SHFE")
        assert futures_series_entry("if0.cffex").product == "IF"


@pytest.mark.unit
def test_parse_main_sina_frame_normal() -> None:
    bars = parse_futures_main_sina_frame(_main_frame())

    assert bars == [
        FuturesDailyBar(
            trade_date=date(2024, 1, 2),
            open=Decimal("3450.2"),
            high=Decimal("3488.0"),
            low=Decimal("3440.0"),
            close=Decimal("3480.6"),
            volume=Decimal("123456"),
            open_interest=Decimal("234567"),
            settle=Decimal("3475.0"),
        ),
        FuturesDailyBar(
            trade_date=date(2024, 1, 3),
            open=Decimal("3461.0"),
            high=Decimal("3490.5"),
            low=Decimal("3452.2"),
            close=Decimal("3475.4"),
            volume=Decimal("111111"),
            open_interest=Decimal("238000"),
            settle=Decimal("3470.8"),
        ),
    ]


@pytest.mark.unit
def test_parse_main_sina_missing_required_column_raises() -> None:
    frame = _main_frame().drop(columns=["收盘价"])

    with pytest.raises(ValueError, match="收盘价"):
        parse_futures_main_sina_frame(frame)


@pytest.mark.unit
def test_parse_main_sina_optional_columns_absent_and_bad_rows_skipped() -> None:
    frame = pd.DataFrame(
        {
            "日期": ["2024-01-02", "--", "2024-01-04"],
            "开盘价": [3450.2, 1.0, 3461.0],
            "最高价": [3488.0, 1.0, None],
            "最低价": [3440.0, 1.0, 3452.2],
            "收盘价": [3480.6, 1.0, 3475.4],
        }
    )

    bars = parse_futures_main_sina_frame(frame)
    assert [item.trade_date for item in bars] == [date(2024, 1, 2)]
    assert bars[0].volume is None
    assert bars[0].settle is None


def _official_frame() -> pd.DataFrame:
    # 形制对齐 akshare get_futures_daily(CFFEX 分支已统一为英文列)。
    return pd.DataFrame(
        {
            "symbol": ["IF2406", "IF2407", "IF2406"],
            "date": ["20240515", "20240515", "20240516"],
            "open": [3450.2, 3452.0, 3461.0],
            "high": [3488.0, 3486.0, 3490.5],
            "low": [3440.0, 3441.0, 3452.2],
            "close": [3480.6, 3479.8, 3475.4],
            "volume": [101.0, 22.0, 88.0],
            "open_interest": [50000.0, 300.0, 49000.0],
            "turnover": [1.05e11, 2.3e9, 9.2e10],
            "settle": [3475.0, 3474.2, 3470.8],
            "pre_settle": [3460.0, 3461.5, 3475.0],
        }
    )


@pytest.mark.unit
def test_parse_official_daily_frame_normalizes_contracts() -> None:
    rows = parse_futures_official_daily_frame(_official_frame())

    assert [(row.symbol, row.product) for row in rows] == [
        ("IF2406", "IF"),
        ("IF2407", "IF"),
        ("IF2406", "IF"),
    ]
    assert rows[0].trade_date == date(2024, 5, 15)
    assert rows[0].close == Decimal("3480.6")
    assert rows[0].settle == Decimal("3475.0")
    assert rows[0].pre_settle == Decimal("3460.0")


@pytest.mark.unit
def test_parse_official_daily_missing_column_raises() -> None:
    with pytest.raises(ValueError, match="symbol"):
        parse_futures_official_daily_frame(_official_frame().drop(columns=["symbol"]))


@pytest.mark.unit
async def test_fetch_main_continuous_bars_via_cache_path(tmp_path: Path) -> None:
    """主连日线走 fetch_bars 缓存路径:amount=0(新浪无成交额)、来源 akshare。"""
    provider = AkShareProvider(cache_dir=tmp_path / "cache")
    symbol = make_symbol("IF0.CFFEX")
    assert symbol.market is Market.FUTURE

    with patch("akshare.futures_main_sina", return_value=_main_frame()) as mock_main:
        bars = await provider.fetch_bars(
            symbol,
            BarPeriod.D1,
            date(2024, 1, 1),
            date(2024, 1, 31),
            adjust="qfq",
        )

    mock_main.assert_called_once()
    assert mock_main.call_args.kwargs["symbol"] == "IF0"
    assert [bar.close for bar in bars] == [Decimal("3480.6"), Decimal("3475.4")]
    assert all(bar.amount == 0 for bar in bars)
    assert all(bar.source == "akshare" for bar in bars)
    assert bars[0].timestamp.date() == date(2024, 1, 2)


@pytest.mark.unit
async def test_fetch_specific_contract_fail_visible(tmp_path: Path) -> None:
    """具体合约代码拒绝进逐标的缓存:主连/具体合约语义不混淆。"""
    provider = AkShareProvider(cache_dir=tmp_path / "cache")

    with pytest.raises(ValueError, match="fetch_futures_official_daily"):
        await provider.fetch_bars(
            make_symbol("IF2406.CFFEX"),
            BarPeriod.D1,
            date(2024, 5, 1),
            date(2024, 5, 31),
            adjust="qfq",
        )


@pytest.mark.unit
async def test_fetch_futures_minute_period_rejected(tmp_path: Path) -> None:
    provider = AkShareProvider(cache_dir=tmp_path / "cache")

    with pytest.raises(ValueError, match="仅支持日线"):
        await provider.fetch_bars(
            make_symbol("IF0.CFFEX"),
            BarPeriod.M5,
            date(2024, 5, 1),
            date(2024, 5, 31),
            adjust="qfq",
        )


@pytest.mark.unit
async def test_fetch_official_daily_raw_entry(tmp_path: Path) -> None:
    """交易所官网日线原始入口(按日全市场具体合约),限流退避走既有约定。"""
    provider = AkShareProvider(use_cache=False, max_retries=0)

    with patch(
        "akshare.get_futures_daily", return_value=_official_frame()
    ) as mock_daily:
        rows = await provider.fetch_futures_official_daily(
            start_date=date(2024, 5, 15),
            end_date=date(2024, 5, 16),
            market="CFFEX",
        )

    mock_daily.assert_called_once()
    assert mock_daily.call_args.kwargs["market"] == "CFFEX"
    assert len(rows) == 3
    assert rows[0].symbol == "IF2406"


@pytest.mark.unit
def test_make_symbol_future_suffixes_consistent_with_predicates() -> None:
    """cache 侧期货后缀常量与 akshare_provider.FUTURES_EXCHANGES 同口径。

    cache 不能反向 import akshare_provider(缓存原语被其依赖),一致性
    在此锁定。
    """
    from finboard_data import akshare_provider

    expected = tuple(f".{suffix}" for suffix in sorted(akshare_provider.FUTURES_EXCHANGES))
    assert expected == _FUTURE_SUFFIXES
    for suffix in _FUTURE_SUFFIXES:
        code = f"X0{suffix}"
        assert make_symbol(code).market is Market.FUTURE
    assert make_symbol("600519.SH").market is Market.A_SHARE


class _NoopBudget:
    async def acquire(self) -> None:
        return None


@pytest.mark.unit
async def test_tushare_provider_futures_fail_visible(tmp_path: Path) -> None:
    """tushare 期货代码路由到 fut_daily 专属接口(#395),不触达股票 daily。"""

    class _Client:
        # #395:期货改走 fut_daily 专属接口;未提供该方法的 client 在路由
        # 到期货分支时具名报错(不再触达股票 daily / cb_daily)。
        def daily(self, **kwargs: str) -> object:  # pragma: no cover - 不应被调用
            raise AssertionError("期货代码不得触达 tushare daily")

        def cb_daily(self, **kwargs: str) -> object:  # pragma: no cover
            raise AssertionError("期货代码不得触达 tushare cb_daily")

        def adj_factor(self, **kwargs: str) -> object:  # pragma: no cover
            raise AssertionError("期货代码不得触达 tushare adj_factor")

        def suspend_d(self, **kwargs: str) -> object:  # pragma: no cover
            return []

    provider = TushareBarProvider(
        client=_Client(),
        budget=_NoopBudget(),
        cache_dir=tmp_path / "cache",
        max_retries=0,
    )
    with pytest.raises(RuntimeError, match="不支持 fut_daily"):
        await provider.fetch_bars(
            make_symbol("IF0.CFFEX"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 31),
            adjust="qfq",
        )
