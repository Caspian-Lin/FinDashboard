"""QMT 行情端到端集成测试(需 Windows + miniQMT 环境)。

默认跳过(包括 Windows 本机):QMT 权限受阻、实盘验证暂缓(issue #22),
全量测试不跑真机链路。需要真机验证时显式设置 ``FINBOARD_QMT_E2E=1`` 开启。
CI / Linux 上同样跳过。
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("FINBOARD_QMT_E2E") != "1",
    reason=(
        "QMT 行情测试需 Windows + miniQMT 环境;"
        "设置 FINBOARD_QMT_E2E=1 显式开启"
    ),
)


@pytest.mark.integration
async def test_qmt_market_data_subscribe_tick_live() -> None:
    """真实 xtdata Tick 订阅(仅 Windows + miniQMT)。

    手动验证步骤:
    1. 启动 miniQMT 客户端;
    2. ``pytest tests/integration/test_qmt_market_e2e.py -v``。
    """
    from finboard_broker_qmt.market_adapter import QmtMarketData
    from finboard_shared.models import Symbol
    from finboard_shared.types import Market

    md = QmtMarketData()
    await md.connect()

    sym = Symbol(code="510300.SH", market=Market.A_SHARE)
    await md.subscribe_tick([sym])

    # 等待几个 tick
    import asyncio

    tick_count = 0
    async for event in md.events():
        if event.tick is not None:
            tick_count += 1
            print(f"Tick: {event.tick.symbol.code} price={event.tick.last_price}")
            if tick_count >= 3:
                break
        await asyncio.sleep(0.1)

    assert tick_count > 0, "未收到任何 Tick 推送"
    await md.unsubscribe_tick([sym])
    await md.disconnect()


@pytest.mark.integration
async def test_qmt_market_data_download_history() -> None:
    """真实 xtdata 历史数据下载(仅 Windows + miniQMT)。"""
    from finboard_broker_qmt.market_adapter import QmtMarketData
    from finboard_shared.models import Symbol
    from finboard_shared.types import BarPeriod, Market

    md = QmtMarketData()
    await md.connect()

    sym = Symbol(code="510300.SH", market=Market.A_SHARE)
    bars = await md.download_history_bar(
        sym, BarPeriod.D1, "20240101", "20240131"
    )

    assert len(bars) > 0, "未获取到历史数据"
    bar = bars[0]
    assert bar.symbol.code == "510300.SH"
    assert bar.open > 0
    assert bar.close > 0
    print(f"获取到 {len(bars)} 根日K,首根: open={bar.open} close={bar.close}")

    await md.disconnect()
