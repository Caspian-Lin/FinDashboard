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

import logging
import os
import subprocess
from datetime import UTC, datetime
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobOut
from finboard_api.schemas import (
    AcceptanceThresholdsSchema,
    ExperimentCreate,
    ExperimentDetailOut,
    ExperimentOut,
    ExperimentRejectIn,
    FactorDefinitionOut,
    FactorExperimentCreate,
    FactorExperimentOut,
    FactorSignalOut,
    FeatureSnapshotCreate,
    FeatureSnapshotOut,
    RobustnessPlanSchema,
    TrialCreate,
    TrialOut,
    ValidationPlanSchema,
    VersionStampSchema,
)
from finboard_backtest.factor_lab import (
    FactorAnalysisError,
    build_price_feature_snapshot,
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
from finboard_data.factor_lab import (
    ArtifactIntegrityError,
    FactorExperimentPlan,
    FactorExperimentStatus,
    FactorRole,
    FeatureSnapshot,
    ResearchArtifactStatus,
    factor_lab_catalog,
    new_factor_experiment,
)
from finboard_data.releases import (
    DatasetReleaseError,
    FrozenReleaseProvider,
    ResearchDatasetRelease,
)
from finboard_persistence import BackgroundJobPersistenceConflictError
from finboard_persistence.dataset_release_repo import (
    ResearchDatasetReleaseRepository,
)
from finboard_persistence.factor_lab_repo import (
    FactorExperimentRepository,
    FactorExperimentValidationService,
    FactorSignalRepository,
    FeatureSnapshotRepository,
)
from finboard_persistence.validation_repo import (
    ResearchExperimentRepository as ExpRepo,
)
from finboard_persistence.validation_repo import (
    ResearchTrialRepository as TrialRepo,
)

router = APIRouter(prefix="/api/research", tags=["research"])
logger = logging.getLogger(__name__)

_DEFAULT_RELEASE_ROOT = "data_releases"


def _factor_code_version() -> str:
    """给快照记录服务端因子计算代码版本,不接受网页伪造。"""

    configured = os.getenv("FINBOARD_CODE_VERSION")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f"{value}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else value


def _feature_snapshot_max_concurrency(request: Request) -> int:
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "feature_snapshot_max_concurrency", 8)
    return max(1, min(64, int(configured)))


def _feature_snapshot_process_workers(request: Request) -> int:
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "feature_snapshot_process_workers", 8)
    return max(0, min(64, int(configured)))


