"""统一后台任务队列 API(issue #117 / #142;#221 归档)。

安全边界:本路由只创建 / 查询 / 请求取消 / 归档,不在 HTTP 请求内执行任务;
实际执行由独立 worker 进程(``finboard worker run``)用
``FOR UPDATE SKIP LOCKED`` 从队列领取。所有读写只动 ``background_jobs`` 表,
与实盘 orders / fills / positions / audit_logs 完全隔离。

归档(issue #221)是展示维度:归档后从默认列表(``archived=exclude``)隐藏
但**不删除**,``archived=only|all`` 与单查始终可达,可取消归档;
仅终态任务可归档,归档后 worker 维护路径不再触碰。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import (
    JobArchiveIn,
    JobArchiveResult,
    JobCancelIn,
    JobIn,
    JobOut,
)
from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import (
    ARCHIVE_FILTER_VALUES,
    TERMINAL_STATUSES,
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
    # per-kind payload 入队期契约(#260,与 MCP finboard_job_enqueue 共用):
    # 未知键 / 缺必填 / 枚举非法秒级 422,不再等 worker 执行期才报错。
    try:
        validate_job_payload(body.kind, body.payload)
    except PayloadContractError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"payload 契约校验失败[{exc.code}]: {exc.summary}",
        ) from exc
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
    response: Response,
    kind: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    queue: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    archived: str = Query(default="exclude"),
    session: AsyncSession = Depends(get_db_session),
) -> list[JobOut]:
    valid = {item.value for item in BackgroundJobStatus}
    if status is not None and not set(status).issubset(valid):
        raise HTTPException(status_code=422, detail="未知 job 状态")
    if archived not in ARCHIVE_FILTER_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"未知归档过滤值: {archived}(合法 {sorted(ARCHIVE_FILTER_VALUES)})",
        )
    repo = BackgroundJobRepository(session)
    rows = await repo.list_recent(
        kinds=kind,
        statuses=status,
        queues=queue,
        limit=limit,
        offset=offset,
        archived=archived,
    )
    # 总数经响应头透出(issue #373 分页):与 body 分离,旧调用方零感知。
    total = await repo.count_recent(
        kinds=kind, statuses=status, queues=queue, archived=archived
    )
    response.headers["X-Total-Count"] = str(total)
    return [JobOut.model_validate(row) for row in rows]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    row = await BackgroundJobRepository(session).get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    out = JobOut.model_validate(row)
    if row.kind == "research_run":
        # issue #306:透传关联 research_runs 状态 —— 「run interrupted 但 job
        # 仍 running」的两表不一致在单查视图一眼可见(查不到 run 为 null)。
        from finboard_persistence import ResearchRunRepository

        out.run_status = await ResearchRunRepository(session).get_status_by_job_id(
            job_id
        )
    return out


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


@router.post("/archive", response_model=JobArchiveResult)
async def bulk_archive_jobs(
    body: JobArchiveIn,
    session: AsyncSession = Depends(get_db_session),
) -> JobArchiveResult:
    """批量归档未归档的终态任务(issue #221)。按 created_at 从旧到新归档,
    ``statuses`` 只接受终态子集(空 = 全部终态);只回计数,不回全量任务列表。"""

    if body.statuses is not None and not set(body.statuses).issubset(TERMINAL_STATUSES):
        raise HTTPException(
            status_code=422,
            detail=f"仅终态任务可归档(合法 {sorted(TERMINAL_STATUSES)})",
        )
    try:
        count = await BackgroundJobRepository(session).archive_bulk(
            kinds=body.kinds,
            statuses=body.statuses,
            queues=body.queues,
            finished_before=body.finished_before,
            limit=body.limit,
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobArchiveResult(archived_count=count)


@router.post("/{job_id}/archive", response_model=JobOut)
async def archive_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """归档单个终态任务:从默认列表隐藏但不删除;幂等(已归档原样返回)。
    非终态(排队 / 运行中 / 等待重试)拒绝归档(409)。"""

    repo = BackgroundJobRepository(session)
    try:
        row = await repo.archive(job_id)
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        message = str(exc)
        if "不存在" in message:
            raise HTTPException(status_code=404, detail="后台任务不存在") from exc
        raise HTTPException(status_code=409, detail=message) from exc
    return JobOut.model_validate(row)


@router.post("/{job_id}/unarchive", response_model=JobOut)
async def unarchive_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """取消归档:任务重新出现在默认列表;幂等(未归档原样返回)。"""

    repo = BackgroundJobRepository(session)
    try:
        row = await repo.unarchive(job_id)
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="后台任务不存在") from exc
    return JobOut.model_validate(row)


__all__ = ["router"]
