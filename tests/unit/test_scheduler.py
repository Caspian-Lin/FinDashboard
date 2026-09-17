"""Scheduler 单元测试。

测试覆盖:
* ScheduledTask 构造校验(time / interval 互斥,至少指定一个)
* Scheduler 注册 / 查询 / 手动触发
* interval 模式调度执行
* 异常隔离(任务出错不影响调度循环)
* stop() 即时中断
"""

from __future__ import annotations

import asyncio
from datetime import time

import pytest

from finboard_scheduler import ScheduledTask, Scheduler, TradingCalendar

pytestmark = pytest.mark.unit


class TestScheduledTask:
    def test_must_specify_time_or_interval(self) -> None:
        with pytest.raises(ValueError, match="必须指定"):
            ScheduledTask(name="t1", func=_noop)

    def test_time_and_interval_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="互斥"):
            ScheduledTask(
                name="t1",
                func=_noop,
                time=time(9, 10),
                interval=30.0,
            )

    def test_time_task_default_trading_days_only(self) -> None:
        task = ScheduledTask(name="t1", func=_noop, time=time(9, 10))
        assert task.trading_days_only is True

    def test_interval_task_can_run_any_day(self) -> None:
        task = ScheduledTask(
            name="t1",
            func=_noop,
            interval=30.0,
            trading_days_only=False,
        )
        assert task.interval == 30.0
        assert task.time is None


class TestScheduler:
    def test_register_and_list(self) -> None:
        sched = Scheduler(TradingCalendar())
        task = ScheduledTask(name="heartbeat", func=_noop, interval=1.0)
        sched.schedule(task)
        assert len(sched.list_tasks()) == 1
        assert sched.get_task("heartbeat") is task

    def test_duplicate_name_rejected(self) -> None:
        sched = Scheduler(TradingCalendar())
        sched.schedule(ScheduledTask(name="t1", func=_noop, interval=1.0))
        with pytest.raises(ValueError, match="已注册"):
            sched.schedule(ScheduledTask(name="t1", func=_noop, interval=2.0))

    def test_get_unknown_task_returns_none(self) -> None:
        sched = Scheduler(TradingCalendar())
        assert sched.get_task("nope") is None

    async def test_trigger_executes_task(self) -> None:
        call_count = 0

        async def _increment() -> None:
            nonlocal call_count
            call_count += 1

        sched = Scheduler(TradingCalendar())
        sched.schedule(ScheduledTask(name="counter", func=_increment, interval=1.0))
        await sched.trigger("counter")
        assert call_count == 1

    async def test_trigger_unknown_task_raises(self) -> None:
        sched = Scheduler(TradingCalendar())
        with pytest.raises(KeyError):
            await sched.trigger("nope")

    async def test_interval_task_fires_multiple_times(self) -> None:
        call_count = 0

        async def _increment() -> None:
            nonlocal call_count
            call_count += 1

        sched = Scheduler(TradingCalendar())
        sched.schedule(
            ScheduledTask(name="fast", func=_increment, interval=0.05)
        )
        await sched.start()
        await asyncio.sleep(0.2)
        await sched.stop()
        assert call_count >= 2
        assert not sched.running

    async def test_exception_in_task_does_not_kill_loop(self) -> None:
        success_count = 0
        ran_again = asyncio.Event()

        async def _sometimes_fail() -> None:
            nonlocal success_count
            if success_count == 0:
                success_count += 1
                raise RuntimeError("boom")
            success_count += 1
            ran_again.set()

        sched = Scheduler(TradingCalendar())
        sched.schedule(
            ScheduledTask(name="flaky", func=_sometimes_fail, interval=0.05)
        )
        await sched.start()
        # 事件等待而不是固定 sleep:全量测试(coverage 负载)下固定 0.25s
        # 窗口内可能只跑到 1 次(issue #167 时序脆弱修复)。
        async with asyncio.timeout(2.0):
            await ran_again.wait()
        await sched.stop()
        # Even though first run threw, subsequent runs should continue
        assert success_count >= 2

    async def test_stop_interrupts_sleep(self) -> None:
        started = asyncio.Event()

        async def _slow() -> None:
            started.set()

        sched = Scheduler(TradingCalendar())
        sched.schedule(ScheduledTask(name="t", func=_slow, interval=0.01))
        await sched.start()
        await started.wait()
        # Stop should complete quickly even with a long interval ahead
        await asyncio.wait_for(sched.stop(), timeout=2.0)

    async def test_trigger_does_not_require_start(self) -> None:
        called = False

        async def _flag() -> None:
            nonlocal called
            called = True

        sched = Scheduler(TradingCalendar())
        sched.schedule(ScheduledTask(name="manual", func=_flag, interval=1.0))
        await sched.trigger("manual")
        assert called


async def _noop() -> None:
    pass
