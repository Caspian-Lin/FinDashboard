"""后台任务执行器协议(issue #117 / #142)。

worker 主循环从队列领取任务后,按 ``kind`` 在 :class:`JobExecutorRegistry` 查找
执行器并调用 ``execute``。执行器只负责"跑这一票"——领取 / 状态转移 / 心跳 /
取消检测由 worker 与 :class:`BackgroundJobRepository` 协作完成,executor 通过
``progress`` 回调上报进度、通过 ``job`` 读取冻结的 payload。

边界:executor 不得自己开数据库连接 / 直接连 broker / 触发实盘下单。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class JobRecord:
    """传给 executor 的任务快照(从 ``BackgroundJobModel`` 投影)。"""

    job_id: str
    kind: str
    queue: str
    payload: dict[str, object]
    attempt: int
    max_attempts: int
    requested_by: str
    progress_total: int = 0
    progress_done: int = 0
    phase: str | None = None


@dataclass(slots=True)
class JobResult:
    """executor 返回给 worker 的执行结果。

    * ``status``:成功 → ``succeeded``;可重试错误 → ``retry_waiting``;
      不可重试 / 已达 max_attempts → ``failed``;协作式取消 → ``cancelled``。
    * ``result_ref``:产物引用字符串(run_id / snapshot_id 等),写入 ``result_ref`` 列。
    * ``progress_total``:executor 知道总量后回填,供后续进度百分比计算。
    """

    status: str
    result_ref: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    progress_total: int | None = None


#: 进度回调:(done, total, phase) → None。worker 实现里会续约心跳 + 检测取消。
ProgressCallback = Callable[[int, int | None, str | None], Awaitable[None]]


@runtime_checkable
class JobExecutor(Protocol):
    """按 ``kind`` 注册的任务执行器协议。"""

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        """执行单个任务;worker 保证调用前后已做好状态转移与租约。"""
        ...


@dataclass
class ExecutorError(Exception):
    """executor 内部抛出的、由 worker 兜底为 failed 的异常。"""

    code: str
    summary: str
    retryable: bool = False
    context: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ExecutorError",
    "JobExecutor",
    "JobRecord",
    "JobResult",
    "ProgressCallback",
]
