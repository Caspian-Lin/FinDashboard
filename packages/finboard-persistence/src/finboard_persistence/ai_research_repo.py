"""AI 研究助手 / 因子假设持久化仓储(issue #84)。

为 :class:`FactorHypothesis`、实验登记、AI 草案和审计事件建立 PostgreSQL
Repository。DB 是真实来源;重启后通过 :meth:`HypothesisWorkflowService.reload`
重建内存 :class:`ResearchWorkflow`。

红线:
* 失败 / 被拒绝的假设与草案同样保留,不得丢失;
* ``validated_oos`` 只能由绑定的 #57 ``research_experiments`` 机器结果决定
  (见 :meth:`HypothesisWorkflowService.complete_experiment`);
* 不写入实盘 ``orders`` / ``fills`` / ``positions`` / ``audit_logs``。

Repository 模式与交易域一致:不控制事务边界,commit 由调用方决定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.factor_research.ai_contracts import (
    DraftArtifact,
    DraftKind,
    DraftStatus,
    Provenance,
    UncertaintyLevel,
)
from finboard_backtest.factor_research.audit import AuditEntry, AuditEventType
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    HypothesisStatus,
    ParameterSpec,
    Reference,
)
from finboard_backtest.factor_research.workflow import (
    ExperimentRegistration,
    MachineValidationOutcome,
    ResearchWorkflow,
)
from finboard_persistence.models import (
    AIAuditEventModel,
    AIDraftModel,
    FactorHypothesisExperimentModel,
    FactorHypothesisModel,
)


class HypothesisPersistenceError(RuntimeError):
    """假设持久化错误(状态冲突 / 不存在)。"""


# ---------------------------------------------------------------------------
# FactorHypothesisRepository
# ---------------------------------------------------------------------------


class FactorHypothesisRepository:
    """:class:`FactorHypothesis` 持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, hypothesis: FactorHypothesis) -> FactorHypothesisModel:
        existing = await self._get_model(hypothesis.hypothesis_id)
        if existing is None:
            model = _hypothesis_to_model(hypothesis)
            self._session.add(model)
        else:
            _apply_hypothesis_to_model(existing, hypothesis)
            model = existing
        await self._session.flush()
        return model

    async def get(self, hypothesis_id: str) -> FactorHypothesis | None:
        model = await self._get_model(hypothesis_id)
        return _model_to_hypothesis(model) if model else None

    async def list_all(self) -> list[FactorHypothesis]:
        stmt = select(FactorHypothesisModel).order_by(
            FactorHypothesisModel.created_at.asc()
        )
        result = await self._session.execute(stmt)
        return [_model_to_hypothesis(m) for m in result.scalars()]

    async def list_by_status(self, status: HypothesisStatus) -> list[FactorHypothesis]:
        stmt = (
            select(FactorHypothesisModel)
            .where(FactorHypothesisModel.status == status.value)
            .order_by(FactorHypothesisModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return [_model_to_hypothesis(m) for m in result.scalars()]

    async def _get_model(self, hypothesis_id: str) -> FactorHypothesisModel | None:
        stmt = select(FactorHypothesisModel).where(
            FactorHypothesisModel.hypothesis_id == hypothesis_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# FactorHypothesisExperimentRepository
# ---------------------------------------------------------------------------


class FactorHypothesisExperimentRepository:
    """:class:`ExperimentRegistration` 持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, reg: ExperimentRegistration) -> FactorHypothesisExperimentModel:
        existing = await self._get_model(reg.experiment_id)
        if existing is None:
            model = _experiment_to_model(reg)
            self._session.add(model)
        else:
            _apply_experiment_to_model(existing, reg)
            model = existing
        await self._session.flush()
        return model

    async def get(self, experiment_id: str) -> ExperimentRegistration | None:
        model = await self._get_model(experiment_id)
        return _model_to_experiment(model) if model else None

    async def list_all(self) -> list[ExperimentRegistration]:
        stmt = select(FactorHypothesisExperimentModel).order_by(
            FactorHypothesisExperimentModel.registered_at.asc()
        )
        result = await self._session.execute(stmt)
        return [_model_to_experiment(m) for m in result.scalars()]

    async def list_by_hypothesis(
        self, hypothesis_id: str
    ) -> list[ExperimentRegistration]:
        stmt = (
            select(FactorHypothesisExperimentModel)
            .where(FactorHypothesisExperimentModel.hypothesis_id == hypothesis_id)
            .order_by(FactorHypothesisExperimentModel.registered_at.asc())
        )
        result = await self._session.execute(stmt)
        return [_model_to_experiment(m) for m in result.scalars()]

    async def _get_model(self, experiment_id: str) -> FactorHypothesisExperimentModel | None:
        stmt = select(FactorHypothesisExperimentModel).where(
            FactorHypothesisExperimentModel.experiment_id == experiment_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# AIDraftRepository
# ---------------------------------------------------------------------------


class AIDraftRepository:
    """:class:`DraftArtifact` 持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, draft: DraftArtifact) -> AIDraftModel:
        existing = await self._get_model(draft.draft_id)
        if existing is None:
            model = _draft_to_model(draft)
            self._session.add(model)
        else:
            _apply_draft_to_model(existing, draft)
            model = existing
        await self._session.flush()
        return model

    async def get(self, draft_id: str) -> DraftArtifact | None:
        model = await self._get_model(draft_id)
        return _model_to_draft(model) if model else None

    async def list_by_kind_status(
        self,
        kind: DraftKind | None = None,
        status: DraftStatus | None = None,
        *,
        limit: int = 100,
    ) -> list[DraftArtifact]:
        stmt = select(AIDraftModel).order_by(AIDraftModel.created_at.desc())
        if kind is not None:
            stmt = stmt.where(AIDraftModel.kind == kind.value)
        if status is not None:
            stmt = stmt.where(AIDraftModel.status == status.value)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_model_to_draft(m) for m in result.scalars()]

    async def _get_model(self, draft_id: str) -> AIDraftModel | None:
        stmt = select(AIDraftModel).where(AIDraftModel.draft_id == draft_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# AIAuditEventRepository
# ---------------------------------------------------------------------------


class AIAuditEventRepository:
    """AI / 假设审批审计事件仓储(只追加)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, entry: AuditEntry) -> AIAuditEventModel:
        model = AIAuditEventModel(
            event_type=entry.event_type.value,
            hypothesis_id=entry.hypothesis_id or None,
            draft_id=None,
            actor=entry.actor,
            details=dict(entry.details),
            timestamp=entry.timestamp,
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def append_many(self, entries: list[AuditEntry]) -> list[AIAuditEventModel]:
        models: list[AIAuditEventModel] = []
        for entry in entries:
            models.append(await self.append(entry))
        return models

    async def list_by_hypothesis(self, hypothesis_id: str) -> list[AuditEntry]:
        stmt = (
            select(AIAuditEventModel)
            .where(AIAuditEventModel.hypothesis_id == hypothesis_id)
            .order_by(AIAuditEventModel.timestamp.asc())
        )
        result = await self._session.execute(stmt)
        return [_model_to_audit(m) for m in result.scalars()]

    async def list_recent(self, *, limit: int = 100) -> list[AuditEntry]:
        stmt = (
            select(AIAuditEventModel)
            .order_by(AIAuditEventModel.timestamp.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        entries = [_model_to_audit(m) for m in result.scalars()]
        entries.reverse()
        return entries


# ---------------------------------------------------------------------------
# HypothesisWorkflowService —— DB 真实来源 + 重启恢复
# ---------------------------------------------------------------------------


class HypothesisWorkflowService:
    """以 PostgreSQL 为真实来源的因子假设工作流服务(issue #84)。

    每个操作:从 DB 重建内存 :class:`ResearchWorkflow` → 执行状态转换 →
    把结果(假设 / 实验 / 审计增量)写回 DB。

    重启恢复:进程重启后 DB 已持久化,``reload`` / 下一次操作自动重建。
    失败 / 被拒绝记录不丢失。

    ``complete_experiment`` 强制绑定 :class:`MachineValidationOutcome`
    (由调用方从持久化的 #57 实验读取),不接受 ``passed_oos: bool``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._hypo_repo = FactorHypothesisRepository(session)
        self._exp_repo = FactorHypothesisExperimentRepository(session)
        self._audit_repo = AIAuditEventRepository(session)

    async def _build_workflow(self) -> ResearchWorkflow:
        hypotheses = await self._hypo_repo.list_all()
        experiments = await self._exp_repo.list_all()
        return ResearchWorkflow.restore(hypotheses, experiments)

    async def _persist_workflow_delta(
        self,
        wf: ResearchWorkflow,
        audit_before: int,
        *,
        hypothesis: FactorHypothesis | None = None,
        experiment: ExperimentRegistration | None = None,
    ) -> None:
        if hypothesis is not None:
            await self._hypo_repo.save(hypothesis)
        if experiment is not None:
            await self._exp_repo.save(experiment)
        new_entries = wf.audit.get_all()[audit_before:]
        if new_entries:
            await self._audit_repo.append_many(new_entries)
        await self._session.flush()

    async def reload(self) -> ResearchWorkflow:
        """从 DB 重建内存工作流(重启恢复)。"""
        return await self._build_workflow()

    async def submit(
        self, hypothesis: FactorHypothesis, actor: str = "system"
    ) -> FactorHypothesis:
        wf = await self._build_workflow()
        before = len(wf.audit)
        result = wf.submit(hypothesis, actor)
        await self._persist_workflow_delta(wf, before, hypothesis=result)
        return result

    async def approve(self, hypothesis_id: str, approver: str) -> FactorHypothesis:
        wf = await self._build_workflow()
        before = len(wf.audit)
        result = wf.approve(hypothesis_id, approver)
        await self._persist_workflow_delta(wf, before, hypothesis=result)
        return result

    async def reject(
        self, hypothesis_id: str, approver: str, reason: str
    ) -> FactorHypothesis:
        wf = await self._build_workflow()
        before = len(wf.audit)
        result = wf.reject(hypothesis_id, approver, reason)
        await self._persist_workflow_delta(wf, before, hypothesis=result)
        return result

    async def register_experiment(
        self,
        hypothesis_id: str,
        *,
        model_version: str,
        prompt_version: str,
        dataset_version: str,
        code_version: str,
        registered_by: str,
        validation_experiment_id: str | None = None,
    ) -> ExperimentRegistration:
        wf = await self._build_workflow()
        before = len(wf.audit)
        reg = wf.register_experiment(
            hypothesis_id,
            model_version=model_version,
            prompt_version=prompt_version,
            dataset_version=dataset_version,
            code_version=code_version,
            registered_by=registered_by,
            validation_experiment_id=validation_experiment_id,
        )
        updated = wf.get_hypothesis(hypothesis_id)
        await self._persist_workflow_delta(
            wf,
            before,
            hypothesis=updated,
            experiment=reg,
        )
        return reg

    async def complete_experiment(
        self,
        experiment_id: str,
        *,
        validation: MachineValidationOutcome,
        completed_by: str,
    ) -> FactorHypothesis:
        """完成实验 —— ``validation`` 必须来自持久化的 #57 机器验证实验。

        红线: 不接受 ``passed_oos: bool``,不接受 API 请求体伪造。
        """
        wf = await self._build_workflow()
        before = len(wf.audit)
        result = wf.complete_experiment(
            experiment_id, validation=validation, completed_by=completed_by
        )
        reg = wf.get_experiment(experiment_id)
        await self._persist_workflow_delta(
            wf, before, hypothesis=result, experiment=reg
        )
        return result

    async def interrupt_experiment(
        self, experiment_id: str, reason: str, actor: str
    ) -> ExperimentRegistration:
        wf = await self._build_workflow()
        before = len(wf.audit)
        reg = wf.interrupt_experiment(experiment_id, reason, actor)
        await self._persist_workflow_delta(wf, before, experiment=reg)
        return reg

    async def supersede(
        self,
        old_id: str,
        new_hypothesis: FactorHypothesis,
        actor: str = "system",
    ) -> FactorHypothesis:
        wf = await self._build_workflow()
        before = len(wf.audit)
        result = wf.supersede(old_id, new_hypothesis, actor)
        old = wf.get_hypothesis(old_id)
        await self._persist_workflow_delta(
            wf, before, hypothesis=old
        )
        await self._hypo_repo.save(result)
        await self._session.flush()
        return result

    async def get_hypothesis(self, hypothesis_id: str) -> FactorHypothesis | None:
        return await self._hypo_repo.get(hypothesis_id)

    async def list_hypotheses(
        self, status: HypothesisStatus | None = None
    ) -> list[FactorHypothesis]:
        if status is None:
            return await self._hypo_repo.list_all()
        return await self._hypo_repo.list_by_status(status)

    async def list_experiments(
        self, hypothesis_id: str | None = None
    ) -> list[ExperimentRegistration]:
        if hypothesis_id is None:
            return await self._exp_repo.list_all()
        return await self._exp_repo.list_by_hypothesis(hypothesis_id)

    async def list_audit(self, hypothesis_id: str | None = None) -> list[AuditEntry]:
        if hypothesis_id is None:
            return await self._audit_repo.list_recent()
        return await self._audit_repo.list_by_hypothesis(hypothesis_id)


# ---------------------------------------------------------------------------
# 转换函数(ORM ↔ Domain)
# ---------------------------------------------------------------------------


def _hypothesis_to_model(h: FactorHypothesis) -> FactorHypothesisModel:
    return FactorHypothesisModel(
        hypothesis_id=h.hypothesis_id,
        name=h.name,
        economic_mechanism=h.economic_mechanism,
        input_fields=list(h.input_fields),
        decision_timing=h.decision_timing,
        formula=h.formula,
        direction=h.direction,
        applicable_assets=list(h.applicable_assets),
        expected_failure_scenarios=list(h.expected_failure_scenarios),
        parameters=[_parameter_to_dict(p) for p in h.parameters],
        references=[_reference_to_dict(r) for r in h.references],
        status=h.status.value,
        version=h.version,
        supersedes_id=h.supersedes_id,
        created_at=h.created_at,
        approved_by=h.approved_by,
        approved_at=h.approved_at,
        rejection_reason=h.rejection_reason,
        experiment_count=h.experiment_count,
        updated_at=datetime.now(UTC),
    )


def _apply_hypothesis_to_model(
    model: FactorHypothesisModel, h: FactorHypothesis
) -> None:
    model.name = h.name
    model.economic_mechanism = h.economic_mechanism
    model.input_fields = list(h.input_fields)
    model.decision_timing = h.decision_timing
    model.formula = h.formula
    model.direction = h.direction
    model.applicable_assets = list(h.applicable_assets)
    model.expected_failure_scenarios = list(h.expected_failure_scenarios)
    model.parameters = [_parameter_to_dict(p) for p in h.parameters]
    model.references = [_reference_to_dict(r) for r in h.references]
    model.status = h.status.value
    model.version = h.version
    model.supersedes_id = h.supersedes_id
    model.approved_by = h.approved_by
    model.approved_at = h.approved_at
    model.rejection_reason = h.rejection_reason
    model.experiment_count = h.experiment_count
    model.updated_at = datetime.now(UTC)


def _model_to_hypothesis(m: FactorHypothesisModel) -> FactorHypothesis:
    parameters = tuple(_parameter_from_dict(p) for p in (m.parameters or []))
    references = tuple(_reference_from_dict(r) for r in (m.references or []))
    return FactorHypothesis(
        name=m.name,
        economic_mechanism=m.economic_mechanism,
        input_fields=tuple(m.input_fields or ()),
        decision_timing=m.decision_timing,
        formula=m.formula,
        direction=m.direction,
        applicable_assets=tuple(m.applicable_assets or ()),
        expected_failure_scenarios=tuple(m.expected_failure_scenarios or ()),
        parameters=parameters,
        references=references,
        status=HypothesisStatus(m.status),
        hypothesis_id=m.hypothesis_id,
        version=m.version,
        supersedes_id=m.supersedes_id,
        created_at=m.created_at or datetime.now(UTC),
        approved_by=m.approved_by,
        approved_at=m.approved_at,
        rejection_reason=m.rejection_reason,
        experiment_count=m.experiment_count,
    )


def _parameter_to_dict(p: ParameterSpec) -> dict[str, Any]:
    return {
        "name": p.name,
        "min_value": p.min_value,
        "max_value": p.max_value,
        "grid_size": p.grid_size,
    }


def _parameter_from_dict(d: Any) -> ParameterSpec:
    return ParameterSpec(
        name=str(d["name"]),
        min_value=float(d["min_value"]),
        max_value=float(d["max_value"]),
        grid_size=int(d.get("grid_size", 1)),
    )


def _reference_to_dict(r: Reference) -> dict[str, Any]:
    return {
        "title": r.title,
        "authors": r.authors,
        "year": r.year,
        "url": r.url,
        "doi": r.doi,
    }


def _reference_from_dict(d: Any) -> Reference:
    return Reference(
        title=str(d["title"]),
        authors=str(d.get("authors", "")),
        year=int(d["year"]) if d.get("year") else None,
        url=d.get("url"),
        doi=d.get("doi"),
    )


def _experiment_to_model(e: ExperimentRegistration) -> FactorHypothesisExperimentModel:
    return FactorHypothesisExperimentModel(
        experiment_id=e.experiment_id,
        hypothesis_id=e.hypothesis_id,
        model_version=e.model_version,
        prompt_version=e.prompt_version,
        dataset_version=e.dataset_version,
        code_version=e.code_version,
        registered_at=e.registered_at,
        registered_by=e.registered_by,
        references=[_reference_to_dict(r) for r in e.references],
        status=e.status,
        validation_experiment_id=e.validation_experiment_id,
    )


def _apply_experiment_to_model(
    model: FactorHypothesisExperimentModel, e: ExperimentRegistration
) -> None:
    model.hypothesis_id = e.hypothesis_id
    model.model_version = e.model_version
    model.prompt_version = e.prompt_version
    model.dataset_version = e.dataset_version
    model.code_version = e.code_version
    model.registered_at = e.registered_at
    model.registered_by = e.registered_by
    model.references = [_reference_to_dict(r) for r in e.references]
    model.status = e.status
    model.validation_experiment_id = e.validation_experiment_id


def _model_to_experiment(m: FactorHypothesisExperimentModel) -> ExperimentRegistration:
    references = tuple(_reference_from_dict(r) for r in (m.references or []))
    return ExperimentRegistration(
        experiment_id=m.experiment_id,
        hypothesis_id=m.hypothesis_id,
        model_version=m.model_version,
        prompt_version=m.prompt_version,
        dataset_version=m.dataset_version,
        code_version=m.code_version,
        registered_at=m.registered_at or datetime.now(UTC),
        registered_by=m.registered_by,
        references=references,
        status=m.status,
        validation_experiment_id=m.validation_experiment_id,
    )


def _draft_to_model(d: DraftArtifact) -> AIDraftModel:
    return AIDraftModel(
        draft_id=d.draft_id,
        kind=d.kind.value,
        provenance=d.provenance.as_dict(),
        status=d.status.value,
        payload=d.payload,
        uncertainty=d.uncertainty.value,
        references=[
            {"title": r.title, "authors": r.authors, "year": r.year, "url": r.url}
            for r in d.references
        ],
        created_at=d.created_at,
        approved_by=d.approved_by,
        approved_at=d.approved_at,
        rejection_reason=d.rejection_reason,
        consumed_ref=d.consumed_ref,
        updated_at=datetime.now(UTC),
    )


def _apply_draft_to_model(model: AIDraftModel, d: DraftArtifact) -> None:
    model.kind = d.kind.value
    model.provenance = d.provenance.as_dict()
    model.status = d.status.value
    model.payload = d.payload
    model.uncertainty = d.uncertainty.value
    model.references = [
        {"title": r.title, "authors": r.authors, "year": r.year, "url": r.url}
        for r in d.references
    ]
    model.approved_by = d.approved_by
    model.approved_at = d.approved_at
    model.rejection_reason = d.rejection_reason
    model.consumed_ref = d.consumed_ref
    model.updated_at = datetime.now(UTC)


def _model_to_draft(m: AIDraftModel) -> DraftArtifact:
    references = tuple(
        Reference(
            title=str(r["title"]),
            authors=str(r.get("authors", "")),
            year=int(r["year"]) if r.get("year") else None,
            url=str(r["url"]) if r.get("url") else None,
        )
        for r in (cast("list[dict[str, Any]]", m.references or []))
    )
    provenance_data = m.provenance or {}
    provenance = _provenance_from_dict(provenance_data)
    return DraftArtifact(
        draft_id=m.draft_id,
        kind=DraftKind(m.kind),
        provenance=provenance,
        status=DraftStatus(m.status),
        payload=dict(m.payload or {}),
        uncertainty=UncertaintyLevel(m.uncertainty or "medium"),
        references=references,
        created_at=m.created_at or datetime.now(UTC),
        approved_by=m.approved_by,
        approved_at=m.approved_at,
        rejection_reason=m.rejection_reason,
        consumed_ref=m.consumed_ref,
    )


def _provenance_from_dict(d: Any) -> Provenance:
    generated_at = d.get("generated_at")
    if isinstance(generated_at, str):
        try:
            generated_at = datetime.fromisoformat(generated_at)
        except ValueError:
            generated_at = datetime.now(UTC)
    elif not isinstance(generated_at, datetime):
        generated_at = datetime.now(UTC)
    return Provenance(
        provider=str(d.get("provider", "unknown")),
        model_version=str(d.get("model_version", "unknown")),
        prompt_version=str(d.get("prompt_version", "unknown")),
        generated_at=generated_at,
        latency_ms=int(d.get("latency_ms", 0)),
        request_checksum=str(d.get("request_checksum", "")),
    )


def _model_to_audit(m: AIAuditEventModel) -> AuditEntry:
    try:
        event_type = AuditEventType(m.event_type)
    except ValueError:
        event_type = AuditEventType.VERSION_DRIFT
    return AuditEntry(
        timestamp=m.timestamp or datetime.now(UTC),
        event_type=event_type,
        hypothesis_id=m.hypothesis_id or "",
        actor=m.actor,
        details=tuple(
            sorted(
                (str(k), str(v))
                for k, v in cast("dict[str, Any]", m.details or {}).items()
            )
        ),
    )


__all__ = [
    "AIAuditEventRepository",
    "AIDraftRepository",
    "FactorHypothesisExperimentRepository",
    "FactorHypothesisRepository",
    "HypothesisPersistenceError",
    "HypothesisWorkflowService",
]
