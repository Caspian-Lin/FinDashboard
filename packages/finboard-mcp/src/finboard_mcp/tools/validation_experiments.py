"""``finboard.validation_experiment.*`` 工具 ——
#57 机器验证实验(OOS 样本外验证)元数据 CRUD(issue #138)。

暴露 REST `/api/research/experiments` 的 6 个端点:
create / list / get / reject / add_trial / delete。复用现有
`ResearchExperimentRepository` / `ResearchTrialRepository` / 领域契约函数
(`new_experiment` / `transition_status` / `increment_trials_used`),
不重复业务逻辑。

与因子实验(`finboard.factor.experiment.*`,issue #78/#125)的关系:
**两套独立但耦合的系统** —— 因子实验通过 `validation_experiment_id` 引用
#57 验证实验,本批工具给 agent 提供「创建验证实验 → 登记 trial →
因子实验引用 → sync_validation 同步终态」的源头,补齐 OOS 样本外验证闭环。

边界:
* 本批工具只做实验元数据 CRUD,不触发 ValidationRunner 执行(长耗时执行
  任务化见 #117/#136);揭盲端点(`unseal-final`)未实现,不在本批覆盖。
* 写操作(create / reject / add_trial / delete)受 `_require_write_enabled`
  守卫(`mcp_readonly_only`);只读(list / get)自动允许。
* 不触及交易安全红线(不创建订单 / 持仓 / 回测 / 模拟盘)。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from finboard_backtest.validation.contracts import (
        AcceptanceThresholds,
        RobustnessPlan,
        ValidationPlan,
        VersionStamp,
    )


# ---------------------------------------------------------------------------
# 辅助:参数 schema → 领域对象(与 REST 路由 `_to_plan` 等口径一致)
# ---------------------------------------------------------------------------


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""

    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _build_version_stamp(raw: dict[str, Any]) -> VersionStamp:
    from finboard_backtest.validation.contracts import VersionStamp

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
        raise McpToolError(
            "invalid_argument",
            f"version_stamp 参数不完整: {exc}",
        ) from exc


def _build_plan(raw: dict[str, Any]) -> ValidationPlan:
    from finboard_backtest.validation.contracts import ValidationMode, ValidationPlan

    try:
        return ValidationPlan(
            mode=ValidationMode(raw["mode"]),
            train_start=date.fromisoformat(raw["train_start"]),
            train_end=date.fromisoformat(raw["train_end"]),
            validation_start=date.fromisoformat(raw["validation_start"]),
            validation_end=date.fromisoformat(raw["validation_end"]),
            test_start=date.fromisoformat(raw["test_start"]),
            test_end=date.fromisoformat(raw["test_end"]),
            train_window_days=int(raw.get("train_window_days", 504)),
            test_window_days=int(raw.get("test_window_days", 63)),
            step_days=int(raw.get("step_days", 63)),
            trial_budget=int(raw.get("trial_budget", 50)),
            random_seed=int(raw.get("random_seed", 0)),
            benchmark_symbol=raw.get("benchmark_symbol"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise McpToolError(
            "invalid_argument",
            f"plan 参数非法: {exc}",
        ) from exc


def _build_thresholds(raw: dict[str, Any] | None) -> AcceptanceThresholds:
    from finboard_backtest.validation.contracts import AcceptanceThresholds

    if not raw:
        return AcceptanceThresholds()
    try:
        return AcceptanceThresholds(**raw)
    except (TypeError, ValueError) as exc:
        raise McpToolError(
            "invalid_argument",
            f"thresholds 参数非法: {exc}",
        ) from exc


def _build_robustness(raw: dict[str, Any] | None) -> RobustnessPlan:
    from finboard_backtest.validation.contracts import RobustnessPlan

    if not raw:
        return RobustnessPlan()
    try:
        return RobustnessPlan(
            neighbourhood_steps=int(raw.get("neighbourhood_steps", 5)),
            neighbourhood_relative_step=float(raw.get("neighbourhood_relative_step", 0.1)),
            cost_multipliers=tuple(float(x) for x in raw.get("cost_multipliers", [1.0, 2.0, 3.0])),
            slippage_stress_bps=tuple(
                float(x) for x in raw.get("slippage_stress_bps", [0.0, 5.0, 10.0, 20.0])
            ),
            execution_delay_bars=tuple(int(x) for x in raw.get("execution_delay_bars", [1, 2])),
            stress_phases=tuple(
                raw.get(
                    "stress_phases",
                    ["2018-Q4", "2020-Q1", "2022-Q1", "2024-Q1"],
                )
            ),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise McpToolError(
            "invalid_argument",
            f"robustness 参数非法: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# 只读:list / get
# ---------------------------------------------------------------------------


async def validation_experiment_list(
    app: McpAppContext,
    *,
    status: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    """列出 #57 验证实验(可选按状态过滤)。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_backtest.validation.contracts import ExperimentStatus
        from finboard_persistence.validation_repo import ResearchExperimentRepository

        resolved = None
        if status is not None:
            try:
                resolved = ExperimentStatus(status)
            except ValueError as exc:
                raise McpToolError(
                    "invalid_argument",
                    f"非法实验状态: {status}",
                ) from exc
        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            experiments = await ResearchExperimentRepository(session).list_by_status(
                resolved, limit=safe_limit
            )
            return [cast(dict[str, Any], to_jsonable(e.as_dict())) for e in experiments]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.list",
        arguments={"status": status, "limit": limit},
        handler=_do,
    )


