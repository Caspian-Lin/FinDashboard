"""``finboard.research_code.*`` 工具 —— 研究代码仓库提交/查询(issue #215)。

L3 路线第一环:agent 通过 MCP 受控提交策略/因子 Python 代码到本地 bare
git 仓库(``settings.research_code_repo_path``)。git 写操作收敛在服务端,
agent 容器文件系统只读;**本工具集只做存储与版本化,不执行任何代码**
(执行见后续沙箱 issue)。

权限:submit/rollback 是研究写操作(agent 可自主执行,#122 先例),仍尊重
``mcp_readonly_only``;list/get 只读。每次调用照常审计(mcp_audit_events)。

静态校验(import 白名单/入口签名/manifest/上限/拒二进制)是纵深防御第一层,
硬边界在后续沙箱容器。
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.research_code import (
    IMPORT_WHITELIST,
    ResearchCodeError,
    ResearchCodeService,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import ResearchCodeArtifactRepository

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
            )
            await session.commit()
            return {
                **result,
                "artifact_id": record.artifact_id,
                "status": record.status,
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
    include_files: bool = False,
    commit: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            records = await repo.list_artifacts(
                kind=kind, name=name, status=status, limit=limit
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
            raise LookupError(
                f"代码不存在: kind={kind} name={name} commit={ref}"
            )
        data: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "ref": ref,
            "files": service.read(kind=kind, name=name, commit=commit),
            "history": service.log(kind=kind, name=name, limit=50),
        }
        if diff_from is not None:
            target = commit or service.log(kind=kind, name=name, limit=1)[0][
                "commit"
            ]
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


async def rollback(
    app: McpAppContext, *, kind: str, name: str, commit: str
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            record = await repo.rollback_to(
                kind=kind, name=name, commit=commit
            )
            await session.commit()
            return _artifact_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.research_code.rollback",
        arguments={"kind": kind, "name": name, "commit": commit},
        handler=_do,
    )


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
            "逐条列出可操作问题。重复提交同名生成新 commit,旧版本自动 retired。"
            f" {_KIND_DESC} files 为「路径→文件内容」映射。"
            "返回 {name, kind, commit, checksum, artifact_id}。"
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
            "status 过滤,新→旧)。include_files=true 且结果唯一时附带该版本"
            "文件内容。"
        ),
    )
    async def _list(
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
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
            "把 (kind, name) 的 active 引用回滚到指定历史 commit(现 active "
            "行 retired,历史版本重新登记 active;git 历史不重写)。"
        ),
    )
    async def _rollback(
        kind: str,
        name: str,
        commit: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await rollback(
            app_context(ctx), kind=kind, name=name, commit=commit
        )


__all__ = ["get", "list_artifacts", "register", "rollback", "submit"]
