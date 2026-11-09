"""统一后台任务队列的共享 enqueue 核心(issue #143 / #144 / #513)。

各语义化端点(批量下载 / 特征快照 / 数据集发布 / 回测 / 数据同步 / 全量拉取 /
质量修复 / 研究运行)都要在**同事务**内幂等登记一个 ``queued`` 任务。本模块是
FastAPI 无关的核心:只计算 payload checksum 并调用
``BackgroundJobRepository.create_or_get``,返回 ``(job_row, created)``。

* REST 侧 ``finboard_api.job_helpers.enqueue_job`` 是 10 行包装,额外负责
  ``response.status_code = 200``(幂等命中)并转成 ``JobOut``;
* MCP 侧工具直接消费 ``(job_row, created)``,不再 import ``finboard_api``
  (issue #513 的跨包 import 收敛)。

事务契约:本模块**不 commit**(调用方统一 commit / rollback),保证 job 行与
同批业务写入原子生效。

边界:只写 ``background_jobs`` 表,与实盘 ``orders`` / ``fills`` /
``positions`` / ``audit_logs`` 完全隔离。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_persistence.models import BackgroundJobModel
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)


def payload_checksum(payload: dict[str, Any]) -> str:
    """计算 background_jobs payload checksum(与 ``jobs.py`` 路由口径一致)。"""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def enqueue_job_core(
    session: AsyncSession,
    *,
    kind: str,
    queue: str,
    idempotency_key: str,
    payload: dict[str, Any],
    requested_by: str,
    priority: int = 0,
    max_attempts: int = 3,
) -> tuple[BackgroundJobModel, bool]:
    """在同事务内幂等创建一个 ``queued`` 任务,返回 ``(row, created)``。

    ``created=False`` 表示同 ``idempotency_key`` 的活跃任务已存在(幂等命中)。
    ``IntegrityError``(并发插入撞唯一索引)统一转
    :class:`BackgroundJobPersistenceConflictError`,与既有 REST 语义一致。
    """

    try:
        return await BackgroundJobRepository(session).create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=idempotency_key,
            kind=kind,
            queue=queue,
            status=BackgroundJobStatus.QUEUED.value,
            priority=priority,
            payload=payload,
            payload_checksum=payload_checksum(payload),
            max_attempts=max_attempts,
            requested_by=requested_by,
        )
    except IntegrityError as exc:
        raise BackgroundJobPersistenceConflictError("重复 idempotency_key") from exc


__all__ = ["enqueue_job_core", "payload_checksum"]
