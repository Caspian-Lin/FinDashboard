"""``finboard.factor.*`` / ``finboard.feature_snapshot.*`` 工具 ——
因子实验室(catalog / snapshot / signal / experiment,issue #125)。

复用现有 repository / domain 函数,不重复业务逻辑:

* 只读查询(catalog / snapshot list/get / signal list/get / experiment list/get /
  job status)—— 调 ``factor_lab_catalog`` /
  :class:`~finboard_persistence.FeatureSnapshotRepository` /
  :class:`~finboard_persistence.FactorSignalRepository` /
  :class:`~finboard_persistence.FactorExperimentRepository`;
* 写操作(同步构建快照 / 异步快照任务 / 冻结实验 / 同步终态)—— 调
  ``build_price_feature_snapshot`` /
  ``new_factor_experiment`` /
  :class:`~finboard_persistence.FactorExperimentValidationService`。

feature_snapshot 异步任务(issue #136 / #144):``job_start`` 登记一个
``kind=feature_snapshot`` 任务到统一 ``background_jobs`` 队列(复用
``finboard_api.job_helpers.enqueue_job``),由独立 worker 消费执行;
``job_status`` 读持久化 ``background_jobs`` 行返回 ``JobOut``。
不再使用进程内 :class:`~finboard_backtest.FeatureSnapshotJobManager`(已下线)。

权限:写操作依赖 #122 放开审批门(agent 可自主执行),但尊重
``settings.mcp_readonly_only`` / ``app.write_tools_enabled`` 开关;只读工具自动
允许。不触及交易安全红线(不创建订单 / 持仓 / 回测 / 模拟盘)。
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_DEFAULT_RELEASE_ROOT = "data_releases"


# ---------------------------------------------------------------------------
# 辅助:code_version / release_root / 并发参数
# ---------------------------------------------------------------------------


def _factor_code_version() -> str:
    """给快照记录服务端因子计算代码版本,不接受 agent 伪造。

    复用 ``finboard_api.routes.research._factor_code_version`` 的逻辑。
    """

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


def _release_root() -> Path:
    return Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT))


def _max_concurrency(app: McpAppContext) -> int:
    configured = getattr(app.settings, "feature_snapshot_max_concurrency", 8)
    return max(1, min(64, int(configured)))


def _process_workers(app: McpAppContext) -> int:
    configured = getattr(app.settings, "feature_snapshot_process_workers", 8)
    return max(0, min(64, int(configured)))


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""

    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _plan_date(value: object) -> date:
    """把 agent 传入的 ISO 日期字符串归一为 ``date``(兼容已是 date 的输入)。"""

    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"日期必须是 ISO 字符串或 date,收到 {type(value).__name__}")
    return date.fromisoformat(value)


async def _validate_snapshot_input(
    session: AsyncSession,
    dataset_release_id: str,
    decision_at: datetime,
) -> tuple[Any, datetime]:
    """校验发布可用性 + decision_at 在范围内(复用路由的校验逻辑)。"""

    from finboard_data.releases import DatasetReleaseError
    from finboard_persistence import ResearchDatasetReleaseRepository

    release_repo = ResearchDatasetReleaseRepository(session)
    try:
        release = await release_repo.require_usable(dataset_release_id)
    except (ValueError, DatasetReleaseError) as exc:
        raise McpToolError("invalid_argument", str(exc)) from exc

    normalized = decision_at.astimezone(UTC)
    if not release.start_date <= normalized.date() <= release.end_date:
        raise McpToolError(
            "invalid_argument",
            f"decision_at 日期必须在数据发布范围内: {release.start_date}~{release.end_date}",
        )
    if normalized > datetime.now(UTC):
        raise McpToolError("invalid_argument", "decision_at 不能晚于当前时间")
    return release, normalized


# ---------------------------------------------------------------------------
# finboard.factor.catalog(混合视图:builtin 目录 + user_defined artifacts)
# ---------------------------------------------------------------------------


async def factor_catalog(
    app: McpAppContext, *, role: str | None = None, include_user_defined: bool = True
) -> ToolEnvelope:
    """混合目录:builtin(FACTOR_LAB_CATALOG)+ user_defined(#217)。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_data.factor_lab import FactorRole, factor_lab_catalog, sandbox_factor_name
        from finboard_persistence import ResearchCodeArtifactRepository

        resolved = FactorRole(role) if role else None
        definitions = factor_lab_catalog(resolved)
        items = [
            {
                **cast(dict[str, Any], to_jsonable(d.as_dict())),
                "origin": "builtin",
            }
            for d in definitions
        ]
        if include_user_defined:
            async with app.session_maker() as session:
                artifacts = await ResearchCodeArtifactRepository(session).list_artifacts(
                    kind="factor", limit=500
                )
            for artifact in artifacts:
                items.append(
                    {
                        "name": sandbox_factor_name(artifact.name),
                        "artifact_name": artifact.name,
                        "origin": "user_defined",
                        "status": artifact.status,
                        "promotion_status": artifact.promotion_status,
                        "validation_experiment_id": artifact.validation_experiment_id,
                        "screen_run_id": artifact.screen_run_id,
                        "commit": artifact.commit,
                        "artifact_id": artifact.artifact_id,
                        "code_checksum": artifact.checksum,
                        "created_at": to_jsonable(artifact.created_at),
                        "note": (
                            "沙箱用户因子;仅 status=active 且 promotion_status=passed 可被"
                            "规格引用,晋级证据需 screen + #57 OOS;"
                            "观测来自 finboard_research_code_run 产出的快照"
                        ),
                    }
                )
        return items

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.catalog",
        arguments={"role": role, "include_user_defined": include_user_defined},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# finboard.feature_snapshot.*(FeatureSnapshotRepository)
# ---------------------------------------------------------------------------


async def feature_snapshot_list(
    app: McpAppContext,
    *,
    dataset_release_id: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import FeatureSnapshotRepository

        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            repo = FeatureSnapshotRepository(session)
            snapshots = await repo.list(
                dataset_release_id=dataset_release_id,
                limit=safe_limit,
            )
            return [cast(dict[str, Any], to_jsonable(s.as_dict())) for s in snapshots]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.feature_snapshot.list",
        arguments={"dataset_release_id": dataset_release_id, "limit": limit},
        handler=_do,
    )


async def feature_snapshot_get(app: McpAppContext, snapshot_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_persistence import FeatureSnapshotRepository

        async with app.session_maker() as session:
            repo = FeatureSnapshotRepository(session)
            snapshot = await repo.get(snapshot_id)
            if snapshot is None:
                raise McpToolError("not_found", f"未找到特征快照: {snapshot_id}")
            return cast(dict[str, Any], to_jsonable(snapshot.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.feature_snapshot.get",
        arguments={"snapshot_id": snapshot_id},
        handler=_do,
    )


async def feature_snapshot_create(
    app: McpAppContext,
    *,
    dataset_release_id: str,
    decision_at: datetime,
) -> ToolEnvelope:
    """同步构建并发布价格特征快照(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.factor_lab import (
            FactorAnalysisError,
            build_price_feature_snapshot,
        )
        from finboard_data.releases import (
            DatasetReleaseError,
            FrozenReleaseProvider,
        )
        from finboard_persistence import FeatureSnapshotRepository

        max_concurrency = _max_concurrency(app)
        process_workers = _process_workers(app)
        async with app.session_maker() as session:
            release, normalized_dt = await _validate_snapshot_input(
                session, dataset_release_id, decision_at
            )
            try:
                provider = FrozenReleaseProvider(
                    release_root=_release_root(),
                    release_id=release.release_id,
                    max_concurrency=max_concurrency,
                    expected_checksum=release.release_checksum,
                )
                snapshot = await build_price_feature_snapshot(
                    provider=provider,
                    decision_at=normalized_dt,
                    code_version=_factor_code_version(),
                    max_concurrency=max_concurrency,
                    process_workers=process_workers,
                )
                await FeatureSnapshotRepository(session).publish(snapshot)
                await session.commit()
            except (DatasetReleaseError, FactorAnalysisError, ValueError) as exc:
                await session.rollback()
                raise McpToolError("invalid_argument", f"特征快照生成失败: {exc}") from exc
            except OSError as exc:
                await session.rollback()
                raise McpToolError(
                    "unavailable",
                    "特征快照生成失败: 服务端无法读取冻结发布文件,"
                    "请检查 FINBOARD_DATA_RELEASE_ROOT 目录和文件权限",
                ) from exc
            except Exception:
                await session.rollback()
                raise
            return cast(dict[str, Any], to_jsonable(snapshot.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.feature_snapshot.create",
        arguments={
            "dataset_release_id": dataset_release_id,
            "decision_at": decision_at.isoformat(),
        },
        handler=_do,
    )


async def feature_snapshot_job_start(
    app: McpAppContext,
    *,
    dataset_release_id: str,
    decision_at: datetime,
) -> ToolEnvelope:
    """异步登记特征快照计算任务到统一队列,返回可轮询的 JobOut(issue #136)。

    复用 REST ``POST /factors/features/jobs`` 的口径(已迁移到持久化队列,
    issue #144):同步校验发布可用 + decision_at 范围(早失败),然后 enqueue
    一个 ``kind=feature_snapshot`` 任务,由独立 worker 消费执行
    (``FrozenReleaseProvider`` + ``build_price_feature_snapshot`` + 发布)。
    单并发约束由 worker ``kind_concurrency`` 保证。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_api.job_helpers import enqueue_job
        from finboard_persistence.background_job_repo import (
            BackgroundJobPersistenceConflictError,
        )

        async with app.session_maker() as session:
            release, normalized_dt = await _validate_snapshot_input(
                session, dataset_release_id, decision_at
            )
            await session.rollback()  # 校验只读,释放行锁;executor 会重读

            payload: dict[str, Any] = {
                "dataset_release_id": release.release_id,
                "decision_at": normalized_dt.isoformat(),
            }
            idempotency_key = (
                f"feature_snapshot:{release.release_id}:{normalized_dt.date().isoformat()}"
            )
            try:
                job = await enqueue_job(
                    session,
                    None,
                    kind="feature_snapshot",
                    queue="research",
                    idempotency_key=idempotency_key,
                    payload=payload,
                    requested_by="mcp:feature_snapshot",
                )
                await session.commit()
            except BackgroundJobPersistenceConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            return cast(
                dict[str, Any],
                to_jsonable(job.model_dump(mode="json")),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.feature_snapshot.job_start",
        arguments={
            "dataset_release_id": dataset_release_id,
            "decision_at": decision_at.isoformat(),
        },
        handler=_do,
        idempotency_key=(
            f"feature_snapshot:{dataset_release_id}:"
            f"{decision_at.astimezone(UTC).date().isoformat()}"
        ),
    )


async def feature_snapshot_job_status(app: McpAppContext, job_id: str) -> ToolEnvelope:
    """查询特征快照任务进度(读持久化 background_jobs 表,issue #136)。

    返回 JobOut:status / progress_done / progress_total / phase /
    result_ref(成功后为 snapshot_id)/ error_code / error_summary。
    等价于 ``finboard_job_get(job_id)``,保留语义化命名便于 agent 选择。
    """

    async def _do() -> dict[str, Any]:
        from finboard_api.job_schemas import JobOut
        from finboard_persistence.background_job_repo import (
            BackgroundJobRepository,
        )

        async with app.session_maker() as session:
            row = await BackgroundJobRepository(session).get(job_id)
            if row is None:
                raise McpToolError("not_found", f"未找到特征快照任务: {job_id}")
            return cast(
                dict[str, Any],
                to_jsonable(JobOut.model_validate(row).model_dump(mode="json")),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.feature_snapshot.job_status",
        arguments={"job_id": job_id},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# finboard.factor.signal.*(FactorSignalRepository)
# ---------------------------------------------------------------------------


async def factor_signal_list(
    app: McpAppContext,
    *,
    factor_name: str | None = None,
    research_status: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_data.factor_lab import ResearchArtifactStatus
        from finboard_persistence import FactorSignalRepository

        safe_limit = max(1, min(500, limit))
        resolved_status = ResearchArtifactStatus(research_status) if research_status else None
        async with app.session_maker() as session:
            repo = FactorSignalRepository(session)
            signals = await repo.list(
                factor_name=factor_name,
                research_status=resolved_status,
                limit=safe_limit,
            )
            return [cast(dict[str, Any], to_jsonable(s.as_dict())) for s in signals]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.signal.list",
        arguments={
            "factor_name": factor_name,
            "research_status": research_status,
            "limit": limit,
        },
        handler=_do,
    )


async def factor_signal_get(app: McpAppContext, signal_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_persistence import FactorSignalRepository

        async with app.session_maker() as session:
            repo = FactorSignalRepository(session)
            signal = await repo.get(signal_id)
            if signal is None:
                raise McpToolError("not_found", f"未找到因子信号: {signal_id}")
            return cast(dict[str, Any], to_jsonable(signal.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.signal.get",
        arguments={"signal_id": signal_id},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# finboard.factor.experiment.*(FactorExperimentRepository)
# ---------------------------------------------------------------------------


async def factor_experiment_list(
    app: McpAppContext,
    *,
    status: str | None = None,
    comparison_group: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_data.factor_lab import FactorExperimentStatus
        from finboard_persistence import FactorExperimentRepository

        safe_limit = max(1, min(500, limit))
        resolved_status = FactorExperimentStatus(status) if status else None
        async with app.session_maker() as session:
            repo = FactorExperimentRepository(session)
            experiments = await repo.list(
                status=resolved_status,
                comparison_group=comparison_group,
                limit=safe_limit,
            )
            return [cast(dict[str, Any], to_jsonable(e.as_dict())) for e in experiments]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.experiment.list",
        arguments={
            "status": status,
            "comparison_group": comparison_group,
            "limit": limit,
        },
        handler=_do,
    )


async def factor_experiment_get(app: McpAppContext, experiment_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_persistence import FactorExperimentRepository

        async with app.session_maker() as session:
            repo = FactorExperimentRepository(session)
            experiment = await repo.get(experiment_id)
            if experiment is None:
                raise McpToolError("not_found", f"未找到因子实验: {experiment_id}")
            return cast(dict[str, Any], to_jsonable(experiment.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.experiment.get",
        arguments={"experiment_id": experiment_id},
        handler=_do,
    )


async def factor_experiment_create(
    app: McpAppContext,
    *,
    hypothesis: str,
    factor_names: list[str],
    dataset_release_id: str,
    feature_snapshot_id: str,
    plan: dict[str, Any],
    comparison_group: str,
    validation_experiment_id: str | None = None,
) -> ToolEnvelope:
    """冻结因子实验注册(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_data.factor_lab import (
            ArtifactIntegrityError,
            FactorExperimentPlan,
            new_factor_experiment,
        )
        from finboard_persistence import (
            FactorExperimentRepository,
            FeatureSnapshotRepository,
            ResearchDatasetReleaseRepository,
        )

        async with app.session_maker() as session:
            release_repo = ResearchDatasetReleaseRepository(session)
            release = await release_repo.require_usable(dataset_release_id)
            snapshot_repo = FeatureSnapshotRepository(session)
            snapshot = await snapshot_repo.get(feature_snapshot_id)
            if snapshot is None:
                raise McpToolError("not_found", f"未找到特征快照: {feature_snapshot_id}")
            if snapshot.dataset_release_id != release.release_id:
                raise McpToolError(
                    "conflict",
                    "feature snapshot and dataset release do not match",
                )
            try:
                # plan 日期字段必须以 date 构造 —— 直接传 ISO 字符串会让
                # 与 #57 验证计划的一致性校验(ArtifactIntegrityError)失败,
                # 且 ``plan.as_dict()`` 调用 isoformat() 会崩。
                experiment_plan = FactorExperimentPlan(
                    in_sample_start=_plan_date(plan["in_sample_start"]),
                    in_sample_end=_plan_date(plan["in_sample_end"]),
                    oos_start=_plan_date(plan["oos_start"]),
                    oos_end=_plan_date(plan["oos_end"]),
                    trial_budget=plan["trial_budget"],
                    benchmark_symbol=plan["benchmark_symbol"],
                    transaction_cost_bps=plan["transaction_cost_bps"],
                    quantiles=plan.get("quantiles", 5),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise McpToolError("invalid_argument", f"plan 参数非法: {exc}") from exc
            try:
                experiment = new_factor_experiment(
                    hypothesis=hypothesis,
                    factor_names=tuple(factor_names),
                    dataset_release_id=release.release_id,
                    dataset_release_checksum=release.release_checksum,
                    feature_snapshot_id=snapshot.snapshot_id,
                    plan=experiment_plan,
                    comparison_group=comparison_group,
                    validation_experiment_id=validation_experiment_id,
                )
                await FactorExperimentRepository(session).save(experiment)
                await session.commit()
            except (ArtifactIntegrityError, ValueError) as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            return cast(dict[str, Any], to_jsonable(experiment.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.experiment.create",
        arguments={
            "hypothesis": hypothesis,
            "factor_names": factor_names,
            "dataset_release_id": dataset_release_id,
            "feature_snapshot_id": feature_snapshot_id,
            "plan": plan,
            "comparison_group": comparison_group,
            "validation_experiment_id": validation_experiment_id,
        },
        handler=_do,
    )


async def factor_experiment_sync_validation(app: McpAppContext, experiment_id: str) -> ToolEnvelope:
    """同步因子实验的 #57 机器验证终态(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_data.factor_lab import ArtifactIntegrityError
        from finboard_persistence import FactorExperimentValidationService

        async with app.session_maker() as session:
            try:
                experiment = await FactorExperimentValidationService(session).sync(experiment_id)
                await session.commit()
            except (ArtifactIntegrityError, ValueError) as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            return cast(dict[str, Any], to_jsonable(experiment.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor.experiment.sync_validation",
        arguments={"experiment_id": experiment_id},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """把因子实验室工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_factor_catalog",
        description=(
            "查询因子混合目录:builtin(26 个 alpha/risk/market_input 因子,"
            "每条含 name/version/role/preference/source_fields/"
            "economic_hypothesis/checksum 等,标注 origin=builtin)+ "
            "user_defined(沙箱执行的自定义因子,标注 origin=user_defined 与 "
            "artifact commit/status/promotion_status;仅 status=active 且 "
            "promotion_status=passed 可被规格引用,引用名为 "
            "u_<artifact_name>,观测来自 finboard_research_code_run 快照)。"
            "可选过滤 role(仅过滤 builtin);include_user_defined=false 只看内置。"
            "用于了解系统与 agent 各自提供哪些因子。"
        ),
    )
    async def _factor_catalog(
        role: str | None = None,
        include_user_defined: bool = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_catalog(
            app_context(ctx), role=role, include_user_defined=include_user_defined
        )

    @mcp.tool(
        name="finboard_feature_snapshot_list",
        description=(
            "列出已发布的特征快照(版本化、时点化、不可变)。"
            "每条含 snapshot_id/dataset_release_id/decision_at/checksum/"
            "observations 等。可选过滤 dataset_release_id / limit(默认 100)。"
        ),
    )
    async def _feature_snapshot_list(
        dataset_release_id: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await feature_snapshot_list(
            app_context(ctx),
            dataset_release_id=dataset_release_id,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_feature_snapshot_get",
        description=("查询单个特征快照详情(含完整 observations 因子值)。未找到返回 not_found。"),
    )
    async def _feature_snapshot_get(
        snapshot_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await feature_snapshot_get(app_context(ctx), snapshot_id)

    @mcp.tool(
        name="finboard_feature_snapshot_create",
        description=(
            "[写] 同步构建并发布价格特征快照(从冻结数据发布计算因子值)。"
            "参数:dataset_release_id(冻结发布 ID)/ decision_at(决策时点,"
            "必须在发布范围内且不晚于当前时间)。"
            "计算可能耗时较长(标的数 x 因子数);大发布建议用 "
            "finboard_feature_snapshot_job_start 异步模式。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _feature_snapshot_create(
        dataset_release_id: str,
        decision_at: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await feature_snapshot_create(
            app_context(ctx),
            dataset_release_id=dataset_release_id,
            decision_at=datetime.fromisoformat(decision_at),
        )

    @mcp.tool(
        name="finboard_feature_snapshot_job_start",
        description=(
            "[写] 异步登记特征快照计算任务到统一队列,立即返回 202 + job_id"
            "(不等待执行,由独立 worker 消费)。"
            "参数同 finboard_feature_snapshot_create。"
            "返回 JobOut(job_id/status=queued/progress_*/result_ref/...)。"
            "轮询模式:调用后用 finboard_feature_snapshot_job_status(job_id) "
            "或 finboard_job_get(job_id) 定期查询,直到 status 为 succeeded"
            "(result_ref=snapshot_id)或 failed(含 error_*)。"
            "同一时刻只允许一个 feature_snapshot 任务(worker 单并发),"
            "幂等冲突返回 conflict。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _feature_snapshot_job_start(
        dataset_release_id: str,
        decision_at: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await feature_snapshot_job_start(
            app_context(ctx),
            dataset_release_id=dataset_release_id,
            decision_at=datetime.fromisoformat(decision_at),
        )

    @mcp.tool(
        name="finboard_feature_snapshot_job_status",
        description=(
            "查询特征快照任务进度(读持久化 background_jobs 表)。"
            "返回 JobOut:job_id/status(queued|running|succeeded|failed|...)/"
            "progress_done/progress_total/phase/result_ref(成功后为 snapshot_id)/"
            "error_code/error_summary。"
            "等价于 finboard_job_get(job_id),保留语义化命名。未找到返回 not_found。"
        ),
    )
    async def _feature_snapshot_job_status(
        job_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await feature_snapshot_job_status(app_context(ctx), job_id)

    @mcp.tool(
        name="finboard_factor_signal_list",
        description=(
            "列出因子信号(版本化、带研究状态)。"
            "每条含 signal_id/factor_name/factor_version/feature_snapshot_id/"
            "research_status(hypothesis|validated_oos|rejected)/items/"
            "validation_experiment_id。"
            "可选过滤 factor_name / research_status / limit(默认 100)。"
        ),
    )
    async def _factor_signal_list(
        factor_name: str | None = None,
        research_status: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_signal_list(
            app_context(ctx),
            factor_name=factor_name,
            research_status=research_status,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_factor_signal_get",
        description=("查询单个因子信号详情(含完整 items 逐标的信号)。未找到返回 not_found。"),
    )
    async def _factor_signal_get(
        signal_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_signal_get(app_context(ctx), signal_id)

    @mcp.tool(
        name="finboard_factor_experiment_list",
        description=(
            "列出因子实验(冻结、带状态机和验证终态)。"
            "每条含 experiment_id/hypothesis/factor_names/plan/status/"
            "validation_experiment_id/result/failure_reason。"
            "可选过滤 status(hypothesis|running|validated_oos|rejected|"
            "failed|interrupted) / comparison_group / limit(默认 100)。"
        ),
    )
    async def _factor_experiment_list(
        status: str | None = None,
        comparison_group: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_experiment_list(
            app_context(ctx),
            status=status,
            comparison_group=comparison_group,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_factor_experiment_get",
        description=("查询单个因子实验详情(含 plan/result/failure_reason)。未找到返回 not_found。"),
    )
    async def _factor_experiment_get(
        experiment_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_experiment_get(app_context(ctx), experiment_id)

    @mcp.tool(
        name="finboard_factor_experiment_create",
        description=(
            "[写] 冻结因子实验注册(不启动回测/模拟盘)。"
            "参数:hypothesis(假设描述)/ factor_names(列表)/ "
            "dataset_release_id / feature_snapshot_id / plan(含 in_sample/"
            "oos 区间 + trial_budget + benchmark_symbol + transaction_cost_bps + "
            "quantiles) / comparison_group / validation_experiment_id?(#57 实验 ID)。"
            "snapshot 与 release 必须匹配,否则 conflict。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _factor_experiment_create(
        hypothesis: str,
        factor_names: list[str],
        dataset_release_id: str,
        feature_snapshot_id: str,
        plan: dict[str, Any],
        comparison_group: str,
        validation_experiment_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_experiment_create(
            app_context(ctx),
            hypothesis=hypothesis,
            factor_names=factor_names,
            dataset_release_id=dataset_release_id,
            feature_snapshot_id=feature_snapshot_id,
            plan=plan,
            comparison_group=comparison_group,
            validation_experiment_id=validation_experiment_id,
        )

    @mcp.tool(
        name="finboard_factor_experiment_sync_validation",
        description=(
            "[写] 同步因子实验的 #57 机器验证终态。"
            "读取绑定的 validation_experiment_id 的 trial 结果,推进实验状态机:"
            "HYPOTHESIS/INTERRUPTED→RUNNING;VALIDATED_OOS→VALIDATED_OOS(含 result);"
            "REJECTED→REJECTED(含 trial_status_counts)。"
            "不接受调用者传入 passed_oos(#57 终态是唯一来源)。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _factor_experiment_sync_validation(
        experiment_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await factor_experiment_sync_validation(app_context(ctx), experiment_id)


__all__ = [
    "factor_catalog",
    "factor_experiment_create",
    "factor_experiment_get",
    "factor_experiment_list",
    "factor_experiment_sync_validation",
    "factor_signal_get",
    "factor_signal_list",
    "feature_snapshot_create",
    "feature_snapshot_get",
    "feature_snapshot_job_start",
    "feature_snapshot_job_status",
    "feature_snapshot_list",
    "register",
]
