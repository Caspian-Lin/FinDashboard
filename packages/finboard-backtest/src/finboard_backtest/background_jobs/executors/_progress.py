"""进度回调桥接(issue #144;#212 改为单飞合并)。

数据域 service(``provider.update_cache_batch`` / ``build_price_feature_snapshot``)
用**同步** ``on_progress: Callable[[str, int, int], None]`` 回调上报进度(code, done, total);
而 :type:`ProgressCallback` 是 **async** ``(done, total, phase)``。worker 在单一
事件循环里调用 executor,因此可在同步回调里安全地 ``create_task`` 调度 async 回调。

本模块提供工厂:把 async ``ProgressCallback`` 包成同步 ``on_progress`` 适配器,
executor 不必各自重复样板代码。

issue #212 教训:逐标的高频回调(全市场 5534 只的联合因子快照)若每次调用都
fire-and-forget 一个 async 任务,每个任务又各自开 session 写 job 行,任务积压
速度远超 DB 写入速度,会把引擎连接池(5+10)全部占满——worker 维护循环拿不到
连接,整个进程被 QueuePool TimeoutError 掀翻。因此适配器改为**单飞合并**:同一
时刻至多一个在途任务,高频触发只更新「最新数值」槽,在途任务完成后若槽里有
更新的数值再补发一次。进度是尽力而为的展示信号,丢弃中间帧无语义影响。

issue #441 增 :func:`make_stage_phase_progress`:execute 长阶段内部的批量计数
(k/n)只进 phase 文本,数值列停在阶段档位 —— 全局 done/total 单调契约不被
高频批量帧打断。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from finboard_backtest.background_jobs.contracts import ProgressCallback


def make_sync_progress(
    progress: ProgressCallback,
    *,
    phase_prefix: str,
    total_holder: dict[str, int | None] | None = None,
) -> Callable[[str, int, int], None]:
    """把 async ``ProgressCallback`` 包成同步 ``on_progress``(单飞合并)。

    回调签名 ``(code, done, total) -> None``:
    * ``done`` / ``total`` 透传给 ``progress(done, total, phase)``;
    * ``phase`` 形如 ``f"{phase_prefix}:{done}/{total}"``,便于前端在 ``JobOut.phase`` 看到;
    * 同一时刻至多一个在途上报任务,后续调用只更新最新数值槽,由在途任务
      结束后补发(高频回调下丢弃中间帧,进度本就是尽力而为的展示信号)。

    ``total_holder``(可选)用于在闭包外读取最后一次上报的 total,方便 executor
    在 ``JobResult.progress_total`` 回填。
    """
    state = _ProgressState()
    render = _prefix_phase_renderer(phase_prefix)

    def _on_progress(code: str, done: int, total: int) -> None:
        if total_holder is not None:
            total_holder["total"] = total if total > 0 else total_holder.get("total")
        if state.inflight:
            state.latest = (done, total)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行 loop(理论上不会发生在 worker 内),静默丢弃
        state.inflight = True
        task = loop.create_task(_drain(progress, render, state, first=(done, total)))
        _pending_progress_tasks.add(task)
        task.add_done_callback(_pending_progress_tasks.discard)

    return _on_progress


def make_stage_phase_progress(
    progress: ProgressCallback,
    *,
    phase_prefix: str,
    done: int,
    total: int,
) -> Callable[[int, int], None]:
    """固定数值档位的单飞合并 phase 上报(execute 长阶段内部细粒度,#441)。

    回调签名 ``(k, n) -> None``(阶段内批量计数):``k/n`` 只渲染进
    ``phase`` 文本(``f"{phase_prefix} {k}/{n}"``),``progress`` 的
    done/total 数值恒为调用方指定的**阶段档位** —— 数值列保持阶段级
    单调(done 不减、total 只增,worker ``update_progress`` 夹紧规则),
    批量计数不打断;phase 逐帧变化同时让 #306 僵尸指纹
    (progress_done, phase) 保持活跃。单飞合并语义与
    :func:`make_sync_progress` 一致(高频触发只留最新一帧)。
    """
    state = _ProgressState()
    render = _stage_phase_renderer(phase_prefix, done, total)

    def _on_batch(k: int, n: int) -> None:
        if state.inflight:
            state.latest = (k, n)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行 loop(理论上不会发生在 worker 内),静默丢弃
        state.inflight = True
        task = loop.create_task(_drain(progress, render, state, first=(k, n)))
        _pending_progress_tasks.add(task)
        task.add_done_callback(_pending_progress_tasks.discard)

    return _on_batch


class _ProgressState:
    """单飞合并状态:是否有一个在途任务 + 最新一帧进度数值。"""

    def __init__(self) -> None:
        self.inflight = False
        self.latest: tuple[int, int] | None = None


async def _drain(
    progress: ProgressCallback,
    render: Callable[[int, int], tuple[int, int, str]],
    state: _ProgressState,
    *,
    first: tuple[int, int],
) -> None:
    """串行发帧:首发启动时捕获的帧,后续消费「最新数值」槽直到槽空。

    ``render`` 把帧数值映射为 ``(done, total, phase)`` 三元组(两种工厂
    的唯一差异点)。异常逐帧吞掉(进度是尽力而为的展示信号),``finally``
    保证 in-flight 复位。
    """
    frame: tuple[int, int] | None = first
    try:
        while frame is not None:
            with contextlib.suppress(Exception):
                await progress(*render(*frame))
            frame = state.latest
            state.latest = None
    finally:
        state.inflight = False


def _prefix_phase_renderer(
    phase_prefix: str,
) -> Callable[[int, int], tuple[int, int, str]]:
    """:func:`make_sync_progress` 的帧渲染:数值透传,phase 冒号计数。"""

    def render(done: int, total: int) -> tuple[int, int, str]:
        phase = (
            f"{phase_prefix}:{done}/{total}"
            if total
            else f"{phase_prefix}:{done}"
        )
        return done, total, phase

    return render


def _stage_phase_renderer(
    phase_prefix: str, done: int, total: int
) -> Callable[[int, int], tuple[int, int, str]]:
    """:func:`make_stage_phase_progress` 的帧渲染:数值固定档位,k/n 进 phase。"""

    def render(k: int, n: int) -> tuple[int, int, str]:
        phase = f"{phase_prefix} {k}/{n}" if n else phase_prefix
        return done, total, phase

    return render


#: 保存 fire-and-forget 的进度任务引用,避免被 GC 提前回收(RUF006)。
_pending_progress_tasks: set[asyncio.Task[None]] = set()


__all__ = ["make_stage_phase_progress", "make_sync_progress"]
