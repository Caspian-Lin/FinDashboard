"""``finboard.run.*`` 工具 —— ResearchRun 查询与写操作(复用 ``ResearchRunRepository``)。

只读(3):list / get / artifacts —— 自动允许。
写(4,issue #127):queue(冻结 + 登记 queued)/ cancel / replay(复制 completed)/
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


async def get_run(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            return _run_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.get",
        arguments={"run_id": run_id},
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
    from finboard_backtest.research_run.contracts import JsonValue
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
    # issue #183:多期回放(parameters.rebalance_frequency)由管线按冻结发布
    # 每日重算价格因子,不要求为每月预建因子快照;仍缺失的来源(如基本面因子)
    # 在执行期 fail-closed,避免静默产出残缺信号。
    is_multi_period = body.parameters.get("rebalance_frequency") in ("monthly", "quarterly")
    if required_factor_sources and not snapshots and not is_multi_period:
        raise McpToolError(
            "invalid_argument",
            f"策略依赖因子输入但未冻结 factor_snapshot_ids: {sorted(required_factor_sources)}",
        )
    release_ids = {release.release_id for release in releases}
    for snapshot in snapshots:
        if snapshot.dataset_release_id not in release_ids:
            raise McpToolError(
                "invalid_argument",
                f"因子快照 {snapshot.snapshot_id} 绑定的数据发布不在本次冻结清单中",
            )

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
        ),
    )
    if preview.is_empty:
        raise McpToolError(
            "invalid_argument",
            "运行数据发布候选池为空,拒绝入队: "
            f"{describe_empty_pool(preview, decision_date=releases[0].end_date)}",
        )

    run_id = "RR-" + hashlib.sha256(
        body.idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    manifest = ResearchRunManifest(
        run_id=run_id,
        idempotency_key=body.idempotency_key,
        strategy_spec=spec,
        strategy_spec_checksum=strategy_row.checksum,
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
        manifest, _ = await _build_queued_manifest(body, session)
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
        return _run_detail(row)


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
    """复制 completed ResearchRun 为新 queued 运行(写,不执行)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        import hashlib
        from dataclasses import replace

        from sqlalchemy.exc import IntegrityError

        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import (
            ResearchActorType,
            ResearchRunStatus,
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
            if source.status is not ResearchRunStatus.COMPLETED:
                raise McpToolError("conflict", "仅允许重放已完成运行")
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
        description="查询单个 ResearchRun 详情(含 manifest / result)。",
    )
    async def _get(run_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await get_run(app_context(ctx), run_id)

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
            "(finboard_feature_snapshot_list 查询);策略依赖因子输入时必填\n"
            '- "parameters": {} —— 可声明 rebalance_frequency=monthly|quarterly '
            "触发多期再平衡回放(#183)\n"
            '- "validation_config"/"portfolio_config"/"risk_config"/'
            '"execution_config"/"fee_config"/"benchmark_config": {} —— '
            "政策覆盖,一般留空\n"
            '- "code_version"*: 7-64 字符,**本 run 自身的代码版本标识**(如 '
            "FinBoard git commit),冻结进 manifest/checksum 供追溯;与数据集发布"
            "的 code_version 只是同名字段、互不校验,别拿数据集 git hash 顶替\n"
            '- "initial_capital"*: 100000-500000 数字\n'
            '- "requested_by"*: 归属人(如 user:xxx / agent:mcp)\n'
            '- "actor_type": "human"(固定;LLM 不能触发运行)\n'
            "校验失败返回 invalid_argument 并附原因(复用 ResearchRunQueueIn "
            "schema)。入队预检(#186):universe 候选池为空秒级 invalid_argument,"
            "错误附各过滤条件排除统计与缺失字段名。"
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
            "复制 completed ResearchRun 为新 queued 运行(写,不执行)。"
            "需要新 idempotency_key + requested_by。"
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
