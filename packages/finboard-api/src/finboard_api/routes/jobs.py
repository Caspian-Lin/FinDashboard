"""统一后台任务队列 API(issue #117 / #142)。

安全边界:本路由只创建 / 查询 / 请求取消,不在 HTTP 请求内执行任务;
实际执行由独立 worker 进程(``finboard worker run``)用
``FOR UPDATE SKIP LOCKED`` 从队列领取。所有读写只动 ``background_jobs`` 表,
与实盘 orders / fills / positions / audit_logs 完全隔离。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobCancelIn, JobIn, JobOut
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: 允许通过 API 直接提交的 kind 白名单(只放 echo 自检与研究数据摄取;
#: 其余业务 kind 由 #143/#144/#171 在各自语义化端点 / MCP 里创建,
#: 避免前端任意起 job)。
_ALLOWED_KINDS: frozenset[str] = frozenset({"echo", "research_data_sync"})


@router.post("", response_model=JobOut, status_code=202)
async def create_job(
    body: JobIn,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记一个 queued 任务并立即返回 202 + job_id;不等待执行。"""

    if body.kind not in _ALLOWED_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"未开放 kind: {body.kind}(当前仅允许 {sorted(_ALLOWED_KINDS)})",
        )
    job_id = generate_background_job_id()
    payload_checksum = hashlib.sha256(
        json.dumps(body.payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    try:
        row, created = await BackgroundJobRepository(session).create_or_get(
            job_id=job_id,
            idempotency_key=body.idempotency_key,
            kind=body.kind,
            queue=body.queue,
            status=BackgroundJobStatus.QUEUED.value,
            priority=body.priority,
            payload=body.payload,
            payload_checksum=payload_checksum,
            max_attempts=body.max_attempts,
            requested_by=body.requested_by,
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="重复 idempotency_key") from exc
    if not created:
        response.status_code = 200
    return JobOut.model_validate(row)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    kind: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    queue: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[JobOut]:
    valid = {item.value for item in BackgroundJobStatus}
    if status is not None and not set(status).issubset(valid):
        raise HTTPException(status_code=422, detail="未知 job 状态")
    rows = await BackgroundJobRepository(session).list_recent(
        kinds=kind,
        statuses=status,
        queues=queue,
        limit=limit,
    )
    return [JobOut.model_validate(row) for row in rows]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    row = await BackgroundJobRepository(session).get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return JobOut.model_validate(row)


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: str,
    _body: JobCancelIn,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """请求协作式取消(running → cancel_requested);executor checkpoint 时退出。

    若任务已在终态(succeeded/failed/cancelled/interrupted),返回当前状态不报错。
    """

    repo = BackgroundJobRepository(session)
    row = await repo.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    try:
        await repo.request_cancel(job_id)
        await session.commit()
    except BackgroundJobPersistenceConflictError:
        await session.rollback()
        # 不在 running(可能已排队/已终态)—— 直接返回当前状态给调用方判断。
    row = await repo.get(job_id)
    assert row is not None
    return JobOut.model_validate(row)


__all__ = ["router"]
