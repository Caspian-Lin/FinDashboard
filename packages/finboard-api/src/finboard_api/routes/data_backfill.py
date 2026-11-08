"""补数 REST(issue #511)。

课题发现缺口 → 预检 → 显式提交 → 进程/取消/重试的语义端点:

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | ``/api/data/backfills/preview`` | 纯读预检(缺口/步骤/配额/发布影响) |
| POST | ``/api/data/backfills`` | 提交(幂等;`202` 返回请求 + job + 是否复用) |
| GET | ``/api/data/backfills`` | 列表(状态/数据集/提交人/时间过滤) |
| GET | ``/api/data/backfills/{request_id}`` | 详情(含逐项明细) |
| POST | ``/api/data/backfills/{request_id}/cancel`` | 协作式取消 |
| POST | ``/api/data/backfills/{request_id}/retry`` | 只为失败/取消项新建请求 |

边界:本路由不实现同步/发布,只做参数收敛与错误码映射;真正的执行在
``kind=data_backfill`` 队列任务里(执行器组合既有数据域执行器)。

错误码映射(与方案 2.3 一致):预检前置不满足 → 422 ``{code,message}``;
提交前计划漂移 → 409 ``backfill_plan_drift``(带 expected/actual checksum);
请求不存在 → 404;状态冲突(不可取消/不可重试)→ 409。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_backtest.data_backfill.contracts import (
    BackfillPlan,
    BackfillPlanDriftError,
    BackfillPlanError,
    BackfillRequestSpec,
)
from finboard_backtest.data_backfill.planner import plan_backfill
from finboard_backtest.data_backfill.service import (
    BackfillRequestError,
    cancel_backfill,
    retry_backfill,
    submit_backfill,
)
from finboard_backtest.research_run.contracts import ResearchActorType
from finboard_persistence.data_backfill_repo import DataBackfillRepository
from finboard_persistence.models import (
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
)

router = APIRouter(prefix="/api/data/backfills", tags=["data-backfill"])

#: 列表页上限(方案 2.3:补数列表 limit 上限 100)。
_MAX_LIST_LIMIT = 100


class BackfillPreviewRequest(BaseModel):
    """预检入参(范围 + 数据集目标 + 质量档)。"""

    model_config = ConfigDict(extra="forbid")
    spec: BackfillRequestSpec
    as_of: datetime | None = None


class BackfillSubmitRequest(BaseModel):
    """提交入参;``expected_plan_checksum`` 用于提交前 drift 检测。"""

    model_config = ConfigDict(extra="forbid")
    spec: BackfillRequestSpec
    expected_plan_checksum: str | None = None
    actor_type: Literal["human", "agent"] = "human"
    #: 幂等键 body 兜底(优先级低于 ``Idempotency-Key`` 请求头)。
    idempotency_key: str | None = None


class BackfillRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str | None = None
    actor_type: Literal["human", "agent"] = "human"


class BackfillItemOut(BaseModel):
    id: int
    dataset: str
    source: str
    adjustment: str
    action: str
    status: str
    start_date: date
    end_date: date
    symbols: list[str]
    universe_filters: dict[str, Any]
    missing_ranges: list[list[str]]
    repair_symbols: list[str]
    estimated_requests: int
    batch_ids: list[int]
    release_id: str | None
    accepted_rows: int
    rejected_rows: int
    error_code: str | None
    error_summary: str | None


class BackfillRequestOut(BaseModel):
    request_id: str
    idempotency_key: str
    fingerprint: str
    scope_checksum: str
    status: str
    job_id: str | None
    release_ids: list[str]
    reused_request_ids: list[str]
    retry_of_request_id: str | None
    requested_by: str
    actor_type: str
    error_code: str | None
    error_summary: str | None
    blockers: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class BackfillRequestDetailOut(BackfillRequestOut):
    items: list[BackfillItemOut] = Field(default_factory=list)


class BackfillPageOut(BaseModel):
    items: list[BackfillRequestOut]
    total: int
    limit: int
    offset: int


class BackfillSubmitOut(BaseModel):
    request_id: str
    job_id: str | None
    status: str
    created: bool
    reused: bool
    plan_checksum: str
    items: list[BackfillItemOut] = Field(default_factory=list)


def _request_out(row: DataBackfillRequestModel) -> BackfillRequestOut:
    return BackfillRequestOut(
        request_id=row.request_id,
        idempotency_key=row.idempotency_key,
        fingerprint=row.fingerprint,
        scope_checksum=row.scope_checksum,
        status=row.status,
        job_id=row.job_id,
        release_ids=list(row.release_ids or []),
        reused_request_ids=list(row.reused_request_ids or []),
        retry_of_request_id=row.retry_of_request_id,
        requested_by=row.requested_by,
        actor_type=row.actor_type,
        error_code=row.error_code,
        error_summary=row.error_summary,
        blockers=[dict(item) for item in (row.blockers or [])],
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _item_out(row: DataBackfillRequestItemModel) -> BackfillItemOut:
    return BackfillItemOut(
        id=int(row.id),
        dataset=row.dataset,
        source=row.source,
        adjustment=row.adjustment,
        action=row.action,
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
        symbols=[str(item) for item in (row.symbols or [])],
        universe_filters=dict(row.universe_filters or {}),
        missing_ranges=[
            [str(start), str(end)] for start, end in (row.missing_ranges or [])
        ],
        repair_symbols=[str(item) for item in (row.repair_symbols or [])],
        estimated_requests=int(row.estimated_requests or 0),
        batch_ids=[int(item) for item in (row.batch_ids or [])],
        release_id=row.release_id,
        accepted_rows=int(row.accepted_rows or 0),
        rejected_rows=int(row.rejected_rows or 0),
        error_code=row.error_code,
        error_summary=row.error_summary,
    )


def _plan_out(plan: BackfillPlan) -> dict[str, Any]:
    payload = plan.model_dump(mode="json")
    payload["plan_checksum"] = plan.checksum()
    return payload


def _actor(value: str) -> ResearchActorType:
    return ResearchActorType.AGENT if value == "agent" else ResearchActorType.HUMAN


def _plan_error(exc: BackfillPlanError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": exc.code,
            "message": exc.message,
            "dataset": exc.dataset.value if exc.dataset else None,
        },
    )


def _request_error(exc: BackfillRequestError) -> HTTPException:
    status = 404 if exc.code == "backfill_not_found" else 409
    return HTTPException(
        status_code=status, detail={"code": exc.code, "message": exc.message}
    )


@router.post("/preview")
async def preview_backfill(
    body: BackfillPreviewRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """纯读预检:缺口范围、支持来源、配额、补齐步骤与发布影响。"""

    try:
        plan = await plan_backfill(session, body.spec, as_of=body.as_of)
    except BackfillPlanError as exc:
        raise _plan_error(exc) from exc
    return _plan_out(plan)


@router.post("", status_code=202)
async def create_backfill(
    body: BackfillSubmitRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    requested_by: str = Header(default="api", alias="X-Requested-By"),
    session: AsyncSession = Depends(get_db_session),
) -> BackfillSubmitOut:
    """提交补数请求(幂等;命中幂等键/在途同 fingerprint 时 ``created=false``)。"""

    key = (idempotency_key or body.idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "backfill_idempotency_required",
                "message": "需要 Idempotency-Key 请求头(或改用 retry 端点)",
            },
        )
    try:
        result = await submit_backfill(
            session,
            body.spec,
            idempotency_key=key,
            requested_by=requested_by,
            actor_type=_actor(body.actor_type),
            expected_plan_checksum=body.expected_plan_checksum,
        )
        await session.commit()
    except BackfillPlanError as exc:
        await session.rollback()
        raise _plan_error(exc) from exc
    except BackfillPlanDriftError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "backfill_plan_drift",
                "message": str(exc),
                "expected_checksum": exc.expected,
                "actual_checksum": exc.actual,
            },
        ) from exc
    except BackfillRequestError as exc:
        await session.rollback()
        raise _request_error(exc) from exc
    repo = DataBackfillRepository(session)
    items = await repo.list_items(result.request_id)
    return BackfillSubmitOut(
        request_id=result.request_id,
        job_id=result.request.job_id,
        status=result.request.status,
        created=result.created,
        reused=result.reused,
        plan_checksum=result.plan.checksum(),
        items=[_item_out(item) for item in items],
    )


@router.get("")
async def list_backfills(
    status: str | None = Query(default=None),
    dataset: str | None = Query(default=None),
    requested_by: str | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> BackfillPageOut:
    """补数请求列表(按状态/数据集/提交人/创建时间过滤)。"""

    repo = DataBackfillRepository(session)
    rows, total = await repo.list_requests(
        statuses=[item.strip() for item in status.split(",")] if status else None,
        dataset=(dataset or None),
        requested_by=(requested_by or None),
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return BackfillPageOut(
        items=[_request_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{request_id}")
async def get_backfill(
    request_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> BackfillRequestDetailOut:
    """补数请求详情(含逐数据集明细、批次与发布产物)。"""

    repo = DataBackfillRepository(session)
    row = await repo.get(request_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "backfill_not_found",
                "message": f"补数请求不存在: {request_id}",
            },
        )
    items = await repo.list_items(request_id)
    payload = _request_out(row).model_dump()
    return BackfillRequestDetailOut(
        **payload, items=[_item_out(item) for item in items]
    )


@router.post("/{request_id}/cancel")
async def cancel_backfill_request(
    request_id: str,
    requested_by: str = Header(default="api", alias="X-Requested-By"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """协作式取消(job 取消 + 未完成明细标 cancelled;已发布不回滚)。"""

    try:
        row = await cancel_backfill(
            session, request_id, requested_by=requested_by
        )
        await session.commit()
    except BackfillRequestError as exc:
        await session.rollback()
        raise _request_error(exc) from exc
    return {
        "request_id": row.request_id,
        "status": row.status,
        "job_id": row.job_id,
        "cancel_requested": row.status == "cancelled",
    }


@router.post("/{request_id}/retry", status_code=202)
async def retry_backfill_request(
    request_id: str,
    body: BackfillRetryRequest,
    requested_by: str = Header(default="api", alias="X-Requested-By"),
    session: AsyncSession = Depends(get_db_session),
) -> BackfillSubmitOut:
    """只为失败/取消的剩余项新建请求(``retry_of_request_id`` 链)。"""

    try:
        result = await retry_backfill(
            session,
            request_id,
            requested_by=requested_by,
            actor_type=_actor(body.actor_type),
            idempotency_key=body.idempotency_key,
        )
        await session.commit()
    except BackfillPlanError as exc:
        # 冻结 spec 重算 plan 时日历缺失 / 窗口越界:422,不是 500。
        await session.rollback()
        raise _plan_error(exc) from exc
    except BackfillRequestError as exc:
        await session.rollback()
        raise _request_error(exc) from exc
    repo = DataBackfillRepository(session)
    items = await repo.list_items(result.request_id)
    return BackfillSubmitOut(
        request_id=result.request_id,
        job_id=result.request.job_id,
        status=result.request.status,
        created=result.created,
        reused=result.reused,
        plan_checksum=result.plan.checksum(),
        items=[_item_out(item) for item in items],
    )


__all__ = ["router"]
