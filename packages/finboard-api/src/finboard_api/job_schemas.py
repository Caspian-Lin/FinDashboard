"""统一后台任务队列 API 契约(issue #117 / #142)。

命名约定:无 ``XxxCreate`` 后缀,用 ``XxxIn`` / ``XxxOut`` / ``XxxUpdate``。
API 只负责创建 / 查询 / 请求取消,不在 HTTP 请求内执行任务(由独立 worker 进程承接)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobIn(BaseModel):
    """提交一个后台任务。本期仅 ``echo`` kind 用于自检;业务 kind 由 #143/#144 启用。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    queue: str = Field(default="default", min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=8, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-1000, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    requested_by: str = Field(min_length=1, max_length=128)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    kind: str
    queue: str
    status: str
    priority: int
    payload: dict[str, Any]
    payload_checksum: str
    idempotency_key: str
    progress_total: int
    progress_done: int
    phase: str | None
    result_ref: str | None
    error_code: str | None
    error_summary: str | None
    attempt: int
    max_attempts: int
    worker_id: str | None
    heartbeat_at: datetime | None
    lease_until: datetime | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # 归档时间(issue #221);默认 None=未归档(兼容 archived_at 之前的行 / mock)。
    archived_at: datetime | None = None
    updated_at: datetime
    # 关联 research_runs 的状态(issue #306):kind=research_run 且单查
    # ``GET /api/jobs/{id}`` 时服务端填充(查不到关联 run 为 None);列表与写
    # 端点不 join,恒为 None。让「run interrupted 但 job 仍 running」的两表
    # 不一致一眼可见。
    run_status: str | None = None


class JobUpdate(BaseModel):
    """保留给未来管理接口(重新入队 / 改优先级);本期 cancel 端点不使用 body。"""

    model_config = ConfigDict(extra="forbid")


class JobCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=256)


class JobArchiveIn(BaseModel):
    """批量归档过滤条件(issue #221)。全部可选;默认归档全部未归档终态任务中
    最旧的 ``limit`` 条。``statuses`` 只接受终态子集(路由层校验)。"""

    model_config = ConfigDict(extra="forbid")

    kinds: list[str] | None = None
    statuses: list[str] | None = None
    queues: list[str] | None = None
    finished_before: datetime | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class JobArchiveResult(BaseModel):
    """批量归档回执(issue #206 精神:只回计数,不回全量任务列表)。"""

    archived_count: int


__all__ = [
    "JobArchiveIn",
    "JobArchiveResult",
    "JobCancelIn",
    "JobIn",
    "JobOut",
    "JobUpdate",
]
