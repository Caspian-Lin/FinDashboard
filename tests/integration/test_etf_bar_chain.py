"""ETF 行情链路端到端集成测试(issue #257)。

链路:mock akshare ETF 接口 → ``AkShareProvider`` 同步入 parquet 缓存 →
事件驱动 ``BacktestEngine`` 用 **TushareBarProvider**(mock tushare client
对 ETF 恒返回空,复刻 2000 积分拉不到 ``fund_daily`` 的真实边界)回测 →
异源缓存 read-through 拿到 bars → 策略出成交。

不联网(akshare 接口 monkeypatch 为内存 DataFrame)、不需要数据库;
复现 2026-09-01 反馈的「ETF 轮动策略全链路空转」在修复后应当跑通。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_backtest.config import BacktestConfig
from finboard_backtest.engine import BacktestEngine
from finboard_data import AkShareProvider, FallbackBarProvider, TushareBarProvider
from finboard_data.cache import make_symbol
from finboard_shared.types import BarPeriod

SYMBOL_CODE = "510300.SH"


def _etf_frame() -> pd.DataFrame:
    """模拟 ``fund_etf_hist_em`` 返回:先跌后涨,足以触发均线交叉金叉。"""
    prices = [5.0 - i * 0.08 for i in range(10)] + [4.2 + i * 0.08 for i in range(10)]
    trading_days = [
        (date(2024, 1, 1), date(2024, 1, 31)),
    ]
    dates: list[str] = []
    for start, end in trading_days:
        cursor = start
        while cursor <= end and len(dates) < len(prices):
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor = date.fromordinal(cursor.toordinal() + 1)
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": prices,
            "收盘": prices,
            "最高": [p + 0.05 for p in prices],
            "最低": [p - 0.05 for p in prices],
            "成交量": [1_000_000.0] * len(prices),
            "成交额": [4_000_000.0] * len(prices),
            "振幅": [1.0] * len(prices),
            "涨跌幅": [0.0] * len(prices),
            "涨跌额": [0.0] * len(prices),
            "换手率": [1.0] * len(prices),
        }
    )


class _EmptyTushareClient:
    """对任何标的 daily/adj_factor 均返回空,复刻 tushare 2000 积分拉不到 ETF。"""

    def daily(self, **kwargs: str) -> list[dict[str, object]]:
        return []

    def adj_factor(self, **kwargs: str) -> list[dict[str, object]]:
        return []

    def suspend_d(self, **kwargs: str) -> list[dict[str, object]]:
        return []


class _NoopBudget:
    async def acquire(self) -> None:
        return None


async def _sync_etf_into_cache(cache_dir: Path) -> AkShareProvider:
    """步骤一:经 akshare ETF 接口同步 510300.SH 日线入 parquet 缓存。"""
    from unittest.mock import patch

    provider = AkShareProvider(cache_dir=cache_dir)
    symbol = make_symbol(SYMBOL_CODE)
    with patch("akshare.fund_etf_hist_em", return_value=_etf_frame()):
        ok = await provider.update_cache(
            symbol,
            BarPeriod.D1,
            date(2024, 1, 1),
            date(2024, 1, 26),
            adjust="qfq",
        )
    assert ok is True
    assert provider._cache is not None
    metadata = await provider._cache.metadata_for(symbol, BarPeriod.D1, "qfq")
    assert metadata is not None
    assert metadata.bar_count == 20
    assert metadata.source == "akshare"
    return provider


@pytest.mark.integration
async def test_etf_sync_cache_and_backtest_with_tushare_provider(tmp_path: Path) -> None:
    """同步 → 缓存 → tushare 源引擎回测出成交(异源缓存 read-through)。"""
    akshare_provider = await _sync_etf_into_cache(tmp_path / "cache")
    assert akshare_provider._cache is not None
    cached = await akshare_provider._cache.read(
        make_symbol(SYMBOL_CODE),
        BarPeriod.D1,
        "qfq",
    )
    assert len(cached) == 20

    strategy = MaCrossStrategy(
        strategy_id="etf_rotation_e2e",
        symbol_code=SYMBOL_CODE,
        short_window=3,
        long_window=6,
    )
    tushare_provider = TushareBarProvider(
        client=_EmptyTushareClient(),
        budget=_NoopBudget(),
        cache_dir=tmp_path / "cache",
        max_retries=0,
    )
    config = BacktestConfig(
        symbols=[SYMBOL_CODE],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        initial_capital=Decimal("100000"),
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=tushare_provider,
        config=config,
    )
    result = await engine.run()

    # 修复前:tushare provider 丢弃 akshare 缓存 + fund_daily 拉不到 →
    # bars 全空、引擎 0 交易空转。修复后必须拿到 bars 并成交。
    assert result.start_date is not None
    assert result.trade_count > 0
    assert result.fills, "ETF 策略在修复后必须产生成交"
    assert result.final_equity > 0


@pytest.mark.integration
async def test_etf_backtest_with_fallback_chain(tmp_path: Path) -> None:
    """data_fallback_provider 链:tushare 主源拉空 → 回退 akshare → 出成交。"""
    await _sync_etf_into_cache(tmp_path / "cache")
    strategy = MaCrossStrategy(
        strategy_id="etf_fallback_e2e",
        symbol_code=SYMBOL_CODE,
        short_window=3,
        long_window=6,
    )
    # 主源 tushare:无缓存、接口恒空(模拟全新环境只有回退源可用)
    primary = TushareBarProvider(
        client=_EmptyTushareClient(),
        budget=_NoopBudget(),
        cache_dir=tmp_path / "empty_cache",
        max_retries=0,
    )
    fallback = AkShareProvider(cache_dir=tmp_path / "cache")
    config = BacktestConfig(
        symbols=[SYMBOL_CODE],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        initial_capital=Decimal("100000"),
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=FallbackBarProvider(
            primary=primary,
            primary_name="tushare",
            fallback=fallback,
            fallback_name="akshare",
        ),
        config=config,
    )
    result = await engine.run()
    assert result.trade_count > 0
    assert result.fills


@pytest.mark.integration
async def test_etf_akshare_provider_direct_backtest(tmp_path: Path) -> None:
    """基线:akshare ETF 分支直接作引擎数据源也应跑通(主源即 akshare)。"""
    from unittest.mock import patch

    with patch("akshare.fund_etf_hist_em", return_value=_etf_frame()):
        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        strategy = MaCrossStrategy(
            strategy_id="etf_akshare_direct",
            symbol_code=SYMBOL_CODE,
            short_window=3,
            long_window=6,
        )
        config = BacktestConfig(
            symbols=[SYMBOL_CODE],
            start=date(2024, 1, 1),
            end=date(2024, 1, 26),
            initial_capital=Decimal("100000"),
        )
        engine = BacktestEngine(
            strategy=strategy,
            data_provider=provider,
            config=config,
        )
        result = await engine.run()
    assert result.trade_count > 0
    assert result.fills
