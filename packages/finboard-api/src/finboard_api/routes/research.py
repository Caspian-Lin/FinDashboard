"""研究实验端点(issue #57)。

设计原则
========

* **创建实验 = 冻结假设**:``POST /api/research/experiments`` 一次性接受
  假设、计划、门;创建后 ``hypothesis`` 不可修改,只能新建 superseding 实验。
* **trial 必须完整**:``GET /api/research/experiments/{id}`` 返回全部 trial
  (包括 FAILED / REJECTED),前端必须显示出来,不能只显示赢家。
* **揭盲不可重做**:``POST /api/research/experiments/{id}/unseal-final`` 只允许
  一次,二次调用返回 409。
* **不跑回测**:本端点不直接触发回测执行,只持久化实验结构;实际 trial 执行
  由 CLI / 后台 worker 调用 ``ValidationRunner`` 完成,通过 ``PATCH trial`` 回写。
"""

from __future__ import annotations

from datetime import UTC
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import (
    AcceptanceThresholdsSchema,
    ExperimentCreate,
    ExperimentDetailOut,
    ExperimentOut,
    ExperimentRejectIn,
    RobustnessPlanSchema,
    TrialCreate,
    TrialOut,
    ValidationPlanSchema,
    VersionStampSchema,
)
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    new_experiment,
    transition_status,
)
from finboard_persistence.validation_repo import (
    ResearchExperimentRepository as ExpRepo,
)
from finboard_persistence.validation_repo import (
    ResearchTrialRepository as TrialRepo,
)

router = APIRouter(prefix="/api/research", tags=["research"])


def _to_plan(p: ValidationPlanSchema) -> ValidationPlan:
    return ValidationPlan(
        mode=ValidationMode(p.mode),
        train_start=_date.fromisoformat(p.train_start),
        train_end=_date.fromisoformat(p.train_end),
        validation_start=_date.fromisoformat(p.validation_start),
        validation_end=_date.fromisoformat(p.validation_end),
        test_start=_date.fromisoformat(p.test_start),
        test_end=_date.fromisoformat(p.test_end),
        train_window_days=p.train_window_days,
        test_window_days=p.test_window_days,
        step_days=p.step_days,
        trial_budget=p.trial_budget,
        random_seed=p.random_seed,
        benchmark_symbol=p.benchmark_symbol,
    )


def _to_thresholds(t: AcceptanceThresholdsSchema) -> AcceptanceThresholds:
    return AcceptanceThresholds(**t.model_dump())


def _to_robustness(r: RobustnessPlanSchema) -> RobustnessPlan:
    return RobustnessPlan(
        neighbourhood_steps=r.neighbourhood_steps,
        neighbourhood_relative_step=r.neighbourhood_relative_step,
        cost_multipliers=tuple(r.cost_multipliers),
        slippage_stress_bps=tuple(r.slippage_stress_bps),
        execution_delay_bars=tuple(r.execution_delay_bars),
        stress_phases=tuple(r.stress_phases),
    )


def _to_version_stamp(v: VersionStampSchema) -> VersionStamp:
    return VersionStamp(
        matching_model_version=v.matching_model_version,
        asset_rules_version=v.asset_rules_version,
        factor_version=v.factor_version,
        dataset_versions=dict(v.dataset_versions),
        selection_config=dict(v.selection_config),
        strategy_kind=v.strategy_kind,
    )


@router.post(
    "/experiments",
    response_model=ExperimentOut,
    status_code=201,
)
async def create_experiment(
    body: ExperimentCreate,
    session: AsyncSession = Depends(get_db_session),
) -> ExperimentOut:
    """创建研究实验 —— 假设 / 计划 / 门一次性冻结。

    创建后 ``hypothesis`` 不可修改,如需变更请新建 experiment 并在
    ``supersedes_id`` 关联旧版本。
    """
    experiment = new_experiment(
        hypothesis=body.hypothesis,
        version_stamp=_to_version_stamp(body.version_stamp),
        plan=_to_plan(body.plan),
        thresholds=_to_thresholds(body.thresholds),
        robustness=_to_robustness(body.robustness),
        strategy_params_space=dict(body.strategy_params_space),
        supersedes_id=body.supersedes_id,
        notes=body.notes,
    )
    repo = ExpRepo(session)
    await repo.save(experiment)
    await session.commit()
    return ExperimentOut(**experiment.as_dict())  # type: ignore[arg-type]


