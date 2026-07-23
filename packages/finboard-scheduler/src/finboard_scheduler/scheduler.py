"""asyncio 原生定时任务调度器。

支持两种触发模式:

1. **每日定时** (``time=``):在 Asia/Shanghai 时区的指定 ``HH:MM`` 执行。
   ``trading_days_only=True`` 时仅交易日触发。
2. **固定间隔** (``interval=``):每隔 N 秒执行,不受日历限制。

设计原则:
* 零外部依赖(不引入 APScheduler / Celery / Redis);
* ``stop()`` 即时唤醒所有等待中的任务(使用 ``asyncio.Event``);
* 每次执行在独立 ``asyncio.Task`` 中运行,异常隔离不影响调度循环。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog

from finboard_scheduler.calendar import TradingCalendar

logger = structlog.get_logger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ScheduledTask:
    """一个可调度任务。

    Parameters
    ----------
    name:
        唯一名称(用于 ``trigger`` 和日志)。
    func:
        无参 async 可调用对象。
    time:
        每日定时触发时间(Asia/Shanghai)。与 ``interval`` 互斥。
    interval:
        固定间隔(秒)。与 ``time`` 互斥。
    trading_days_only:
        仅交易日触发(仅 ``time`` 模式生效;``interval`` 模式总是触发)。
    """

    name: str
    func: Callable[[], Awaitable[None]]
    time: time | None = None
    interval: float | None = None
    trading_days_only: bool = True

    def __post_init__(self) -> None:
        if self.time is None and self.interval is None:
            raise ValueError(f"ScheduledTask({self.name}) 必须指定 time 或 interval")
        if self.time is not None and self.interval is not None:
            raise ValueError(
                f"ScheduledTask({self.name}) time 和 interval 互斥"
            )


class Scheduler:
    """进程内定时任务调度器。

    Usage::

        sched = Scheduler(calendar)
        sched.schedule(ScheduledTask(name="close_cancel", time=time(14, 55), func=...))
        sched.schedule(ScheduledTask(name="heartbeat", interval=30, func=..., trading_days_only=False))
        await sched.start()
        ...
        await sched.stop()
    """

    def __init__(
        self,
        calendar: TradingCalendar | None = None,
        *,
        tz: ZoneInfo = SHANGHAI,
    ) -> None:
        self._calendar = calendar or TradingCalendar()
        self._tz = tz
        self._tasks: dict[str, ScheduledTask] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ 注册
    def schedule(self, task: ScheduledTask) -> None:
        if task.name in self._tasks:
            raise ValueError(f"任务 {task.name} 已注册")
        self._tasks[task.name] = task
        logger.info("scheduler.registered", task=task.name)

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def get_task(self, name: str) -> ScheduledTask | None:
        return self._tasks.get(name)

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        for task in self._tasks.values():
            loop_task = asyncio.create_task(
                self._task_loop(task), name=f"scheduler:{task.name}"
            )
            self._loops[task.name] = loop_task
        logger.info("scheduler.started", tasks=len(self._loops))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        # 取消所有循环任务(wait_for stop_event 已唤醒,但保险)
        for loop_task in self._loops.values():
            loop_task.cancel()
        for loop_task in self._loops.values():
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        self._loops.clear()
        logger.info("scheduler.stopped")

    async def trigger(self, name: str) -> None:
        """手动触发一次指定任务(不影响调度循环)。"""
        task = self._tasks.get(name)
        if task is None:
            raise KeyError(f"未知任务: {name}")
        await self._safe_run(task)

    # ------------------------------------------------------------------ 内部
    async def _task_loop(self, task: ScheduledTask) -> None:
        """单个任务的调度循环。"""
        while self._running:
            if task.interval is not None:
                wait = task.interval
            elif task.time is not None:
                now = datetime.now(self._tz)
                fire_dt = self._compute_next_fire(task, now)
                if task.trading_days_only and not self._calendar.is_trading_day(
                    fire_dt.date()
                ):
                    # 非交易日:睡到下一天再检查
                    wait = min(
                        (fire_dt - now).total_seconds(),
                        3600.0,  # 最多睡 1 小时再检查日历
                    )
                else:
                    wait = max(0.0, (fire_dt - now).total_seconds())
            else:
                continue  # 不可能(构造时已校验)

            # 使用 stop_event 实现可中断的 sleep
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                return  # stop() 被调用
            except TimeoutError:
                pass

            if not self._running:
                return

            # 再次校验交易日(time 模式)
            if (
                task.time is not None
                and task.trading_days_only
                and not self._calendar.is_trading_day(datetime.now(self._tz).date())
            ):
                continue

            await self._safe_run(task)

    async def _safe_run(self, task: ScheduledTask) -> None:
        try:
            logger.info("scheduler.task_start", task=task.name)
            await task.func()
            logger.info("scheduler.task_done", task=task.name)
        except Exception:
            logger.exception("scheduler.task_failed", task=task.name)

    def _compute_next_fire(self, task: ScheduledTask, now: datetime) -> datetime:
        """计算下一次触发时间(Asia/Shanghai)。"""
        assert task.time is not None
        today = now.date()
        # 今天的触发时间
        fire_today = datetime.combine(today, task.time, tzinfo=self._tz)
        if fire_today > now:
            return fire_today
        # 明天的触发时间
        return datetime.combine(today + timedelta(days=1), task.time, tzinfo=self._tz)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def tz(self) -> ZoneInfo:
        return self._tz
