"""补数提交、去重与重试(issue #511,方案 5.3)。

:func:`submit_backfill` 是「课题缺口 → 共享补数请求」的唯一写入口:

1. 规范化 spec → ``fingerprint``(scope checksum + dataset/source/adjustment/
   窗口/标的/发布开关;等价重试天然同 fingerprint);
2. **幂等键命中** → 直接返回既有请求(``created=False``),不重复入队;
3. **同 fingerprint 在途请求** → 复用返回(``reused=True``),不新建;
   同 fingerprint 但已终态失败 → 允许新建并通过 ``retry_of_request_id`` 留血缘;
4. **部分重叠** → 按预检 diff 只写「剩余缺口」项,``reused_request_ids``
   记录复用来源;剩余为空时直接返回 ``succeeded`` 的复用结果(不建 job);
5. 同事务写 ``data_backfill_requests`` + 明细 + ``background_jobs``
   (``kind=data_backfill``,``idempotency_key=backfill:<request_id>``);
6. **提交前复算 plan**,与调用方看到的 ``plan_checksum`` 不一致即
   :class:`BackfillPlanDriftError`(API 409)—— 禁止「静默按旧计划执行」。

语义边界:

* **异源/不同复权不混接**:``source`` / ``adjustment`` 是 item 身份维度,
  不同源永远新建项(预检已按此 diff,这里不再合并);
* **item 独立成功/失败**:默认 fail-fast 在**执行器**里体现(后续 item 不再
  领取),已完成 item 的发布保留可用;
* **取消**:job 协作式取消;已发布 release 不回滚(不可变),未完成 item 标
  ``cancelled``;
* **重试**:只带失败/取消的剩余项新建请求,``retry_of_request_id`` 链。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_backfill.contracts import (
    BackfillAction,
    BackfillDatasetSpec,
    BackfillItemStatus,
    BackfillPlan,
    BackfillPlanDriftError,
    BackfillRequestSpec,
    BackfillRequestStatus,
    generate_backfill_request_id,
    request_fingerprint,
    utcnow,
)
from finboard_backtest.data_backfill.planner import plan_backfill
from finboard_backtest.data_backfill.support import BackfillCapabilities
from finboard_backtest.research_run.contracts import ResearchActorType
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_persistence.data_backfill_repo import DataBackfillRepository
from finboard_persistence.models import DataBackfillRequestModel
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

#: 可重试的请求状态:失败/取消/部分成功 + **blocked**(来源/额度/数据集开通后
#: 重试同一冻结 spec;blocked 行本身就是「需求登记」,不重试就只能重提)。
_RETRYABLE_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        BackfillRequestStatus.FAILED.value,
        BackfillRequestStatus.CANCELLED.value,
        BackfillRequestStatus.PARTIAL.value,
        BackfillRequestStatus.BLOCKED.value,
    }
)

#: 请求终态(不再流转;取消/重试都按此判定)。
_TERMINAL_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        BackfillRequestStatus.SUCCEEDED.value,
        BackfillRequestStatus.PARTIAL.value,
        BackfillRequestStatus.FAILED.value,
        BackfillRequestStatus.CANCELLED.value,
        BackfillRequestStatus.BLOCKED.value,
    }
)


class BackfillRequestError(RuntimeError):
    """补数请求状态冲突(不存在 / 不可取消 / 不可重试);API 映射 409/404。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BackfillSubmitResult:
    """提交结果(``created=False`` 表示命中幂等键或复用同 fingerprint 请求)。"""

    request: DataBackfillRequestModel
    plan: BackfillPlan
    created: bool
    reused: bool

    @property
    def request_id(self) -> str:
        return self.request.request_id


