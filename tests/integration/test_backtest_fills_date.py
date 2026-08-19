"""回测 fills 落库日期集成测试(issue #205)。

走真实 ``run_backtest_and_persist``(合成行情 provider,monkeypatch
``finboard_data.AkShareProvider``),断言 ``backtest_runs.fills[].date``
== 实际交易日,而非任务运行日。

不触实盘:只写 ``backtest_runs`` 研究域表。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.backtest_service import run_backtest_and_persist
from finboard_api.schemas import BacktestRunRequest
from finboard_persistence import BacktestRunRepository
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market


def _synthetic_bars() -> list[Bar]:
    """先跌后涨触发金叉的日线序列(2024-01 起 60 个交易日语义)。"""
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    prices = (
        [5.0 - i * 0.05 for i in range(20)]  # 下跌: 5.0 → 4.05
        + [4.05 + i * 0.05 for i in range(20)]  # 上涨: 4.05 → 5.0
        + [4.8 + (0.2 if i % 4 < 2 else -0.2) for i in range(20)]
    )
    bars = []
    year, month, day = 2024, 1, 1
    for p in prices:
        d = date(year, month, day)
        c = Decimal(str(p))
        bars.append(
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime(d.year, d.month, d.day, tzinfo=UTC),
                open=c,
                high=c + Decimal("0.05"),
                low=c - Decimal("0.05"),
                close=c,
                volume=Decimal("1000000"),
            )
        )
        # 简单日历推进(跳过月末溢出即可,回测不校验真实交易日历)
        day += 1
        if day > 28:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
    return bars


class _SyntheticProvider:
    """内存行情源,接口对齐 ``HistoricalDataProvider.fetch_bars``。"""

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod | None,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        return _synthetic_bars()


@pytest.mark.asyncio
class TestBacktestFillsDate:
    async def test_persisted_fills_date_is_trading_day(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import finboard_data

        monkeypatch.setattr(finboard_data, "AkShareProvider", _SyntheticProvider)

        request = BacktestRunRequest(
            strategy="ma_cross",
            symbols=["510300.SH"],
            start="2024-01-01",
            end="2024-12-31",
            capital=Decimal("100000"),
            params={"short_window": 5, "long_window": 10},
        )
        run_id = await run_backtest_and_persist(
            db_session, request, provider_name="akshare"
        )

        row = await BacktestRunRepository(db_session).get(run_id)
        assert row is not None
        assert row.fills, "先跌后涨应产生成交"

        trading_days = {str(b.timestamp.date()) for b in _synthetic_bars()}
        fills: list[dict[str, Any]] = row.fills
        for fill in fills:
            assert fill["date"] in trading_days
        # 修复前:fills[].date 全部等于任务运行日(2026 年),不可能落在
        # 2024 交易日内;此处再显式锚定运行日不出现。
        run_day = datetime.now(UTC).date().isoformat()
        assert all(fill["date"] != run_day for fill in fills)
