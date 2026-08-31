"""``finboard.research_code.*`` 工具 —— 研究代码仓库提交/晋级/查询(issue #215/#219)。

L3 路线第一环:agent 通过 MCP 受控提交策略/因子 Python 代码到本地 bare
git 仓库(``settings.research_code_repo_path``)。git 写操作收敛在服务端,
agent 容器文件系统只读;提交只形成 draft,晋级需 screen + #57 OOS;
**本工具集只做存储、版本化与晋级证据登记,不执行任何代码**(执行见沙箱 issue)。

权限:submit/rollback/promote 是研究写操作(agent 可自主执行,#122 先例),仍尊重
``mcp_readonly_only``;list/get 只读。每次调用照常审计(mcp_audit_events)。

静态校验(import 白名单/入口签名/manifest/上限/拒二进制)是纵深防御第一层,
硬边界在后续沙箱容器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.research_code import (
    IMPORT_WHITELIST,
    PromotionScreenThresholds,
    ResearchCodeError,
    ResearchCodeService,
    evaluate_promotion_gates,
    is_promoted_artifact,
)
from finboard_backtest.research_run.contracts import stable_checksum
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeArtifactRepository,
    ResearchCodeRunRepository,
    ResearchExperimentRepository,
    ResearchRunRepository,
)

_AGENT_ACTOR = "agent:mcp"

_KIND_DESC = (
    "kind=factor|strategy;factor 目录约定 factors/<name>/{factor.py, "
    "manifest.toml},strategy 目录约定 strategies/<name>/{strategy.py, "
    "manifest.toml};files 的键是相对该目录的路径。"
)


def _service(app: McpAppContext) -> ResearchCodeService:
    return ResearchCodeService.from_path(app.settings.research_code_repo_path)


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _artifact_to_dict(a: Any) -> dict[str, Any]:
    return {
        "artifact_id": a.artifact_id,
        "kind": a.kind,
        "name": a.name,
        "commit": a.commit,
        "path": a.path,
        "checksum": a.checksum,
        "status": a.status,
        "promotion_status": getattr(a, "promotion_status", None),
        "validation_experiment_id": getattr(a, "validation_experiment_id", None),
        "screen_run_id": getattr(a, "screen_run_id", None),
        "promotion_evidence": to_jsonable(getattr(a, "promotion_evidence", None)),
        "promoted_at": to_jsonable(getattr(a, "promoted_at", None)),
        "retired_at": to_jsonable(getattr(a, "retired_at", None)),
        "created_by": a.created_by,
        "created_at": to_jsonable(a.created_at),
        "updated_at": to_jsonable(a.updated_at),
    }


async def submit(
    app: McpAppContext,
    *,
    kind: str,
    name: str,
    files: dict[str, str],
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        service = _service(app)
        try:
            result = service.submit(
                kind=kind,
                name=name,
                files=files,
                author=_AGENT_ACTOR,
                max_files=app.settings.research_code_max_files,
                max_file_bytes=app.settings.research_code_max_file_bytes,
            )
        except ResearchCodeError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc
        async with app.session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            record = await repo.register(
                kind=kind,
                name=name,
                commit=result["commit"],
                path=result["path"],
                checksum=result["checksum"],
                created_by=_AGENT_ACTOR,
                status="draft",
            )
            await session.commit()
            return {
                **result,
                "artifact_id": record.artifact_id,
                "status": record.status,
                "promotion_status": record.promotion_status,
                "detail_hint": (
                    "草稿不能进入正式研究组合/模拟盘;请先完成 screen + #57 OOS,"
                    "再调用 finboard_research_code_promote"
                ),
            }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.submit",
        arguments={
            "kind": kind,
            "name": name,
            "file_names": sorted(files),
        },
        handler=_do,
    )


async def list_artifacts(
    app: McpAppContext,
    *,
    kind: str | None = None,
    name: str | None = None,
    status: str | None = None,
    promotion_status: str | None = None,
    include_files: bool = False,
    commit: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            records = await repo.list_artifacts(
                kind=kind,
                name=name,
                status=status,
                promotion_status=promotion_status,
                limit=limit,
            )
        data: dict[str, Any] = {
            "artifacts": [_artifact_to_dict(r) for r in records],
            "count": len(records),
        }
        if include_files and records:
            target = records[0] if len(records) == 1 else None
            if target is not None:
                service = _service(app)
                data["files"] = service.read(
                    kind=target.kind,
                    name=target.name,
                    commit=commit or target.commit,
                )
        return data

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.list",
        arguments={
            "kind": kind,
            "name": name,
            "status": status,
            "promotion_status": promotion_status,
            "include_files": include_files,
            "commit": commit,
            "limit": limit,
        },
        handler=_do,
    )


async def get(
    app: McpAppContext,
    *,
    kind: str,
    name: str,
    commit: str | None = None,
    diff_from: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        service = _service(app)
        ref = commit or "main"
        if not service.exists(kind=kind, name=name, commit=commit):
            raise LookupError(f"代码不存在: kind={kind} name={name} commit={ref}")
        data: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "ref": ref,
            "files": service.read(kind=kind, name=name, commit=commit),
            "history": service.log(kind=kind, name=name, limit=50),
        }
        if diff_from is not None:
            target = commit or service.log(kind=kind, name=name, limit=1)[0]["commit"]
            data["diff"] = service.diff(
                kind=kind, name=name, from_commit=diff_from, to_commit=target
            )
        return data

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.get",
        arguments={
            "kind": kind,
            "name": name,
            "commit": commit,
            "diff_from": diff_from,
        },
        handler=_do,
    )


async def rollback(app: McpAppContext, *, kind: str, name: str, commit: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            record = await repo.rollback_to_draft(kind=kind, name=name, commit=commit)
            await session.commit()
            return _artifact_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.rollback",
        arguments={"kind": kind, "name": name, "commit": commit},
        handler=_do,
    )


async def promote(
    app: McpAppContext,
    *,
    artifact_id: str,
    validation_experiment_id: str,
    screen_run_id: str,
) -> ToolEnvelope:
    """校验 screen + OOS 证据并把 draft 晋级为 active。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            artifact_repo = ResearchCodeArtifactRepository(session)
            artifact = await artifact_repo.get(artifact_id)
            if artifact is None:
                raise McpToolError("not_found", f"研究代码产物不存在: {artifact_id}")
            if artifact.status == "active" and is_promoted_artifact(artifact):
                return _artifact_to_dict(artifact)
            if artifact.status != "draft":
                raise McpToolError(
                    "conflict",
                    f"只有 draft 产物可以晋级: artifact_id={artifact_id} "
                    f"status={artifact.status} promotion_status={artifact.promotion_status}",
                )

            experiment = await ResearchExperimentRepository(session).get(validation_experiment_id)
            if experiment is None:
                failure = McpToolError(
                    "not_found",
                    f"OOS 验证实验不存在: {validation_experiment_id}",
                )
                await _record_promotion_failure(
                    session,
                    artifact=artifact,
                    validation_experiment_id=validation_experiment_id,
                    screen_run_id=screen_run_id,
                    failure=failure,
                )
                raise failure
            try:
                screen, execution = await _screen_evidence(
                    session, artifact=artifact, screen_run_id=screen_run_id
                )
            except McpToolError as failure:
                await _record_promotion_failure(
                    session,
                    artifact=artifact,
                    validation_experiment_id=validation_experiment_id,
                    screen_run_id=screen_run_id,
                    failure=failure,
                )
                raise
            thresholds = _promotion_thresholds(app)
            gate = evaluate_promotion_gates(
                screen=screen,
                validation=experiment.as_dict(),
                thresholds=thresholds,
                artifact_id=artifact.artifact_id,
                artifact_kind=artifact.kind,
                artifact_name=artifact.name,
                artifact_commit=artifact.commit,
                require_artifact_binding=True,
            )
            evidence = _promotion_evidence(
                artifact=artifact,
                validation_experiment_id=validation_experiment_id,
                screen_run_id=screen_run_id,
                gates=gate.as_dict(),
                execution=execution,
            )
            if not gate.passed:
                await artifact_repo.mark_promotion_failed(
                    artifact.artifact_id,
                    validation_experiment_id=validation_experiment_id,
                    screen_run_id=screen_run_id,
                    evidence=evidence,
                )
                await session.commit()
                raise McpToolError(
                    "invalid_argument",
                    "研究代码产物未通过晋级门: " + ", ".join(gate.failures),
                )
            promoted = await artifact_repo.promote(
                artifact.artifact_id,
                validation_experiment_id=validation_experiment_id,
                screen_run_id=screen_run_id,
                evidence=evidence,
            )
            await session.commit()
            return _artifact_to_dict(promoted)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.promote",
        arguments={
            "artifact_id": artifact_id,
            "validation_experiment_id": validation_experiment_id,
            "screen_run_id": screen_run_id,
        },
        handler=_do,
    )


