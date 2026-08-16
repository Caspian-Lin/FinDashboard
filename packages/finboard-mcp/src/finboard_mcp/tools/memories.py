"""``finboard.memory.*`` 工具 —— 研究长期记忆 / 研究笔记(issue #110)。

让 OpenCode 研究 Agent 跨会话积累结构化研究上下文。记忆通过 ``source_refs``
关联研究产物(数据集 / 策略 / 实验 / ResearchRun / Simulation),只引用不修改。

权限:记忆工具只操作独立 ``research_memories`` 表,不触及实盘
orders/fills/positions/audit_logs,也不创建 ResearchRun / 回测 / 模拟盘,
因此自动允许(直接执行),无需人工审批。

红线:``source_refs`` 只是引用,绝不修改被引用产物本身。
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import ResearchMemory, ResearchMemoryRepository, SourceRef

_AGENT_ACTOR = "agent:mcp"


def _ref_to_dict(ref: SourceRef) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": ref.kind, "ref_id": ref.ref_id}
    if ref.label is not None:
        d["label"] = ref.label
    return d


def _memory_to_dict(m: ResearchMemory) -> dict[str, Any]:
    return {
        "memory_id": m.memory_id,
        "memory_type": m.memory_type,
        "content": m.content,
        "source_refs": [_ref_to_dict(s) for s in m.source_refs],
        "status": m.status,
        "tags": m.tags,
        "created_by": m.created_by,
        "conversation_id": m.conversation_id,
        "confirmed_by": m.confirmed_by,
        "confirmed_at": to_jsonable(m.confirmed_at),
        "supersedes_id": m.supersedes_id,
        "created_at": to_jsonable(m.created_at),
        "updated_at": to_jsonable(m.updated_at),
    }


def _parse_refs(raw: list[dict[str, Any]] | None) -> list[SourceRef]:
    return [
        SourceRef(
            kind=str(r.get("kind", "unknown")),
            ref_id=str(r.get("ref_id", "")),
            label=r.get("label"),
        )
        for r in (raw or [])
    ]


async def remember(
    app: McpAppContext,
    *,
    memory_type: str,
    content: str,
    source_refs: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    conversation_id: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.remember(
                memory_type=memory_type,
                content=content,
                source_refs=_parse_refs(source_refs),
                tags=tags or [],
                created_by=_AGENT_ACTOR,
                conversation_id=conversation_id,
            )
            await session.commit()
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.remember",
        arguments={
            "memory_type": memory_type,
            "content": content,
            "source_refs": source_refs,
            "tags": tags,
            "conversation_id": conversation_id,
        },
        handler=_do,
        sensitive=("content",),
    )


async def list_memories(
    app: McpAppContext,
    *,
    status: str | None = None,
    memory_type: str | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    tag: str | None = None,
    conversation_id: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            records = await repo.list_memories(
                status=status,
                memory_type=memory_type,
                source_kind=source_kind,
                source_ref=source_ref,
                tag=tag,
                conversation_id=conversation_id,
                limit=limit,
            )
            return [_memory_to_dict(r) for r in records]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.list",
        arguments={
            "status": status,
            "memory_type": memory_type,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "tag": tag,
            "conversation_id": conversation_id,
            "limit": limit,
        },
        handler=_do,
    )


async def get_memory(app: McpAppContext, memory_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.get(memory_id)
            if record is None:
                raise LookupError(f"研究记忆不存在: {memory_id}")
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.get",
        arguments={"memory_id": memory_id},
        handler=_do,
    )


async def forget(app: McpAppContext, memory_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.forget(memory_id)
            await session.commit()
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.forget",
        arguments={"memory_id": memory_id},
        handler=_do,
    )


async def correct(
    app: McpAppContext,
    *,
    memory_id: str,
    content: str,
    source_refs: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    conversation_id: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.correct(
                memory_id=memory_id,
                content=content,
                source_refs=_parse_refs(source_refs),
                tags=tags,
                created_by=_AGENT_ACTOR,
                conversation_id=conversation_id,
            )
            await session.commit()
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.correct",
        arguments={
            "memory_id": memory_id,
            "content": content,
            "source_refs": source_refs,
            "tags": tags,
            "conversation_id": conversation_id,
        },
        handler=_do,
        sensitive=("content",),
    )


async def confirm(
    app: McpAppContext,
    memory_id: str,
    actor: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.confirm(memory_id, actor=actor or _AGENT_ACTOR)
            await session.commit()
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.confirm",
        arguments={"memory_id": memory_id, "actor": actor},
        handler=_do,
    )


async def archive(app: McpAppContext, memory_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchMemoryRepository(session)
            record = await repo.archive(memory_id)
            await session.commit()
            return _memory_to_dict(record)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.memory.archive",
        arguments={"memory_id": memory_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把研究记忆工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_memory_remember",
        description=(
            "记住一条研究记忆(跨会话长期上下文)。memory_type: "
            "note/insight/correction/confirmation。source_refs 关联研究产物"
            "(research_run/strategy/simulation/dataset/experiment/hypothesis),"
            "只引用不修改产物。"
        ),
    )
    async def _remember(
        memory_type: str,
        content: str,
        source_refs: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
        conversation_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await remember(
            app_context(ctx),
            memory_type=memory_type,
            content=content,
            source_refs=source_refs,
            tags=tags,
            conversation_id=conversation_id,
        )

    @mcp.tool(
        name="finboard_memory_list",
        description=(
            "列出研究记忆(可选按 status/memory_type/source/tag/conversation 过滤,"
            "默认返回最近 100 条 active)。"
        ),
    )
    async def _list(
        status: str | None = None,
        memory_type: str | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        tag: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await list_memories(
            app_context(ctx),
            status=status,
            memory_type=memory_type,
            source_kind=source_kind,
            source_ref=source_ref,
            tag=tag,
            conversation_id=conversation_id,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_memory_get",
        description="查询单条研究记忆详情(含 forgotten/archived)。",
    )
    async def _get(
        memory_id: str, ctx: Context = None  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await get_memory(app_context(ctx), memory_id)

    @mcp.tool(
        name="finboard_memory_forget",
        description="软删除一条研究记忆(status=forgotten,保留审计)。",
    )
    async def _forget(
        memory_id: str, ctx: Context = None  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await forget(app_context(ctx), memory_id)

    @mcp.tool(
        name="finboard_memory_correct",
        description=(
            "纠正一条研究记忆:新建 active 记忆,旧记忆标记 forgotten,"
            "经 supersedes_id 形成纠正链。source_refs/tags 不传则继承旧记忆。"
        ),
    )
    async def _correct(
        memory_id: str,
        content: str,
        source_refs: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
        conversation_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await correct(
            app_context(ctx),
            memory_id=memory_id,
            content=content,
            source_refs=source_refs,
            tags=tags,
            conversation_id=conversation_id,
        )

    @mcp.tool(
        name="finboard_memory_confirm",
        description="确认一条研究记忆有效(标记 confirmed_by/confirmed_at)。",
    )
    async def _confirm(
        memory_id: str,
        actor: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await confirm(app_context(ctx), memory_id, actor=actor)

    @mcp.tool(
        name="finboard_memory_archive",
        description="归档一条研究记忆(status=archived)。",
    )
    async def _archive(
        memory_id: str, ctx: Context = None  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await archive(app_context(ctx), memory_id)


__all__ = [
    "archive",
    "confirm",
    "correct",
    "forget",
    "get_memory",
    "list_memories",
    "register",
    "remember",
]
