"""``finboard.strategy.*`` / ``finboard.preset.*`` 工具 ——
策略规格工具测试(mock session / monkeypatch,无需 DB)。

覆盖:

* 只读查询(registry / template / list / history / version_get / diff /
  preset list/get)—— monkeypatch repository 返回 duck-typed ORM 行;
* 写操作(validate / draft_create / supersede / publish / rollback /
  preset create/update/delete)—— monkeypatch compiler / repository /
  ``get_strategy_definition``,验证 ``write_tools_enabled=False`` 时拒绝;
* registry / template 工具无 DB 依赖,直接验证注册表函数返回。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import strategies as strategy_tools

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    return value


def _passthrough_row(row: Any) -> dict[str, Any]:
    """绕过 ``_version_to_dict`` 内部的 ResearchStrategySpec.model_validate,
    直接把 duck-typed 行字段拷成 JSON 字典(用于只读工具单测)。"""
    return {
        "strategy_id": row.strategy_id,
        "version": row.version,
        "schema_version": row.schema_version,
        "name": row.name,
        "strategy_kind": row.strategy_kind,
        "status": row.status,
        "change_type": row.change_type,
        "checksum": row.checksum,
        "spec": dict(row.payload),
        "validation_errors": list(row.validation_errors),
        "parent_version": row.parent_version,
        "rollback_of_version": row.rollback_of_version,
        "created_at": row.created_at,
        "published_at": row.published_at,
    }


def _spec_row(
    strategy_id: str = "mf_test", version: int = 1
) -> SimpleNamespace:
    """构造一个 duck-typed ``ResearchStrategySpecModel`` ORM 行。"""
    return SimpleNamespace(
        strategy_id=strategy_id,
        version=version,
        schema_version="v1",
        name="测试策略",
        strategy_kind="multi_factor",
        status="draft",
        change_type="create",
        checksum="chk123",
        payload={
            "schema_version": "v1",
            "strategy_id": strategy_id,
            "name": "测试策略",
            "description": "测试",
            "strategy_kind": "multi_factor",
        },
        validation_errors=[],
        parent_version=None,
        rollback_of_version=None,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        published_at=None,
    )


def _preset_row(preset_id: int = 1, name: str = "preset1") -> SimpleNamespace:
    """构造一个 duck-typed ``StrategyPresetModel`` ORM 行。"""
    return SimpleNamespace(
        id=preset_id,
        name=name,
        strategy="ma_cross",
        params={"short_window": 5, "long_window": 20},
        selection={"top_k": 10},
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        updated_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


def _plan() -> SimpleNamespace:
    """构造一个 duck-typed ``ResolvedStrategyPlan``。"""
    return SimpleNamespace(
        checksum="chk123",
        feature_order=("momentum", "reversal"),
        required_factor_sources=("price",),
        required_datasets=("daily_bars",),
        dataset_release_ids=("REL-1",),
        lifecycle_stages=("universe", "features", "signal", "portfolio"),
        can_execute=False,
    )


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
# finboard.strategy.registry(无 DB)
# ---------------------------------------------------------------------------


class TestStrategyRegistry:
    async def test_returns_registry(self) -> None:
        app = _make_app()
        env = await strategy_tools.strategy_registry(app)
        assert env.status == "ok"
        assert "strategies" in env.data
        assert "feature_sources" in env.data
        assert "operators" in env.data
        assert env.data["accepts_python"] is False

    async def test_records_audit(self) -> None:
        app = _make_app()
        await strategy_tools.strategy_registry(app)
        assert app.audit.records[0].tool_name == "finboard.strategy.registry"


# ---------------------------------------------------------------------------
# finboard.strategy.template(无 DB)
# ---------------------------------------------------------------------------


class TestStrategyTemplate:
    async def test_invalid_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_backtest.strategy_spec import StrategySpecError

        def _raise(kind: str, **kw: Any) -> Any:
            raise StrategySpecError(f"未知策略 kind: {kind}")

        monkeypatch.setattr(
            "finboard_backtest.strategy_spec.build_strategy_template", _raise
        )
        app = _make_app()
        env = await strategy_tools.strategy_template(
            app,
            kind="nonexistent",
            strategy_id="mf_test",
            dataset_release_ids=["REL-1"],
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# finboard.strategy.list / history / version_get(只读 DB)
# ---------------------------------------------------------------------------


class TestStrategyList:
    async def test_returns_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_latest",
            lambda self, **kw: _async_return([_spec_row()]),
        )
        monkeypatch.setattr(
            strategy_tools, "_version_to_dict", _passthrough_row
        )
        env = await strategy_tools.strategy_list(app)
        assert env.status == "ok"
        assert env.data[0]["strategy_id"] == "mf_test"

    async def test_records_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_latest",
            lambda self, **kw: _async_return([]),
        )
        await strategy_tools.strategy_list(app)
        assert app.audit.records[0].tool_name == "finboard.strategy.list"


class TestStrategyHistory:
    async def test_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_history",
            lambda self, sid: _async_return([]),
        )
        env = await strategy_tools.strategy_history(
            app, strategy_id="missing"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_returns_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_history",
            lambda self, sid: _async_return([_spec_row(version=1), _spec_row(version=2)]),
        )
        monkeypatch.setattr(
            strategy_tools, "_version_to_dict", _passthrough_row
        )
        env = await strategy_tools.strategy_history(app, strategy_id="mf_test")
        assert env.status == "ok"
        assert len(env.data) == 2


class TestStrategyVersionGet:
    async def test_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(None),
        )
        env = await strategy_tools.strategy_version_get(
            app, strategy_id="mf_test", version=99
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_returns_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(_spec_row()),
        )
        monkeypatch.setattr(
            strategy_tools, "_version_to_dict", _passthrough_row
        )
        env = await strategy_tools.strategy_version_get(
            app, strategy_id="mf_test", version=1
        )
        assert env.status == "ok"
        assert env.data["version"] == 1
        assert "spec" in env.data


# ---------------------------------------------------------------------------
# finboard.strategy.diff(只读 DB)
# ---------------------------------------------------------------------------


class TestStrategyDiff:
    async def test_version_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(None),
        )
        env = await strategy_tools.strategy_diff(
            app,
            strategy_id="mf_test",
            from_version=1,
            to_version=2,
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_returns_diff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        change = SimpleNamespace(
            as_dict=lambda: {"path": "name", "before": "A", "after": "B"}
        )
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(_spec_row(version=v)),
        )
        monkeypatch.setattr(
            "finboard_backtest.strategy_spec.structured_diff",
            lambda before, after: (change,),
        )
        env = await strategy_tools.strategy_diff(
            app,
            strategy_id="mf_test",
            from_version=1,
            to_version=2,
        )
        assert env.status == "ok"
        assert env.data["changes"][0]["path"] == "name"
        assert app.audit.records[0].tool_name == "finboard.strategy.diff"


# ---------------------------------------------------------------------------
# finboard.preset.list / get(只读 DB)
# ---------------------------------------------------------------------------


class TestPresetList:
    async def test_returns_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "list_all",
            lambda self: _async_return([_preset_row()]),
        )
        env = await strategy_tools.preset_list(app)
        assert env.status == "ok"
        assert env.data[0]["name"] == "preset1"

    async def test_records_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "list_all",
            lambda self: _async_return([]),
        )
        await strategy_tools.preset_list(app)
        assert app.audit.records[0].tool_name == "finboard.preset.list"


class TestPresetGet:
    async def test_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "get",
            lambda self, pid: _async_return(None),
        )
        env = await strategy_tools.preset_get(app, preset_id=99)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_returns_preset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "get",
            lambda self, pid: _async_return(_preset_row()),
        )
        env = await strategy_tools.preset_get(app, preset_id=1)
        assert env.status == "ok"
        assert env.data["id"] == 1


# ---------------------------------------------------------------------------
# finboard.strategy.validate(纯计算,写权限门)
# ---------------------------------------------------------------------------


class TestStrategyValidate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.strategy_validate(
            app, spec={"strategy_id": "mf_test"}
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_spec(self) -> None:
        """spec 缺少必填字段 → ValidationError → invalid_argument。"""
        app = _make_app()
        env = await strategy_tools.strategy_validate(
            app, spec={"strategy_id": "mf_test"}  # 缺大量必填字段
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# finboard.strategy.draft_create / supersede / publish / rollback(写)
# ---------------------------------------------------------------------------


class TestStrategyDraftCreate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.strategy_draft_create(
            app, spec={"strategy_id": "mf_test"}
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_spec(self) -> None:
        app = _make_app()
        env = await strategy_tools.strategy_draft_create(
            app, spec={"strategy_id": "mf_test"}
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestStrategySupersede:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.strategy_supersede(
            app,
            strategy_id="mf_test",
            spec={"strategy_id": "mf_test"},
            expected_version=1,
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_id_mismatch(self) -> None:
        """path strategy_id 与 spec.strategy_id 不一致 → invalid_argument。"""
        app = _make_app()
        env = await strategy_tools.strategy_supersede(
            app,
            strategy_id="mf_a",
            spec={"strategy_id": "mf_b", "name": "x"},
            expected_version=1,
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestStrategyPublish:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.strategy_publish(
            app, strategy_id="mf_test", version=1, expected_version=1
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_version_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(None),
        )
        env = await strategy_tools.strategy_publish(
            app, strategy_id="mf_test", version=99, expected_version=1
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestStrategyRollback:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.strategy_rollback(
            app, strategy_id="mf_test", target_version=1, expected_version=1
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_target_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchStrategySpecRepository

        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(None),
        )
        env = await strategy_tools.strategy_rollback(
            app,
            strategy_id="mf_test",
            target_version=99,
            expected_version=1,
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


# ---------------------------------------------------------------------------
# finboard.preset.create / update / delete(写)
# ---------------------------------------------------------------------------


class TestPresetCreate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.preset_create(
            app, name="p", strategy="ma_cross"
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_empty_name(self) -> None:
        app = _make_app()
        env = await strategy_tools.preset_create(
            app, name="   ", strategy="ma_cross"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_unknown_strategy_kind(self) -> None:
        app = _make_app()
        env = await strategy_tools.preset_create(
            app, name="p1", strategy="nonexistent_kind"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_name_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "get_by_name",
            lambda self, name: _async_return(_preset_row(name=name)),
        )
        env = await strategy_tools.preset_create(
            app, name="preset1", strategy="ma_cross"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"


class TestPresetUpdate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.preset_update(app, preset_id=1)
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "get",
            lambda self, pid: _async_return(None),
        )
        env = await strategy_tools.preset_update(app, preset_id=99)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestPresetDelete:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await strategy_tools.preset_delete(app, preset_id=1)
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import StrategyPresetRepository

        monkeypatch.setattr(
            StrategyPresetRepository,
            "delete",
            lambda self, pid: _async_return(False),
        )
        env = await strategy_tools.preset_delete(app, preset_id=99)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