async def validation_experiment_get(app: McpAppContext, experiment_id: str) -> ToolEnvelope:
    """读取单个 #57 验证实验详情 + 全部 trial(包括 FAILED / REJECTED)。"""

    async def _do() -> dict[str, Any]:
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        async with app.session_maker() as session:
            experiment = await ResearchExperimentRepository(session).get(experiment_id)
            if experiment is None:
                raise McpToolError("not_found", f"未找到验证实验: {experiment_id}")
            trials = await ResearchTrialRepository(session).list_by_experiment(experiment_id)
            data = dict(experiment.as_dict())
            data["trials"] = [cast(dict[str, Any], to_jsonable(t.as_dict())) for t in trials]
            return cast(dict[str, Any], to_jsonable(data))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.get",
        arguments={"experiment_id": experiment_id},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# 写:create / reject / add_trial / delete
# ---------------------------------------------------------------------------


async def validation_experiment_create(
    app: McpAppContext,
    *,
    hypothesis: str,
    version_stamp: dict[str, Any],
    plan: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    robustness: dict[str, Any] | None = None,
    strategy_params_space: dict[str, Any] | None = None,
    supersedes_id: str | None = None,
    notes: str = "",
) -> ToolEnvelope:
    """创建 #57 验证实验 —— 假设 / 计划 / 门一次性冻结(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.validation.contracts import new_experiment
        from finboard_persistence.validation_repo import ResearchExperimentRepository

        if len(hypothesis.strip()) < 10:
            raise McpToolError("invalid_argument", "hypothesis 至少 10 个字符")
        experiment = new_experiment(
            hypothesis=hypothesis,
            version_stamp=_build_version_stamp(version_stamp),
            plan=_build_plan(plan),
            thresholds=_build_thresholds(thresholds),
            robustness=_build_robustness(robustness),
            strategy_params_space=dict(strategy_params_space or {}),
            supersedes_id=supersedes_id,
            notes=notes or "",
        )
        async with app.session_maker() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        return cast(dict[str, Any], to_jsonable(experiment.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.create",
        arguments={
            "hypothesis": hypothesis,
            "version_stamp": version_stamp,
            "plan": plan,
            "thresholds": thresholds,
            "robustness": robustness,
            "strategy_params_space": strategy_params_space,
            "supersedes_id": supersedes_id,
            "notes": notes,
        },
        handler=_do,
    )


async def validation_experiment_reject(
    app: McpAppContext,
    *,
    experiment_id: str,
    reason: str,
) -> ToolEnvelope:
    """主动拒绝 #57 验证实验(设置 REJECTED + 原因,写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.validation.contracts import (
            ExperimentStatus,
            transition_status,
        )
        from finboard_persistence.validation_repo import ResearchExperimentRepository

        if not reason.strip():
            raise McpToolError("invalid_argument", "reason 不能为空")
        async with app.session_maker() as session:
            repo = ResearchExperimentRepository(session)
            experiment = await repo.get(experiment_id)
            if experiment is None:
                raise McpToolError("not_found", f"未找到验证实验: {experiment_id}")
            if experiment.status in (
                ExperimentStatus.VALIDATED_OOS,
                ExperimentStatus.SUPERSEDED,
            ):
                raise McpToolError(
                    "conflict",
                    f"cannot reject experiment in status={experiment.status.value}",
                )
            try:
                updated = transition_status(
                    experiment,
                    ExperimentStatus.REJECTED,
                    rejection_reason=reason,
                )
            except ValueError as exc:
                raise McpToolError("conflict", str(exc)) from exc
            await repo.save(updated)
            await session.commit()
            return cast(dict[str, Any], to_jsonable(updated.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.reject",
        arguments={"experiment_id": experiment_id, "reason": reason},
        handler=_do,
    )


async def validation_experiment_add_trial(
    app: McpAppContext,
    *,
    experiment_id: str,
    parameters: dict[str, Any],
    status: str = "candidate",
    failure_reason: str | None = None,
) -> ToolEnvelope:
    """手动登记一次 #57 实验 trial(不通过 runner 自动跑,写操作)。

    外部 worker / CLI 跑完回测后把结果回写为 trial;失败也算试验
    (多重试验修正需要真实试验总数)。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from uuid import uuid4

        from finboard_backtest.validation.contracts import (
            TrialRecord,
            TrialStatus,
            increment_trials_used,
        )
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        try:
            trial_status = TrialStatus(status)
        except ValueError as exc:
            raise McpToolError("invalid_argument", f"非法 trial 状态: {status}") from exc
        async with app.session_maker() as session:
            exp_repo = ResearchExperimentRepository(session)
            trial_repo = ResearchTrialRepository(session)
            experiment = await exp_repo.get(experiment_id)
            if experiment is None:
                raise McpToolError("not_found", f"未找到验证实验: {experiment_id}")
            if not experiment.can_run_trial():
                raise McpToolError(
                    "conflict",
                    "experiment cannot accept new trials "
                    f"(status={experiment.status.value}, "
                    f"trials_used={experiment.trials_used}/"
                    f"{experiment.plan.trial_budget})",
                )
            existing = await trial_repo.list_by_experiment(experiment_id)
            trial = TrialRecord(
                trial_id=f"{experiment_id}-mcp-{uuid4().hex[:8]}",
                experiment_id=experiment_id,
                trial_index=len(existing),
                parameters=dict(parameters),
                status=trial_status,
                failure_reason=failure_reason,
            )
            await trial_repo.save(trial)
            try:
                experiment = increment_trials_used(experiment)
            except ValueError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            await exp_repo.save(experiment)
            await session.commit()
            return cast(dict[str, Any], to_jsonable(trial.as_dict()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.add_trial",
        arguments={
            "experiment_id": experiment_id,
            "parameters": parameters,
            "status": status,
            "failure_reason": failure_reason,
        },
        handler=_do,
    )


async def validation_experiment_delete(app: McpAppContext, experiment_id: str) -> ToolEnvelope:
    """删除 #57 验证实验(级联删除 trial,写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from sqlalchemy.exc import IntegrityError

        from finboard_persistence.validation_repo import ResearchExperimentRepository

        async with app.session_maker() as session:
            repo = ResearchExperimentRepository(session)
            try:
                deleted = await repo.delete(experiment_id)
                if not deleted:
                    raise McpToolError("not_found", f"未找到验证实验: {experiment_id}")
                await session.commit()
            except IntegrityError as exc:
                # 因子实验的 validation_experiment_id 外键仍引用本实验
                await session.rollback()
                raise McpToolError(
                    "conflict",
                    "验证实验仍被因子实验引用(validation_experiment_id),无法删除;请先解除引用",
                ) from exc
            return {"deleted": True, "experiment_id": experiment_id}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.delete",
        arguments={"experiment_id": experiment_id},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """把 #57 验证实验工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_validation_experiment_create",
        description=(
            "[写] 创建 #57 机器验证实验(OOS 样本外验证)—— 假设 / 计划 / 门"
            "一次性冻结,创建后 hypothesis 不可修改,如需变更请新建实验并用"
            "supersedes_id 关联旧版本。"
            "参数:hypothesis(≥10 字符)/ version_stamp(matching_model_version, "
            "asset_rules_version, strategy_kind 必填;factor_version, "
            "dataset_versions, selection_config 可选)/ "
            "plan(mode=rolling|expanding, train_start~test_end 六个 ISO 日期必填;"
            "train_window_days=504, test_window_days=63, step_days=63, "
            "trial_budget=50, random_seed=0, benchmark_symbol 有默认)/ "
            "thresholds?(min_in_sample_sharpe=1.0 等 10 项,全部有默认)/ "
            "robustness?(neighbourhood_steps=5 等 6 项,全部有默认)/ "
            "strategy_params_space?(候选参数空间)/ supersedes_id? / notes?。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _validation_experiment_create(
        hypothesis: str,
        version_stamp: dict[str, Any],
        plan: dict[str, Any],
        thresholds: dict[str, Any] | None = None,
        robustness: dict[str, Any] | None = None,
        strategy_params_space: dict[str, Any] | None = None,
        supersedes_id: str | None = None,
        notes: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_create(
            app_context(ctx),
            hypothesis=hypothesis,
            version_stamp=version_stamp,
            plan=plan,
            thresholds=thresholds,
            robustness=robustness,
            strategy_params_space=strategy_params_space,
            supersedes_id=supersedes_id,
            notes=notes,
        )

    @mcp.tool(
        name="finboard_validation_experiment_list",
        description=(
            "列出 #57 机器验证实验(可选按状态过滤 hypothesis|in_sample|"
            "validated_oos|rejected|superseded)。"
            "每条含 experiment_id/hypothesis/version_checksum/plan/thresholds/"
            "robustness/status/trials_used/final_test_unsealed。"
            "参数:status? / limit(默认 100,上限 500)。"
        ),
    )
    async def _validation_experiment_list(
        status: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_list(app_context(ctx), status=status, limit=limit)

    @mcp.tool(
        name="finboard_validation_experiment_get",
        description=(
            "查询单个 #57 机器验证实验详情 + 全部 trial(包括 FAILED / REJECTED,"
            "多重试验修正需要真实试验总数)。"
            "参数:experiment_id。未找到返回 not_found。"
        ),
    )
    async def _validation_experiment_get(
        experiment_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_get(app_context(ctx), experiment_id)

    @mcp.tool(
        name="finboard_validation_experiment_reject",
        description=(
            "[写] 主动拒绝 #57 机器验证实验(设置 REJECTED + 原因,不可回退)。"
            "参数:experiment_id / reason(不能为空)。"
            "validated_oos / superseded 状态不可拒绝 → conflict(409 语义);"
            "其他非法状态转换同样返回 conflict。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _validation_experiment_reject(
        experiment_id: str,
        reason: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_reject(
            app_context(ctx), experiment_id=experiment_id, reason=reason
        )

    @mcp.tool(
        name="finboard_validation_experiment_add_trial",
        description=(
            "[写] 手动登记一次 #57 实验 trial(不通过 runner 自动跑)。"
            "外部 worker / CLI 跑完回测后把结果回写为 trial;失败也算试验。"
            "参数:experiment_id / parameters(参数快照 dict)/ "
            "status?(candidate|running|selected|rejected|failed|skipped,"
            "默认 candidate)/ failure_reason?。"
            "预算耗尽或终态实验不可新增 → conflict(409 语义)。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _validation_experiment_add_trial(
        experiment_id: str,
        parameters: dict[str, Any],
        status: str = "candidate",
        failure_reason: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_add_trial(
            app_context(ctx),
            experiment_id=experiment_id,
            parameters=parameters,
            status=status,
            failure_reason=failure_reason,
        )

    @mcp.tool(
        name="finboard_validation_experiment_delete",
        description=(
            "[写] 删除 #57 机器验证实验(级联删除全部 trial,不可恢复)。"
            "参数:experiment_id。未找到返回 not_found;"
            "仍被因子实验引用(validation_experiment_id 外键)→ conflict。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _validation_experiment_delete(
        experiment_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_delete(app_context(ctx), experiment_id)


__all__ = [
    "register",
    "validation_experiment_add_trial",
    "validation_experiment_create",
    "validation_experiment_delete",
    "validation_experiment_get",
    "validation_experiment_list",
    "validation_experiment_reject",
]
