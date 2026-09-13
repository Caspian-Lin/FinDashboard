"""``finboard.research_code_run.*`` 工具 —— 沙箱执行入队与查询(issue #216)。

L3 沙箱路线第二环:把已提交的因子代码提交到一次性 Docker 容器执行
(``kind=research_code_run`` 后台任务,worker 单并发)。入队工具在 MCP 层做
同步预检(#186 秒级失败风格):

* ``research_sandbox_enabled`` 必须开启(需 Docker Desktop + 已构建镜像);
* v1 仅 ``kind=factor``(策略代码 kind=strategy 经 ``strategy_spec``
  ``code_artifact`` 引用 + ``finboard_run_queue`` 逐决策日执行 decide,
  issue #218,不走独立沙箱 run);
* 默认路径须有 ``status=active/promotion_status=passed`` 产物;显式
  ``artifact_id`` 可运行 draft 以生成供 screen ResearchRun 使用的快照,
  但 retired/active 未晋级一律拒绝;指定 commit 必须等于该 artifact;
* ``dataset_release_ids`` 逐个在 DB 已登记,且至少一个 bars 类发布。

执行端(ResearchCodeRunExecutor)重放静态校验、按 decision_at 生成只读
挂载(PIT 物理隔离)、容器内 harness 校验输出;run 记录三向引用
(code commit x release x scores checksum)。结果经 ``finboard_job_get``
(result_ref=RCR-...)轮询,``finboard_research_code_run_get`` 取 run 详情
(detail 附 scores 预览与失败 error.json)。

权限:run 是研究写操作(agent 可自主执行,#122 先例),受
``mcp_readonly_only`` 守卫;get 只读。容器无网络无凭证,不触实盘。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.research_code import is_promoted_artifact, promotion_status
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    BackgroundJobRepository,
    ResearchCodeArtifactRepository,
    ResearchCodeRunRepository,
    ResearchDatasetReleaseRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

_AGENT_ACTOR = "agent:mcp"

#: detail 视图返回的 scores 预览行数(#206 瘦身精神:全文走 artifact_dir)。
_SCORES_PREVIEW_ROWS = 20


async def _require_write_enabled(app: McpAppContext) -> None:
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _parse_decision_at(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise McpToolError("invalid_argument", f"decision_at 不是合法 ISO 字符串: {raw!r}") from exc
    if value.tzinfo is None:
        raise McpToolError(
            "invalid_argument", "decision_at 必须带时区(如 2024-06-03T15:00:00+08:00)"
        )
    return value


async def run_enqueue(
    app: McpAppContext,
    *,
    kind: str,
    name: str,
    dataset_release_ids: list[str],
    decision_at: str,
    commit: str | None = None,
    artifact_id: str | None = None,
    symbols: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> ToolEnvelope:
    """预检通过后入队 ``kind=research_code_run``,返回 {job_id, run_hint}。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        settings = app.settings
        if not getattr(settings, "research_sandbox_enabled", False):
            raise McpToolError(
                "invalid_argument",
                "研究沙箱未启用(research_sandbox_enabled=false);"
                "启用前置:Docker Desktop 运行 + 构建镜像 "
                "docker/research-sandbox(docker build -t "
                f"{settings.research_sandbox_image} docker/research-sandbox)",
            )
        if kind != "factor":
            raise McpToolError(
                "invalid_argument",
                f"research_code_run v1 仅支持 kind=factor,收到 {kind!r};"
                "策略代码(kind=strategy)不走独立沙箱 run,经 strategy_spec "
                "code_artifact 引用后由 finboard_run_queue(声明 "
                "decision_schedule/rebalance_frequency 走 multi_period)"
                "逐决策日执行 decide(issue #218)",
            )
        if not dataset_release_ids:
            raise McpToolError(
                "invalid_argument", "dataset_release_ids 不能为空(至少一个 bars 类发布)"
            )
        decision = _parse_decision_at(decision_at)

        payload: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "dataset_release_ids": list(dataset_release_ids),
            "decision_at": decision.isoformat(),
        }
        if commit is not None:
            payload["commit"] = commit
        if artifact_id is not None:
            payload["artifact_id"] = artifact_id
        if symbols is not None:
            payload["symbols"] = list(symbols)
        if params is not None:
            payload["params"] = params

        async with app.session_maker() as session:
            artifact_repo = ResearchCodeArtifactRepository(session)
            if artifact_id is not None:
                artifact = await artifact_repo.get(artifact_id)
                if artifact is None:
                    raise McpToolError("not_found", f"研究代码产物不存在: {artifact_id}")
                if artifact.kind != kind or artifact.name != name:
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact_id 与 (kind,name) 不一致: {artifact_id}",
                    )
                if artifact.status == "retired":
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact_id 已 retired,不能启动新沙箱执行: {artifact_id}",
                    )
                if artifact.status == "active" and not is_promoted_artifact(artifact):
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact_id active 但未通过 screen+OOS 晋级门: {artifact_id} "
                        f"promotion_status={promotion_status(artifact)}",
                    )
            else:
                artifact = await artifact_repo.get_active(kind=kind, name=name)
                if artifact is None:
                    raise McpToolError(
                        "not_found",
                        f"没有 active+passed 的研究代码: kind={kind} name={name}"
                        "(先 finboard_research_code_submit 并完成晋级)",
                    )
            if commit is not None and commit != artifact.commit:
                raise McpToolError(
                    "invalid_argument",
                    f"指定 commit {commit[:12]} 不是该 artifact 的 active 引用"
                    f"(artifact={artifact.commit[:12]});历史版本先 "
                    "finboard_research_code_rollback",
                )
            payload.setdefault("commit", artifact.commit)

            release_repo = ResearchDatasetReleaseRepository(session)
            has_bars = False
            for release_id in dataset_release_ids:
                release = await release_repo.get(release_id)
                if release is None:
                    raise McpToolError("not_found", f"研究数据发布不存在: {release_id}")
                if getattr(release.dataset_kind, "value", "") == "bars":
                    has_bars = True
            if not has_bars:
                raise McpToolError(
                    "invalid_argument",
                    "dataset_release_ids 须至少包含一个 bars 类发布(挂载行情面)",
                )

            idempotency_key = _idempotency_key(payload)
            from sqlalchemy.exc import IntegrityError

            try:
                row, created = await BackgroundJobRepository(session).create_or_get(
                    job_id=generate_background_job_id(),
                    idempotency_key=idempotency_key,
                    kind="research_code_run",
                    queue="default",
                    status=BackgroundJobStatus.QUEUED.value,
                    priority=0,
                    payload=payload,
                    payload_checksum=_payload_checksum(payload),
                    max_attempts=1,
                    requested_by=_AGENT_ACTOR,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "重复 idempotency_key") from exc
        result = cast(
            dict[str, Any],
            to_jsonable(
                {
                    "job_id": row.job_id,
                    "kind": "research_code_run",
                    "status": row.status,
                    "created": created,
                    "idempotency_key": idempotency_key,
                    "detail_hint": (
                        "finboard_job_get 轮询执行进度(result_ref=RCR-... 为 run_id,"
                        "终态后 finboard_research_code_run_get 取结果)"
                    ),
                }
            ),
        )
        return result

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code_run.enqueue",
        arguments={
            "kind": kind,
            "name": name,
            "commit": commit,
            "dataset_release_ids": dataset_release_ids,
            "decision_at": decision_at,
            "symbols": symbols,
        },
        handler=_do,
        idempotency_key=_idempotency_key(
            {
                "kind": kind,
                "name": name,
                "commit": commit,
                "dataset_release_ids": dataset_release_ids,
                "decision_at": decision_at,
                "symbols": symbols,
                "params": params,
            }
        ),
    )


