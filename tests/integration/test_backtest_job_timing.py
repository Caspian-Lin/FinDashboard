"""回测 job 级分段耗时落库集成测试(issue #285)。

走真实 ``run_backtest_and_persist``(合成行情 provider,monkeypatch
``finboard_data.AkShareProvider``),断言 ``backtest_runs.metrics`` JSON
携带 ``timing``(总耗时 / 数据加载耗时 / parquet 读取聚合)。

纯可观测性:只写 ``backtest_runs`` 研究域表,不连 broker 不下单。
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
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    bars: list[Bar] = []
    for day in range(1, 11):
        close = Decimal(str(10 + day * 0.1))
        bars.append(
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime(2024, 1, day, tzinfo=UTC),
                open=close,
                high=close + Decimal("0.05"),
                low=close - Decimal("0.05"),
                close=close,
                volume=Decimal("1000000"),
            )
        )
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
        del symbol, period, start, end, adjust
        return _synthetic_bars()


@pytest.mark.asyncio
async def test_persisted_metrics_carry_job_timing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finboard_data

    monkeypatch.setattr(finboard_data, "AkShareProvider", _SyntheticProvider)

    request = BacktestRunRequest(
        strategy="ma_cross",
        symbols=["510300.SH"],
        start="2024-01-01",
        end="2024-01-31",
        capital=Decimal("100000"),
        params={"short_window": 2, "long_window": 5},
    )
    run_id = await run_backtest_and_persist(
        db_session, request, provider_name="akshare"
    )

    row = await BacktestRunRepository(db_session).get(run_id)
    assert row is not None
    metrics: dict[str, Any] = row.metrics
    timing = metrics["timing"]
    assert set(timing) == {
        "total_elapsed_seconds",
        "data_load_elapsed_seconds",
        "parquet_reads",
    }
    assert timing["total_elapsed_seconds"] >= 0
    assert timing["data_load_elapsed_seconds"] >= 0
    reads = timing["parquet_reads"]
    # 合成 provider 不经 ParquetCache:聚合为全零形状(字段仍完整)。
    assert set(reads) == {"read_ops", "read_elapsed_ms", "read_bytes", "ops_by_entry"}
    assert reads["read_ops"] == 0
