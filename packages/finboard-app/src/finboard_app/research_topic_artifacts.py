"""Topic-scoped factor / sandbox / OOS actions and evidence links (#516).

This module is the **whitelist** behind ``POST /api/research/topics/{id}/actions``
and the ``finboard_topic_action`` MCP tool. It never accepts code: the only
executable action is ``code_run.execute``, and that one may solely name an
already-registered ``research_code_artifacts`` row (``artifact_id`` /
``(kind, name)``). Python source, module paths and expressions are not part of
any request model -- unknown fields are rejected by ``extra="forbid"``.

Invariants (mirroring #515):

* the topic must be ``active`` and the declared ``goal_version`` must equal the
  topic's current goal version (409 on drift);
* every action writes an immutable ``research_topic_links`` row plus one
  ``research_audit_events`` row inside the *same* transaction as the artifact /
  job it references (``record`` never commits; this module owns the single
  ``commit``);
* ``operation_id`` is the lost-receipt anchor: replaying it returns the original
  link/entity instead of creating a second artifact;
* ``run.submit`` / ``run.replay`` delegate to
  :mod:`finboard_app.research_topic_actions` (which owns its own link + audit +
  commit), so there is exactly one run-submission implementation;
* research domain only -- no live ``orders`` / ``fills`` / ``positions`` /
  ``audit_logs`` writes and no live execution ability.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.background_jobs import enqueue_job_core
from finboard_app.research_audit import ResearchAuditEvent, research_audit_recorder
from finboard_app.research_operation import (
    ResearchConflictError,
    ResearchInvalidInputError,
    ResearchNotFoundError,
    ResearchOperationContext,
    ResearchOperationError,
    ResearchPermissionDeniedError,
    ResearchPreconditionFailedError,
)
from finboard_app.research_topic_actions import (
    RESEARCH_RUN_ENTITY,
    _require_active_with_goal,
    _require_topic,
    _valid_operation_id,
    derive_link_state,
    retry_topic_run,
    start_topic_run,
)
from finboard_app.research_workspace import (
    WorkspaceInput,
    bounded_experiment_oos,
)
from finboard_backtest.research_code import is_promoted_artifact, promotion_status
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    increment_trials_used,
    new_experiment,
)
from finboard_data.factor_lab import (
    ArtifactIntegrityError,
    FactorExperimentPlan,
    new_factor_experiment,
)
from finboard_data.releases import DatasetReleaseError
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    FactorExperimentRepository,
    ResearchCodeArtifactRepository,
    ResearchExperimentRepository,
    ResearchTrialRepository,
)
from finboard_persistence.dataset_release_repo import (
    ResearchDatasetReleaseRepository,
)
from finboard_persistence.factor_lab_repo import FeatureSnapshotRepository
from finboard_persistence.models import (
    BackgroundJobModel,
    FactorExperimentModel,
    ResearchCodeRunModel,
    ResearchExperimentModel,
    ResearchTopicLinkModel,
)
from finboard_persistence.research_topic_link_repo import (
    ResearchTopicLinkConflictError,
    ResearchTopicLinkRepository,
)

#: Every whitelisted action -> the ``research_topic_links.entity_type`` it writes.
TOPIC_ACTION_ENTITY_TYPES: dict[str, str] = {
    "run.submit": RESEARCH_RUN_ENTITY,
    "run.replay": RESEARCH_RUN_ENTITY,
    "factor_experiment.create": "factor_experiment",
    "code_run.execute": "code_run",
    "validation_experiment.create": "validation_experiment",
    "validation_experiment.add_trial": "validation_experiment",
    "validation_experiment.unseal": "validation_experiment",
}

#: Required capability per action. Actions that start execution (a ResearchRun,
#: a sandbox container, the one-time OOS unseal) need ``research.run``; pure
#: metadata writes need ``research.write``. The Web channel is a loopback human
#: operator and carries no MCP scope (``ctx.scopes is None``).
TOPIC_ACTION_SCOPES: dict[str, str] = {
    "run.submit": "research.run",
    "run.replay": "research.run",
    "factor_experiment.create": "research.write",
    "code_run.execute": "research.run",
    "validation_experiment.create": "research.write",
    "validation_experiment.add_trial": "research.write",
    "validation_experiment.unseal": "research.run",
}

#: Link entity types whose live state is derived at read time (never stored).
ARTIFACT_ENTITY_TYPES: tuple[str, ...] = (
    "factor_experiment",
    "code_run",
    "validation_experiment",
)

#: The one request field that must never gain a code channel.
CODE_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "code",
    "source_code",
    "python",
    "module",
    "module_path",
    "expression",
    "expr",
    "script",
)

_TERMINAL_EXPERIMENT_STATUSES: frozenset[str] = frozenset(
    {
        ExperimentStatus.VALIDATED_OOS.value,
        ExperimentStatus.REJECTED.value,
        ExperimentStatus.SUPERSEDED.value,
    }
)


# --------------------------------------------------------------------------- #
# Request models (extra="forbid" => no code channel)
# --------------------------------------------------------------------------- #


class FactorExperimentPlanIn(WorkspaceInput):
    in_sample_start: date
    in_sample_end: date
    oos_start: date
    oos_end: date
    trial_budget: int = Field(gt=0, le=10000)
    benchmark_symbol: str = Field(min_length=1, max_length=32)
    transaction_cost_bps: float = Field(default=0.0, ge=0)
    quantiles: int = Field(default=5, ge=2, le=20)


class FactorExperimentCreateIn(WorkspaceInput):
    """Freeze one factor experiment registration (writes no execution)."""

    hypothesis: str = Field(min_length=10, max_length=2000)
    factor_names: list[str] = Field(min_length=1, max_length=32)
    dataset_release_id: str = Field(min_length=1, max_length=128)
    feature_snapshot_id: str = Field(min_length=1, max_length=64)
    plan: FactorExperimentPlanIn
    comparison_group: str = Field(min_length=1, max_length=64)
    validation_experiment_id: str | None = Field(default=None, max_length=32)


class CodeRunExecuteIn(WorkspaceInput):
    """Execute an **already registered** factor artifact in the sandbox.

    There is deliberately no code/module/path field: the artifact is resolved
    from the database and its commit is pinned server-side.
    """

    kind: Literal["factor"] = "factor"
    name: str | None = Field(default=None, min_length=1, max_length=64)
    artifact_id: str | None = Field(default=None, min_length=1, max_length=32)
    dataset_release_ids: list[str] = Field(min_length=1, max_length=16)
    decision_at: datetime
    symbols: list[str] | None = Field(default=None, max_length=64)
    params: dict[str, Any] | None = None

    @field_validator("dataset_release_ids")
    @classmethod
    def unique_releases(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("dataset_release_ids 不能重复")
        return value

    @field_validator("decision_at")
    @classmethod
    def aware_decision_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("decision_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> CodeRunExecuteIn:
        if bool(self.artifact_id) == bool(self.name):
            raise ValueError(
                "code_run.execute 必须且只能提供 artifact_id 或 name 之一(不能内联代码)"
            )
        return self


class ValidationExperimentCreateIn(WorkspaceInput):
    hypothesis: str = Field(min_length=10, max_length=4000)
    version_stamp: dict[str, Any]
    plan: dict[str, Any]
    thresholds: dict[str, Any] | None = None
    robustness: dict[str, Any] | None = None
    strategy_params_space: dict[str, Any] | None = None
    supersedes_id: str | None = Field(default=None, max_length=32)
    notes: str = Field(default="", max_length=4000)


class ValidationTrialIn(WorkspaceInput):
    parameters: dict[str, Any]
    status: Literal["candidate", "running", "selected", "rejected", "failed", "skipped"] = (
        "candidate"
    )
    failure_reason: str | None = Field(default=None, max_length=2000)


class ValidationUnsealIn(WorkspaceInput):
    """No payload: the one-time unseal is bounded by the experiment row itself."""

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    requested_by: str = Field(default="user:api", min_length=1, max_length=128)


# --------------------------------------------------------------------------- #
# Domain builders / delegations
# --------------------------------------------------------------------------- #


def _build_version_stamp(raw: dict[str, Any]) -> VersionStamp:
    try:
        return VersionStamp(
            matching_model_version=raw["matching_model_version"],
            asset_rules_version=raw["asset_rules_version"],
            factor_version=raw.get("factor_version"),
            dataset_versions=dict(raw.get("dataset_versions", {})),
            selection_config=dict(raw.get("selection_config", {})),
            strategy_kind=raw["strategy_kind"],
            code_artifact_id=raw.get("code_artifact_id"),
            code_artifact_name=raw.get("code_artifact_name"),
            code_kind=raw.get("code_kind"),
            code_commit=raw.get("code_commit"),
        )
    except (KeyError, TypeError) as exc:
        raise ResearchInvalidInputError(
            f"version_stamp 参数不完整: {exc}", code="invalid_version_stamp"
        ) from exc


def _build_validation_plan(raw: dict[str, Any]) -> ValidationPlan:
    try:
        return ValidationPlan(
            mode=ValidationMode(raw["mode"]),
            train_start=date.fromisoformat(str(raw["train_start"])),
            train_end=date.fromisoformat(str(raw["train_end"])),
            validation_start=date.fromisoformat(str(raw["validation_start"])),
            validation_end=date.fromisoformat(str(raw["validation_end"])),
            test_start=date.fromisoformat(str(raw["test_start"])),
            test_end=date.fromisoformat(str(raw["test_end"])),
            train_window_days=int(raw.get("train_window_days", 504)),
            test_window_days=int(raw.get("test_window_days", 63)),
            step_days=int(raw.get("step_days", 63)),
            trial_budget=int(raw.get("trial_budget", 50)),
            random_seed=int(raw.get("random_seed", 0)),
            benchmark_symbol=raw.get("benchmark_symbol"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchInvalidInputError(
            f"plan 参数非法: {exc}", code="invalid_validation_plan"
        ) from exc


def _build_thresholds(raw: dict[str, Any] | None) -> AcceptanceThresholds:
    if not raw:
        return AcceptanceThresholds()
    try:
        return AcceptanceThresholds(**raw)
    except (TypeError, ValueError) as exc:
        raise ResearchInvalidInputError(
            f"thresholds 参数非法: {exc}", code="invalid_thresholds"
        ) from exc


def _build_robustness(raw: dict[str, Any] | None) -> RobustnessPlan:
    if not raw:
        return RobustnessPlan()
    try:
        return RobustnessPlan(
            neighbourhood_steps=int(raw.get("neighbourhood_steps", 5)),
            neighbourhood_relative_step=float(raw.get("neighbourhood_relative_step", 0.1)),
            cost_multipliers=tuple(
                float(x) for x in raw.get("cost_multipliers", [1.0, 2.0, 3.0])
            ),
            slippage_stress_bps=tuple(
                float(x) for x in raw.get("slippage_stress_bps", [0.0, 5.0, 10.0, 20.0])
            ),
            execution_delay_bars=tuple(
                int(x) for x in raw.get("execution_delay_bars", [1, 2])
            ),
            stress_phases=tuple(
                raw.get("stress_phases", ["2018-Q4", "2020-Q1", "2022-Q1", "2024-Q1"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchInvalidInputError(
            f"robustness 参数非法: {exc}", code="invalid_robustness"
        ) from exc


async def _create_factor_experiment(
    session: AsyncSession, payload: dict[str, Any]
) -> dict[str, Any]:
    data = FactorExperimentCreateIn.model_validate(payload)
    try:
        release = await ResearchDatasetReleaseRepository(session).require_usable(
            data.dataset_release_id
        )
    except DatasetReleaseError as exc:
        raise ResearchPreconditionFailedError(
            f"研究数据发布不可用于因子实验: {exc}", code="release_not_usable"
        ) from exc
    snapshot = await FeatureSnapshotRepository(session).get(data.feature_snapshot_id)
    if snapshot is None:
        raise ResearchNotFoundError(
            f"特征快照不存在: {data.feature_snapshot_id}", code="feature_snapshot_not_found"
        )
    if snapshot.dataset_release_id != release.release_id:
        raise ResearchConflictError(
            "特征快照与数据发布不匹配", code="snapshot_release_mismatch"
        )
    plan = FactorExperimentPlan(
        in_sample_start=data.plan.in_sample_start,
        in_sample_end=data.plan.in_sample_end,
        oos_start=data.plan.oos_start,
        oos_end=data.plan.oos_end,
        trial_budget=data.plan.trial_budget,
        benchmark_symbol=data.plan.benchmark_symbol,
        transaction_cost_bps=data.plan.transaction_cost_bps,
        quantiles=data.plan.quantiles,
    )
    try:
        experiment = new_factor_experiment(
            hypothesis=data.hypothesis,
            factor_names=tuple(data.factor_names),
            dataset_release_id=release.release_id,
            dataset_release_checksum=release.release_checksum,
            feature_snapshot_id=snapshot.snapshot_id,
            plan=plan,
            comparison_group=data.comparison_group,
            validation_experiment_id=data.validation_experiment_id,
        )
        await FactorExperimentRepository(session).save(experiment)
    except (ArtifactIntegrityError, ValueError) as exc:
        raise ResearchInvalidInputError(
            f"因子实验登记失败: {exc}", code="invalid_factor_experiment"
        ) from exc
    return {
        "experiment_id": experiment.experiment_id,
        "status": experiment.status.value,
        "dataset_release_checksum": experiment.dataset_release_checksum,
        "feature_snapshot_id": experiment.feature_snapshot_id,
    }


async def _execute_code_run(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    ctx: ResearchOperationContext,
    sandbox_enabled: bool,
) -> dict[str, Any]:
    data = CodeRunExecuteIn.model_validate(payload)
    if not sandbox_enabled:
        raise ResearchPreconditionFailedError(
            "研究沙箱未启用(research_sandbox_enabled=false);"
            "启用前置:Docker Desktop 运行 + 构建 docker/research-sandbox 镜像",
            code="sandbox_disabled",
        )
    artifact_repo = ResearchCodeArtifactRepository(session)
    if data.artifact_id is not None:
        artifact = await artifact_repo.get(data.artifact_id)
        if artifact is None:
            raise ResearchNotFoundError(
                f"研究代码产物不存在: {data.artifact_id}", code="artifact_not_found"
            )
        if artifact.kind != data.kind:
            raise ResearchInvalidInputError(
                f"artifact 与 kind 不一致: {data.artifact_id}", code="artifact_kind_mismatch"
            )
        if artifact.status == "retired":
            raise ResearchConflictError(
                f"artifact 已 retired,不能启动新沙箱执行: {data.artifact_id}",
                code="artifact_retired",
            )
        if artifact.status == "active" and not is_promoted_artifact(artifact):
            raise ResearchPreconditionFailedError(
                f"artifact active 但未通过 screen+OOS 晋级门: {data.artifact_id} "
                f"promotion_status={promotion_status(artifact)}",
                code="artifact_not_promoted",
            )
        artifact_name = artifact.name
    else:
        artifact = await artifact_repo.get_active(kind=data.kind, name=str(data.name))
        if artifact is None:
            raise ResearchNotFoundError(
                f"没有 active+passed 的研究代码: kind={data.kind} name={data.name}"
                "(先经 MCP 提交并完成晋级)",
                code="artifact_not_found",
            )
        artifact_name = artifact.name
    release_repo = ResearchDatasetReleaseRepository(session)
    has_bars = False
    for release_id in data.dataset_release_ids:
        release = await release_repo.get(release_id)
        if release is None:
            raise ResearchNotFoundError(
                f"研究数据发布不存在: {release_id}", code="release_not_found"
            )
        if getattr(release.dataset_kind, "value", "") == "bars":
            has_bars = True
    if not has_bars:
        raise ResearchInvalidInputError(
            "dataset_release_ids 须至少包含一个 bars 类发布(挂载行情面)",
            code="bars_release_required",
        )
    job_payload: dict[str, Any] = {
        "kind": data.kind,
        "name": artifact_name,
        "commit": artifact.commit,
        "dataset_release_ids": list(data.dataset_release_ids),
        "decision_at": data.decision_at.isoformat(),
    }
    if data.artifact_id is not None:
        job_payload["artifact_id"] = artifact.artifact_id
    if data.symbols is not None:
        job_payload["symbols"] = list(data.symbols)
    if data.params is not None:
        job_payload["params"] = data.params
    idempotency_key = (
        f"research_code_run:{artifact.commit[:12]}:"
        f"{','.join(data.dataset_release_ids)}:{data.decision_at.isoformat()}"
    )
    try:
        job_row, created = await enqueue_job_core(
            session,
            kind="research_code_run",
            queue="default",
            idempotency_key=idempotency_key,
            payload=job_payload,
            requested_by=ctx.actor,
            max_attempts=1,
            context=ctx,
        )
    except BackgroundJobPersistenceConflictError as exc:
        raise ResearchConflictError(
            "重复的沙箱执行幂等键", code="duplicate_code_run"
        ) from exc
    return {
        "job_id": job_row.job_id,
        "kind": "research_code_run",
        "status": job_row.status,
        "created": created,
        "artifact_id": artifact.artifact_id,
        "commit": artifact.commit,
        "idempotency_key": idempotency_key,
        "entity_id": job_row.job_id,
    }


async def _create_validation_experiment(
    session: AsyncSession, payload: dict[str, Any]
) -> dict[str, Any]:
    data = ValidationExperimentCreateIn.model_validate(payload)
    experiment = new_experiment(
        hypothesis=data.hypothesis,
        version_stamp=_build_version_stamp(data.version_stamp),
        plan=_build_validation_plan(data.plan),
        thresholds=_build_thresholds(data.thresholds),
        robustness=_build_robustness(data.robustness),
        strategy_params_space=dict(data.strategy_params_space or {}),
        supersedes_id=data.supersedes_id,
        notes=data.notes or "",
    )
    from finboard_app.spec_validation import freeze_spec_runner

    try:
        experiment = await freeze_spec_runner(session, experiment)
    except (ValueError, LookupError) as exc:
        raise ResearchInvalidInputError(
            f"正式规格验证前置失败: {exc}", code="invalid_validation_experiment"
        ) from exc
    await ResearchExperimentRepository(session).save(experiment)
    return {
        "experiment_id": experiment.experiment_id,
        "status": experiment.status.value,
        "version_checksum": experiment.version_stamp.checksum(),
        "final_test_unsealed": experiment.final_test_unsealed,
    }


async def _add_validation_trial(
    session: AsyncSession, experiment_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    data = ValidationTrialIn.model_validate(payload)
    exp_repo = ResearchExperimentRepository(session)
    trial_repo = ResearchTrialRepository(session)
    experiment = await exp_repo.get(experiment_id)
    if experiment is None:
        raise ResearchNotFoundError(
            f"未找到验证实验: {experiment_id}", code="validation_experiment_not_found"
        )
    if experiment.final_test_unsealed:
        raise ResearchConflictError(
            "实验已揭盲(final_test_unsealed=true),不可再追加 trial",
            code="already_unsealed",
        )
    if not experiment.can_run_trial():
        raise ResearchConflictError(
            "实验不能接受新 trial"
            f"(status={experiment.status.value}, trials_used="
            f"{experiment.trials_used}/{experiment.plan.trial_budget})",
            code="trial_budget_exhausted",
        )
    existing = await trial_repo.list_by_experiment(experiment_id)
    trial = TrialRecord(
        trial_id=f"{experiment_id}-topic-{len(existing)}",
        experiment_id=experiment_id,
        trial_index=len(existing),
        parameters=dict(data.parameters),
        status=TrialStatus(data.status),
        failure_reason=data.failure_reason,
        created_at=datetime.now(UTC),
    )
    await trial_repo.save(trial)
    try:
        experiment = increment_trials_used(experiment)
    except ValueError as exc:
        raise ResearchConflictError(str(exc), code="trial_budget_exhausted") from exc
    await exp_repo.save(experiment)
    return {
        "experiment_id": experiment_id,
        "trial_id": trial.trial_id,
        "trial_index": trial.trial_index,
        "status": trial.status.value,
    }


async def _unseal_validation_experiment(
    session: AsyncSession,
    experiment_id: str,
    payload: dict[str, Any],
    *,
    ctx: ResearchOperationContext,
) -> dict[str, Any]:
    """Enqueue the existing one-time execution that performs the unseal.

    The actual ``final_test_unsealed`` write happens in
    ``ValidationExperimentExecutor``. This preflight is what turns a duplicate
    request into a 409 ``already_unsealed`` before any job is created; the
    executor re-checks it (double guard, fail-closed).
    """

    data = ValidationUnsealIn.model_validate(payload)
    experiment = await ResearchExperimentRepository(session).get(experiment_id)
    if experiment is None:
        raise ResearchNotFoundError(
            f"未找到验证实验: {experiment_id}", code="validation_experiment_not_found"
        )
    if experiment.final_test_unsealed:
        raise ResearchConflictError(
            "实验已揭盲(final_test_unsealed=true),揭盲不可重做",
            code="already_unsealed",
            details={"experiment_id": experiment_id, "final_test_unsealed": True},
        )
    if experiment.status.value in _TERMINAL_EXPERIMENT_STATUSES:
        raise ResearchConflictError(
            f"实验状态不可执行: status={experiment.status.value}",
            code="experiment_not_runnable",
        )
    if not experiment.can_run_trial():
        raise ResearchInvalidInputError(
            "试验预算已耗尽: trials_used="
            f"{experiment.trials_used}/{experiment.plan.trial_budget};"
            "请提高 plan.trial_budget 新建实验(supersedes_id 关联)",
            code="trial_budget_exhausted",
        )
    key = data.idempotency_key or f"validation_experiment:{experiment_id}"
    job_row, created = await enqueue_job_core(
        session,
        kind="validation_experiment",
        queue="research",
        idempotency_key=key,
        payload={"experiment_id": experiment_id},
        requested_by=ctx.actor,
        max_attempts=1,
        context=ctx,
    )
    return {
        "experiment_id": experiment_id,
        "job_id": job_row.job_id,
        "kind": "validation_experiment",
        "status": job_row.status,
        "created": created,
        "idempotency_key": key,
        "final_test_unsealed": experiment.final_test_unsealed,
    }


# --------------------------------------------------------------------------- #
# Read side: immutable links joined with live artifact facts
# --------------------------------------------------------------------------- #


def _code_run_state(rcr_status: str | None, job_status: str | None) -> str:
    if rcr_status == "succeeded":
        return "succeeded"
    if rcr_status == "failed":
        return "failed"
    if rcr_status == "running":
        return "running"
    return derive_link_state(None, job_status)


def _experiment_state(status: str | None) -> str:
    mapping = {
        "hypothesis": "queued",
        "running": "running",
        "in_sample": "running",
        "validated_oos": "succeeded",
        "rejected": "failed",
        "failed": "failed",
        "interrupted": "interrupted",
        "superseded": "cancelled",
    }
    return mapping.get(status or "", "queued")


def _validation_gaps(
    *,
    status: str | None,
    oos: dict[str, Any],
    trials_used: int,
    final_test_unsealed: bool,
) -> list[str]:
    gaps = list(oos.get("evidence_gaps") or [])
    if trials_used == 0 and "no_trials" not in gaps:
        gaps.append("no_trials")
    if not final_test_unsealed and "final_test_not_unsealed" not in gaps:
        gaps.append("final_test_not_unsealed")
    if status in {"hypothesis", "in_sample"}:
        gaps.append("status_not_terminal")
    return gaps[:8]


async def _artifact_facts(
    session: AsyncSession, links: list[ResearchTopicLinkModel]
) -> dict[str, dict[str, Any]]:
    """Batch-load bounded columns for the linked artifacts (no artifact bodies)."""

    facts: dict[str, dict[str, Any]] = {}
    factor_ids = [link.entity_id for link in links if link.entity_type == "factor_experiment"]
    if factor_ids:
        rows = (
            await session.execute(
                select(
                    FactorExperimentModel.experiment_id,
                    FactorExperimentModel.status,
                    FactorExperimentModel.dataset_release_checksum,
                    FactorExperimentModel.feature_snapshot_id,
                    FactorExperimentModel.validation_experiment_id,
                    FactorExperimentModel.failure_reason,
                    FactorExperimentModel.updated_at,
                ).where(FactorExperimentModel.experiment_id.in_(factor_ids))
            )
        ).mappings()
        for row in rows:
            facts[str(row["experiment_id"])] = {
                "entity_type": "factor_experiment",
                "status": row["status"],
                "checksum": row["dataset_release_checksum"],
                "feature_snapshot_id": row["feature_snapshot_id"],
                "validation_experiment_id": row["validation_experiment_id"],
                "failure_reason": row["failure_reason"],
                "state": _experiment_state(row["status"]),
                "updated_at": row["updated_at"].isoformat()
                if row["updated_at"] is not None
                else None,
            }

    validation_ids = [
        link.entity_id for link in links if link.entity_type == "validation_experiment"
    ]
    if validation_ids:
        rows = (
            await session.execute(
                select(
                    ResearchExperimentModel.experiment_id,
                    ResearchExperimentModel.status,
                    ResearchExperimentModel.version_checksum,
                    ResearchExperimentModel.final_test_unsealed,
                    ResearchExperimentModel.trials_used,
                    ResearchExperimentModel.finalized_at,
                ).where(ResearchExperimentModel.experiment_id.in_(validation_ids))
            )
        ).mappings()
        for row in rows:
            experiment_id = str(row["experiment_id"])
            oos = await bounded_experiment_oos(session, experiment_id)
            budget_limit = (
                await session.execute(
                    text(
                        "SELECT CASE WHEN (plan::jsonb->>'trial_budget') ~ '^[0-9]+$' "
                        "THEN (plan::jsonb->>'trial_budget')::int END "
                        "FROM research_experiments WHERE experiment_id=:id"
                    ),
                    {"id": experiment_id},
                )
            ).scalar_one_or_none()
            validation_gaps = _validation_gaps(
                status=row["status"],
                oos=oos,
                trials_used=int(row["trials_used"] or 0),
                final_test_unsealed=bool(row["final_test_unsealed"]),
            )
            facts[experiment_id] = {
                "entity_type": "validation_experiment",
                # oos_outcome is the primary read-side fact; status is secondary.
                "oos_outcome": oos["oos_outcome"],
                "status": row["status"],
                "checksum": row["version_checksum"],
                "trial_budget": {
                    "used": int(row["trials_used"] or 0),
                    "limit": budget_limit,
                },
                "final_test_unsealed": bool(row["final_test_unsealed"]),
                "evidence_gaps": validation_gaps,
                "state": _experiment_state(row["status"]),
                "updated_at": row["finalized_at"].isoformat()
                if row["finalized_at"] is not None
                else None,
            }

    code_job_ids = [link.entity_id for link in links if link.entity_type == "code_run"]
    if code_job_ids:
        run_rows = (
            await session.execute(
                select(
                    ResearchCodeRunModel.run_id,
                    ResearchCodeRunModel.job_id,
                    ResearchCodeRunModel.kind,
                    ResearchCodeRunModel.mode,
                    ResearchCodeRunModel.name,
                    ResearchCodeRunModel.commit,
                    ResearchCodeRunModel.code_checksum,
                    ResearchCodeRunModel.scores_checksum,
                    ResearchCodeRunModel.output_snapshot_id,
                    ResearchCodeRunModel.status,
                    ResearchCodeRunModel.error_code,
                    ResearchCodeRunModel.timed_out,
                    ResearchCodeRunModel.oom_killed,
                )
                .where(ResearchCodeRunModel.job_id.in_(code_job_ids))
                .order_by(ResearchCodeRunModel.id.desc())
            )
        ).mappings()
        runs_by_job: dict[str, Any] = {}
        for row in run_rows:
            runs_by_job.setdefault(str(row["job_id"]), row)
        job_rows = (
            await session.execute(
                select(
                    BackgroundJobModel.job_id,
                    BackgroundJobModel.status,
                    BackgroundJobModel.error_code,
                    BackgroundJobModel.error_summary,
                ).where(BackgroundJobModel.job_id.in_(code_job_ids))
            )
        ).mappings()
        jobs_by_id = {str(row["job_id"]): row for row in job_rows}
        for job_id in code_job_ids:
            run = runs_by_job.get(job_id)
            job = jobs_by_id.get(job_id)
            status = str(run["status"]) if run is not None else (
                str(job["status"]) if job is not None else None
            )
            gaps: list[str] = []
            if run is None:
                gaps.append("run_not_created")
            if run is not None and run["timed_out"]:
                gaps.append("timed_out")
            if run is not None and run["oom_killed"]:
                gaps.append("oom_killed")
            if run is not None and run["error_code"]:
                gaps.append(f"failed:{run['error_code']}")
            facts[job_id] = {
                "entity_type": "code_run",
                "run_id": run["run_id"] if run is not None else None,
                "status": status,
                "kind": run["kind"] if run is not None else None,
                "mode": run["mode"] if run is not None else None,
                "name": run["name"] if run is not None else None,
                "commit": run["commit"] if run is not None else None,
                "checksum": run["scores_checksum"] if run is not None else None,
                "code_checksum": run["code_checksum"] if run is not None else None,
                "output_snapshot_id": run["output_snapshot_id"] if run is not None else None,
                "evidence_gaps": gaps[:8],
                "state": _code_run_state(
                    str(run["status"]) if run is not None else None,
                    str(job["status"]) if job is not None else None,
                ),
                "updated_at": None,
                "error_summary": job["error_summary"] if job is not None else None,
            }
    return facts


def _deep_link(entity_type: str, entity_id: str, facts: dict[str, Any]) -> str:
    if entity_type == "factor_experiment":
        return f"/research/factors?experiment={entity_id}"
    if entity_type == "validation_experiment":
        return f"/research/experiments?experiment={entity_id}"
    run_id = facts.get("run_id")
    if run_id:
        return f"/research/factors?run={run_id}"
    return f"/jobs?job={entity_id}"


def _artifact_link_payload(
    link: ResearchTopicLinkModel, facts: dict[str, Any] | None
) -> dict[str, Any]:
    data = facts or {}
    payload: dict[str, Any] = {
        "link_id": link.id,
        "topic_id": link.topic_id,
        "goal_version": link.goal_version,
        "entity_type": link.entity_type,
        "entity_id": link.entity_id,
        "job_id": link.job_id,
        "operation_id": link.operation_id,
        "created_by": link.created_by,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "state": data.get("state", "queued"),
        "status": data.get("status"),
        "checksum": data.get("checksum"),
        "evidence_gaps": data.get("evidence_gaps") or [],
        "deep_link": _deep_link(link.entity_type, link.entity_id, data),
    }
    if data.get("entity_type") == "validation_experiment":
        # Display contract: oos_outcome first, then status / budget / unsealed.
        payload.update(
            oos_outcome=data.get("oos_outcome"),
            trial_budget=data.get("trial_budget"),
            final_test_unsealed=data.get("final_test_unsealed"),
            diagnostic_status=data.get("status"),
        )
    for key in (
        "run_id",
        "kind",
        "mode",
        "name",
        "commit",
        "code_checksum",
        "output_snapshot_id",
        "feature_snapshot_id",
        "validation_experiment_id",
        "failure_reason",
        "error_summary",
        "updated_at",
    ):
        if data.get(key) is not None:
            payload[key] = data[key]
    return payload


async def list_topic_artifact_links(
    session: AsyncSession, topic_id: str, *, limit: int = 20
) -> dict[str, Any]:
    """Links for factor / code / OOS artifacts, live facts derived at read time."""

    if not 1 <= limit <= 50:
        raise ResearchInvalidInputError("limit 范围: 1-50", code="invalid_pagination")
    await _require_topic(session, topic_id)
    rows = list(
        (
            await session.execute(
                select(ResearchTopicLinkModel)
                .where(
                    ResearchTopicLinkModel.topic_id == topic_id,
                    ResearchTopicLinkModel.entity_type.in_(ARTIFACT_ENTITY_TYPES),
                )
                .order_by(ResearchTopicLinkModel.id.desc())
                .limit(limit + 1)
            )
        ).scalars()
    )
    page = rows[:limit]
    facts = await _artifact_facts(session, page)
    return {
        "topic_id": topic_id,
        "items": [
            _artifact_link_payload(link, facts.get(link.entity_id)) for link in page
        ],
        "has_more": len(rows) > limit,
        "limit": limit,
    }


# --------------------------------------------------------------------------- #
# Action dispatcher
# --------------------------------------------------------------------------- #


def _params_summary(
    action: str,
    payload: Any,
    *,
    topic_id: str,
    goal_version: str,
    operation_id: str | None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    summary: dict[str, Any] = {
        "action": action,
        "topic_id": topic_id,
        "goal_version": goal_version,
        "operation_id": operation_id,
        "entity_id": data.get("artifact_id") or data.get("run_id") or data.get("experiment_id"),
        "kind": data.get("kind"),
        "name": data.get("name"),
        "release_id": data.get("dataset_release_id"),
    }
    release_ids = data.get("dataset_release_ids")
    if isinstance(release_ids, list) and release_ids:
        summary["release_ids"] = ",".join(str(item) for item in release_ids)[:120]
    return {key: value for key, value in summary.items() if value is not None}


def _require_action_scope(ctx: ResearchOperationContext, action: str) -> None:
    required = TOPIC_ACTION_SCOPES[action]
    if not ctx.allows_scope(required):
        raise ResearchPermissionDeniedError(
            f"当前通道缺少所需作用域: {required}",
            code="insufficient_scope",
            details={"required": required, "action": action},
        )


def _action_audit_operation(action: str) -> str:
    return f"topic.action.{action}"


async def _record_success(
    session: AsyncSession,
    *,
    action: str,
    ctx: ResearchOperationContext,
    topic_id: str,
    goal_version: str,
    entity_type: str,
    entity_id: str,
    job_id: str | None,
    result_code: str,
    params: dict[str, Any],
) -> None:
    correlation = {
        "topic_id": topic_id,
        "goal_version": goal_version,
        "entity_id": entity_id,
    }
    if job_id:
        correlation["job_id"] = job_id
    if ctx.operation_id:
        correlation["operation_id"] = ctx.operation_id
    await research_audit_recorder().record(
        session,
        ResearchAuditEvent(
            operation=_action_audit_operation(action),
            outcome="succeeded",
            context=ctx,
            topic_id=topic_id,
            entity_type=entity_type,
            entity_id=entity_id,
            correlation=correlation,
            result_code=result_code,
            params_summary=params,
        ),
    )


async def _record_failure(
    *,
    action: str,
    ctx: ResearchOperationContext,
    topic_id: str | None,
    exc: BaseException,
    params: dict[str, Any],
    correlation: dict[str, str] | None = None,
) -> None:
    denied = isinstance(exc, ResearchPermissionDeniedError)
    rejected = denied or isinstance(
        exc, (ResearchOperationError, ResearchTopicLinkConflictError, ValidationError)
    )
    code = (
        exc.code
        if isinstance(exc, ResearchOperationError)
        else "invalid_argument"
        if isinstance(exc, ValidationError)
        else type(exc).__name__
    )
    merged = {"topic_id": topic_id} if topic_id else {}
    if correlation:
        merged.update(correlation)
    await research_audit_recorder().record_detached(
        ResearchAuditEvent(
            operation=_action_audit_operation(action),
            outcome="denied" if denied else "rejected" if rejected else "failed",
            context=ctx,
            topic_id=topic_id,
            correlation=merged,
            result_code=code,
            params_summary=params,
            error_summary=str(exc),
        )
    )


def _validate_action_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = sorted(set(payload) & set(CODE_FORBIDDEN_FIELDS))
    if forbidden:
        raise ResearchInvalidInputError(
            f"课题动作不接受代码字段({', '.join(forbidden)});"
            "代码只能经 MCP finboard_research_code_submit 登记后按 artifact 引用",
            code="code_channel_forbidden",
            details={"forbidden_fields": forbidden},
        )
    if action == "factor_experiment.create":
        return FactorExperimentCreateIn.model_validate(payload).model_dump(mode="json")
    if action == "code_run.execute":
        return CodeRunExecuteIn.model_validate(payload).model_dump(mode="json")
    if action == "validation_experiment.create":
        return ValidationExperimentCreateIn.model_validate(payload).model_dump(mode="json")
    if action == "validation_experiment.add_trial":
        experiment_id = payload.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ResearchInvalidInputError(
                "validation_experiment.add_trial 需要 experiment_id",
                code="experiment_id_required",
            )
        return {
            "experiment_id": experiment_id,
            "trial": ValidationTrialIn.model_validate(
                {k: v for k, v in payload.items() if k != "experiment_id"}
            ).model_dump(mode="json"),
        }
    if action == "validation_experiment.unseal":
        experiment_id = payload.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ResearchInvalidInputError(
                "validation_experiment.unseal 需要 experiment_id",
                code="experiment_id_required",
            )
        rest = {k: v for k, v in payload.items() if k != "experiment_id"}
        return {
            "experiment_id": experiment_id,
            "unseal": ValidationUnsealIn.model_validate(rest).model_dump(mode="json"),
        }
    return payload


def _validated_action_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate per-action input; pydantic errors become research 422s."""

    try:
        return _validate_action_payload(action, payload)
    except ValidationError as exc:
        raise ResearchInvalidInputError(
            f"{action} 参数非法: {exc}", code="invalid_action_payload"
        ) from exc


