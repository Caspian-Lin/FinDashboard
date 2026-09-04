"""``finboard.validation_experiment.*`` 工具 ——
#57 机器验证实验(OOS 样本外验证)元数据 CRUD 与执行入队(issue #138/#233)。

暴露 REST `/api/research/experiments` 的 6 个端点:
create / list / get / reject / add_trial / delete。复用现有
`ResearchExperimentRepository` / `ResearchTrialRepository` / 领域契约函数
(``new_experiment`` / ``transition_status`` / ``increment_trials_used``),
不重复业务逻辑。

执行入队(#233):``finboard_validation_experiment_run`` 把「按计划真正跑
walk-forward 并一次性揭盲」登记为 ``kind=validation_experiment`` 后台任务
(worker 单并发),入队预检秒级拒绝不可运行 / 预算耗尽 / 已揭盲的实验;
执行由 ValidationExperimentExecutor 完成,``finboard_job_get`` 轮询
(result_ref=experiment_id,终态 validated_oos → succeeded)。这是
#219 ``finboard_research_code_promote`` OOS 半边的运营入口。

与因子实验(``finboard.factor.experiment.*``,issue #78/#125)的关系:
**两套独立但耦合的系统** —— 因子实验通过 `validation_experiment_id` 引用
#57 验证实验,本批工具给 agent 提供「创建验证实验 → 登记 trial →
因子实验引用 → sync_validation 同步终态」的源头,补齐 OOS 样本外验证闭环。

边界:
* 元数据 CRUD 工具不触发 ValidationRunner;执行只能经 #233 的后台任务。
* 写操作(run / create / reject / add_trial / delete)受
  ``_require_write_enabled`` 守卫(``mcp_readonly_only``);只读(list / get)
  自动允许。
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
    """列出 #57 验证实验(可选按状态过滤),每条附派生 oos_outcome。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_backtest.validation.contracts import (
            ExperimentStatus,
            derive_oos_outcome,
        )
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

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
            trials_map = await ResearchTrialRepository(session).map_by_experiment(
                [e.experiment_id for e in experiments]
            )
            return [
                cast(
                    dict[str, Any],
                    to_jsonable(
                        {
                            **e.as_dict(),
                            # issue #310:派生结论语义(OOS 流程完成 ≠ 假设获支持)
                            "oos_outcome": derive_oos_outcome(
                                e, trials_map.get(e.experiment_id, [])
                            ).value,
                        }
                    ),
                )
                for e in experiments
            ]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.list",
        arguments={"status": status, "limit": limit},
        handler=_do,
    )


async def validation_experiment_get(app: McpAppContext, experiment_id: str) -> ToolEnvelope:
    """读取单个 #57 验证实验详情 + 全部 trial(包括 FAILED / REJECTED)。"""

    async def _do() -> dict[str, Any]:
        from finboard_backtest.validation.contracts import derive_oos_outcome
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
            # issue #310:派生结论语义(不落库,序列化时由 trial OOS 状态 +
            # 揭盲指标推导)——validated_oos 只代表 OOS 流程完成。
            data["oos_outcome"] = derive_oos_outcome(experiment, trials).value
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
# 写:run 入队(issue #233)
# ---------------------------------------------------------------------------