async def run_get(
    app: McpAppContext,
    *,
    run_id: str,
    view: str = "summary",
) -> ToolEnvelope:
    """查询单次沙箱执行记录;detail 附 scores 预览与失败 error.json。"""

    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            record = await ResearchCodeRunRepository(session).get(run_id)
        if record is None:
            raise McpToolError("not_found", f"研究代码执行不存在: {run_id}")
        data: dict[str, Any] = cast(
            dict[str, Any],
            to_jsonable(
                {
                    "run_id": record.run_id,
                    "job_id": record.job_id,
                    "kind": record.kind,
                    "name": record.name,
                    "commit": record.commit,
                    "code_checksum": record.code_checksum,
                    "artifact_id": record.artifact_id,
                    "dataset_release_ids": record.dataset_release_ids,
                    "dataset_release_checksums": record.dataset_release_checksums,
                    "decision_at": record.decision_at,
                    "params": record.params,
                    "params_checksum": _payload_checksum(record.params or {}),
                    "image": record.image,
                    "image_digest": record.image_digest,
                    "status": record.status,
                    "error_code": record.error_code,
                    "error_summary": record.error_summary,
                    "exit_code": record.exit_code,
                    "timed_out": record.timed_out,
                    "oom_killed": record.oom_killed,
                    "usage": record.usage,
                    "metrics": record.metrics,
                    "scores_checksum": record.scores_checksum,
                    "mount_manifest_checksum": record.mount_manifest_checksum,
                    # issue #217:成功 run 落库的 feature snapshot 引用(可进
                    # factor_snapshot_ids 被 research run 引用)
                    "output_snapshot_id": record.output_snapshot_id,
                    "artifact_dir": record.artifact_dir,
                    "created_at": record.created_at,
                    "audit_refs": {
                        "code": {
                            "artifact_id": record.artifact_id,
                            "name": record.name,
                            "kind": record.kind,
                            "commit": record.commit,
                            "checksum": record.code_checksum,
                        },
                        "data": {
                            "release_ids": record.dataset_release_ids,
                            "release_checksums": record.dataset_release_checksums,
                            "decision_at": record.decision_at,
                            "mount_manifest_checksum": record.mount_manifest_checksum,
                        },
                        "parameters": {
                            "value": record.params or {},
                            "checksum": _payload_checksum(record.params or {}),
                        },
                        "output": {
                            "scores_checksum": record.scores_checksum,
                            "output_snapshot_id": record.output_snapshot_id,
                        },
                        "container": {
                            "image": record.image,
                            "image_digest": record.image_digest,
                            "usage": record.usage,
                            "archive_dir": record.artifact_dir,
                            "stdout": str(Path(record.artifact_dir) / "stdout.txt"),
                            "stderr": str(Path(record.artifact_dir) / "stderr.txt"),
                            "container_metadata": str(
                                Path(record.artifact_dir) / "container.json"
                            ),
                        },
                    },
                }
            ),
        )
        if view == "detail":
            artifact_dir = Path(record.artifact_dir)
            scores = artifact_dir / "out" / "scores.parquet"
            if scores.exists():
                data["scores_preview"] = _scores_preview(scores)
            error_path = artifact_dir / "out" / "error.json"
            if error_path.exists():
                try:
                    data["error"] = json.loads(error_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data["error"] = None
        return data

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code_run.get",
        arguments={"run_id": run_id, "view": view},
        handler=_do,
    )


