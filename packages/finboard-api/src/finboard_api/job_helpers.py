"""统一后台任务队列的共享 enqueue 助手(issue #143 / #144)。

各语义化端点(批量下载 / 特征快照 / 数据集发布 / 回测 / 数据同步 / 全量拉取 /
质量修复 / 研究运行)在自己的路由里调 :func:`enqueue_job` 登记一个 ``queued``
任务并立即返回 202 + ``JobOut``,**不在 HTTP 请求内执行任务**(由独立 worker
进程消费)。本模块集中 payload checksum + 幂等创建逻辑,避免各路由重复样板。

边界:只写 ``background_jobs`` 表,与实盘 orders / fills / positions / audit_logs
完全隔离。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.job_schemas import JobOut
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)


def payload_checksum(payload: dict[str, Any]) -> str:
    """计算 background_jobs payload checksum(与 ``jobs.py`` 路由口径一致)。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def enqueue_job(
    session: AsyncSession,
    response: Response | None,
    *,
    kind: str,
    queue: str,
    idempotency_key: str,
    payload: dict[str, Any],
    requested_by: str,
    priority: int = 0,
    max_attempts: int = 3,
) -> JobOut:
    """在同事务内幂等创建一个 ``queued`` 任务,返回 ``JobOut``。

    * 共用 ``idempotency_key``:同 key 已存在则返回旧记录,``response.status_code``
      置 200(新建默认 202),调用方据此区分"首次提交"与"幂等命中";
    * 不 commit(调用方在路由里统一 commit / rollback),保证与其他写入原子。
    """
    checksum = payload_checksum(payload)
    try:
        row, created = await BackgroundJobRepository(session).create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=idempotency_key,
            kind=kind,
            queue=queue,
            status=BackgroundJobStatus.QUEUED.value,
            priority=priority,
            payload=payload,
            payload_checksum=checksum,
            max_attempts=max_attempts,
            requested_by=requested_by,
        )
    except BackgroundJobPersistenceConflictError as exc:
        raise BackgroundJobPersistenceConflictError(str(exc)) from exc
    except IntegrityError as exc:
        raise BackgroundJobPersistenceConflictError("重复 idempotency_key") from exc
    if response is not None and not created:
        response.status_code = 200
    return JobOut.model_validate(row)


__all__ = ["enqueue_job", "payload_checksum"]
