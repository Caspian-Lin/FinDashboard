"""受控的冻结发布包导出和脱敏状态查询。"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.release_bundle import (
    export_registered_bundle,
    managed_bundle_path,
    new_bundle_id,
    release_root,
)
from finboard_app.release_bundle_jobs import BUNDLE_JOB_KIND, bundle_receipt
from finboard_data.release_bundle import ReleaseBundleError
from finboard_data.releases import DatasetReleaseError
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._audit import record_failure, record_success
from finboard_mcp.tools.data_write import _require_write_enabled
from finboard_persistence import BackgroundJobRepository


async def data_bundle_export(app: McpAppContext, release_ids: list[str]) -> ToolEnvelope:
    operation = "bundle.export"
    arguments = {"release_count": len(release_ids)}

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if (
            not 0 < len(release_ids) <= 32
            or len(set(release_ids)) != len(release_ids)
            or any(not 0 < len(item) <= 160 for item in release_ids)
        ):
            raise McpToolError("invalid_argument", "请提供 1 到 32 个不重复的冻结发布 ID")
        bundle_id = new_bundle_id()
        path = managed_bundle_path(bundle_id)
        committed = False
        try:
            async with app.session_maker() as session:
                await export_registered_bundle(session, release_ids, path, root=release_root())
                result = await bundle_receipt(bundle_id)
                await record_success(
                    session, app=app, operation=operation, params_summary=arguments,
                    entity_type="release_bundle", entity_id=bundle_id,
                    correlation={"bundle_id": bundle_id},
                )
                await session.commit()
                committed = True
                return result
        except (DatasetReleaseError, ReleaseBundleError, OSError) as exc:
            await record_failure(
                app, operation=operation, params_summary=arguments, error=exc,
                entity_type="release_bundle", entity_id=bundle_id,
            )
            raise McpToolError("invalid_argument", "导出失败。请检查发布可用性或磁盘空间") from exc
        finally:
            # 返回前的失败只处理本次由唯一 ID 生成的文件。
            if not committed:
                with suppress(OSError):
                    path.unlink(missing_ok=True)

    return await run_tool(
        audit=app.audit, tool_name="finboard_data_bundle_export",
        arguments=arguments, handler=_do, identity=app.identity,
    )


async def data_bundle_status(
    app: McpAppContext, bundle_id: str | None, job_id: str | None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        if bool(bundle_id) == bool(job_id):
            raise McpToolError("invalid_argument", "请选择文件标识或任务 ID")
        if job_id:
            async with app.session_maker() as session:
                job = await BackgroundJobRepository(session).get(job_id)
                if job is None or job.kind != BUNDLE_JOB_KIND:
                    raise McpToolError("not_found", "冻结包任务不存在")
                return {
                    "job_id": job.job_id, "status": job.status, "phase": job.phase,
                    "progress_done": job.progress_done, "progress_total": job.progress_total,
                    "bundle_id": job.result_ref, "error_code": job.error_code,
                }
        assert bundle_id is not None
        try:
            return await bundle_receipt(bundle_id)
        except ReleaseBundleError as exc:
            raise McpToolError("not_found", "冻结包不存在或清单不可读") from exc

    return await run_tool(
        audit=app.audit, tool_name="finboard_data_bundle_status",
        arguments={"bundle_id": bundle_id, "job_id": job_id}, handler=_do,
        identity=app.identity,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="finboard_data_bundle_export",
        description="导出冻结发布包到专用目录。需 data.write。返回文件标识、校验和和大小。",
    )
    async def _export(
        release_ids: list[str], ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_bundle_export(app_context(ctx), release_ids)

    @mcp.tool(
        name="finboard_data_bundle_status",
        description="查询已知文件标识或冻结包后台任务的脱敏元数据。",
    )
    async def _status(
        bundle_id: str | None = None, job_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_bundle_status(app_context(ctx), bundle_id, job_id)