async def submit_backfill(
    session: AsyncSession,
    spec: BackfillRequestSpec,
    *,
    idempotency_key: str,
    requested_by: str,
    actor_type: ResearchActorType,
    expected_plan_checksum: str | None = None,
    capabilities: BackfillCapabilities | None = None,
    settings_factory: Any = None,
    budget: object | None = None,
    retry_of_request_id: str | None = None,
) -> BackfillSubmitResult:
    """提交补数请求(事务边界由调用方控制:本函数只 flush,不 commit)。"""

    if not idempotency_key.strip():
        raise BackfillRequestError(
            "backfill_idempotency_required", "idempotency_key 不能为空"
        )
    repo = DataBackfillRepository(session)
    fingerprint = request_fingerprint(spec)
    await _serialize_submit(session, fingerprint, idempotency_key)

    existing = await repo.get_by_idempotency_key(idempotency_key)
    if existing is not None and existing.status not in (
        BackfillRequestStatus.FAILED.value,
        BackfillRequestStatus.CANCELLED.value,
    ):
        return BackfillSubmitResult(
            request=existing,
            plan=BackfillPlan.model_validate(existing.plan),
            created=False,
            reused=True,
        )

    plan = await plan_backfill(
        session,
        spec,
        capabilities=capabilities,
        settings_factory=settings_factory,
        budget=budget,
    )
    if expected_plan_checksum is not None and plan.checksum() != expected_plan_checksum:
        raise BackfillPlanDriftError(expected_plan_checksum, plan.checksum())

    # 同 fingerprint 的在途请求优先复用(不重复消耗上游配额)。
    inflight = await repo.find_active_by_fingerprint(
        fingerprint, statuses=sorted(("queued", "running"))
    )
    if inflight:
        return BackfillSubmitResult(
            request=inflight[0],
            plan=BackfillPlan.model_validate(inflight[0].plan),
            created=False,
            reused=True,
        )

    request_id = generate_backfill_request_id()
    # 落库的 spec 用**解析后**值(source/adjustment 已落定):retry 时按
    # (dataset, source, adjustment) 精确匹配明细行,不会因 None 回退到错误来源。
    frozen_spec = _freeze_spec(spec, plan).model_dump(mode="json")
    frozen_plan = plan.model_dump(mode="json")
    reused_request_ids = sorted(
        {
            request_id_hint
            for item in plan.items
            for request_id_hint in item.reused_request_ids
        }
    )
    blockers = [
        {
            "code": blocker.code,
            "message": blocker.message,
            "dataset": blocker.dataset.value if blocker.dataset else None,
        }
        for blocker in plan.blockers
    ]

    if plan.blocked:
        # 需求登记:落 blocked 行,不建 job(可查询、可审计,来源开通后可 retry)。
        row = await _create_request(
            repo,
            request_id=request_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            scope_checksum=plan.scope_checksum,
            status=BackfillRequestStatus.BLOCKED.value,
            spec=frozen_spec,
            plan=frozen_plan,
            requested_by=requested_by,
            actor_type=actor_type.value,
            reused_request_ids=reused_request_ids,
            retry_of_request_id=retry_of_request_id,
            blockers=blockers,
            error_code="backfill_blocked",
            error_summary="; ".join(
                f"{blocker.code}: {blocker.message}" for blocker in plan.blockers
            )[:1000],
            completed_at=utcnow(),
        )
        await _add_plan_items(repo, request_id, plan)
        return BackfillSubmitResult(
            request=row, plan=plan, created=True, reused=False
        )

    if plan.is_empty:
        # 无剩余缺口:直接 succeeded 复用结果,不建 job(HTTP 200 语义)。
        row = await _create_request(
            repo,
            request_id=request_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            scope_checksum=plan.scope_checksum,
            status=BackfillRequestStatus.SUCCEEDED.value,
            spec=frozen_spec,
            plan=frozen_plan,
            requested_by=requested_by,
            actor_type=actor_type.value,
            reused_request_ids=reused_request_ids,
            retry_of_request_id=retry_of_request_id,
            blockers=blockers,
            started_at=utcnow(),
            completed_at=utcnow(),
        )
        await _add_plan_items(repo, request_id, plan)
        row.release_ids = sorted(
            {
                coverage.id
                for item in plan.items
                for coverage in item.covered_by
                if coverage.kind == "release"
            }
        )
        await session.flush()
        return BackfillSubmitResult(
            request=row, plan=plan, created=True, reused=False
        )

    row = await _create_request(
        repo,
        request_id=request_id,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        scope_checksum=plan.scope_checksum,
        status=BackfillRequestStatus.QUEUED.value,
        spec=frozen_spec,
        plan=frozen_plan,
        requested_by=requested_by,
        actor_type=actor_type.value,
        reused_request_ids=reused_request_ids,
        retry_of_request_id=retry_of_request_id,
        blockers=blockers,
    )
    await _add_plan_items(repo, request_id, plan)
    job = await _enqueue_job(
        session,
        request_id=request_id,
        requested_by=requested_by,
    )
    row.job_id = job.job_id
    await session.flush()
    return BackfillSubmitResult(request=row, plan=plan, created=True, reused=False)


