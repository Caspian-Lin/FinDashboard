"""``finboard.run.*`` 工具 —— ResearchRun 查询与写操作(复用 ``ResearchRunRepository``)。

只读(3):list / get / artifacts —— 自动允许。
写(4,issue #127):queue(冻结 + 登记 queued)/ cancel /
replay(复制 completed;interrupted 即事故恢复通道,#305)/
lineage(artifact 血缘)。

写工具在 ``mcp_readonly_only=false`` 时由 agent 自主执行(#122),不执行回测本身
(回测执行由离线 worker 调 ``ResearchRunCoordinator`` 完成)。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_persistence.research_run_repo import ResearchRunRepository
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel


def _run_summary(row: ResearchRunModel) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "strategy_id": row.strategy_id,
        "strategy_kind": row.strategy_kind,
        "status": row.status,
        "schema_version": row.schema_version,
        "requested_by": row.requested_by,
        "created_at": to_jsonable(row.created_at),
        "started_at": to_jsonable(row.started_at),
        "completed_at": to_jsonable(row.completed_at),
        "error_code": row.error_code,
        # issue #183:执行模式(single_shot|multi_period),入队即标注。
        "execution_mode": _execution_mode_from_manifest(row.manifest),
    }


def _execution_mode_from_manifest(manifest: object) -> str:
    """从存储的 manifest dict 推导 execution_mode(issue #183 quick win)。"""
    from finboard_backtest.research_run.contracts import execution_mode_for

    if not isinstance(manifest, dict):
        return "single_shot"
    parameters = manifest.get("parameters")
    if not isinstance(parameters, dict):
        return "single_shot"
    return execution_mode_for(parameters).value


def _run_detail(row: ResearchRunModel) -> dict[str, Any]:
    detail = _run_summary(row)
    detail.update(
        {
            "idempotency_key": row.idempotency_key,
            "replay_of_run_id": row.replay_of_run_id,
            "manifest_checksum": row.manifest_checksum,
            "result_checksum": row.result_checksum,
            "manifest": row.manifest,
            "result": row.result,
            "error_summary": row.error_summary,
            # issue #183:agent 用 execution_mode 区分单时点决策与全区间回放。
            "execution_mode": _execution_mode_from_manifest(row.manifest),
            # issue #143:关联 background_jobs.job_id,agent 可用 finboard_job_* 轮询。
            "job_id": getattr(row, "job_id", None),
        }
    )
    return cast(dict[str, Any], to_jsonable(detail))


def _run_view(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel] | None = None,
    *,
    view: str = "summary",
) -> dict[str, Any]:
    """run 详情视图(issue #206):summary 默认聚合计数,detail 全量。

    summary 在 detail 的头部字段之上,把 result 剔除 equity_curve(以点数
    提示),并附 universe / fills 服务端聚合计数,不序列化 manifest/result
    与逐标的全量 payload。
    """
    if view not in ("summary", "detail"):
        raise McpToolError("invalid_argument", f"未知视图: {view}")
    if view == "detail":
        return _run_detail(row)
    from finboard_mcp.reporting import (
        metrics_without_equity_curve,
        summarize_run_artifacts,
    )

    payload = _run_summary(row)
    payload.update(
        {
            "idempotency_key": row.idempotency_key,
            "replay_of_run_id": row.replay_of_run_id,
            "manifest_checksum": row.manifest_checksum,
            "result_checksum": row.result_checksum,
            "error_summary": row.error_summary,
            "job_id": getattr(row, "job_id", None),
            "metrics": metrics_without_equity_curve(
                dict(row.result) if row.result else {}
            ),
            "view": "summary",
        }
    )
    if artifacts is not None:
        payload["artifact_count"] = len(artifacts)
        payload.update(summarize_run_artifacts(artifacts))
    return cast(dict[str, Any], to_jsonable(payload))


def _run_ack(row: ResearchRunModel) -> dict[str, Any]:
    """写操作精简回执(issue #206 P1):id/status/checksum/execution_mode/created_at。

    全量详情走 ``finboard_run_get(run_id)``;回执附带 job_id 供轮询。
    """
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "run_id": row.run_id,
                "job_id": getattr(row, "job_id", None),
                "strategy_id": row.strategy_id,
                "strategy_kind": row.strategy_kind,
                "status": row.status,
                "checksum": row.manifest_checksum,
                "execution_mode": _execution_mode_from_manifest(row.manifest),
                "created_at": to_jsonable(row.created_at),
                "view": "ack",
                "detail_hint": f"finboard_run_get(run_id={row.run_id!r})",
            }
        ),
    )


