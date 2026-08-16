"""进度回调桥接(issue #144)。

数据域 service(``provider.update_cache_batch`` / ``build_price_feature_snapshot``)
用**同步** ``on_progress: Callable[[str, int, int], None]`` 回调上报进度(code, done, total);
而 :type:`ProgressCallback` 是 **async** ``(done, total, phase)``。worker 在单一
事件循环里调用 executor,因此可在同步回调里安全地 ``create_task`` 调度 async 回调。

本模块提供工厂:把 async ``ProgressCallback`` 包成同步 ``on_progress`` 适配器,
executor 不必各自重复样板代码。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from finboard_backtest.background_jobs.contracts import ProgressCallback

#: 保存 fire-and-forget 的进度任务引用,避免被 GC 提前回收(RUF006)。
_pending_progress_tasks: set[asyncio.Task[None]] = set()


def make_sync_progress(
    progress: ProgressCallback,
    *,
    phase_prefix: str,
    total_holder: dict[str, int | None] | None = None,
) -> Callable[[str, int, int], None]:
    """把 async ``ProgressCallback`` 包成同步 ``on_progress``。

    回调签名 ``(code, done, total) -> None``:
    * ``done`` / ``total`` 透传给 ``progress(done, total, phase)``;
    * ``phase`` 形如 ``f"{phase_prefix}:{done}/{total}"``,便于前端在 ``JobOut.phase`` 看到;
    * 用 ``asyncio.create_task`` 调度 async 调用,fire-and-forget(worker 单 loop,
      失败会被 loop 异常处理器吞掉,不影响主流程)。

    ``total_holder``(可选)用于在闭包外读取最后一次上报的 total,方便 executor
    在 ``JobResult.progress_total`` 回填。
    """

    def _on_progress(code: str, done: int, total: int) -> None:
        if total_holder is not None:
            total_holder["total"] = total if total > 0 else total_holder.get("total")
        phase = f"{phase_prefix}:{done}/{total}" if total else f"{phase_prefix}:{done}"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行 loop(理论上不会发生在 worker 内),静默丢弃
        task = loop.create_task(_safe_progress(progress, done, total, phase))
        _pending_progress_tasks.add(task)
        task.add_done_callback(_pending_progress_tasks.discard)

    return _on_progress


async def _safe_progress(
    progress: ProgressCallback,
    done: int,
    total: int | None,
    phase: str,
) -> None:
    with contextlib.suppress(Exception):
        await progress(done, total, phase)


__all__ = ["make_sync_progress"]