async def retry_backfill(
    session: AsyncSession,
    request_id: str,
    *,
    requested_by: str,
    actor_type: ResearchActorType,
    idempotency_key: str | None = None,
    capabilities: BackfillCapabilities | None = None,
) -> BackfillSubmitResult:
    """只为失败/取消的剩余项新建请求(``retry_of_request_id`` 链)。"""

    repo = DataBackfillRepository(session)
    source = await repo.get(request_id)
    if source is None:
        raise BackfillRequestError(
            "backfill_not_found", f"补数请求不存在: {request_id}"
        )
    if source.status not in _RETRYABLE_REQUEST_STATUSES:
        raise BackfillRequestError(
            "backfill_not_retryable",
            f"补数请求 {request_id} 状态 {source.status} 不可重试"
            f"(仅 {sorted(_RETRYABLE_REQUEST_STATUSES)})",
        )
    frozen = BackfillRequestSpec.model_validate(source.spec)
    remaining = await _remaining_spec(repo, source, frozen)
    if remaining is None:
        raise BackfillRequestError(
            "backfill_nothing_to_retry",
            f"补数请求 {request_id} 没有失败/取消的剩余项",
        )
    return await submit_backfill(
        session,
        remaining,
        # 默认键每次唯一:blocked/failed 请求重试要**重新算 plan**,固定键会
        # 命中上一次重试生成的行(仍是 blocked)而直接返回旧快照。
        idempotency_key=(
            idempotency_key
            or f"retry:{request_id}:{uuid.uuid4().hex[:12].upper()}"
        ),
        requested_by=requested_by,
        actor_type=actor_type,
        capabilities=capabilities,
        retry_of_request_id=request_id,
    )


