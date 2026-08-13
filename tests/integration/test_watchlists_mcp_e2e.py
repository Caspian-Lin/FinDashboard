"""""#140 端到端:自选股标的组 CRUD(MCP 工具,需要 DB)。

链路:finboard_watchlist_create → list/get → add_symbols(自动去重)→
update(partial)→ remove_symbol → delete(级联删除成员)→ 只读工具
not_found。

同时验证:MCP 工具与 REST/领域共用同一张表(MCP session 能读到
db_session 提交的数据,反之亦然);级联删除在真实 DB 上生效
(watchlist_items 外键 ondelete=CASCADE)。
"""""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import watchlists as wl_tools
from finboard_persistence import WatchlistItemModel, WatchlistModel, session_factory

pytestmark = pytest.mark.asyncio


def _make_app(_engine: AsyncEngine) -> McpAppContext:
    provider = FakeLLMProvider()
    from finboard_app.config import Settings

    return McpAppContext(
        settings=Settings(),
        session_maker=session_factory(_engine),
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=_engine,
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


async def _item_codes(session: AsyncSession, watchlist_id: int) -> list[str]:
    """直接用 repository 读 watchlist_items,验证跨 session 可见性。"""

    from finboard_persistence import WatchlistRepository

    await session.commit()  # 结束 db_session 侧悬挂事务,读 MCP 已提交数据
    repo = WatchlistRepository(session)
    return [it.symbol_code for it in await repo.items(watchlist_id)]


async def test_watchlist_mcp_full_lifecycle(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    app = _make_app(_engine)

    # 1) 创建标的组(写工具)
    create_env = await wl_tools.watchlist_create(
        app, name="回测候选池", description="日频回测标的集合"
    )
    assert create_env.status == "ok", create_env.error
    watchlist_id = create_env.data["id"]
    assert isinstance(watchlist_id, int)
    assert create_env.data["item_count"] == 0
    assert create_env.data["description"] == "日频回测标的集合"

    # 2) 只读工具在真实 DB 上读到刚创建的行
    list_env = await wl_tools.watchlist_list(app)
    assert list_env.status == "ok"
    assert any(w["id"] == watchlist_id for w in list_env.data)

    get_env = await wl_tools.watchlist_get(app, watchlist_id=watchlist_id)
    assert get_env.status == "ok"
    assert get_env.data["symbols"] == []
    assert get_env.data["name"] == "回测候选池"

    # 3) 加标的:输入重复代码 → 自动去重,不触发 (watchlist_id, symbol_code) 唯一约束
    add_env = await wl_tools.watchlist_add_symbols(
        app,
        watchlist_id=watchlist_id,
        symbols=["510300.SH", "513100.SH", "510300.SH"],
    )
    assert add_env.status == "ok", add_env.error
    assert add_env.data["symbols"] == ["510300.SH", "513100.SH"]
    assert add_env.data["item_count"] == 2

    # 4) 再加已存在的标的 → 已存在跳过,仍 2 个;真实 DB 可见(跨 session)
    add_env = await wl_tools.watchlist_add_symbols(
        app, watchlist_id=watchlist_id, symbols=["510300.SH"]
    )
    assert add_env.status == "ok"
    assert add_env.data["item_count"] == 2
    assert await _item_codes(db_session, watchlist_id) == [
        "510300.SH",
        "513100.SH",
    ]

    # 5) partial 更新:只改 name,description 保持
    update_env = await wl_tools.watchlist_update(
        app, watchlist_id=watchlist_id, name="宽基候选池"
    )
    assert update_env.status == "ok", update_env.error
    assert update_env.data["name"] == "宽基候选池"
    assert update_env.data["description"] == "日频回测标的集合"

    # 6) partial 更新:只改 description,name 保持
    update_env = await wl_tools.watchlist_update(
        app, watchlist_id=watchlist_id, description="含债券 ETF"
    )
    assert update_env.status == "ok"
    assert update_env.data["name"] == "宽基候选池"
    assert update_env.data["description"] == "含债券 ETF"

    # 7) 移除单个标的
    remove_env = await wl_tools.watchlist_remove_symbol(
        app, watchlist_id=watchlist_id, symbol_code="510300.SH"
    )
    assert remove_env.status == "ok", remove_env.error
    assert remove_env.data["symbols"] == ["513100.SH"]

    # 8) 移除不存在的标的 → 空操作成功(与 REST 一致)
    remove_env = await wl_tools.watchlist_remove_symbol(
        app, watchlist_id=watchlist_id, symbol_code="600000.SH"
    )
    assert remove_env.status == "ok"
    assert remove_env.data["symbols"] == ["513100.SH"]

    # 9) 删除标的组 → DB 外键级联删除成员
    delete_env = await wl_tools.watchlist_delete(
        app, watchlist_id=watchlist_id
    )
    assert delete_env.status == "ok", delete_env.error
    assert delete_env.data == {"deleted": True, "watchlist_id": watchlist_id}

    # 10) 级联删除验证:watchlist 行与 items 行都消失(直接用 SQLAlchemy 读)
    await db_session.commit()
    watchlist_row = (
        await db_session.execute(
            select(WatchlistModel).where(WatchlistModel.id == watchlist_id)
        )
    ).scalar_one_or_none()
    assert watchlist_row is None
    item_rows = (
        await db_session.execute(
            select(WatchlistItemModel).where(
                WatchlistItemModel.watchlist_id == watchlist_id
            )
        )
    ).scalars().all()
    assert list(item_rows) == []

    # 11) 已删除的标的组 → not_found
    get_env = await wl_tools.watchlist_get(app, watchlist_id=watchlist_id)
    assert get_env.status == "error"
    assert get_env.error is not None
    assert get_env.error.kind == "not_found"
    update_env = await wl_tools.watchlist_update(
        app, watchlist_id=watchlist_id, name="x"
    )
    assert update_env.status == "error"
    assert update_env.error is not None
    assert update_env.error.kind == "not_found"

    # 12) 全部写工具在只读模式(模拟 mcp_readonly_only)下拒绝
    from finboard_mcp.context import McpAppContext as _McpAppContext

    readonly_app = _McpAppContext(
        settings=app.settings,
        session_maker=app.session_maker,
        research_assistant=app.research_assistant,
        audit=AuditRecorder(),
        write_tools_enabled=False,
        engine=_engine,
        provider=app.provider,
        feature_snapshot_jobs=app.feature_snapshot_jobs,
    )
    denied = await wl_tools.watchlist_create(readonly_app, name="x")
    assert denied.status == "denied"
    assert denied.error is not None
    assert denied.error.kind == "permission_denied"
    denied = await wl_tools.watchlist_add_symbols(
        readonly_app, watchlist_id=1, symbols=["510300.SH"]
    )
    assert denied.status == "denied"

    # 13) 不存在的标的组删除 → not_found
    delete_env = await wl_tools.watchlist_delete(
        app, watchlist_id=watchlist_id
    )
    assert delete_env.status == "error"
    assert delete_env.error is not None
    assert delete_env.error.kind == "not_found"