@router.get("/experiments", response_model=list[ExperimentOut])
async def list_experiments(
    status: ExperimentStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExperimentOut]:
    """列出实验(可选按状态过滤)。"""
    repo = ExpRepo(session)
    exps = await repo.list_by_status(status, limit=limit)
    return [ExperimentOut(**e.as_dict()) for e in exps]  # type: ignore[arg-type]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetailOut)
async def get_experiment(
    experiment_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ExperimentDetailOut:
    """读取实验详情 + 全部 trial(包括 FAILED / REJECTED)。"""
    exp_repo = ExpRepo(session)
    trial_repo = TrialRepo(session)
    exp = await exp_repo.get(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    trials = await trial_repo.list_by_experiment(experiment_id)
    return ExperimentDetailOut(
        **exp.as_dict(),  # type: ignore[arg-type]
        trials=[TrialOut(**t.as_dict()) for t in trials],  # type: ignore[arg-type]
    )


@router.post("/experiments/{experiment_id}/reject", response_model=ExperimentOut)
async def reject_experiment(
    experiment_id: str,
    body: ExperimentRejectIn,
    session: AsyncSession = Depends(get_db_session),
) -> ExperimentOut:
    """主动拒绝实验(设置 REJECTED + 原因)。"""
    repo = ExpRepo(session)
    exp = await repo.get(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if exp.status in (ExperimentStatus.VALIDATED_OOS, ExperimentStatus.SUPERSEDED):
        raise HTTPException(
            status_code=409,
            detail=f"cannot reject experiment in status={exp.status.value}",
        )
    exp = transition_status(exp, ExperimentStatus.REJECTED, rejection_reason=body.reason)
    await repo.save(exp)
    await session.commit()
    return ExperimentOut(**exp.as_dict())  # type: ignore[arg-type]


@router.post(
    "/experiments/{experiment_id}/trials",
    response_model=TrialOut,
    status_code=201,
)
async def add_trial(
    experiment_id: str,
    body: TrialCreate,
    session: AsyncSession = Depends(get_db_session),
) -> TrialOut:
    """手动登记一次 trial(不通过 runner 自动跑)。

    适用场景:外部 worker / CLI 跑完回测后,把结果回写为 trial。
    """
    from datetime import datetime
    from uuid import uuid4

    from finboard_backtest.validation.contracts import TrialRecord

    exp_repo = ExpRepo(session)
    trial_repo = TrialRepo(session)
    exp = await exp_repo.get(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if not exp.can_run_trial():
        raise HTTPException(
            status_code=409,
            detail=f"experiment cannot accept new trials (status={exp.status.value}, "
            f"trials_used={exp.trials_used}/{exp.plan.trial_budget})",
        )
    existing_trials = await trial_repo.list_by_experiment(experiment_id)
    next_index = len(existing_trials)
    trial = TrialRecord(
        trial_id=f"{experiment_id}-manual-{uuid4().hex[:8]}",
        experiment_id=experiment_id,
        trial_index=next_index,
        parameters=dict(body.parameters),
        status=TrialStatus(body.status),
        failure_reason=body.failure_reason,
        created_at=datetime.now(UTC),
    )
    await trial_repo.save(trial)

    from finboard_backtest.validation.contracts import increment_trials_used

    exp = increment_trials_used(exp)
    await exp_repo.save(exp)
    await session.commit()
    return TrialOut(**trial.as_dict())  # type: ignore[arg-type]


@router.delete("/experiments/{experiment_id}", status_code=204)
async def delete_experiment(
    experiment_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """删除实验(级联删除 trial)。"""
    repo = ExpRepo(session)
    deleted = await repo.delete(experiment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="experiment not found")
    await session.commit()