async def run_topic_action(
    session: AsyncSession,
    *,
    topic_id: str,
    action: str,
    goal_version: str,
    payload: dict[str, Any],
    ctx: ResearchOperationContext,
    sandbox_enabled: bool = False,
    release_root: Any = None,
) -> dict[str, Any]:
    """Execute one whitelisted topic action inside a single transaction.

    ``run.submit`` / ``run.replay`` stay delegated to #515 (which owns its own
    link + audit + commit); every other action writes the artifact/job, the
    immutable link and the audit event here, then commits exactly once.
    """

    if action not in TOPIC_ACTION_ENTITY_TYPES:
        raise ResearchInvalidInputError(
            f"未知课题动作: {action};允许: {', '.join(sorted(TOPIC_ACTION_ENTITY_TYPES))}",
            code="unknown_action",
            details={"allowed": sorted(TOPIC_ACTION_ENTITY_TYPES)},
        )
    entity_type = TOPIC_ACTION_ENTITY_TYPES[action]
    params = _params_summary(
        action, payload, topic_id=topic_id, goal_version=goal_version, operation_id=ctx.operation_id
    )
    link_repo = ResearchTopicLinkRepository(session)
    entity_id: str | None = None
    job_id: str | None = None
    try:
        # Inside the try so scope denials and invalid operation ids are audited
        # as rejected/denied instead of silently disappearing.
        _valid_operation_id(ctx.operation_id)
        _require_action_scope(ctx, action)
        topic = await _require_topic(session, topic_id, lock=True)
        _require_active_with_goal(topic, goal_version)

        if action in {"run.submit", "run.replay"}:
            if action == "run.submit":
                run_receipt = await start_topic_run(
                    session,
                    topic_id=topic_id,
                    goal_version=goal_version,
                    payload=payload,
                    ctx=ctx,
                    sandbox_enabled=sandbox_enabled,
                    release_root=release_root,
                )
            else:
                run_id = payload.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise ResearchInvalidInputError(
                        "run.replay 需要 payload.run_id", code="run_id_required"
                    )
                replay_payload = {
                    key: value for key, value in payload.items() if key != "run_id"
                }
                run_receipt = await retry_topic_run(
                    session,
                    topic_id=topic_id,
                    run_id=run_id,
                    payload=replay_payload,
                    ctx=ctx,
                    sandbox_enabled=sandbox_enabled,
                    release_root=release_root,
                )
            return {**run_receipt, "action": action, "entity_type": entity_type}

        # Lost receipt: the operation id already names an artifact link.
        if ctx.operation_id:
            bound = await link_repo.get_by_operation_id(ctx.operation_id)
            if bound is not None:
                if bound.topic_id != topic_id or bound.entity_type != entity_type:
                    raise ResearchConflictError(
                        "operation_id 已绑定其他课题或产物;不能跨课题复用",
                        code="operation_id_bound",
                        details={
                            "operation_id": ctx.operation_id,
                            "topic_id": bound.topic_id,
                            "entity_type": bound.entity_type,
                            "entity_id": bound.entity_id,
                        },
                    )
                facts = await _artifact_facts(session, [bound])
                return {
                    "action": action,
                    "topic_id": topic_id,
                    "goal_version": bound.goal_version,
                    "entity_type": entity_type,
                    "entity_id": bound.entity_id,
                    "job_id": bound.job_id,
                    "operation_id": bound.operation_id,
                    "created": False,
                    "result": _artifact_link_payload(bound, facts.get(bound.entity_id)),
                }

        data = _validated_action_payload(action, payload)
        receipt: dict[str, Any]
        if action == "factor_experiment.create":
            receipt = await _create_factor_experiment(session, data)
            entity_id = str(receipt["experiment_id"])
        elif action == "code_run.execute":
            receipt = await _execute_code_run(
                session, data, ctx=ctx, sandbox_enabled=sandbox_enabled
            )
            entity_id = str(receipt["entity_id"])
            job_id = str(receipt["job_id"])
            receipt.pop("entity_id", None)
        elif action == "validation_experiment.create":
            receipt = await _create_validation_experiment(session, data)
            entity_id = str(receipt["experiment_id"])
        elif action == "validation_experiment.add_trial":
            entity_id = str(data["experiment_id"])
            receipt = await _add_validation_trial(session, entity_id, data["trial"])
        else:  # validation_experiment.unseal
            entity_id = str(data["experiment_id"])
            receipt = await _unseal_validation_experiment(
                session, entity_id, data["unseal"], ctx=ctx
            )
            job_id = str(receipt["job_id"])
        try:
            link, link_created = await link_repo.create_or_get(
                topic_id=topic_id,
                goal_version=goal_version,
                entity_type=entity_type,
                entity_id=entity_id,
                job_id=job_id,
                operation_id=ctx.operation_id,
                created_by=ctx.actor,
            )
        except ResearchTopicLinkConflictError as exc:
            raise ResearchConflictError(str(exc), code="link_conflict") from exc
        params = {
            **params,
            **_params_summary(
                action,
                {**payload, "entity_id": entity_id},
                topic_id=topic_id,
                goal_version=goal_version,
                operation_id=ctx.operation_id,
            ),
        }
        created = link_created
        await _record_success(
            session,
            action=action,
            ctx=ctx,
            topic_id=topic_id,
            goal_version=goal_version,
            entity_type=entity_type,
            entity_id=entity_id,
            job_id=job_id,
            result_code="created" if created else "idempotent_hit",
            params=params,
        )
        facts = await _artifact_facts(session, [link])
        await session.commit()
        return {
            "action": action,
            "topic_id": topic_id,
            "goal_version": goal_version,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "job_id": job_id,
            "operation_id": link.operation_id,
            "created": created,
            "result": receipt,
            "link": _artifact_link_payload(link, facts.get(link.entity_id)),
        }
    except Exception as exc:
        correlation: dict[str, str] = {}
        if entity_id:
            correlation["entity_id"] = entity_id
        if job_id:
            correlation["job_id"] = job_id
        await _record_failure(
            action=action,
            ctx=ctx,
            topic_id=topic_id,
            exc=exc,
            params=params,
            correlation=correlation,
        )
        raise


async def unseal_preflight(
    session: AsyncSession, experiment_id: str
) -> dict[str, Any]:
    """Read-only helper for tests/UI: is this experiment still unsealable?"""

    experiment = await ResearchExperimentRepository(session).get(experiment_id)
    if experiment is None:
        raise ResearchNotFoundError(
            f"未找到验证实验: {experiment_id}", code="validation_experiment_not_found"
        )
    return {
        "experiment_id": experiment_id,
        "status": experiment.status.value,
        "final_test_unsealed": experiment.final_test_unsealed,
        "trials_used": experiment.trials_used,
        "trial_budget": experiment.plan.trial_budget,
        "can_unseal": experiment.can_unseal_final(),
    }


__all__ = [
    "ARTIFACT_ENTITY_TYPES",
    "CODE_FORBIDDEN_FIELDS",
    "TOPIC_ACTION_ENTITY_TYPES",
    "TOPIC_ACTION_SCOPES",
    "CodeRunExecuteIn",
    "FactorExperimentCreateIn",
    "ValidationExperimentCreateIn",
    "ValidationTrialIn",
    "ValidationUnsealIn",
    "list_topic_artifact_links",
    "run_topic_action",
    "unseal_preflight",
]
