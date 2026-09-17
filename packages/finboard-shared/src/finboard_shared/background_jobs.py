"""统一持久化后台任务队列的状态枚举与 ID 生成(issue #117 / #142)。

约定:DB 列存 ``status.value`` 字符串,与 ``ResearchRunStatus`` 一致,
不用 ``sa.Enum``。``generate_background_job_id`` 产生 ``BJ-<uuid16>`` 前缀 ID,
便于在日志 / 审计中识别后台任务,且与实盘订单 ``F-`` 前缀互不混淆。
"""

from __future__ import annotations

import uuid
from enum import StrEnum


class BackgroundJobStatus(StrEnum):
    """后台任务状态机(issue #117;#161 补自动重排路径)。

    状态流转::

        queued ─▶ running ─▶ succeeded
                          └▶ failed
                          └▶ retry_waiting ─▶ queued(worker 周期维护自动重排)
        running ─▶ cancel_requested ─▶ cancelled(协作式取消)
        running ─▶ interrupted ─▶ queued(lease 过期回收后自动重排)
        queued / retry_waiting ─▶ cancelled(取消还没被领取的任务)

    ``retry_waiting`` / ``interrupted`` 由 worker 周期性维护
    (``requeue_due``)在退避窗口过后自动重排为 ``queued``;
    attempt 耗尽则置 ``failed``(error_code=max_retries_exceeded)。
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

#: 列表接口对归档维度(``archived_at`` 列,issue #221)的过滤取值:
#: ``exclude`` 默认只看未归档;``only`` 只看已归档;``all`` 不区分。
#: 归档独立于 status(不新增枚举值),仅终态任务可归档,归档后 worker 维护
#: 路径(``requeue_due``)不再触碰,数据不删除、单查始终可达。
ARCHIVE_FILTER_VALUES: frozenset[str] = frozenset({"exclude", "only", "all"})


def generate_background_job_id() -> str:
    """生成全局唯一的后台任务 ID(``BJ-<16hex>``)。"""

    return f"BJ-{uuid.uuid4().hex[:16].upper()}"


__all__ = [
    "ARCHIVE_FILTER_VALUES",
    "TERMINAL_STATUSES",
    "BackgroundJobStatus",
    "generate_background_job_id",
]