def _promotion_thresholds(app: McpAppContext) -> PromotionScreenThresholds:
    return PromotionScreenThresholds(
        min_abs_rank_ic=float(getattr(app.settings, "research_promotion_min_abs_rank_ic", 0.02)),
        max_average_turnover=float(
            getattr(app.settings, "research_promotion_max_average_turnover", 0.80)
        ),
        max_abs_correlation=float(
            getattr(app.settings, "research_promotion_max_abs_correlation", 0.80)
        ),
        min_periods=int(getattr(app.settings, "research_promotion_min_periods", 2)),
    )


async def _record_promotion_failure(
    session: Any,
    *,
    artifact: Any,
    validation_experiment_id: str,
    screen_run_id: str,
    failure: McpToolError,
) -> None:
    """把可识别的证据缺失/绑定错误也归档为 draft/failed。"""
    evidence = _promotion_evidence(
        artifact=artifact,
        validation_experiment_id=validation_experiment_id,
        screen_run_id=screen_run_id,
        gates={
            "passed": False,
            "failures": [failure.message],
            "error_kind": failure.kind,
        },
        execution={},
    )
    await ResearchCodeArtifactRepository(session).mark_promotion_failed(
        artifact.artifact_id,
        validation_experiment_id=validation_experiment_id,
        screen_run_id=screen_run_id,
        evidence=evidence,
    )
    await session.commit()


