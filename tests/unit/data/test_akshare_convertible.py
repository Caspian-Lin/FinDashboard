"""akshare 可转债兜底接口归一化测试(issue #265)。

不联网:解析函数对内存 DataFrame 直接断言;fetch 方法 monkeypatch akshare
模块属性(ETF 链路测试同风格,见 tests/integration/test_etf_bar_chain.py)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from finboard_data import (
    AkShareProvider,
    ConvertibleOverviewEntry,
    is_convertible_code,
    parse_convertible_overview_frame,
    parse_convertible_redeem_frame,
)

OBSERVED_AT = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("110059.SH", True),
        ("113050.SH", True),
        ("118048.SH", True),
        ("123101.SZ", True),
        ("127063.SZ", True),
        ("128100.SZ", True),
        ("600519.SH", False),  # 沪市股票
        ("000001.SZ", False),  # 深市股票
        ("510300.SH", False),  # ETF(#257)
        ("000300.SH", False),  # 指数(#256)
        ("110059", False),  # 缺后缀不猜交易所
        ("113050.BJ", False),  # 北交所转债暂无场内上游
    ],
)
def test_is_convertible_code_segments(code: str, expected: bool) -> None:
    assert is_convertible_code(code) is expected


def _overview_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "债券代码": ["113050", "123101", "500001"],
            "债券简称": ["南银转债", "天赐转2", "已兑付归档"],
            "正股代码": ["601009", "002709", "600000"],
            "债券评级": ["AA+", "AA", None],
            "转股价": [8.5, 45.12, 9.0],
        }
    )


@pytest.mark.unit
def test_parse_overview_normalizes_codes_and_skips_non_convertible() -> None:
    entries = parse_convertible_overview_frame(_overview_frame())

    assert entries == [
        ConvertibleOverviewEntry(
            code="113050.SH",
            name="南银转债",
            underlying_symbol="601009.SH",
            rating="AA+",
            conversion_price=Decimal("8.5"),
        ),
        ConvertibleOverviewEntry(
            code="123101.SZ",
            name="天赐转2",
            underlying_symbol="002709.SZ",
            rating="AA",
            conversion_price=Decimal("45.12"),
        ),
    ]


@pytest.mark.unit
def test_parse_overview_missing_required_column_raises() -> None:
    frame = _overview_frame().drop(columns=["正股代码"])

    with pytest.raises(ValueError, match="正股代码"):
        parse_convertible_overview_frame(frame)


@pytest.mark.unit
def test_parse_overview_all_rows_unrecognized_raises() -> None:
    frame = pd.DataFrame(
        {
            "债券代码": ["500001"],
            "债券简称": ["已兑付归档"],
            "正股代码": ["600000"],
        }
    )

    with pytest.raises(ValueError, match="无可识别的转债代码段"):
        parse_convertible_overview_frame(frame)


@pytest.mark.unit
def test_parse_overview_optional_columns_absent_and_bad_price() -> None:
    frame = pd.DataFrame(
        {
            "债券代码": ["113050"],
            "债券简称": ["南银转债"],
            "正股代码": ["601009"],
            "转股价": ["--"],  # 上游占位值按缺失处理
        }
    )

    entries = parse_convertible_overview_frame(frame)
    assert entries[0].rating is None
    assert entries[0].conversion_price is None


def _redeem_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "债券代码": ["113050", "123101", "127063"],
            "债券简称": ["南银转债", "天赐转2", "相邻转债"],
            "正股代码": ["601009", "002709", "000001"],
            "赎回日": ["2026-08-20", None, "20260915"],
            "停止交易日": [None, "2026-08-19", None],
            "赎回价": ["100.32", None, 101.0],
        }
    )


@pytest.mark.unit
def test_parse_redeem_prefers_redemption_date_with_fallbacks() -> None:
    events = parse_convertible_redeem_frame(_redeem_frame(), observed_at=OBSERVED_AT)

    assert [item.code for item in events] == ["113050.SH", "123101.SZ", "127063.SZ"]
    # 赎回日优先;缺赎回日回退停止交易日;格式容错 20260915。
    effective = [item.effective_date for item in events]
    assert effective[0] is not None
    assert effective[0].isoformat() == "2026-08-20"  # 赎回日优先
    assert events[1].redemption_date is None
    assert events[1].stop_transfer_date is not None
    assert effective[1] is not None
    assert effective[1].isoformat() == "2026-08-19"  # 回退停止交易日
    assert events[2].redemption_date is not None
    assert events[2].redemption_date.isoformat() == "2026-09-15"  # 格式容错 20260915
    # 赎回价解析;缺失为 None。
    assert events[0].redemption_price == Decimal("100.32")
    assert events[1].redemption_price is None
    assert events[2].redemption_price == Decimal("101.0")
    # 快照观察时间 = 注入时钟(PIT 语义见 ConvertibleRedemptionEvent)。
    assert all(item.observed_at == OBSERVED_AT for item in events)


@pytest.mark.unit
def test_parse_redeem_skips_rows_without_effective_date() -> None:
    frame = pd.DataFrame(
        {
            "债券代码": ["113050", "123101"],
            "债券简称": ["南银转债", "无日期行"],
            "赎回日": ["2026-08-20", None],
            "停止交易日": [None, None],
        }
    )

    events = parse_convertible_redeem_frame(frame, observed_at=OBSERVED_AT)
    assert [item.code for item in events] == ["113050.SH"]


@pytest.mark.unit
async def test_fetch_overview_and_redeem_go_through_rate_limiter() -> None:
    """fetch 方法走统一调用路径(限流 + 解析),monkeypatch akshare 接口。"""
    provider = AkShareProvider(use_cache=False, max_retries=0)
    with (
        patch("akshare.bond_zh_cov", return_value=_overview_frame()) as mock_overview,
        patch(
            "akshare.bond_cb_redeem_jsl", return_value=_redeem_frame()
        ) as mock_redeem,
    ):
        entries = await provider.fetch_convertible_overview()
        events = await provider.fetch_convertible_redeem_events(now=lambda: OBSERVED_AT)

    mock_overview.assert_called_once()
    mock_redeem.assert_called_once()
    assert [item.code for item in entries] == ["113050.SH", "123101.SZ"]
    assert [item.code for item in events] == ["113050.SH", "123101.SZ", "127063.SZ"]


@pytest.mark.unit
async def test_fetch_overview_retries_then_raises() -> None:
    """接口失败按 provider 退避约定重试,最终抛最后异常。"""
    provider = AkShareProvider(use_cache=False, max_retries=1, retry_backoff=0.0)
    calls: list[int] = []

    def _boom() -> object:
        calls.append(1)
        raise ConnectionError("jsl 限流")

    with patch("akshare.bond_zh_cov", side_effect=_boom):
        with pytest.raises(ConnectionError):
            await provider.fetch_convertible_overview()

    assert len(calls) == 2  # 首次 + 1 次重试


@pytest.mark.unit
async def test_akshare_convertible_bars_fail_visible(tmp_path: Path) -> None:
    """akshare 转债日线 fail-visible 拒绝(上游无可靠接口),不静默误路由。"""
    from datetime import date

    from finboard_data.cache import make_symbol
    from finboard_shared.types import BarPeriod

    provider = AkShareProvider(cache_dir=tmp_path / "cache")
    with pytest.raises(ValueError, match="cb_daily"):
        await provider.fetch_bars(
            make_symbol("113050.SH"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 31),
            adjust="qfq",
        )