async def _load_feature_snapshot_input(
    body: FeatureSnapshotCreate,
    session: AsyncSession,
) -> tuple[list[ResearchDatasetRelease], datetime]:
    """校验发布与决策时点,供同步和后台入口共用。

    issue #187:联合发布 —— ``dataset_release_id`` 为 bars 主发布,
    ``additional_release_ids`` 为 daily_metrics / financial_indicators 研究
    数据发布;决策时点按 bars 主发布范围校验。
    """

    release_repo = ResearchDatasetReleaseRepository(session)
    try:
        release_ids = [body.dataset_release_id, *body.additional_release_ids]
        releases = [await release_repo.require_usable(release_id) for release_id in release_ids]
    except (ValueError, DatasetReleaseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    primary = releases[0]
    decision_at = body.decision_at.astimezone(UTC)
    if not primary.start_date <= decision_at.date() <= primary.end_date:
        raise HTTPException(
            status_code=422,
            detail=(
                f"decision_at 日期必须在 bars 主发布范围内: "
                f"{primary.start_date}~{primary.end_date}"
            ),
        )
    if decision_at > datetime.now(UTC):
        raise HTTPException(status_code=422, detail="decision_at 不能晚于当前时间")
    return releases, decision_at


async def _build_feature_snapshot_from_releases(
    *,
    releases: list[ResearchDatasetRelease],
    release_root: Path,
    decision_at: datetime,
    max_concurrency: int,
    process_workers: int = 0,
) -> FeatureSnapshot:
    """按发布 kind 构建特征快照(issue #187)。

    * 仅 bars 主发布:复用 ``build_price_feature_snapshot``(价格因子,行为不变);
    * bars + daily_metrics / financial_indicators 联合发布:走
      ``build_cross_section_feature_snapshot_from_releases``,额外产出
      pb / 市值 / 换手 / ROE 等基本面因子。
    """
    from finboard_data.releases import ReleaseDatasetKind

    primary = releases[0]
    additional = releases[1:]
    if not additional or all(
        item.dataset_kind is ReleaseDatasetKind.BARS for item in additional
    ):
        provider = FrozenReleaseProvider(
            release_root=release_root,
            release_id=primary.release_id,
            max_concurrency=max_concurrency,
        )
        return await build_price_feature_snapshot(
            provider=provider,
            decision_at=decision_at,
            code_version=_factor_code_version(),
            max_concurrency=max_concurrency,
            process_workers=process_workers,
        )
    from finboard_backtest.factor_lab import (
        build_cross_section_feature_snapshot_from_releases,
    )

    providers = {
        release.release_id: FrozenReleaseProvider(
            release_root=release_root,
            release_id=release.release_id,
            max_concurrency=max_concurrency,
        )
        for release in releases
    }
    return await build_cross_section_feature_snapshot_from_releases(
        releases=releases,
        providers=providers,
        decision_at=decision_at,
        code_version=_factor_code_version(),
        max_concurrency=max_concurrency,
        on_progress=None,
    )



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


# ---------------------------------------------------------------------------
# 因子实验室(issue #78)
# ---------------------------------------------------------------------------


@router.get("/factors/catalog", response_model=list[FactorDefinitionOut])
async def get_factor_catalog(
    role: FactorRole | None = Query(default=None),
) -> list[FactorDefinitionOut]:
    """返回有真实实现的版本化目录;未实现因子不会出现在列表中。"""

    return [
        FactorDefinitionOut.model_validate(definition.as_dict())
        for definition in factor_lab_catalog(role)
    ]


@router.post(
    "/factors/features",
    response_model=FeatureSnapshotOut,
    status_code=201,
)
async def create_feature_snapshot(
    body: FeatureSnapshotCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> FeatureSnapshotOut:
    """从冻结数据发布显式生成并发布价格特征快照。

    这是研究计算入口,只读冻结发布并写入 ``factor_feature_snapshots``;
    不启动回测、模拟盘或任何实盘动作。
    """

    releases, decision_at = await _load_feature_snapshot_input(body, session)
    max_concurrency = _feature_snapshot_max_concurrency(request)
    process_workers = _feature_snapshot_process_workers(request)

    release_root = Path(
        os.getenv("FINBOARD_DATA_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT)
    )
    try:
        snapshot = await _build_feature_snapshot_from_releases(
            releases=releases,
            release_root=release_root,
            decision_at=decision_at,
            max_concurrency=max_concurrency,
            process_workers=process_workers,
        )
        await FeatureSnapshotRepository(session).publish(snapshot)
        await session.commit()
    except (DatasetReleaseError, FactorAnalysisError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=f"特征快照生成失败: {exc}") from exc
    except OSError as exc:
        await session.rollback()
        logger.exception(
            "research.feature_snapshot_release_read_failed",
            extra={"release_id": releases[0].release_id, "release_root": str(release_root)},
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "特征快照生成失败: 服务端无法读取冻结发布文件,"
                "请检查 FINBOARD_DATA_RELEASE_ROOT 目录和文件权限"
            ),
        ) from exc
    except Exception:
        await session.rollback()
        raise

    return FeatureSnapshotOut.model_validate(snapshot.as_dict())


@router.post(
    "/factors/features/jobs",
    response_model=JobOut,
    status_code=202,
)
async def start_feature_snapshot_job(
    body: FeatureSnapshotCreate,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记特征快照计算任务,立即返回 202 + job_id(issue #144)。

    实际执行(``FrozenReleaseProvider`` + ``build_price_feature_snapshot`` + 发布)
    由 worker 消费 ``kind=feature_snapshot`` 任务。单并发约束由 worker
    ``kind_concurrency={"feature_snapshot": 1}`` 保证(取代旧
    FeatureSnapshotJobManager 单 job gate)。快照计算完成后
    ``JobOut.result_ref = snapshot_id``;进度 / 状态 / 取消统一通过
    ``/api/jobs/{job_id}`` 轮询。

    ``GET /factors/features/jobs/{job_id}`` 已删除——前端统一用 ``/api/jobs/{job_id}``。
    """

    from finboard_api.job_helpers import enqueue_job

    # 仍同步校验发布可用 + 决策时点范围(早失败,避免入队后才在 worker 端失败)。
    releases, decision_at = await _load_feature_snapshot_input(body, session)
    await session.rollback()  # 校验只读,释放行锁;executor 会重读

    payload: dict[str, Any] = {
        "dataset_release_id": releases[0].release_id,
        "additional_release_ids": [item.release_id for item in releases[1:]],
        "decision_at": decision_at.isoformat(),
    }
    idempotency_key = (
        f"feature_snapshot:{releases[0].release_id}:{decision_at.date().isoformat()}"
    )
    try:
        job = await enqueue_job(
            session,
            response,
            kind="feature_snapshot",
            queue="research",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="api:feature_snapshot",
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.get(
    "/factors/features",
    response_model=list[FeatureSnapshotOut],
)
async def list_feature_snapshots(
    dataset_release_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[FeatureSnapshotOut]:
    snapshots = await FeatureSnapshotRepository(session).list(
        dataset_release_id=dataset_release_id,
        limit=limit,
    )
    return [
        FeatureSnapshotOut.model_validate(item.as_dict()) for item in snapshots
    ]


@router.get(
    "/factors/features/{snapshot_id}",
    response_model=FeatureSnapshotOut,
)
async def get_feature_snapshot(
    snapshot_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FeatureSnapshotOut:
    snapshot = await FeatureSnapshotRepository(session).get(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="feature snapshot not found")
    return FeatureSnapshotOut.model_validate(snapshot.as_dict())


@router.get("/factors/signals", response_model=list[FactorSignalOut])
async def list_factor_signals(
    factor_name: str | None = Query(default=None),
    research_status: ResearchArtifactStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[FactorSignalOut]:
    signals = await FactorSignalRepository(session).list(
        factor_name=factor_name,
        research_status=research_status,
        limit=limit,
    )
    return [FactorSignalOut.model_validate(item.as_dict()) for item in signals]


@router.get(
    "/factors/signals/{signal_id}",
    response_model=FactorSignalOut,
)
async def get_factor_signal(
    signal_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FactorSignalOut:
    signal = await FactorSignalRepository(session).get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="factor signal not found")
    return FactorSignalOut.model_validate(signal.as_dict())


@router.post(
    "/factors/experiments",
    response_model=FactorExperimentOut,
    status_code=201,
)
async def create_factor_experiment(
    body: FactorExperimentCreate,
    session: AsyncSession = Depends(get_db_session),
) -> FactorExperimentOut:
    """冻结因子实验登记;保存不会启动回测、模拟盘或实盘。"""

    release = await ResearchDatasetReleaseRepository(session).require_usable(
        body.dataset_release_id
    )
    snapshot = await FeatureSnapshotRepository(session).get(
        body.feature_snapshot_id
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="feature snapshot not found")
    if snapshot.dataset_release_id != release.release_id:
        raise HTTPException(
            status_code=409,
            detail="feature snapshot and dataset release do not match",
        )
    plan = FactorExperimentPlan(
        in_sample_start=body.plan.in_sample_start,
        in_sample_end=body.plan.in_sample_end,
        oos_start=body.plan.oos_start,
        oos_end=body.plan.oos_end,
        trial_budget=body.plan.trial_budget,
        benchmark_symbol=body.plan.benchmark_symbol,
        transaction_cost_bps=body.plan.transaction_cost_bps,
        quantiles=body.plan.quantiles,
    )
    try:
        experiment = new_factor_experiment(
            hypothesis=body.hypothesis,
            factor_names=tuple(body.factor_names),
            dataset_release_id=release.release_id,
            dataset_release_checksum=release.release_checksum,
            feature_snapshot_id=snapshot.snapshot_id,
            plan=plan,
            comparison_group=body.comparison_group,
            validation_experiment_id=body.validation_experiment_id,
        )
        await FactorExperimentRepository(session).save(experiment)
        await session.commit()
    except (ArtifactIntegrityError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FactorExperimentOut.model_validate(experiment.as_dict())


@router.get(
    "/factors/experiments",
    response_model=list[FactorExperimentOut],
)
async def list_factor_experiments(
    status: FactorExperimentStatus | None = Query(default=None),
    comparison_group: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[FactorExperimentOut]:
    experiments = await FactorExperimentRepository(session).list(
        status=status,
        comparison_group=comparison_group,
        limit=limit,
    )
    return [
        FactorExperimentOut.model_validate(item.as_dict())
        for item in experiments
    ]


@router.get(
    "/factors/experiments/{factor_experiment_id}",
    response_model=FactorExperimentOut,
)
async def get_factor_experiment(
    factor_experiment_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FactorExperimentOut:
    experiment = await FactorExperimentRepository(session).get(
        factor_experiment_id
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="factor experiment not found")
    return FactorExperimentOut.model_validate(experiment.as_dict())


@router.post(
    "/factors/experiments/{factor_experiment_id}/sync-validation",
    response_model=FactorExperimentOut,
)
async def sync_factor_experiment_validation(
    factor_experiment_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FactorExperimentOut:
    """读取 #57 机器结果同步终态;不接受调用者传入 passed_oos。"""

    try:
        experiment = await FactorExperimentValidationService(session).sync(
            factor_experiment_id
        )
        await session.commit()
    except (ArtifactIntegrityError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FactorExperimentOut.model_validate(experiment.as_dict())