def _artifact_summary(row: ResearchRunArtifactModel) -> dict[str, Any]:
    return cast(dict[str, Any], to_jsonable(
        {
            "artifact_id": row.artifact_id,
            "sequence": row.sequence,
            "stage": row.stage,
            "trace_id": row.trace_id,
            "decision_id": row.decision_id,
            "checksum": row.checksum,
            "payload": row.payload,
        }
    ))


def _payload_checksum(payload: dict[str, object]) -> str:
    """计算 background_jobs payload checksum(与 routes/research_runs.py 一致)。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue_research_run_job(
    session: AsyncSession,
    *,
    run_id: str,
    strategy_kind: str,
    idempotency_key: str,
    requested_by: str,
) -> str:
    """同事务为研究运行创建 background_jobs 行,返回 job_id(issue #143)。"""
    payload: dict[str, object] = {
        "run_id": run_id,
        "strategy_kind": strategy_kind,
    }
    job_repo = BackgroundJobRepository(session)
    job_row, _ = await job_repo.create_or_get(
        job_id=generate_background_job_id(),
        idempotency_key=idempotency_key,
        kind="research_run",
        queue="research",
        status=BackgroundJobStatus.QUEUED.value,
        priority=0,
        payload=payload,
        payload_checksum=_payload_checksum(payload),
        max_attempts=3,
        requested_by=requested_by,
    )
    return job_row.job_id


async def list_runs(
    app: McpAppContext,
    *,
    statuses: list[str] | None = None,
    strategy_kind: str | None = None,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            rows = await repo.list_recent(
                statuses=tuple(statuses) if statuses else None,
                strategy_kind=strategy_kind,
                limit=limit,
            )
            return [_run_summary(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.list",
        arguments={
            "statuses": statuses,
            "strategy_kind": strategy_kind,
            "limit": limit,
        },
        handler=_do,
    )


async def get_run(
    app: McpAppContext,
    run_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            artifacts = (
                await repo.list_artifacts(run_id) if view == "summary" else None
            )
            return _run_view(row, artifacts, view=view)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.get",
        arguments={"run_id": run_id, "view": view},
        handler=_do,
    )


async def list_artifacts(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            artifacts = await repo.list_artifacts(run_id)
            return [_artifact_summary(item) for item in artifacts]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.artifacts",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 写工具(issue #127):queue / cancel / replay / lineage
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


async def _build_queued_manifest(
    body: Any,
    session: Any,
    *,
    sandbox_enabled: bool = False,
) -> tuple[Any, Any]:
    """复用 API 路由 ``queue_research_run`` 的冻结 + manifest 构建逻辑。

    返回 ``(manifest, strategy_row)``。校验失败抛 ``McpToolError``。
    """
    import hashlib
    from typing import cast as _cast

    from finboard_backtest.research_run import (
        FrozenArtifactRef,
        ResearchActorType,
        ResearchRunManifest,
        UnsupportedResearchCapabilityError,
        validate_strategy_dataset_capabilities,
    )
    from finboard_backtest.research_run.config_overrides import (
        research_portfolio_gate_error,
    )
    from finboard_backtest.research_run.contracts import JsonValue
    from finboard_backtest.research_run.signal_engine import (
        multi_period_feature_gate_error,
        single_shot_snapshot_gate_error,
    )
    from finboard_backtest.strategy_spec import ResearchStrategySpec
    from finboard_backtest.strategy_spec.contracts import FeatureKind
    from finboard_backtest.strategy_spec.universe_precheck import (
        describe_empty_pool,
        preview_universe_pool,
        resolvable_feature_names,
    )
    from finboard_data.releases import ReleaseCapabilityError
    from finboard_persistence import (
        FeatureSnapshotRepository,
        ResearchDatasetReleaseRepository,
        ResearchStrategySpecRepository,
    )

    spec_repo = ResearchStrategySpecRepository(session)
    strategy_row = await spec_repo.get_version(body.strategy_id, body.strategy_version)
    if strategy_row is None:
        raise McpToolError("not_found", "策略规格版本不存在")
    if strategy_row.status != "published":
        raise McpToolError("conflict", "仅已发布策略规格可以进入研究运行")
    spec = ResearchStrategySpec.model_validate(strategy_row.payload)
    if sorted(body.dataset_release_ids) != sorted(
        spec.validation_plan.dataset_release_ids
    ):
        raise McpToolError(
            "invalid_argument",
            "运行数据发布必须与策略验证计划完全一致",
        )
    try:
        releases = [
            await ResearchDatasetReleaseRepository(session).require_usable(rid)
            for rid in body.dataset_release_ids
        ]
    except ReleaseCapabilityError as exc:
        raise McpToolError("invalid_argument", str(exc)) from exc
    try:
        validate_strategy_dataset_capabilities(
            spec.strategy_kind,
            {
                item.key
                for release in releases
                for item in release.capabilities
                if item.ready
            },
        )
    except UnsupportedResearchCapabilityError as exc:
        raise McpToolError("invalid_argument", str(exc)) from exc

    feature_repo = FeatureSnapshotRepository(session)
    snapshots: list[Any] = []
    for snapshot_id in body.factor_snapshot_ids:
        snapshot = await feature_repo.get(snapshot_id)
        if snapshot is None:
            raise McpToolError(
                "invalid_argument", f"因子快照不存在: {snapshot_id}"
            )
        snapshots.append(snapshot)
    required_factor_sources = {
        node.source
        for node in spec.feature_graph.nodes
        if node.source is not None
        and node.kind in {FeatureKind.FACTOR, FeatureKind.RISK_FACTOR}
    }
    # issue #203:入队期 single_shot 缺快照秒级拒绝(与 REST 路由共用同一门控
    # 函数,对齐 #186 预检风格)。multi_period 声明 rebalance_frequency 后不受影响。
    gate_error = single_shot_snapshot_gate_error(
        strategy_kind=spec.strategy_kind,
        required_factor_sources=required_factor_sources,
        frozen_snapshot_count=len(snapshots),
        parameters=body.parameters,
    )
    if gate_error is not None:
        raise McpToolError("invalid_argument", gate_error)
    # issue #217:用户因子(u_ 前缀)入队门控(与 REST 路由共用同一函数)。
    from finboard_backtest.research_code import (
        active_user_factor_names,
        resolve_screen_bindings,
        screen_factor_snapshot_gate_error,
        user_factor_reference_gate_error,
    )
    from finboard_backtest.research_sandbox.factor_publish import (
        sandbox_snapshot_dataset_release_ids,
    )

    # issue #234:screen 绑定实绑校验(REST+MCP 共用同一门控)—— 声明了
    # screen_artifact_bindings 的规格按 DB 逐条校验,通过后绑定名并入可引用
    # 名单;未声明绑定的普通规格行为完全不变。
    resolution, bind_error = await resolve_screen_bindings(session, spec=spec)
    if bind_error is not None:
        raise McpToolError("invalid_argument", bind_error)

    active_factors = await active_user_factor_names(session)
    if resolution is not None:
        active_factors = active_factors | resolution.user_factor_names
    user_gate_error = user_factor_reference_gate_error(
        required_factor_sources=required_factor_sources,
        active_user_factors=active_factors,
        parameters=body.parameters,
    )
    if user_gate_error is not None:
        raise McpToolError("invalid_argument", user_gate_error)
    # issue #253:multi_period 特征可用性入队门控(与 REST 路由共用同一函数)。
    # 放在用户因子门控之后:multi_period 引用 u_ 因子先按 #217 具名拒绝。
    feature_gate_error = multi_period_feature_gate_error(
        identity_sources={
            node.source
            for node in spec.feature_graph.nodes
            if node.source is not None
        },
        parameters=body.parameters,
        research_release_kinds=[release.dataset_kind for release in releases],
        snapshot_feature_names=[
            observation.feature_name
            for snapshot in snapshots
            for observation in snapshot.observations
        ],
    )
    if feature_gate_error is not None:
        raise McpToolError("invalid_argument", feature_gate_error)
    # issue #218:user_code 策略入队门控(与 REST 路由共用同一函数);放行时
    # 把 active commit 冻结进 spec(manifest input_checksum 覆盖代码版本);
    # issue #234:screen 绑定的 draft 产物 commit/ID 一并冻结。
    spec_checksum = strategy_row.checksum
    if spec.code_artifact is not None:
        from finboard_backtest.research_code import (
            active_user_strategy_commits,
            freeze_user_code_commit,
            user_code_reference_gate_error,
        )

        merged_strategies = dict(await active_user_strategy_commits(session))
        if resolution is not None:
            merged_strategies.update(resolution.strategy_commits)
        code_gate_error = user_code_reference_gate_error(
            code_artifact_name=spec.code_artifact.name,
            code_artifact_commit=spec.code_artifact.commit,
            active_user_strategies=merged_strategies,
            sandbox_enabled=sandbox_enabled,
            frozen_snapshot_count=len(snapshots),
            parameters=body.parameters,
        )
        if code_gate_error is not None:
            raise McpToolError("invalid_argument", code_gate_error)
        frozen_spec = freeze_user_code_commit(
            spec,
            merged_strategies,
            artifact_ids=(
                resolution.strategy_artifact_ids if resolution is not None else None
            ),
        )
        if frozen_spec is not spec:
            # 冻结 commit/artifact_id 改变了 payload:manifest 内的 checksum
            # 按 post_init 契约必须等于冻结后规格的 checksum(发布版本本身的
            # 追溯仍由 strategy_version + 冻结绑定字段承载)。
            from finboard_backtest.research_run.contracts import stable_checksum

            spec_checksum = stable_checksum(frozen_spec.canonical_payload())
            spec = frozen_spec
    release_ids = {release.release_id for release in releases}
    for snapshot in snapshots:
        # issue #217:沙箱快照(dataset_release_id=None)按其锚定 run 冻结的
        # 发布集合校验 ⊆ 本次冻结清单。
        sandbox_release_ids = await sandbox_snapshot_dataset_release_ids(
            session, snapshot
        )
        if sandbox_release_ids is None:
            if snapshot.dataset_release_id not in release_ids:
                raise McpToolError(
                    "invalid_argument",
                    f"因子快照 {snapshot.snapshot_id} 绑定的数据发布不在本次冻结清单中",
                )
        elif not sandbox_release_ids <= release_ids:
            raise McpToolError(
                "invalid_argument",
                f"沙箱因子快照 {snapshot.snapshot_id} 锚定 run 的数据发布 "
                f"{sorted(sandbox_release_ids - release_ids)} 不在本次冻结清单中",
            )

    # issue #234:factor 通道 screen 运行的快照证据预检(与 REST 共用)——
    # 绑定的 draft 产物必须已有其 RCR 产出的快照进入 factor_snapshot_ids。
    if resolution is not None:
        snapshot_gate_error = await screen_factor_snapshot_gate_error(
            session, resolution=resolution, snapshots=snapshots
        )
        if snapshot_gate_error is not None:
            raise McpToolError("invalid_argument", snapshot_gate_error)

    # issue #186:入队同步候选池非空校验(与 REST 路由同一评估函数)。
    # 空池秒级 invalid_argument,附各过滤条件排除统计与缺失字段名。
    preview = preview_universe_pool(
        spec.universe,
        releases[0].instruments,
        decision_date=releases[0].end_date,
        available_features=resolvable_feature_names(
            feature_graph_sources=[
                node.source for node in spec.feature_graph.nodes
                if node.source is not None
            ],
            snapshot_feature_names=[
                observation.feature_name
                for snapshot in snapshots
                for observation in snapshot.observations
            ],
            research_release_kinds=[release.dataset_kind for release in releases],
        ),
    )
    if preview.is_empty:
        raise McpToolError(
            "invalid_argument",
            "运行数据发布候选池为空,拒绝入队: "
            f"{describe_empty_pool(preview, decision_date=releases[0].end_date)}",
        )

    # issue #303:入队组合可行性预检(与 REST 路由共用同一门控函数)—— 静态
    # 候选池 < ceil(1/生效 max_risk_contribution) 秒级拒绝(附排除统计、生效
    # 阈值与键位修复路径);阈值非法值与 risk_config.overrides 形态错误同样
    # 入队即拒。逐期真实买入池不可精确预知,运行期 fail-closed 兜底不变。
    portfolio_gate_error = research_portfolio_gate_error(
        preview=preview,
        risk_exit_policy=spec.risk_exit_policy,
        portfolio_overrides=body.portfolio_config,
        risk_overrides=body.risk_config,
        decision_date=releases[0].end_date,
    )
    if portfolio_gate_error is not None:
        raise McpToolError("invalid_argument", portfolio_gate_error)

    run_id = "RR-" + hashlib.sha256(
        body.idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    manifest = ResearchRunManifest(
        run_id=run_id,
        idempotency_key=body.idempotency_key,
        strategy_spec=spec,
        strategy_spec_checksum=spec_checksum,
        dataset_releases=tuple(
            FrozenArtifactRef(
                artifact_id=release.release_id,
                version=release.version,
                checksum=release.release_checksum,
                capabilities=tuple(
                    item.key for item in release.capabilities if item.ready
                ),
            )
            for release in releases
        ),
        strategy_version=body.strategy_version,
        factor_snapshots=tuple(
            FrozenArtifactRef(
                artifact_id=snapshot.snapshot_id,
                version=snapshot.framework_version,
                checksum=snapshot.checksum,
                capabilities=tuple(
                    sorted(
                        {
                            f"factor:{item.feature_name}"
                            for item in snapshot.observations
                        }
                    )
                ),
            )
            for snapshot in snapshots
        ),
        parameters=_cast(dict[str, JsonValue], body.parameters),
        validation_config=_cast(
            dict[str, JsonValue],
            {
                "strategy_spec": spec.validation_plan.model_dump(mode="json"),
                "overrides": body.validation_config,
            },
        ),
        portfolio_config=_cast(
            dict[str, JsonValue],
            {
                "strategy_spec": spec.portfolio_policy.model_dump(mode="json"),
                "overrides": body.portfolio_config,
            },
        ),
        risk_config=_cast(
            dict[str, JsonValue],
            {
                "strategy_spec": spec.risk_exit_policy.model_dump(mode="json"),
                "overrides": body.risk_config,
            },
        ),
        execution_config=_cast(
            dict[str, JsonValue],
            {
                "strategy_spec": spec.execution_model.model_dump(mode="json"),
                "overrides": body.execution_config,
            },
        ),
        fee_config=_cast(
            dict[str, JsonValue],
            {
                "commission_rate": spec.execution_model.commission_rate,
                "minimum_commission": spec.execution_model.minimum_commission,
                "sell_tax_rate": spec.execution_model.sell_tax_rate,
                "slippage_bps": spec.execution_model.slippage_bps,
                "overrides": body.fee_config,
            },
        ),
        benchmark_config=_cast(
            dict[str, JsonValue],
            {
                "symbol": spec.validation_plan.benchmark_symbol,
                "overrides": body.benchmark_config,
            },
        ),
        code_version=body.code_version,
        initial_capital=body.initial_capital,
        requested_by=body.requested_by,
        actor_type=ResearchActorType.HUMAN,
    )
    return manifest, strategy_row


def parse_queue_payload(payload: dict[str, Any]) -> Any:
    """规范化并校验 ``ResearchRunQueueIn`` 入队 payload(issue #174 复用)。

    ``initial_capital`` 从 JSON str / number 构造 ``Decimal``;校验失败抛
    ``McpToolError(invalid_argument)``。
    """
    from decimal import Decimal

    from finboard_api.research_run_schemas import ResearchRunQueueIn

    payload_copy = dict(payload)
    if "initial_capital" in payload_copy and not isinstance(
        payload_copy["initial_capital"], Decimal
    ):
        payload_copy["initial_capital"] = Decimal(
            str(payload_copy["initial_capital"])
        )
    try:
        return ResearchRunQueueIn.model_validate(payload_copy)
    except Exception as exc:
        raise McpToolError("invalid_argument", str(exc)) from exc


async def enqueue_research_run(
    app: McpAppContext,
    body: Any,
) -> dict[str, Any]:
    """冻结 + 登记 queued ResearchRun 的核心事务(写,不执行回测)。

    供 ``queue_run`` 与 ``backtest_run(strategy_spec=...)`` 复用:
    复用 ``_build_queued_manifest`` 的冻结校验,同事务双写 background_jobs。
    """
    from sqlalchemy.exc import IntegrityError

    from finboard_backtest.research_run import (
        ResearchRunStatus,
        to_json_value,
    )
    from finboard_persistence import (
        ResearchRunPersistenceConflictError,
        ResearchRunRepository,
    )

    async with app.session_maker() as session:
        manifest, _ = await _build_queued_manifest(
            body,
            session,
            sandbox_enabled=app.settings.research_sandbox_enabled,
        )
        try:
            row, _ = await ResearchRunRepository(session).create_or_get(
                run_id=manifest.run_id,
                idempotency_key=manifest.idempotency_key,
                replay_of_run_id=None,
                strategy_id=manifest.strategy_spec.strategy_id,
                strategy_kind=manifest.strategy_kind,
                status=ResearchRunStatus.QUEUED.value,
                schema_version=manifest.schema_version,
                manifest_checksum=manifest.checksum,
                manifest=cast(dict[str, object], to_json_value(manifest)),
                requested_by=manifest.requested_by,
            )
            # issue #143:同事务双写 background_jobs(共用 idempotency_key)。
            if not getattr(row, "job_id", None):
                row.job_id = await _enqueue_research_run_job(
                    session,
                    run_id=manifest.run_id,
                    strategy_kind=manifest.strategy_kind,
                    idempotency_key=manifest.idempotency_key,
                    requested_by=manifest.requested_by,
                )
            await session.commit()
        except (
            ResearchRunPersistenceConflictError,
            BackgroundJobPersistenceConflictError,
            IntegrityError,
        ) as exc:
            await session.rollback()
            raise McpToolError("conflict", str(exc)) from exc
        # issue #206 P1:写操作返回精简回执(六字段 + job 指针),
        # 全量详情走 finboard_run_get。
        return _run_ack(row)


async def queue_run(
    app: McpAppContext,
    *,
    payload: dict[str, Any],
) -> ToolEnvelope:
    """冻结输入 + 登记 queued ResearchRun(写,不执行回测)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        body = parse_queue_payload(payload)
        return await enqueue_research_run(app, body)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.queue",
        arguments={"strategy_id": payload.get("strategy_id"),
                   "strategy_version": payload.get("strategy_version")},
        handler=_do,
    )


async def cancel_run(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import (
            ResearchRunConflictError,
            ResearchRunCoordinator,
        )
        from finboard_persistence import (
            ResearchRunPersistenceConflictError,
            ResearchRunRepository,
        )

        async with app.session_maker() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            coordinator = ResearchRunCoordinator(store)
            try:
                record = await coordinator.cancel(run_id)
                # issue #143:协作式取消联动 background_jobs(已终态 job 幂等 suppress)。
                if getattr(record, "job_id", None):
                    import contextlib

                    with contextlib.suppress(BackgroundJobPersistenceConflictError):
                        await BackgroundJobRepository(session).request_cancel(
                            record.job_id  # type: ignore[arg-type]
                        )
                await session.commit()
            except (
                ResearchRunConflictError,
                ResearchRunPersistenceConflictError,
            ) as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            row = await ResearchRunRepository(session).get(record.manifest.run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            return _run_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.cancel",
        arguments={"run_id": run_id},
        handler=_do,
    )


async def replay_run(
    app: McpAppContext,
    run_id: str,
    *,
    idempotency_key: str,
    requested_by: str,
) -> ToolEnvelope:
    """复制 completed/interrupted ResearchRun 为新 queued 运行(写,不执行)。

    issue #305:interrupted 源即事故恢复通道 —— 新 run 经 ``replace(manifest)``
    自动继承全部冻结输入(含 factor_snapshots 全部 ID,零手工),血缘标注
    ``replay_of_run_id`` + ``replay_source_status``;cancelled 仍拒绝。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        import hashlib
        from dataclasses import replace

        from sqlalchemy.exc import IntegrityError

        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import (
            REPLAYABLE_SOURCE_STATUSES,
            ResearchActorType,
            replay_guard_error,
        )
        from finboard_persistence import (
            ResearchRunPersistenceConflictError,
            ResearchRunRepository,
        )

        async with app.session_maker() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            source = await store.get(run_id)
            if source is None:
                raise McpToolError("not_found", f"源研究运行不存在: {run_id}")
            if source.status not in REPLAYABLE_SOURCE_STATUSES:
                raise McpToolError("conflict", replay_guard_error(source.status))
            new_run_id = "RR-" + hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()[:24]
            manifest = replace(
                source.manifest,
                run_id=new_run_id,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                actor_type=ResearchActorType.HUMAN,
                replay_of_run_id=run_id,
                replay_source_status=source.status.value,
            )
            try:
                record, created = await store.create_or_get(manifest)
                # issue #143:重放也走双写;background_jobs 用重放后的新 idempotency_key。
                if created:
                    row_created = await ResearchRunRepository(session).get(
                        record.manifest.run_id
                    )
                    if row_created is not None and not getattr(
                        row_created, "job_id", None
                    ):
                        row_created.job_id = await _enqueue_research_run_job(
                            session,
                            run_id=manifest.run_id,
                            strategy_kind=manifest.strategy_kind,
                            idempotency_key=manifest.idempotency_key,
                            requested_by=manifest.requested_by,
                        )
                await session.commit()
            except (
                ResearchRunPersistenceConflictError,
                BackgroundJobPersistenceConflictError,
                IntegrityError,
            ) as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            row = await ResearchRunRepository(session).get(record.manifest.run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {new_run_id}")
            return _run_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.replay",
        arguments={"run_id": run_id, "idempotency_key": idempotency_key,
                   "requested_by": requested_by},
        handler=_do,
    )


async def lineage_run(
    app: McpAppContext,
    run_id: str,
    trace_id: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import ResearchRunCoordinator

        async with app.session_maker() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            coordinator = ResearchRunCoordinator(store)
            artifacts = await coordinator.lineage(run_id, trace_id)
            if not artifacts:
                raise McpToolError("not_found", f"血缘 trace 不存在: {trace_id}")
            return cast(
                dict[str, Any],
                to_jsonable(
                    {
                        "run_id": run_id,
                        "leaf_trace_id": trace_id,
                        "artifacts": [
                            {
                                "artifact_id": item.artifact_id,
                                "run_id": item.run_id,
                                "decision_id": item.decision_id,
                                "sequence": item.sequence,
                                "stage": item.stage.value,
                                "trace_id": item.trace_id,
                                "parent_trace_ids": list(item.parent_trace_ids),
                                "payload": item.payload,
                                "checksum": item.checksum,
                                "created_at": to_jsonable(item.created_at),
                            }
                            for item in artifacts
                        ],
                    }
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.lineage",
        arguments={"run_id": run_id, "trace_id": trace_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把 ResearchRun 工具注册到 MCP server(3 只读 + 4 写)。"""

    @mcp.tool(
        name="finboard_run_list",
        description="列出 ResearchRun(可选按状态/策略类型过滤,默认最近 50 条)。",
    )
    async def _list(
        statuses: list[str] | None = None,
        strategy_kind: str | None = None,
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await list_runs(
            app_context(ctx),
            statuses=statuses,
            strategy_kind=strategy_kind,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_run_get",
        description=(
            "查询单个 ResearchRun。view=summary(默认):头部字段 + metrics"
            "(剔除 equity_curve,以 equity_point_count 提示)+ universe 聚合计数"
            "(total/included/excluded_by_reason)+ fills 按决策计数 + artifact_count,"
            "不序列化 manifest/result 全量;view=detail:含 manifest / result / "
            "逐标的全量 payload(诊断用,可达 MB 级)。失败运行的 error_summary"
            "(issue #263)头部自带定位上下文"
            "[stage=...; decision=...; decision_index=...; release=...; "
            "dataset_releases=...],原始异常消息在尾部,排障先读头部。"
        ),
    )
    async def _get(
        run_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await get_run(app_context(ctx), run_id, view=view)

    @mcp.tool(
        name="finboard_run_artifacts",
        description="列出某 ResearchRun 的逐阶段 artifact(含 trace 与 payload)。",
    )
    async def _artifacts(run_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await list_artifacts(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_run_queue",
        description=(
            "冻结输入 + 登记 queued ResearchRun(写,不执行回测,由离线 worker "
            "执行)。payload 为 JSON 对象,模板与取值来源(必填标 *):\n"
            '- "idempotency_key"*: 8-128 字符去重键;重提交返回同一 run\n'
            '- "strategy_id"*: 已发布策略规格 id(finboard_strategy_list / '
            "registry 查询)\n"
            '- "strategy_version"*: 整数 >=1(策略规格版本)\n'
            '- "dataset_release_ids"*: 冻结数据发布 release_id 列表,必须与策略'
            "验证计划完全一致(finboard_dataset_release_list 查询)\n"
            '- "factor_snapshot_ids": 冻结特征快照 snapshot_id 列表'
            "(finboard_feature_snapshot_list 查询);single_shot 必填(决策时点"
            "只能来自快照,缺快照入队即拒 #203);multi_period 声明频率后价格"
            "因子不需要,基本面因子(pb/ROE 等)仍需快照/研究数据发布\n"
            '- "parameters": {} —— 不声明即 single_shot;声明 '
            "rebalance_frequency=monthly|quarterly 触发多期再平衡回放(#183,"
            "仅价格因子按发布每期重算;非法值入队即拒)。multi_period 特征"
            "可用性入队即判(#253):规格引用的特征必须可由多期供给派生"
            "(标准价格特征/close/attached daily_metrics 与 financial_indicators"
            " 发布/快照观测),声明 pb/roe 等财务因子而未附加对应研究数据发布"
            " → invalid_argument 具名缺失特征与所需发布 kind\n"
            '- "validation_config"/"execution_config"/"fee_config"/'
            '"benchmark_config": {} —— 政策覆盖,一般留空\n'
            '- "portfolio_config"/"risk_config": {} —— 组合约束 / 风险退出'
            "分区覆盖(#303,键位不可混):\n"
            "  · portfolio_config 管组合约束(overrides 直接就是键值),如 "
            '{"max_risk_contribution": 0.5}(默认 0.35,合法域 0<值<=1,=1 '
            "关闭该约束;隐含买入池 n>=ceil(1/值))、risk_factor_limits(#266)\n"
            "  · risk_config 管风险退出(stop-loss 等),形态 "
            '{"rules": [{"rule_type": "price_stop_loss", "enabled": true, '
            '"threshold": 0.08}]},按 rule_type 与规格策略同名合并、覆盖同名键'
            "(未声明字段继承基准值;新 rule_type 须给全字段含 rationale)\n"
            '- "code_version"*: 7-64 字符,**本 run 自身的代码版本标识**(如 '
            "FinBoard git commit),冻结进 manifest/checksum 供追溯;与数据集发布"
            "的 code_version 只是同名字段、互不校验,别拿数据集 git hash 顶替\n"
            '- "initial_capital"*: 100000-500000 数字\n'
            '- "requested_by"*: 归属人(如 user:xxx / agent:mcp)\n'
            '- "actor_type": "human"(固定;LLM 不能触发运行)\n'
            "校验失败返回 invalid_argument 并附原因(复用 ResearchRunQueueIn "
            "schema)。入队预检(#186):universe 候选池为空秒级 invalid_argument,"
            "错误附各过滤条件排除统计与缺失字段名。single_shot 缺冻结快照同样"
            "入队秒级拒绝(#203,报错附 execution_mode 与缺失因子源)。"
            "组合可行性预检(#303):静态候选池 < ceil(1/生效 "
            "max_risk_contribution)秒级 invalid_argument(错误附排除统计、"
            "生效阈值与 portfolio_config.overrides 键位修复路径),"
            "max_risk_contribution 非法值与 risk_config.overrides 形态错误"
            "同样入队即拒;逐期真实买入池不可精确预知,运行期 fail-closed 兜底"
            "不变。"
            "user_code 策略(#218):strategy_kind=user_code 的规格已声明 "
            "code_artifact(name+可选 commit,引用 kind=strategy 的 active "
            "artifact);入队门控 artifact 非 active / commit 不一致 / 沙箱未启用 "
            "/ single_shot 缺快照 → 秒级拒绝,放行时 active commit 冻结进 "
            "manifest;逐决策日沙箱 decide(ctx)→目标权重 复用组合管线,report "
            "附 sandbox_provenance(commit+镜像 digest)。\n"
            "screen 通道(#234):首次晋级需要 screen 证据而 draft 产物不可被"
            "普通运行引用 —— 已发布规格声明 screen_artifact_bindings"
            "({kind, name, artifact_id, commit?})时,入队按 DB 实绑校验"
            "(存在/非 retired/name/commit 一致,错绑秒级 invalid_argument)"
            "并放行 draft 引用,strategy 绑定的 commit+artifact_id 冻结进 "
            "manifest(code_artifact),factor 绑定要求其沙箱快照"
            "(finboard_research_code_run 显式 artifact_id 产出)已进入 "
            "factor_snapshot_ids。未声明绑定的普通运行引用 draft/retired 仍被"
            "秒级拒绝;promote 侧四向校验兜底,screen 运行不可挪作他版代码"
            "的证据。"
            "返回精简回执(issue #206):run_id/job_id/status/checksum/"
            "execution_mode/created_at,全量详情走 finboard_run_get。"
        ),
    )
    async def _queue(
        payload: dict[str, Any],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await queue_run(app_context(ctx), payload=payload)

    @mcp.tool(
        name="finboard_run_cancel",
        description="取消 ResearchRun(queued/running/interrupted/failed → cancelled,写)。",
    )
    async def _cancel(
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await cancel_run(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_run_replay",
        description=(
            "复制 completed 或 interrupted ResearchRun 为新 queued 运行"
            "(写,不执行)。需要新 idempotency_key + requested_by。"
            "interrupted 源即事故恢复通道(#305):新 run 自动继承原 manifest "
            "全部冻结输入(dataset_release_ids / factor_snapshots 全部 ID,"
            "零手工重填),血缘标注 replay_of_run_id + replay_source_status;"
            "completed 源保持确定性重放对照(结果漂移判 failed);"
            "cancelled 是显式用户意图,拒绝重放。"
        ),
    )
    async def _replay(
        run_id: str,
        idempotency_key: str,
        requested_by: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await replay_run(
            app_context(ctx),
            run_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )

    @mcp.tool(
        name="finboard_run_lineage",
        description="查询某 ResearchRun 内指定 trace_id 的 artifact 血缘(只读,BFS 向上)。",
    )
    async def _lineage(
        run_id: str,
        trace_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await lineage_run(app_context(ctx), run_id, trace_id)


__all__ = [
    "cancel_run",
    "enqueue_research_run",
    "get_run",
    "lineage_run",
    "list_artifacts",
    "list_runs",
    "parse_queue_payload",
    "queue_run",
    "register",
    "replay_run",
]
