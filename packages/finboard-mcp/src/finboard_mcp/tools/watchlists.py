"""``finboard.watchlist.*`` 工具 —— 自选股标的组 CRUD(issue #140)。

把 REST ``/api/watchlists``(7 个端点,``routes/watchlist.py``)暴露为 MCP 工具:
agent 可管理「用户标的组」—— 保存常用回测标的集合,为回测 / 研究准备标的池。
自选股是独立用户管理查找列表(``WatchlistModel`` / ``WatchlistItemModel``,
``(watchlist_id, symbol_code)`` 唯一约束,删除级联),与 strategies / simulation
无关联,不触及交易安全红线(不连 broker / 账户 / 订单 / 持仓)。

实现策略(复用现有 repository,不裸 SQL):

* REST 路由直接调用 ``WatchlistRepository``(无独立 service 层),MCP 复用同一
  repository,行为与 REST 一致(404 语义 → ``not_found``);
* 7 个工具 = 2 只读(list / get)+ 5 写(create / update / delete / add_symbols /
  remove_symbol),写工具受 ``_require_write_enabled``(``mcp_readonly_only``)守卫;
* schema 与 REST 一致:``WatchlistCreate{name, description?}`` /
  ``WatchlistUpdate{name?, description?}``(partial,只更新提供的字段)/
  ``WatchlistAddSymbols{symbols}``(输入按序去重 + 已存在跳过,不触发唯一约束冲突);
* ``symbol_code`` 是普通 ``String(20)`` 代码(如 ``000001.SZ``),无外键约束。
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import WatchlistRepository


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""

    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _watchlist_out(row: Any, item_count: int) -> dict[str, Any]:
    """序列化为 ``WatchlistOut`` 兼容 dict(与 REST 字段一致)。"""

    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "item_count": item_count,
                "created_at": row.created_at,
            }
        ),
    )


def _watchlist_detail_out(row: Any, items: list[Any]) -> dict[str, Any]:
    """序列化为 ``WatchlistDetailOut`` 兼容 dict(含成员列表)。"""

    detail = _watchlist_out(row, len(items))
    detail["symbols"] = [it.symbol_code for it in items]
    return detail


# --------------------------------------------------------------------------- #
# 1. watchlist_list(只读)
# --------------------------------------------------------------------------- #


async def watchlist_list(app: McpAppContext) -> ToolEnvelope:
    """列出全部标的组(含成员数)。只读。"""

    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            rows = await repo.list_all()
            result: list[dict[str, Any]] = []
            for w in rows:
                items = await repo.items(w.id)
                result.append(_watchlist_out(w, len(items)))
            await session.commit()
            return result

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.list",
        arguments={},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 2. watchlist_get(只读)
# --------------------------------------------------------------------------- #


async def watchlist_get(
    app: McpAppContext, *, watchlist_id: int
) -> ToolEnvelope:
    """获取标的组详情(含成员列表)。只读。"""

    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            row = await repo.get(watchlist_id)
            if row is None:
                raise McpToolError("not_found", "标的组不存在")
            items = await repo.items(watchlist_id)
            await session.commit()
            return _watchlist_detail_out(row, items)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.get",
        arguments={"watchlist_id": watchlist_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 3. watchlist_create(写)
# --------------------------------------------------------------------------- #


async def watchlist_create(
    app: McpAppContext,
    *,
    name: str,
    description: str | None = None,
) -> ToolEnvelope:
    """创建标的组。写操作。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            row = await repo.create(name, description)
            await session.commit()
            return _watchlist_out(row, 0)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.create",
        arguments={"name": name, "description": description},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 4. watchlist_update(写,partial)
# --------------------------------------------------------------------------- #


