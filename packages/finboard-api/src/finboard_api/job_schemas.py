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
    updated_at: datetime


class JobUpdate(BaseModel):
    """保留给未来管理接口(重新入队 / 改优先级);本期 cancel 端点不使用 body。"""

    model_config = ConfigDict(extra="forbid")


class JobCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=256)


__all__ = [
    "JobCancelIn",
    "JobIn",
    "JobOut",
    "JobUpdate",
]