async def cancel_backfill(
    session: AsyncSession,
    request_id: str,
    *,
    requested_by: str = "api",
) -> DataBackfillRequestModel:
    """协作式取消:job 走既有取消语义,未完成 item 标 ``cancelled``。

    已成功的 item 与其发布**不回滚**(发布不可变);运行中的 item 由执行器在
    下一次 ``progress`` 检查点感知取消后自行收敛。
    """

    repo = DataBackfillRepository(session)
    row = await repo.get(request_id)
    if row is None:
        raise BackfillRequestError("backfill_not_found", f"补数请求不存在: {request_id}")
    if row.status == BackfillRequestStatus.CANCELLED.value:
        # 幂等:重复取消返回当前行(不报错),执行器可能还在收敛。
        return row
    if row.status in _TERMINAL_REQUEST_STATUSES:
        raise BackfillRequestError(
            "backfill_not_cancellable",
            f"补数请求 {request_id} 状态 {row.status} 已终态,不支持取消",
        )
    job_state = "missing"
    if row.job_id:
        job_repo = BackgroundJobRepository(session)
        job = await job_repo.get(row.job_id)
        if job is not None:
            job_state = job.status
            if job.status in (
                BackgroundJobStatus.QUEUED.value,
                BackgroundJobStatus.RETRY_WAITING.value,
            ):
                # 还没被领取:直接取消 job,请求同步收敛为 cancelled。
                await job_repo.request_cancel(job.job_id)
                job_state = BackgroundJobStatus.CANCELLED.value
            elif job.status == BackgroundJobStatus.RUNNING.value:
                # 协作式取消:执行器在下一次 progress 检查点感知并自行收敛
                # (请求/明细由执行器标 cancelled),这里不抢跑状态机。
                await job_repo.request_cancel(job.job_id)
    await repo.mark_cancelled_items(request_id)
    if job_state not in (BackgroundJobStatus.RUNNING.value, "running"):
        row = await repo.set_request_status(
            request_id,
            status=BackfillRequestStatus.CANCELLED.value,
            error_code="backfill_cancelled",
            error_summary=f"由 {requested_by} 取消",
            finished=True,
            expected=(
                BackfillRequestStatus.QUEUED.value,
                BackfillRequestStatus.RUNNING.value,
            ),
        ) or row
    assert row is not None  # set_request_status 只在并发删除时返回 None
    return row


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _freeze_spec(
    spec: BackfillRequestSpec, plan: BackfillPlan
) -> BackfillRequestSpec:
    """把预检解析出的 source/adjustment/窗口/标的写回冻结 spec。

    提交后的 spec 是执行与重试的唯一依据:来源不能留 ``None``(否则重试时
    无法与明细行的解析来源对齐),窗口/标的也用预检落位后的值。
    """

    datasets: list[BackfillDatasetSpec] = []
    for item in plan.items:
        # 冻结 spec 的 adjustment 是 Literal 三值之一(planner 已收敛)。
        adjustment: Literal["qfq", "hqfq", "none"] = (
            "qfq"
            if item.adjustment == "qfq"
            else "hqfq"
            if item.adjustment == "hqfq"
            else "none"
        )
        datasets.append(
            BackfillDatasetSpec(
                dataset=item.dataset,
                source=item.source,
                adjustment=adjustment,
                start=item.start,
                end=item.end,
                symbols=item.symbols,
                publish=item.publish,
            )
        )
    return BackfillRequestSpec(
        scope=spec.scope,
        datasets=tuple(datasets),
        quality_profile=spec.quality_profile,
    )


async def _serialize_submit(
    session: AsyncSession, fingerprint: str, idempotency_key: str
) -> None:
    """把「查重 + 建请求」串行化(issue #511 审核 B8:并发提交)。

    两个并发提交可能在同一事务快照里都看不到对方,各自通过「无幂等键命中 /
    无在途 fingerprint」检查后双建请求。这里按 fingerprint 与幂等键取事务级
    advisory lock(固定顺序防死锁),第二个提交等锁后重新查重即命中复用。
    非 PostgreSQL 方言(测试 sqlite)跳过。
    """

    if session.get_bind().dialect.name != "postgresql":
        return
    from sqlalchemy import text

    for key in (f"backfill:{fingerprint}", f"backfill-idem:{idempotency_key}"):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key), 0)"), {"key": key}
        )


async def _create_request(
    repo: DataBackfillRepository, **values: Any
) -> DataBackfillRequestModel:
    """创建请求头,把并发撞幂等键映射为具名冲突(而不是裸 IntegrityError)."""

    try:
        return await repo.create_request(**values)
    except IntegrityError as exc:
        raise BackfillRequestError(
            "backfill_idempotency_conflict",
            "同一幂等键的补数请求正在并发创建,请重试或换 Idempotency-Key",
        ) from exc