async def watchlist_update(
    app: McpAppContext,
    *,
    watchlist_id: int,
    name: str | None = None,
    description: str | None = None,
) -> ToolEnvelope:
    """更新标的组名称 / 描述(partial:只更新提供的字段)。写操作。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            row = await repo.get(watchlist_id)
            if row is None:
                raise McpToolError("not_found", "标的组不存在")
            if name is None and description is None:
                items = await repo.items(watchlist_id)
                await session.commit()
                return _watchlist_out(row, len(items))
            updated = await repo.rename(
                watchlist_id,
                name if name is not None else row.name,
                description if description is not None else row.description,
            )
            items = await repo.items(watchlist_id)
            await session.commit()
            return _watchlist_out(updated if updated is not None else row, len(items))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.update",
        arguments={
            "watchlist_id": watchlist_id,
            "name": name,
            "description": description,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 5. watchlist_delete(写,级联删除成员)
# --------------------------------------------------------------------------- #


async def watchlist_delete(
    app: McpAppContext, *, watchlist_id: int
) -> ToolEnvelope:
    """删除标的组(DB 外键 ``ondelete=CASCADE`` 级联删除成员)。写操作。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            ok = await repo.delete(watchlist_id)
            if not ok:
                raise McpToolError("not_found", "标的组不存在")
            await session.commit()
            return {"deleted": True, "watchlist_id": watchlist_id}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.delete",
        arguments={"watchlist_id": watchlist_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 6. watchlist_add_symbols(写,自动去重)
# --------------------------------------------------------------------------- #


async def watchlist_add_symbols(
    app: McpAppContext,
    *,
    watchlist_id: int,
    symbols: list[str],
) -> ToolEnvelope:
    """向标的组添加标的(输入按序去重 + 已存在跳过)。写操作。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        deduped = list(dict.fromkeys(symbols))
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            if await repo.get(watchlist_id) is None:
                raise McpToolError("not_found", "标的组不存在")
            await repo.add_symbols(watchlist_id, deduped)
            row = await repo.get(watchlist_id)
            items = await repo.items(watchlist_id)
            await session.commit()
            assert row is not None
            return _watchlist_detail_out(row, items)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.add_symbols",
        arguments={"watchlist_id": watchlist_id, "symbols": symbols},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 7. watchlist_remove_symbol(写)
# --------------------------------------------------------------------------- #


async def watchlist_remove_symbol(
    app: McpAppContext,
    *,
    watchlist_id: int,
    symbol_code: str,
) -> ToolEnvelope:
    """从标的组移除单个标的(不存在时为空操作,与 REST 一致)。写操作。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = WatchlistRepository(session)
            if await repo.get(watchlist_id) is None:
                raise McpToolError("not_found", "标的组不存在")
            await repo.remove_symbol(watchlist_id, symbol_code)
            row = await repo.get(watchlist_id)
            items = await repo.items(watchlist_id)
            await session.commit()
            assert row is not None
            return _watchlist_detail_out(row, items)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.watchlist.remove_symbol",
        arguments={"watchlist_id": watchlist_id, "symbol_code": symbol_code},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def register(mcp: MCPServer) -> None:
    """把自选股工具注册到 MCP server(2 只读 + 5 写)。"""

    @mcp.tool(
        name="finboard_watchlist_list",
        description=(
            "列出全部标的组(含成员数 item_count)。自选股是用户标的组 —— 保存常用"
            "回测标的集合(如候选池),供回测/研究复用。无参数。只读。"
        ),
    )
    async def _watchlist_list(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_list(app_context(ctx))

    @mcp.tool(
        name="finboard_watchlist_get",
        description=(
            "获取标的组详情(含成员列表 symbols)。参数:watchlist_id(整数 ID)。"
            "标的组不存在 → not_found。只读。"
        ),
    )
    async def _watchlist_get(
        watchlist_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_get(app_context(ctx), watchlist_id=watchlist_id)

    @mcp.tool(
        name="finboard_watchlist_create",
        description=(
            "[写] 创建标的组。参数:name(必填)/ description(可选)。"
            "返回 WatchlistOut(id/name/description/item_count/created_at)。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _watchlist_create(
        name: str,
        description: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_create(
            app_context(ctx), name=name, description=description
        )

    @mcp.tool(
        name="finboard_watchlist_update",
        description=(
            "[写] 更新标的组名称/描述(partial:只更新提供的字段,不传的字段保持"
            "原值)。参数:watchlist_id(必填)/ name / description,均可选。"
            "返回 WatchlistOut。标的组不存在 → not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _watchlist_update(
        watchlist_id: int,
        name: str | None = None,
        description: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_update(
            app_context(ctx),
            watchlist_id=watchlist_id,
            name=name,
            description=description,
        )

    @mcp.tool(
        name="finboard_watchlist_delete",
        description=(
            "[写] 删除标的组(DB 外键级联删除成员)。参数:watchlist_id(必填)。"
            "返回 {deleted, watchlist_id}。标的组不存在 → not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _watchlist_delete(
        watchlist_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_delete(
            app_context(ctx), watchlist_id=watchlist_id
        )

    @mcp.tool(
        name="finboard_watchlist_add_symbols",
        description=(
            "[写] 向标的组添加标的(输入按序自动去重,已存在的自动跳过,不会触发"
            "(watchlist_id, symbol_code) 唯一约束冲突)。参数:watchlist_id(必填)/ "
            "symbols(标的代码列表,如 000001.SZ、510300.SH)。"
            "返回 WatchlistDetailOut(含最新 symbols)。标的组不存在 → not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _watchlist_add_symbols(
        watchlist_id: int,
        symbols: list[str],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_add_symbols(
            app_context(ctx), watchlist_id=watchlist_id, symbols=symbols
        )

    @mcp.tool(
        name="finboard_watchlist_remove_symbol",
        description=(
            "[写] 从标的组移除单个标的(不存在时为空操作,与 REST 一致)。"
            "参数:watchlist_id(必填)/ symbol_code(必填,如 000001.SZ)。"
            "返回 WatchlistDetailOut(含最新 symbols)。标的组不存在 → not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _watchlist_remove_symbol(
        watchlist_id: int,
        symbol_code: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await watchlist_remove_symbol(
            app_context(ctx),
            watchlist_id=watchlist_id,
            symbol_code=symbol_code,
        )


__all__ = [
    "register",
    "watchlist_add_symbols",
    "watchlist_create",
    "watchlist_delete",
    "watchlist_get",
    "watchlist_list",
    "watchlist_remove_symbol",
    "watchlist_update",
]
