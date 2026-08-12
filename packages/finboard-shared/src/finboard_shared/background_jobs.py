"""统一持久化后台任务队列的状态枚举与 ID 生成(issue #117 / #142)。

约定:DB 列存 ``status.value`` 字符串,与 ``ResearchRunStatus`` 一致,
不用 ``sa.Enum``。``generate_background_job_id`` 产生 ``BJ-<uuid16>`` 前缀 ID,
便于在日志 / 审计中识别后台任务,且与实盘订单 ``F-`` 前缀互不混淆。
"""

from __future__ import annotations

import uuid
from enum import StrEnum


class BackgroundJobStatus(StrEnum):
    """后台任务状态机(issue #117)。

    状态流转::

        queued ─▶ running ─▶ succeeded
                          └▶ failed
                          └▶ retry_waiting ─▶ queued(重新入队)
        running ─▶ cancel_requested ─▶ cancelled(协作式取消)
        running ─▶ interrupted(worker 崩溃 / lease 过期,reclaim_stale 回收)
    """

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAITING = "retry_waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


#: 进入终态的状态集合(不再流转,只能重新排队重放)。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        BackgroundJobStatus.SUCCEEDED.value,
        BackgroundJobStatus.FAILED.value,
        BackgroundJobStatus.CANCELLED.value,
        BackgroundJobStatus.INTERRUPTED.value,
    }
)


def generate_background_job_id() -> str:
    """生成全局唯一的后台任务 ID(``BJ-<16hex>``)。"""

    return f"BJ-{uuid.uuid4().hex[:16].upper()}"


__all__ = [
    "TERMINAL_STATUSES",
    "BackgroundJobStatus",
    "generate_background_job_id",
]
