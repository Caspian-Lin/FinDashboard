"""AI 研究助手路由(issue #84)。

提供:

* 因子假设审批闭环(提交 / 审批 / 拒绝 / 实验登记 / 完成绑定 #57 机器验证)
* AI 草案生成(假设 / 策略组件 / 策略 diff)+ 持久化与审批
* 上下文式金融问答(引用来源 / 不确定性)
* 审计查询

红线:
* AI 只读研究上下文,写入草案 / 解释 / 假设;
* ``complete_experiment`` 从持久化的 #57 实验读取结果,不接受伪造 ``passed_oos``;
* 不提供 Python 代码编辑或执行入口;
* AI 永远不位于订单执行链路中。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session, get_research_assistant
from finboard_backtest.factor_research import (
    AIDegradedError,
    AnswerResult,
    DraftArtifact,
    DraftKind,
    DraftStatus,
    ExperimentRegistration,
    FactorHypothesis,
    HypothesisStatus,
    MachineValidationOutcome,
    ParameterSpec,
    PermissionDeniedError,
    Provenance,
    Reference,
    ResearchAssistant,
    StrategyDiffPayload,
    StrategyDraftPayload,
    UncertaintyLevel,
    hypothesis_to_draft_payload,
)
from finboard_backtest.validation.contracts import ExperimentStatus
from finboard_persistence import (
    AIDraftRepository,
    HypothesisWorkflowService,
    ResearchExperimentRepository,
)

router = APIRouter(prefix="/api/research/ai", tags=["ai-research"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class _ProvenanceOut(BaseModel):
    provider: str
    model_version: str
    prompt_version: str
    generated_at: datetime
    latency_ms: int = 0
    request_checksum: str = ""


class ParameterSpecSchema(BaseModel):
    name: str
    min_value: float
    max_value: float
    grid_size: int = 1


class ReferenceSchema(BaseModel):
    title: str
    authors: str = ""
    year: int | None = None
    url: str | None = None
    doi: str | None = None


class HypothesisCreate(BaseModel):
    name: str
    economic_mechanism: str
    input_fields: list[str]
    decision_timing: str
    formula: str
    direction: str
    applicable_assets: list[str] = Field(default_factory=list)
    expected_failure_scenarios: list[str] = Field(default_factory=list)
    parameters: list[ParameterSpecSchema] = Field(default_factory=list)
    references: list[ReferenceSchema] = Field(default_factory=list)
    actor: str = "system"


class HypothesisOut(BaseModel):
    hypothesis_id: str
    name: str
    economic_mechanism: str
    input_fields: list[str]
    decision_timing: str
    formula: str
    direction: str
    status: str
    version: str
    experiment_count: int
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime


class ApproveIn(BaseModel):
    approver: str


class RejectIn(BaseModel):
    approver: str
    reason: str


class ExperimentRegisterIn(BaseModel):
    model_version: str
    prompt_version: str
    dataset_version: str
    code_version: str
    registered_by: str
    validation_experiment_id: str | None = None


class ExperimentOut(BaseModel):
    experiment_id: str
    hypothesis_id: str
    model_version: str
    prompt_version: str
    dataset_version: str
    code_version: str
    registered_at: datetime
    registered_by: str
    status: str
    validation_experiment_id: str | None = None


class ExperimentCompleteIn(BaseModel):
    validation_experiment_id: str
    completed_by: str


class ExperimentInterruptIn(BaseModel):
    reason: str
    actor: str


class AskIn(BaseModel):
    question: str


class CitationOut(BaseModel):
    source_type: str
    title: str
    locator: str = ""
    snippet: str = ""


class AnswerOut(BaseModel):
    question: str
    answer: str
    technical_detail: str = ""
    citations: list[CitationOut]
    uncertainty: str
    data_sufficient: bool
    disclaimer: str = ""
    provenance: _ProvenanceOut
    draft_id: str


class DraftOut(BaseModel):
    draft_id: str
    kind: str
    provenance: _ProvenanceOut
    status: str
    payload: dict[str, Any]
    uncertainty: str
    references: list[ReferenceSchema] = Field(default_factory=list)
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    consumed_ref: str | None = None


class DraftApproveIn(BaseModel):
    approver: str


class DraftRejectIn(BaseModel):
    approver: str
    reason: str


class DraftConsumeIn(BaseModel):
    approver: str
    consumed_ref: str


class AuditEventOut(BaseModel):
    timestamp: datetime
    event_type: str
    hypothesis_id: str
    actor: str
    details: list[tuple[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 转换辅助
# ---------------------------------------------------------------------------


def _provenance_out(p: Provenance) -> _ProvenanceOut:
    return _ProvenanceOut(
        provider=p.provider,
        model_version=p.model_version,
        prompt_version=p.prompt_version,
        generated_at=p.generated_at,
        latency_ms=p.latency_ms,
        request_checksum=p.request_checksum,
    )


def _hypothesis_out(h: FactorHypothesis) -> HypothesisOut:
    return HypothesisOut(
        hypothesis_id=h.hypothesis_id,
        name=h.name,
        economic_mechanism=h.economic_mechanism,
        input_fields=list(h.input_fields),
        decision_timing=h.decision_timing,
        formula=h.formula,
        direction=h.direction,
        status=h.status.value,
        version=h.version,
        experiment_count=h.experiment_count,
        approved_by=h.approved_by,
        approved_at=h.approved_at,
        rejection_reason=h.rejection_reason,
        created_at=h.created_at,
    )


def _experiment_out(e: ExperimentRegistration) -> ExperimentOut:
    return ExperimentOut(
        experiment_id=e.experiment_id,
        hypothesis_id=e.hypothesis_id,
        model_version=e.model_version,
        prompt_version=e.prompt_version,
        dataset_version=e.dataset_version,
        code_version=e.code_version,
        registered_at=e.registered_at,
        registered_by=e.registered_by,
        status=e.status,
        validation_experiment_id=e.validation_experiment_id,
    )


def _draft_out(d: DraftArtifact) -> DraftOut:
    return DraftOut(
        draft_id=d.draft_id,
        kind=d.kind.value,
        provenance=_provenance_out(d.provenance),
        status=d.status.value,
        payload=d.payload,
        uncertainty=d.uncertainty.value,
        references=[
            ReferenceSchema(
                title=r.title,
                authors=r.authors,
                year=r.year,
                url=r.url,
                doi=r.doi,
            )
            for r in d.references
        ],
        created_at=d.created_at,
        approved_by=d.approved_by,
        approved_at=d.approved_at,
        rejection_reason=d.rejection_reason,
        consumed_ref=d.consumed_ref,
    )


def _build_hypothesis(body: HypothesisCreate) -> FactorHypothesis:
    return FactorHypothesis(
        name=body.name,
        economic_mechanism=body.economic_mechanism,
        input_fields=tuple(body.input_fields),
        decision_timing=body.decision_timing,
        formula=body.formula,
        direction=body.direction,
        applicable_assets=tuple(body.applicable_assets),
        expected_failure_scenarios=tuple(body.expected_failure_scenarios),
        parameters=tuple(
            ParameterSpec(
                name=p.name,
                min_value=p.min_value,
                max_value=p.max_value,
                grid_size=p.grid_size,
            )
            for p in body.parameters
        ),
        references=tuple(
            Reference(
                title=r.title,
                authors=r.authors,
                year=r.year,
                url=r.url,
                doi=r.doi,
            )
            for r in body.references
        ),
    )


async def _run_assistant(coro_factory: Any) -> Any:
    """在线程中运行同步 ResearchAssistant 调用,避免阻塞事件循环。"""
    return await asyncio.to_thread(coro_factory)


# ---------------------------------------------------------------------------
# 假设审批闭环
# ---------------------------------------------------------------------------


@router.post("/hypotheses", response_model=HypothesisOut, status_code=201)
async def create_hypothesis(
    body: HypothesisCreate,
    session: AsyncSession = Depends(get_db_session),
) -> HypothesisOut:
    service = HypothesisWorkflowService(session)
    hypothesis = _build_hypothesis(body)
    try:
        result = await service.submit(hypothesis, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _hypothesis_out(result)


@router.get("/hypotheses", response_model=list[HypothesisOut])
async def list_hypotheses(
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[HypothesisOut]:
    service = HypothesisWorkflowService(session)
    target = HypothesisStatus(status) if status else None
    items = await service.list_hypotheses(target)
    return [_hypothesis_out(h) for h in items]


@router.get("/hypotheses/{hypothesis_id}", response_model=HypothesisOut)
async def get_hypothesis(
    hypothesis_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> HypothesisOut:
    service = HypothesisWorkflowService(session)
    h = await service.get_hypothesis(hypothesis_id)
    if h is None:
        raise HTTPException(status_code=404, detail="假设不存在")
    return _hypothesis_out(h)


@router.post("/hypotheses/{hypothesis_id}/approve", response_model=HypothesisOut)
async def approve_hypothesis(
    hypothesis_id: str,
    body: ApproveIn,
    session: AsyncSession = Depends(get_db_session),
) -> HypothesisOut:
    service = HypothesisWorkflowService(session)
    try:
        result = await service.approve(hypothesis_id, body.approver)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _hypothesis_out(result)


@router.post("/hypotheses/{hypothesis_id}/reject", response_model=HypothesisOut)
async def reject_hypothesis(
    hypothesis_id: str,
    body: RejectIn,
    session: AsyncSession = Depends(get_db_session),
) -> HypothesisOut:
    service = HypothesisWorkflowService(session)
    try:
        result = await service.reject(hypothesis_id, body.approver, body.reason)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _hypothesis_out(result)


# ---------------------------------------------------------------------------
# 实验登记 + 完成绑定 #57
# ---------------------------------------------------------------------------


@router.post(
    "/hypotheses/{hypothesis_id}/experiments",
    response_model=ExperimentOut,
    status_code=201,
)
async def register_experiment(
    hypothesis_id: str,
    body: ExperimentRegisterIn,
    session: AsyncSession = Depends(get_db_session),
) -> ExperimentOut:
    service = HypothesisWorkflowService(session)
    try:
        reg = await service.register_experiment(
            hypothesis_id,
            model_version=body.model_version,
            prompt_version=body.prompt_version,
            dataset_version=body.dataset_version,
            code_version=body.code_version,
            registered_by=body.registered_by,
            validation_experiment_id=body.validation_experiment_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _experiment_out(reg)


@router.post("/experiments/{experiment_id}/complete", response_model=HypothesisOut)
async def complete_experiment(
    experiment_id: str,
    body: ExperimentCompleteIn,
    session: AsyncSession = Depends(get_db_session),
) -> HypothesisOut:
    """完成实验 —— 从持久化的 #57 机器验证实验读取结果,不接受伪造 passed_oos。"""
    validation = await ResearchExperimentRepository(session).get(
        body.validation_experiment_id
    )
    if validation is None:
        raise HTTPException(
            status_code=404,
            detail=f"#57 验证实验不存在: {body.validation_experiment_id}",
        )
    if validation.status not in (
        ExperimentStatus.VALIDATED_OOS,
        ExperimentStatus.REJECTED,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"#57 验证实验未完成(状态 {validation.status.value}),"
                " 不能据此完成假设实验"
            ),
        )
    outcome = MachineValidationOutcome(
        validation_experiment_id=validation.experiment_id,
        status=validation.status.value,
        trials_used=validation.trials_used,
        rejection_reason=validation.rejection_reason or "",
    )
    service = HypothesisWorkflowService(session)
    try:
        result = await service.complete_experiment(
            experiment_id, validation=outcome, completed_by=body.completed_by
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _hypothesis_out(result)


@router.post(
    "/experiments/{experiment_id}/interrupt", response_model=ExperimentOut
)
async def interrupt_experiment(
    experiment_id: str,
    body: ExperimentInterruptIn,
    session: AsyncSession = Depends(get_db_session),
) -> ExperimentOut:
    service = HypothesisWorkflowService(session)
    try:
        reg = await service.interrupt_experiment(
            experiment_id, body.reason, body.actor
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _experiment_out(reg)


@router.get("/experiments", response_model=list[ExperimentOut])
async def list_experiments(
    hypothesis_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExperimentOut]:
    service = HypothesisWorkflowService(session)
    items = await service.list_experiments(hypothesis_id)
    return [_experiment_out(e) for e in items]


# ---------------------------------------------------------------------------
# AI 草案生成
# ---------------------------------------------------------------------------


def _assistant_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AIDegradedError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/drafts/hypothesis", response_model=DraftOut, status_code=201)
async def generate_hypothesis_draft(
    body: AskIn,
    session: AsyncSession = Depends(get_db_session),
    assistant: ResearchAssistant = Depends(get_research_assistant),
) -> DraftOut:
    try:
        response = await asyncio.to_thread(assistant.propose_hypothesis, body.question)
    except (PermissionDeniedError, AIDegradedError) as exc:
        raise _assistant_error(exc) from exc
    hypothesis, provenance = response.as_tuple()
    draft = DraftArtifact(
        draft_id=f"ai-draft-hypo-{hypothesis.hypothesis_id}",
        kind=DraftKind.HYPOTHESIS,
        provenance=provenance,
        status=DraftStatus.PROPOSED,
        payload=hypothesis_to_draft_payload(hypothesis),
        uncertainty=UncertaintyLevel.MEDIUM,
        references=hypothesis.references,
    )
    await AIDraftRepository(session).save(draft)
    await session.commit()
    return _draft_out(draft)


@router.post(
    "/drafts/strategy-component", response_model=DraftOut, status_code=201
)
async def generate_strategy_draft(
    body: AskIn,
    session: AsyncSession = Depends(get_db_session),
    assistant: ResearchAssistant = Depends(get_research_assistant),
) -> DraftOut:
    try:
        response = await asyncio.to_thread(
            assistant.propose_strategy_draft, body.question
        )
    except (PermissionDeniedError, AIDegradedError) as exc:
        raise _assistant_error(exc) from exc
    draft_payload: StrategyDraftPayload
    draft_payload, provenance = response.as_tuple()
    draft = DraftArtifact(
        draft_id=f"ai-draft-comp-{provenance.request_checksum or 'x'}",
        kind=DraftKind.STRATEGY_COMPONENT,
        provenance=provenance,
        status=DraftStatus.PROPOSED,
        payload=draft_payload.as_dict(),
        uncertainty=UncertaintyLevel.MEDIUM,
    )
    await AIDraftRepository(session).save(draft)
    await session.commit()
    return _draft_out(draft)


@router.post("/drafts/strategy-diff", response_model=DraftOut, status_code=201)
async def generate_strategy_diff(
    body: AskIn,
    session: AsyncSession = Depends(get_db_session),
    assistant: ResearchAssistant = Depends(get_research_assistant),
) -> DraftOut:
    try:
        response = await asyncio.to_thread(
            assistant.propose_strategy_diff, body.question
        )
    except (PermissionDeniedError, AIDegradedError) as exc:
        raise _assistant_error(exc) from exc
    diff_payload: StrategyDiffPayload
    diff_payload, provenance = response.as_tuple()
    draft = DraftArtifact(
        draft_id=f"ai-draft-diff-{provenance.request_checksum or 'x'}",
        kind=DraftKind.STRATEGY_DIFF,
        provenance=provenance,
        status=DraftStatus.PROPOSED,
        payload=diff_payload.as_dict(),
        uncertainty=UncertaintyLevel.MEDIUM,
    )
    await AIDraftRepository(session).save(draft)
    await session.commit()
    return _draft_out(draft)


@router.post("/ask", response_model=AnswerOut)
async def ask_question(
    body: AskIn,
    session: AsyncSession = Depends(get_db_session),
    assistant: ResearchAssistant = Depends(get_research_assistant),
) -> AnswerOut:
    try:
        response = await asyncio.to_thread(assistant.ask, body.question)
    except (PermissionDeniedError, AIDegradedError) as exc:
        raise _assistant_error(exc) from exc
    answer: AnswerResult
    answer, provenance = response.as_tuple()
    draft = DraftArtifact(
        draft_id=f"ai-draft-answer-{provenance.request_checksum or 'x'}",
        kind=DraftKind.ANSWER,
        provenance=provenance,
        status=DraftStatus.PROPOSED,
        payload=answer.as_dict(),
        uncertainty=answer.uncertainty,
    )
    await AIDraftRepository(session).save(draft)
    await session.commit()
    return AnswerOut(
        question=answer.question,
        answer=answer.answer,
        technical_detail=answer.technical_detail,
        citations=[
            CitationOut(
                source_type=c.source_type.value,
                title=c.title,
                locator=c.locator,
                snippet=c.snippet,
            )
            for c in answer.citations
        ],
        uncertainty=answer.uncertainty.value,
        data_sufficient=answer.data_sufficient,
        disclaimer=answer.disclaimer,
        provenance=_provenance_out(provenance),
        draft_id=draft.draft_id,
    )


# ---------------------------------------------------------------------------
# 草案审批
# ---------------------------------------------------------------------------


@router.get("/drafts", response_model=list[DraftOut])
async def list_drafts(
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[DraftOut]:
    repo = AIDraftRepository(session)
    target_kind = DraftKind(kind) if kind else None
    target_status = DraftStatus(status) if status else None
    items = await repo.list_by_kind_status(target_kind, target_status)
    return [_draft_out(d) for d in items]


@router.get("/drafts/{draft_id}", response_model=DraftOut)
async def get_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    repo = AIDraftRepository(session)
    draft = await repo.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草案不存在")
    return _draft_out(draft)


@router.post("/drafts/{draft_id}/approve", response_model=DraftOut)
async def approve_draft(
    draft_id: str,
    body: DraftApproveIn,
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    repo = AIDraftRepository(session)
    draft = await repo.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草案不存在")
    try:
        approved = draft.with_status(
            DraftStatus.APPROVED,
            approved_by=body.approver,
            approved_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await repo.save(approved)
    await session.commit()
    return _draft_out(approved)


@router.post("/drafts/{draft_id}/reject", response_model=DraftOut)
async def reject_draft(
    draft_id: str,
    body: DraftRejectIn,
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    repo = AIDraftRepository(session)
    draft = await repo.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草案不存在")
    try:
        rejected = draft.with_status(
            DraftStatus.REJECTED, rejection_reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await repo.save(rejected)
    await session.commit()
    return _draft_out(rejected)


@router.post("/drafts/{draft_id}/consume", response_model=DraftOut)
async def consume_draft(
    draft_id: str,
    body: DraftConsumeIn,
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    repo = AIDraftRepository(session)
    draft = await repo.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草案不存在")
    try:
        consumed = draft.with_status(
            DraftStatus.CONSUMED, consumed_ref=body.consumed_ref
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await repo.save(consumed)
    await session.commit()
    return _draft_out(consumed)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=list[AuditEventOut])
async def list_audit(
    hypothesis_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[AuditEventOut]:
    service = HypothesisWorkflowService(session)
    entries = await service.list_audit(hypothesis_id)
    return [
        AuditEventOut(
            timestamp=e.timestamp,
            event_type=e.event_type.value,
            hypothesis_id=e.hypothesis_id,
            actor=e.actor,
            details=list(e.details),
        )
        for e in entries
    ]