async def validation_experiment_run(
    app: McpAppContext,
    *,
    experiment_id: str,
    idempotency_key: str | None = None,
    requested_by: str = "agent:mcp",
) -> ToolEnvelope:
    """把 #57 验证实验执行登记为后台任务(kind=validation_experiment,写)。

    入队预检(#186 秒级失败风格):实验存在 / 状态可接受 trial /
    试验预算未超 / 未揭盲;不满足直接 invalid_argument / conflict。
    执行端(ValidationExperimentExecutor)重放同一组检查(双保险)。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from sqlalchemy.exc import IntegrityError

        from finboard_backtest.validation.contracts import ExperimentStatus
        from finboard_persistence import BackgroundJobRepository
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )
        from finboard_shared.background_jobs import (
            BackgroundJobStatus,
            generate_background_job_id,
        )

        async with app.session_maker() as session:
            experiment = await ResearchExperimentRepository(session).get(
                experiment_id
            )
            if experiment is None:
                raise McpToolError("not_found", f"未找到验证实验: {experiment_id}")
            if experiment.status not in (
                ExperimentStatus.HYPOTHESIS,
                ExperimentStatus.IN_SAMPLE,
            ):
                raise McpToolError(
                    "conflict",
                    f"实验状态不可执行: status={experiment.status.value}"
                    "(终态实验不可重复执行;已 validated_oos 的实验请直接用于 "
                    "finboard_research_code_promote)",
                )
            if experiment.final_test_unsealed:
                raise McpToolError(
                    "conflict",
                    "实验已揭盲(final_test_unsealed=true),不可再次执行"
                    "(揭盲不可重做)",
                )
            if not experiment.can_run_trial():
                raise McpToolError(
                    "invalid_argument",
                    "试验预算已耗尽: trials_used="
                    f"{experiment.trials_used}/{experiment.plan.trial_budget};"
                    "请提高 plan.trial_budget 新建实验(supersedes_id 关联)"
                    "或改用 finboard_validation_experiment_add_trial 手动登记",
                )
            key = idempotency_key or f"validation_experiment:{experiment_id}"
            payload: dict[str, Any] = {"experiment_id": experiment_id}
            checksum = _payload_checksum(payload)
            try:
                row, created = await BackgroundJobRepository(
                    session
                ).create_or_get(
                    job_id=generate_background_job_id(),
                    idempotency_key=key,
                    kind="validation_experiment",
                    queue="research",
                    status=BackgroundJobStatus.QUEUED.value,
                    priority=0,
                    payload=payload,
                    payload_checksum=checksum,
                    # 揭盲是一次性门:不自动重试(重试只会重复消耗试验预算);
                    # 失败后 agent 排查后换新 idempotency_key 重新入队(续跑安全:
                    # 已落库 trial 不重复执行,trials_used 不重复递增)。
                    max_attempts=1,
                    requested_by=requested_by,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "重复 idempotency_key") from exc
            return cast(
                dict[str, Any],
                to_jsonable(
                    {
                        "job_id": row.job_id,
                        "kind": "validation_experiment",
                        "experiment_id": experiment_id,
                        "status": row.status,
                        "created": created,
                        "idempotency_key": key,
                        "detail_hint": (
                            "finboard_job_get 轮询(result_ref=experiment_id,"
                            "终态 validated_oos → succeeded / rejected → failed "
                            "附原因);完成后把实验 id 传给 "
                            "finboard_research_code_promote"
                        ),
                    }
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.validation_experiment.run",
        arguments={"experiment_id": experiment_id, "requested_by": requested_by},
        handler=_do,
        idempotency_key=idempotency_key or f"validation_experiment:{experiment_id}",
    )


def _payload_checksum(payload: dict[str, Any]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


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
            "robustness/status/trials_used/final_test_unsealed/notes,以及派生"
            "oos_outcome(supported|not_supported|inconclusive,#310):"
            "validated_oos 只代表 OOS 流程完成,不代表假设获支持——best trial"
            "OOS 被拒后仍揭盲是 #245 决策 A 的设计使然,此时 status=validated_oos "
            "而 oos_outcome=not_supported。"
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
            "多重试验修正需要真实试验总数)。返回附派生 oos_outcome"
            "(supported|not_supported|inconclusive,#310):supported=best trial "
            "OOS 门过且揭盲达标;not_supported=best trial OOS 被拒或揭盲未达标"
            "(status=validated_oos 也可能 not_supported——OOS 流程完成 ≠ 假设"
            "获支持);inconclusive=无 trial / OOS 无可判定证据 / 流程未走完。"
            "全 trial OOS 被拒仍揭盲时 notes 含 unseal_with_rejected_trials 警示。"
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

    @mcp.tool(
        name="finboard_validation_experiment_run",
        description=(
            "[写] 入队执行 #57 机器验证实验(#233:kind=validation_experiment "
            "后台任务,worker 单并发)—— 按冻结计划跑 IS 参数搜索 → walk-forward "
            "OOS(含稳健性与统计修正)→ 一次性揭盲最终测试集,trial 与实验状态"
            "逐部落库。入队预检秒级拒绝:实验不存在(not_found)/ 终态或已揭盲"
            "(conflict,揭盲不可重做)/ 预算耗尽(invalid_argument)。实验的 "
            "version_stamp.selection_config 须声明 validation_trial_runner:"
            "{strategy, symbols, provider?, params?, capital?}(注册表策略回测),"
            "未声明执行期报 trial_runner_unconfigured。"
            "参数:experiment_id / idempotency_key?(默认 "
            "validation_experiment:<experiment_id>) / requested_by?(默认 "
            "agent:mcp)。返回 job_id + created;finboard_job_get 轮询"
            "(result_ref=experiment_id,validated_oos → succeeded,rejected → "
            "failed 附阈值原因)。完成后把 experiment_id 传给 "
            "finboard_research_code_promote 补 OOS 门。写操作,"
            "mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _validation_experiment_run(
        experiment_id: str,
        idempotency_key: str | None = None,
        requested_by: str = "agent:mcp",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await validation_experiment_run(
            app_context(ctx),
            experiment_id=experiment_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )


__all__ = [
    "register",
    "validation_experiment_add_trial",
    "validation_experiment_create",
    "validation_experiment_delete",
    "validation_experiment_get",
    "validation_experiment_list",
    "validation_experiment_reject",
    "validation_experiment_run",
]