def _promotion_evidence(
    *,
    artifact: Any,
    validation_experiment_id: str,
    screen_run_id: str,
    gates: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    """形成晋级证据 envelope,统一保存四向引用和失败原因。"""
    return {
        "schema_version": "research_code_promotion.v1",
        "artifact": {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "name": artifact.name,
            "commit": artifact.commit,
            "checksum": artifact.checksum,
        },
        "validation_experiment_id": validation_experiment_id,
        "screen_run_id": screen_run_id,
        "gates": gates,
        # code commit / data releases / params / output checksum 均由
        # screen source 固定,供历史执行四向重建。
        "execution": execution,
    }


async def _screen_evidence(
    session: Any, *, artifact: Any, screen_run_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取已完成 ResearchRun/RCR 的 screen 与四向审计引用。"""
    if not screen_run_id:
        raise McpToolError("invalid_argument", "screen_run_id 不能为空")

    run_row = None
    if screen_run_id.startswith("RR-") or not screen_run_id.startswith("RCR-"):
        run_row = await ResearchRunRepository(session).get(screen_run_id)
    if run_row is not None:
        if run_row.status != "completed" or not isinstance(run_row.result, dict):
            raise McpToolError(
                "invalid_argument",
                f"screen ResearchRun 未完成或缺少 report: {screen_run_id}",
            )
        manifest = run_row.manifest if isinstance(run_row.manifest, dict) else {}
        spec = manifest.get("strategy_spec")
        if not isinstance(spec, dict):
            raise McpToolError("invalid_argument", "screen ResearchRun 缺少 strategy_spec")
        factor_snapshot_bindings: list[dict[str, Any]] = []
        if artifact.kind == "strategy":
            ref = spec.get("code_artifact")
            if not isinstance(ref, dict) or ref.get("name") != artifact.name:
                raise McpToolError("invalid_argument", "screen ResearchRun 未执行该策略 artifact")
            if ref.get("kind") not in (None, "strategy"):
                raise McpToolError("invalid_argument", "screen ResearchRun 的 artifact kind 不一致")
            if ref.get("artifact_id") not in (None, artifact.artifact_id):
                raise McpToolError("invalid_argument", "screen ResearchRun 的 artifact ID 不一致")
            if ref.get("commit") != artifact.commit:
                raise McpToolError(
                    "invalid_argument", "screen ResearchRun 的 code commit 与 artifact 不一致"
                )
            screen = run_row.result.get("strategy_screen")
        else:
            feature_graph = spec.get("feature_graph")
            nodes = feature_graph.get("nodes", []) if isinstance(feature_graph, dict) else []
            sources = {
                str(node.get("source"))
                for node in (nodes or [])
                if isinstance(node, dict)
            }
            if f"u_{artifact.name}" not in sources:
                raise McpToolError(
                    "invalid_argument", "screen ResearchRun 未引用该用户因子 artifact"
                )
            # 用户因子快照由 RCR 产出;沿 snapshot.source_run_id 追溯到精确
            # artifact,避免只凭 u_<name> 把另一版代码的 screen 结论挪用过来。
            snapshot_bindings: list[dict[str, Any]] = []
            raw_snapshot_refs = manifest.get("factor_snapshots", [])
            snapshot_refs = raw_snapshot_refs if isinstance(raw_snapshot_refs, list) else []
            for raw_ref in snapshot_refs:
                if not isinstance(raw_ref, dict) or not raw_ref.get("artifact_id"):
                    continue
                snapshot = await FeatureSnapshotRepository(session).get(
                    str(raw_ref["artifact_id"])
                )
                if snapshot is None or snapshot.source_run_id is None:
                    continue
                source_run = await ResearchCodeRunRepository(session).get(snapshot.source_run_id)
                if source_run is None:
                    continue
                snapshot_bindings.append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "source_run_id": snapshot.source_run_id,
                        "artifact_id": source_run.artifact_id,
                        "kind": source_run.kind,
                        "name": source_run.name,
                        "commit": source_run.commit,
                        "execution": _code_run_evidence(source_run),
                    }
                )
            if not any(
                binding["artifact_id"] == artifact.artifact_id
                and binding["kind"] == artifact.kind
                and binding["name"] == artifact.name
                and binding["commit"] == artifact.commit
                for binding in snapshot_bindings
            ):
                raise McpToolError(
                    "invalid_argument",
                    "screen ResearchRun 的用户因子快照未绑定同一 artifact/name/kind/commit",
                )
            screen = run_row.result.get("factor_screen")
        if not isinstance(screen, dict):
            raise McpToolError(
                "invalid_argument",
                f"screen ResearchRun 缺少对应 screen 段: {screen_run_id}",
            )
        raw_release_refs = manifest.get("dataset_releases", [])
        refs = raw_release_refs if isinstance(raw_release_refs, list) else []
        dataset_ids = [
            str(item.get("artifact_id"))
            for item in refs
            if isinstance(item, dict) and item.get("artifact_id")
        ]
        dataset_checksums = {
            str(item["artifact_id"]): str(item["checksum"])
            for item in refs
            if isinstance(item, dict) and item.get("artifact_id") and item.get("checksum")
        }
        params = manifest.get("parameters", {})
        result = run_row.result
        output_checksum = run_row.result_checksum or stable_checksum(result)
        execution = {
            "run_id": run_row.run_id,
            "run_kind": "research_run",
            "manifest_checksum": run_row.manifest_checksum or stable_checksum(manifest),
            "result_checksum": output_checksum,
            "output_checksum": output_checksum,
            "code_commit": artifact.commit,
            "code_checksum": artifact.checksum,
            "dataset_release_ids": dataset_ids,
            "dataset_release_checksums": dataset_checksums,
            "parameters": params,
            "parameters_checksum": stable_checksum(params),
        }
        if factor_snapshot_bindings:
            execution["factor_snapshot_bindings"] = factor_snapshot_bindings
        sandbox_provenance = result.get("sandbox_provenance")
        if isinstance(sandbox_provenance, dict):
            execution["sandbox_provenance"] = sandbox_provenance
            audit_refs = sandbox_provenance.get("audit_refs")
            if isinstance(audit_refs, dict):
                execution["audit_refs"] = audit_refs
        return screen, execution

    code_run = await ResearchCodeRunRepository(session).get(screen_run_id)
    if code_run is None:
        raise McpToolError("not_found", f"screen 执行不存在: {screen_run_id}")
    if code_run.status != "succeeded":
        raise McpToolError(
            "invalid_argument", f"screen 执行未成功: {screen_run_id} status={code_run.status}"
        )
    if (
        code_run.artifact_id != artifact.artifact_id
        or code_run.kind != artifact.kind
        or code_run.name != artifact.name
        or code_run.commit != artifact.commit
    ):
        raise McpToolError(
            "invalid_argument",
            "screen 执行与 artifact 的 name/kind/commit/ID 不一致",
        )
    metrics = code_run.metrics if isinstance(code_run.metrics, dict) else {}
    screen = (
        metrics.get("screen")
        or metrics.get("promotion_screen")
        or metrics.get("factor_screen")
        or metrics.get("strategy_screen")
    )
    if not isinstance(screen, dict):
        raise McpToolError(
            "invalid_argument",
            "research_code_run 没有机器 screen 指标;请提供带 factor_screen/strategy_screen 的完成运行",
        )
    return screen, _code_run_evidence(code_run)


def _code_run_evidence(code_run: Any) -> dict[str, Any]:
    """把 RCR 的四向引用与容器归档位置收敛成晋级证据。"""
    params = code_run.params or {}
    output_checksum = code_run.scores_checksum or code_run.output_snapshot_id
    artifact_dir = Path(code_run.artifact_dir)
    container = {
        "image": code_run.image,
        "image_digest": code_run.image_digest,
        "usage": code_run.usage,
        "archive_dir": str(artifact_dir),
        "stdout": str(artifact_dir / "stdout.txt"),
        "stderr": str(artifact_dir / "stderr.txt"),
        "container_metadata": str(artifact_dir / "container.json"),
    }
    return {
        "run_id": code_run.run_id,
        "run_kind": "research_code_run",
        "code_commit": code_run.commit,
        "code_checksum": code_run.code_checksum,
        "dataset_release_ids": code_run.dataset_release_ids,
        "dataset_release_checksums": code_run.dataset_release_checksums,
        "parameters": params,
        "parameters_checksum": stable_checksum(params),
        "output_checksum": output_checksum,
        "mount_manifest_checksum": code_run.mount_manifest_checksum,
        "image": code_run.image,
        "image_digest": code_run.image_digest,
        "artifact_dir": str(artifact_dir),
        "usage": code_run.usage,
        "exit_code": code_run.exit_code,
        "timed_out": code_run.timed_out,
        "oom_killed": code_run.oom_killed,
        "audit_refs": {
            "code": {
                "artifact_id": code_run.artifact_id,
                "kind": code_run.kind,
                "name": code_run.name,
                "commit": code_run.commit,
                "checksum": code_run.code_checksum,
            },
            "data": {
                "release_ids": code_run.dataset_release_ids,
                "release_checksums": code_run.dataset_release_checksums,
                "decision_at": to_jsonable(code_run.decision_at),
                "mount_manifest_checksum": code_run.mount_manifest_checksum,
            },
            "parameters": {"value": params, "checksum": stable_checksum(params)},
            "output": {
                "checksum": output_checksum,
                "output_snapshot_id": code_run.output_snapshot_id,
            },
            "container": container,
        },
        "container": container,
    }


def register(mcp: MCPServer) -> None:
    """把研究代码仓库工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_research_code_submit",
        description=(
            "提交一版研究代码(factor 或 strategy)到研究代码仓库(本地 bare "
            "git,只存储与版本化,不执行)。静态校验:manifest.toml 必填"
            "(manifest.entry)、入口函数签名(factor.compute / strategy.decide,"
            "至少一个输入参数)、import 白名单 "
            f"({sorted(IMPORT_WHITELIST)})、禁 subprocess/socket/文件写模式"
            "open/eval/exec、文件数与单文件大小上限、拒二进制。校验失败报错"
            "逐条列出可操作问题。提交只生成 draft,不会替换当前 active;"
            "只有通过 screen + #57 OOS 后调用 promote 才成为 active。"
            f" {_KIND_DESC} files 为「路径→文件内容」映射。"
            "返回 {name, kind, commit, checksum, artifact_id, status=draft}。"
        ),
    )
    async def _submit(
        kind: str,
        name: str,
        files: dict[str, str],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await submit(app_context(ctx), kind=kind, name=name, files=files)

    @mcp.tool(
        name="finboard_research_code_list",
        description=(
            "列出研究代码产物登记(research_code_artifacts,可按 kind/name/"
            "status/promotion_status 过滤,新→旧)。include_files=true 且结果"
            "唯一时附带该版本文件内容。"
        ),
    )
    async def _list(
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
        promotion_status: str | None = None,
        include_files: bool = False,
        commit: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await list_artifacts(
            app_context(ctx),
            kind=kind,
            name=name,
            status=status,
            promotion_status=promotion_status,
            include_files=include_files,
            commit=commit,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_research_code_get",
        description=(
            "读取研究代码某版本的全部文件与提交历史(commit 省略=最新 main;"
            "diff_from 传旧 commit 时附带两版 unified diff)。"
            f" {_KIND_DESC}"
        ),
    )
    async def _get(
        kind: str,
        name: str,
        commit: str | None = None,
        diff_from: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await get(
            app_context(ctx),
            kind=kind,
            name=name,
            commit=commit,
            diff_from=diff_from,
        )

    @mcp.tool(
        name="finboard_research_code_rollback",
        description=(
            "把 (kind, name) 的历史 commit 重新登记为 draft(不替换当前 active),"
            "旧 commit 必须重新完成 screen + #57 OOS 后才能 promote;git 历史不重写。"
        ),
    )
    async def _rollback(
        kind: str,
        name: str,
        commit: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await rollback(app_context(ctx), kind=kind, name=name, commit=commit)

    @mcp.tool(
        name="finboard_research_code_promote",
        description=(
            "[写] 将指定 draft 研究代码晋级为 active。必须同时提供已完成的"
            "screen_run_id(完成 ResearchRun 的 factor_screen/strategy_screen 或"
            "成功 RCR screen 指标)与 validation_experiment_id(#57):状态必须"
            "validated_oos 且 final_test_unsealed=true,并绑定同一 artifact/name/"
            "kind/commit。screen 机器门默认要求 abs(rank_ic)>=0.02、"
            "average_turnover<=0.80、相关性绝对值<=0.80、至少 2 期;失败返回"
            "具体缺失/超阈值门名并保留 draft + promotion_status=failed 证据。"
            "通过后旧 active 自动 retired。纯研究治理操作,不连接 broker、不下单。"
        ),
    )
    async def _promote(
        artifact_id: str,
        validation_experiment_id: str,
        screen_run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await promote(
            app_context(ctx),
            artifact_id=artifact_id,
            validation_experiment_id=validation_experiment_id,
            screen_run_id=screen_run_id,
        )


__all__ = ["get", "list_artifacts", "promote", "register", "rollback", "submit"]