def _payload_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue_job(
    session: AsyncSession, *, request_id: str, requested_by: str
) -> Any:
    """同事务登记 ``kind=data_backfill`` 任务(复用既有队列幂等语义)。"""

    payload: dict[str, object] = {"request_id": request_id}
    try:
        row, _created = await BackgroundJobRepository(session).create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=f"backfill:{request_id}",
            kind="data_backfill",
            queue="data",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=_payload_checksum(payload),
            max_attempts=3,
            requested_by=requested_by,
        )
    except BackgroundJobPersistenceConflictError as exc:  # pragma: no cover - 极少并发
        raise BackfillRequestError("backfill_job_conflict", str(exc)) from exc
    return row


async def _add_plan_items(
    repo: DataBackfillRepository, request_id: str, plan: BackfillPlan
) -> None:
    """把预检项落成明细(noop → succeeded,reuse → reused,其余 pending)。"""

    items: list[dict[str, Any]] = []
    for item in plan.items:
        if item.action is BackfillAction.NOOP:
            status = BackfillItemStatus.SUCCEEDED.value
        elif item.action is BackfillAction.REUSE:
            status = BackfillItemStatus.REUSED.value
        else:
            status = BackfillItemStatus.PENDING.value
        items.append(
            {
                "dataset": item.dataset.value,
                "source": item.source,
                "adjustment": item.adjustment,
                "action": item.action.value,
                "start_date": item.start,
                "end_date": item.end,
                "symbols": list(item.symbols),
                "universe_filters": dict(item.universe_filters),
                "missing_ranges": [
                    [start.isoformat(), end.isoformat()]
                    for start, end in item.missing_ranges
                ],
                "repair_symbols": list(item.repair_symbols),
                "status": status,
                "estimated_requests": item.estimated_requests,
                "release_id": None,
            }
        )
    try:
        await repo.add_items(request_id, items)
    except IntegrityError as exc:
        # item 唯一身份(request_id, dataset, source, adjustment, 窗口)冲突:
        # 规范化后本不应发生(预检已按身份 diff),出现即并发/契约异常,
        # 映射为 409 而不是裸 500。
        raise BackfillRequestError(
            "backfill_item_conflict",
            "补数明细身份冲突(同一 dataset/source/adjustment/窗口重复),"
            "请重新预览后再提交",
        ) from exc


async def _remaining_spec(
    repo: DataBackfillRepository,
    source: DataBackfillRequestModel,
    frozen: BackfillRequestSpec,
) -> BackfillRequestSpec | None:
    """把原请求收窄成「失败/取消项」的 spec(重试用)。"""

    # 同一 dataset 可有多项(不同 source / adjustment),必须按完整身份取行,
    # 否则后一项会覆盖前一项,重试丢项或重跑已成功项。落库 spec 已冻结解析后
    # 的 source/adjustment,这里做**精确匹配**(不再按 dataset 猜来源)。
    rows = {
        (row.dataset, row.source, row.adjustment): row
        for row in await repo.list_items(source.request_id)
    }
    kept: list[BackfillDatasetSpec] = []
    for item in frozen.ordered_datasets():
        row = rows.get((item.dataset.value, item.source or "", item.adjustment))
        if row is None:
            # 身份对不上(理论上只出现在旧 plan):整项保留,宁可重跑也不丢项。
            kept.append(item)
            continue
        if row.status in (
            BackfillItemStatus.FAILED.value,
            BackfillItemStatus.CANCELLED.value,
            BackfillItemStatus.PENDING.value,
            # 运行中(进程崩溃/外部领取)也属于「未完成」,重试必须带上。
            BackfillItemStatus.RUNNING.value,
        ):
            kept.append(item)
    if not kept:
        return None
    return BackfillRequestSpec(
        scope=frozen.scope,
        datasets=tuple(kept),
        quality_profile=frozen.quality_profile,
    )


__all__ = [
    "BackfillRequestError",
    "BackfillSubmitResult",
    "cancel_backfill",
    "retry_backfill",
    "submit_backfill",
]
