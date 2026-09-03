"""issue #309:feature_snapshot_list 响应瘦身 + source_run_id 过滤。

三层单测(mock session / monkeypatch,无需 DB):

* MCP ``finboard_feature_snapshot_list``——默认 header-only(不调全量
  ``list``)、``source_run_id`` 透传、``include_observations=true`` 兼容旧全量;
* REST ``GET /api/research/factors/features``——默认响应无 observations 键、
  ``include_observations=true`` / ``source_run_id`` 参数转发。

repo 层过滤正确性见 ``tests/integration/test_issue_309_snapshot_list_slim.py``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_api.deps import get_db_session
from finboard_api.routes import research_router
from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import factors as factor_tools
from finboard_persistence import FeatureSnapshotHeader

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_DECISION = datetime(2026, 1, 15, tzinfo=UTC)
_RUN_A = "RCR-309aaaaaaaaaaaaaaaa"


async def _async_return(value: object) -> object:
    return value


def _header() -> FeatureSnapshotHeader:
    """沙箱因子快照(#217)的 header 投影:dataset_release_id=None。"""
    return FeatureSnapshotHeader(
        snapshot_id="feature-309aaaa",
        dataset_release_id=None,
        source_run_id=_RUN_A,
        dataset_release_checksum="m" * 64,
        decision_at=_DECISION,
        published_at=_DECISION,
        framework_version="v2",
        feature_names=["u_mom20"],
        symbol_count=2,
        observation_count=2,
        code_version="c" * 40,
        checksum="chk309",
        created_at=_DECISION,
    )


def _full_as_dict() -> dict[str, Any]:
    """含 observations 的全量 as_dict(duck-typed list 返回值)。"""
    header = _header().as_dict()
    header["calculation_windows"] = {}
    header["transformations"] = {}
    header["neutralization"] = {}
    header["issues"] = []
    header["observations"] = [
        {
            "symbol": "S0",
            "feature_name": "u_mom20",
            "value": 1.5,
            "observed_at": _DECISION.isoformat(),
            "available_at": _DECISION.isoformat(),
            "source": "sandbox",
            "source_version": "c" * 40,
        }
    ]
    return header


def _snapshot_domain() -> Any:
    """duck-typed FeatureSnapshot:as_dict 返回全量 payload。"""
    payload = _full_as_dict()
    return type(
        "_SnapshotStub",
        (),
        {"as_dict": staticmethod(lambda: payload)},
    )()


def _make_app() -> McpAppContext:
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


# ---------------------------------------------------------------------------
# MCP:finboard.feature_snapshot.list(#309)
# ---------------------------------------------------------------------------


class TestMcpFeatureSnapshotListSlim309:
    async def test_default_header_only(self, monkeypatch) -> None:
        """默认响应 header-only,且不触碰全量 list(payload 不出内存)。"""
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list_headers(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return [_header()]

        async def fail_list(self: object, **kw: Any) -> list[Any]:
            raise AssertionError("默认 header-only 路径不应调用全量 list")

        monkeypatch.setattr(
            FeatureSnapshotRepository, "list_headers", fake_list_headers
        )
        monkeypatch.setattr(FeatureSnapshotRepository, "list", fail_list)

        env = await factor_tools.feature_snapshot_list(
            _make_app(), source_run_id=_RUN_A
        )
        assert env.status == "ok"
        assert isinstance(env.data, list)
        item = env.data[0]
        assert item["snapshot_id"] == "feature-309aaaa"
        assert item["source_run_id"] == _RUN_A
        assert item["dataset_release_id"] is None
        assert item["feature_names"] == ["u_mom20"]
        assert item["symbol_count"] == 2
        assert item["observation_count"] == 2
        # 瘦身核心断言:逐条值不出现在 list 默认响应
        assert "observations" not in item
        assert captured["source_run_id"] == _RUN_A
        assert captured["dataset_release_id"] is None

    async def test_include_observations_compat(self, monkeypatch) -> None:
        """include_observations=true 走全量 list,响应含 observations。"""
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return [_snapshot_domain()]

        async def fail_list_headers(self: object, **kw: Any) -> list[Any]:
            raise AssertionError("include_observations=true 不应走 header 投影")

        monkeypatch.setattr(FeatureSnapshotRepository, "list", fake_list)
        monkeypatch.setattr(
            FeatureSnapshotRepository, "list_headers", fail_list_headers
        )

        env = await factor_tools.feature_snapshot_list(
            _make_app(), include_observations=True
        )
        assert env.status == "ok"
        item = env.data[0]
        assert item["observations"][0]["symbol"] == "S0"
        assert captured["source_run_id"] is None

    async def test_limit_clamped(self, monkeypatch) -> None:
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list_headers(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return []

        monkeypatch.setattr(
            FeatureSnapshotRepository, "list_headers", fake_list_headers
        )
        env = await factor_tools.feature_snapshot_list(_make_app(), limit=9999)
        assert env.status == "ok"
        assert env.data == []
        assert captured["limit"] == 500

    async def test_records_audit(self, monkeypatch) -> None:
        from finboard_persistence import FeatureSnapshotRepository

        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "list_headers",
            lambda self, **kw: _async_return([]),
        )
        app = _make_app()
        await factor_tools.feature_snapshot_list(app)
        assert app.audit.records[0].tool_name == "finboard.feature_snapshot.list"


# ---------------------------------------------------------------------------
# REST:GET /api/research/factors/features(#309)
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    """构造只挂 research 路由的 TestClient(session 为 AsyncMock)。"""
    from unittest.mock import AsyncMock

    a = FastAPI()
    a.include_router(research_router)
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    a.dependency_overrides[get_db_session] = lambda: session
    return TestClient(a)


class TestRestFeatureSnapshotListSlim309:
    async def test_default_header_only(self, monkeypatch: Any) -> None:
        """默认响应不再含 observations 键,header 字段齐全。"""
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list_headers(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return [_header()]

        monkeypatch.setattr(
            FeatureSnapshotRepository, "list_headers", fake_list_headers
        )
        client = _client()
        resp = client.get("/api/research/factors/features")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        item = items[0]
        assert "observations" not in item
        assert item["source_run_id"] == _RUN_A
        assert item["dataset_release_id"] is None
        assert item["feature_names"] == ["u_mom20"]
        assert item["symbol_count"] == 2
        assert item["observation_count"] == 2
        assert captured["source_run_id"] is None  # 未传参数 → 不过滤
        assert captured["dataset_release_id"] is None

    async def test_include_observations_returns_full(self, monkeypatch: Any) -> None:
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return [_snapshot_domain()]

        monkeypatch.setattr(FeatureSnapshotRepository, "list", fake_list)
        client = _client()
        resp = client.get("/api/research/factors/features", params={"include_observations": "true"})
        assert resp.status_code == 200
        item = resp.json()[0]
        assert item["observations"][0]["symbol"] == "S0"
        assert item["checksum"] == "chk309"
        assert captured["dataset_release_id"] is None

    async def test_source_run_id_filter_forwarded(self, monkeypatch: Any) -> None:
        from finboard_persistence import FeatureSnapshotRepository

        captured: dict[str, Any] = {}

        async def fake_list_headers(self: object, **kw: Any) -> list[Any]:
            captured.update(kw)
            return []

        monkeypatch.setattr(
            FeatureSnapshotRepository, "list_headers", fake_list_headers
        )
        client = _client()
        resp = client.get("/api/research/factors/features", params={"source_run_id": _RUN_A})
        assert resp.status_code == 200
        assert resp.json() == []
        assert captured["source_run_id"] == _RUN_A
