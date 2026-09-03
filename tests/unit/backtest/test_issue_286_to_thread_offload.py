"""回测 CPU 密集段 to_thread 卸载单测(issue #286)。

断言:
* ``_EventLoopBridge`` 语义 —— 纯计算协程在工作线程内联驱动到完成;发生真实
  await / 依赖运行中事件循环的协程被投递回事件循环线程重跑;异常照常透传。
* 引擎逐日回放经 ``asyncio.to_thread`` 卸载后,事件循环线程被解放 —— 回放期间
  短超时的 asyncio 操作不被饿死(旧实现中「同步实现被当协程 await」的纯计算
  回调会阻塞整个循环,探测协程在整个回放期间得不到调度)。
* 回放结果与 #285 timing 字段不受卸载影响(行为保持)。

纯并发模型改造:不改回测语义、不触交易安全红线。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_backtest.engine import _EventLoopBridge
from finboard_core import Strategy
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


def _bar(day: int, close: str = "10") -> Bar:
    return Bar(
        symbol=SYMBOL,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


def _burn(seconds: float) -> None:
    """持 GIL 的纯 CPU 忙等(模拟逐日回放中的纯 Python 计算段)。"""

    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pass


class MemoryProvider:
    """返回 ``days`` 根固定日线的内存行情源。"""

    def __init__(self, days: int) -> None:
        self._days = days

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del symbol, period, start, end, adjust
        return [_bar(day) for day in range(1, self._days + 1)]


class CpuBurnStrategy(Strategy):
    """每次行情回调做一段纯 CPU 忙等的策略(模拟 CPU 密集策略回调)。"""

    def __init__(self, per_call: float) -> None:
        self._per_call = per_call
        self.calls = 0

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("cpu-burn")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx
        self.calls += 1
        _burn(self._per_call)


class NopStrategy(Strategy):
    """最小策略桩。"""

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("nop")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx


# ---------------------------------------------------------------- bridge 语义


class TestEventLoopBridge:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_pure_coroutine_completes_inline(self) -> None:
        """纯计算协程(无真实 await)在调用线程内联驱动到完成。"""

        async def pure() -> int:
            return 42

        bridge = _EventLoopBridge(asyncio.get_running_loop())
        result = await asyncio.to_thread(bridge.call, pure)
        assert result == 42

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_yielding_coroutine_redispatched_to_loop(self) -> None:
        """发生真实 await 的协程被投递回事件循环线程重跑并返回结果。"""

        async def yielder() -> str:
            await asyncio.sleep(0)
            return "ok"

        bridge = _EventLoopBridge(asyncio.get_running_loop())
        result = await asyncio.to_thread(bridge.call, yielder)
        assert result == "ok"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_loop_dependent_coroutine_redispatched_to_loop(self) -> None:
        """依赖运行中事件循环的协程(RuntimeError)回事件循环线程重跑。"""

        main_loop = asyncio.get_running_loop()

        async def loop_user() -> object:
            return asyncio.get_running_loop()

        bridge = _EventLoopBridge(main_loop)
        result = await asyncio.to_thread(bridge.call, loop_user)
        # 返回的是事件循环线程上拿到的 loop —— 证明协程确实在 loop 线程重跑。
        assert result is main_loop

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_exception_propagates(self) -> None:
        """协程体异常从 bridge.call 透传到调用线程。"""

        async def boom() -> None:
            raise ValueError("boom")

        bridge = _EventLoopBridge(asyncio.get_running_loop())
        with pytest.raises(ValueError, match="boom"):
            await asyncio.to_thread(bridge.call, boom)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_body_runtime_error_still_surfaces_after_redispatch(self) -> None:
        """协程体自身的 RuntimeError 会触发重跑,但异常最终仍透传(不吞错)。"""

        async def runtime_boom() -> None:
            raise RuntimeError("body failure")

        bridge = _EventLoopBridge(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="body failure"):
            await asyncio.to_thread(bridge.call, runtime_boom)


# ------------------------------------------------------------- 引擎回放卸载


class TestEngineReplayOffload:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_replay_does_not_starve_event_loop(self) -> None:
        """回放期间事件循环保持响应:短超时 asyncio 操作不被饿死。

        旧实现中逐日回放(含「同步实现被当协程 await」的纯计算回调)直接在
        事件循环线程执行,回放全程探测协程得不到调度;卸载后探测协程在每个
        GIL 切换间隔都能被调度。回放总计算 ~0.5s,探测步长 0.01s。
        """

        strategy = CpuBurnStrategy(per_call=0.04)
        engine = BacktestEngine(
            strategy=strategy,
            data_provider=MemoryProvider(days=12),
            config=BacktestConfig(
                symbols=["510300.SH"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 12),
            ),
        )
        engine_task = asyncio.create_task(engine.run())
        probe_iterations = 0
        while not engine_task.done():
            await asyncio.sleep(0.01)
            probe_iterations += 1
        result = await engine_task

        # 旧实现(阻塞循环)下探测次数 ≈ 0(引擎完成前循环完全被占住);
        # 卸载后每个 GIL 切片都能调度探测协程,阈值取保守下界。
        assert strategy.calls == 12
        assert probe_iterations >= 3
        assert len(result.equity_curve) == 12

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_replay_result_and_timing_unchanged(self) -> None:
        """卸载不改变回放结果与 #285 timing 字段语义。"""

        result = await BacktestEngine(
            strategy=NopStrategy(),
            data_provider=MemoryProvider(days=3),
            config=BacktestConfig(
                symbols=["510300.SH"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
            ),
        ).run()

        assert [point[0] for point in result.equity_curve] == [
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 1, 3),
        ]
        assert result.total_return == 0.0
        timing = cast(dict[str, object], result.timing)
        assert set(timing) == {
            "total_elapsed_seconds",
            "data_load_elapsed_seconds",
            "parquet_reads",
        }
        assert cast(float, timing["total_elapsed_seconds"]) >= cast(
            float, timing["data_load_elapsed_seconds"]
        )