def _scores_preview(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path).slice(0, _SCORES_PREVIEW_ROWS)
    rows: list[dict[str, Any]] = [{str(k): v for k, v in row.items()} for row in table.to_pylist()]
    return rows


def _payload_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _idempotency_key(payload: dict[str, Any]) -> str:
    digest = _payload_checksum(payload)[:16]
    return f"research_code_run:{digest}"


def register(mcp: MCPServer) -> None:
    """把沙箱执行工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_research_code_run",
        description=(
            "入队一次研究代码沙箱执行(kind=research_code_run 后台任务,worker "
            "单并发):已晋级 active 因子代码在一次性 Docker 容器内执行 factor.compute(ctx),"
            "输出截面 scores + metrics(coverage/nan_ratio)。容器 --network none /"
            " --read-only / cap-drop ALL / 非 root / CPU 与内存限额 / 墙钟超时 kill;"
            "数据面为按 decision_at 物化的只读挂载(PIT 物理隔离,容器内不存在未来"
            "数据文件)。入队预检:sandbox 开启(research_sandbox_enabled)、"
            "kind=factor、(kind,name) 有已晋级 active+passed 产物(显式 artifact_id"
            "也可执行 draft 以产出供 screen ResearchRun 使用的快照,但 retired/"
            "active 未晋级一律拒绝)、"
            "dataset_release_ids 均已登记"
            "且至少一个 bars 类发布、decision_at 带时区。返回 job_id;"
            "finboard_job_get 轮询(result_ref=RCR-...),终态后 "
            "finboard_research_code_run_get 取结果。params 覆盖 manifest.params"
            "(合并注入 ctx.params)。失败分类:static_validation_failed / "
            "runtime_error / timeout / oom_killed / output_contract_violation / "
            "sandbox_unavailable / quality_gate_failed。成功输出经质量门"
            "(NaN 比例/覆盖率,阈值 research_sandbox_max_nan_ratio 与 "
            "research_sandbox_min_coverage,默认各 0.5)后落库为 feature "
            "snapshot(u_<name> 因子观测),run_get 可见 output_snapshot_id,"
            "该快照可被 research run 的 factor_snapshot_ids 引用。纯离线研究域,"
            "不连 broker 不下单。"
        ),
    )
    async def _run(
        kind: str,
        name: str,
        dataset_release_ids: list[str],
        decision_at: str,
        commit: str | None = None,
        artifact_id: str | None = None,
        symbols: list[str] | None = None,
        params: dict[str, Any] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await run_enqueue(
            app_context(ctx),
            kind=kind,
            name=name,
            dataset_release_ids=dataset_release_ids,
            decision_at=decision_at,
            commit=commit,
            artifact_id=artifact_id,
            symbols=symbols,
            params=params,
        )

    @mcp.tool(
        name="finboard_research_code_run_get",
        description=(
            "查询单次沙箱执行记录(research_code_runs,RCR- 前缀):四向审计引用"
            "(code commit / dataset_release_ids / scores_checksum)、镜像 digest、"
            "失败分类与资源用量、质量门结果(metrics.quality_gate:nan_ratio/"
            "coverage/阈值/失败原因)与落库快照引用(output_snapshot_id,可进 "
            "research run 的 factor_snapshot_ids)。view=summary 默认;view=detail "
            "附 scores 预览(前 20 行)与容器 error.json。job 维度进度走 "
            "finboard_job_get。"
        ),
    )
    async def _get(
        run_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await run_get(app_context(ctx), run_id=run_id, view=view)


__all__ = ["register", "run_enqueue", "run_get"]
