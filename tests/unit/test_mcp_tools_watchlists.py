"""``finboard.watchlist.*`` 工具 —— 自选股标的组 CRUD 测试
(mock session / monkeypatch,无需 DB)。

覆盖:

* 只读查询(list / get)—— monkeypatch repository 返回真实 ORM 行;
* 写操作(create / update / delete / add_symbols / remove_symbol)—— 验证
  ``write_tools_enabled=False`` 时拒绝(permission_denied);
* 自动去重(add_symbols 输入按序去重后再交给 repository);
* 404 语义(不存在的 watchlist_id → ``not_found``);
* partial 更新(update 只更新提供的字段)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import watchlists as wl_tools
from finboard_persistence import WatchlistItemModel, WatchlistModel

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _watchlist(
    *,
    watchlist_id: int = 1,
    name: str = "核心 ETF",
    description: str | None = "宽基 ETF 候选池",
) -> WatchlistModel:
    return WatchlistModel(
        id=watchlist_id,
        name=name,
        description=description,
        created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
    )


def _items(*codes: str) -> list[WatchlistItemModel]:
    return [
        WatchlistItemModel(
            watchlist_id=1,
            symbol_code=code,
            created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
        )
        for code in codes
    ]


async def _async_return(value: object) -> object:
    return value


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


# ---------------------------------------------------------------------------
# finboard.watchlist.list(只读)
# ---------------------------------------------------------------------------


class TestWatchlistList:
    async def test_returns_watchlists_with_item_count(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "list_all",
            lambda self: _async_return([_watchlist()]),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("510300.SH", "513100.SH")),
        )
        env = await wl_tools.watchlist_list(app)
        assert env.status == "ok"
        assert env.data[0]["id"] == 1
        assert env.data[0]["name"] == "核心 ETF"
        assert env.data[0]["item_count"] == 2
        assert env.data[0]["created_at"]

    async def test_read_allowed_in_readonly_mode(self, monkeypatch: Any) -> None:
        app = _make_app(write_enabled=False)
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "list_all",
            lambda self: _async_return([]),
        )
        env = await wl_tools.watchlist_list(app)
        assert env.status == "ok"
        assert env.data == []

    async def test_records_audit(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "list_all",
            lambda self: _async_return([]),
        )
        await wl_tools.watchlist_list(app)
        assert app.audit.records[0].tool_name == "finboard.watchlist.list"
        assert app.audit.records[0].status == "ok"


# ---------------------------------------------------------------------------
# finboard.watchlist.get(只读)
# ---------------------------------------------------------------------------


class TestWatchlistGet:
    async def test_returns_detail_with_symbols(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("510300.SH")),
        )
        env = await wl_tools.watchlist_get(app, watchlist_id=1)
        assert env.status == "ok"
        assert env.data["symbols"] == ["510300.SH"]
        assert env.data["item_count"] == 1

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(None),
        )
        env = await wl_tools.watchlist_get(app, watchlist_id=999)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_read_allowed_in_readonly_mode(self, monkeypatch: Any) -> None:
        app = _make_app(write_enabled=False)
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return([]),
        )
        env = await wl_tools.watchlist_get(app, watchlist_id=1)
        assert env.status == "ok"


# ---------------------------------------------------------------------------
# finboard.watchlist.create(写)
# ---------------------------------------------------------------------------


class TestWatchlistCreate:
    async def test_creates_and_commits(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "create",
            lambda self, name, description=None: _async_return(_watchlist()),
        )
        env = await wl_tools.watchlist_create(
            app, name="回测候选池", description="日频回测标的"
        )
        assert env.status == "ok"
        assert env.data["name"] == "核心 ETF"
        assert env.data["item_count"] == 0
        session = app.session_maker.return_value.__aenter__.return_value  # type: ignore[attr-defined]
        session.commit.assert_awaited_once()

    async def test_creates_without_description(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "create",
            lambda self, name, description=None: _async_return(
                _watchlist(description=description),
            ),
        )
        env = await wl_tools.watchlist_create(app, name="空描述组")
        assert env.status == "ok"
        assert env.data["description"] is None

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await wl_tools.watchlist_create(app, name="x")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_records_audit(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "create",
            lambda self, name, description=None: _async_return(_watchlist()),
        )
        await wl_tools.watchlist_create(app, name="x")
        assert app.audit.records[0].tool_name == "finboard.watchlist.create"
        assert app.audit.records[0].status == "ok"


# ---------------------------------------------------------------------------
# finboard.watchlist.update(写,partial)
# ---------------------------------------------------------------------------


class TestWatchlistUpdate:
    async def test_partial_update_name_only(self, monkeypatch: Any) -> None:
        """只传 name:rename 用新 name + 保持原 description。"""

        app = _make_app()
        from finboard_persistence import WatchlistRepository

        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )

        async def _fake_rename(self: Any, wid: int, name: str, description: Any) -> Any:
            captured["name"] = name
            captured["description"] = description
            return _watchlist(name=name, description=description)

        monkeypatch.setattr(WatchlistRepository, "rename", _fake_rename)
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return([]),
        )
        env = await wl_tools.watchlist_update(
            app, watchlist_id=1, name="新名字"
        )
        assert env.status == "ok"
        assert captured["name"] == "新名字"
        assert captured["description"] == "宽基 ETF 候选池"

    async def test_partial_update_description_only(self, monkeypatch: Any) -> None:
        """只传 description:name 保持原值(REST PUT 会把 name 清空,这里 partial)。"""

        app = _make_app()
        from finboard_persistence import WatchlistRepository

        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )

        async def _fake_rename(self: Any, wid: int, name: str, description: Any) -> Any:
            captured["name"] = name
            captured["description"] = description
            return _watchlist(name=name, description=description)

        monkeypatch.setattr(WatchlistRepository, "rename", _fake_rename)
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return([]),
        )
        env = await wl_tools.watchlist_update(
            app, watchlist_id=1, description="新描述"
        )
        assert env.status == "ok"
        assert captured["name"] == "核心 ETF"
        assert captured["description"] == "新描述"

    async def test_no_fields_is_noop(self, monkeypatch: Any) -> None:
        """name / description 都不传 → 空操作,返回当前状态。"""

        app = _make_app()
        from finboard_persistence import WatchlistRepository

        rename_mock = AsyncMock()
        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(WatchlistRepository, "rename", rename_mock)
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("510300.SH")),
        )
        env = await wl_tools.watchlist_update(app, watchlist_id=1)
        assert env.status == "ok"
        assert env.data["name"] == "核心 ETF"
        rename_mock.assert_not_awaited()

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(None),
        )
        env = await wl_tools.watchlist_update(
            app, watchlist_id=999, name="x"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await wl_tools.watchlist_update(app, watchlist_id=1, name="x")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# finboard.watchlist.delete(写)
# ---------------------------------------------------------------------------


class TestWatchlistDelete:
    async def test_deletes_and_commits(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "delete",
            lambda self, wid: _async_return(True),
        )
        env = await wl_tools.watchlist_delete(app, watchlist_id=1)
        assert env.status == "ok"
        assert env.data == {"deleted": True, "watchlist_id": 1}
        session = app.session_maker.return_value.__aenter__.return_value  # type: ignore[attr-defined]
        session.commit.assert_awaited_once()

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "delete",
            lambda self, wid: _async_return(False),
        )
        env = await wl_tools.watchlist_delete(app, watchlist_id=999)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await wl_tools.watchlist_delete(app, watchlist_id=1)
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# finboard.watchlist.add_symbols(写,自动去重)
# ---------------------------------------------------------------------------


class TestWatchlistAddSymbols:
    async def test_dedupes_input_before_repo(self, monkeypatch: Any) -> None:
        """输入重复代码 → 按序去重后再交给 repository,不触发唯一约束冲突。"""

        app = _make_app()
        from finboard_persistence import WatchlistRepository

        captured: dict[str, Any] = {}

        async def _fake_add(self: Any, wid: int, codes: list[str]) -> Any:
            captured["codes"] = codes
            return []

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(WatchlistRepository, "add_symbols", _fake_add)
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(
                _items("000001.SZ", "510300.SH"),
            ),
        )
        env = await wl_tools.watchlist_add_symbols(
            app,
            watchlist_id=1,
            symbols=["000001.SZ", "510300.SH", "000001.SZ", "510300.SH"],
        )
        assert env.status == "ok"
        assert captured["codes"] == ["000001.SZ", "510300.SH"]
        assert env.data["symbols"] == ["000001.SZ", "510300.SH"]

    async def test_returns_detail(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "add_symbols",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("510300.SH", "513100.SH")),
        )
        env = await wl_tools.watchlist_add_symbols(
            app, watchlist_id=1, symbols=["510300.SH"]
        )
        assert env.status == "ok"
        assert env.data["item_count"] == 2
        assert env.data["symbols"] == ["510300.SH", "513100.SH"]
        session = app.session_maker.return_value.__aenter__.return_value  # type: ignore[attr-defined]
        session.commit.assert_awaited_once()

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(None),
        )
        env = await wl_tools.watchlist_add_symbols(
            app, watchlist_id=999, symbols=["510300.SH"]
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await wl_tools.watchlist_add_symbols(
            app, watchlist_id=1, symbols=["510300.SH"]
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# finboard.watchlist.remove_symbol(写)
# ---------------------------------------------------------------------------


class TestWatchlistRemoveSymbol:
    async def test_removes_symbol(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        remove_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(
            WatchlistRepository, "remove_symbol", remove_mock
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("513100.SH")),
        )
        env = await wl_tools.watchlist_remove_symbol(
            app, watchlist_id=1, symbol_code="510300.SH"
        )
        assert env.status == "ok"
        remove_mock.assert_awaited_once_with(1, "510300.SH")
        assert env.data["symbols"] == ["513100.SH"]

    async def test_remove_missing_symbol_is_noop(self, monkeypatch: Any) -> None:
        """移除不存在的标的 → 空操作成功(与 REST 一致)。"""

        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(_watchlist()),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "remove_symbol",
            AsyncMock(return_value=False),
        )
        monkeypatch.setattr(
            WatchlistRepository,
            "items",
            lambda self, wid: _async_return(_items("510300.SH")),
        )
        env = await wl_tools.watchlist_remove_symbol(
            app, watchlist_id=1, symbol_code="600000.SH"
        )
        assert env.status == "ok"
        assert env.data["symbols"] == ["510300.SH"]

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence import WatchlistRepository

        monkeypatch.setattr(
            WatchlistRepository,
            "get",
            lambda self, wid: _async_return(None),
        )
        env = await wl_tools.watchlist_remove_symbol(
            app, watchlist_id=999, symbol_code="510300.SH"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await wl_tools.watchlist_remove_symbol(
            app, watchlist_id=1, symbol_code="510300.SH"
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


__all__: list[str] = []
