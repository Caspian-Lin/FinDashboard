"""``finboard_research_code_run`` MCP 工具 —— 入队预检与查询(issue #216)。

DB 相关路径(artifact/release 解析、job 幂等创建)由集成测试覆盖;此处用
mock session + class 级 patch 覆盖秒级失败分支:只读模式 / 沙箱未启用 /
kind 非 factor / decision_at 非法 / 无 active 代码 / 指定 commit 非
active / 发布缺 bars。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from finboard_app.config import Settings
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import research_code_run
from finboard_persistence import ResearchDatasetReleaseRepository


@dataclass
class _ArtifactRow:
    artifact_id: str = "RC-1"
    kind: str = "factor"
    name: str = "mom20"
    commit: str = "a" * 40
    path: str = "factors/mom20"
    checksum: str = "deadbeef"
    status: str = "active"
    created_by: str = "agent:mcp"
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 29, tzinfo=UTC)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 29, tzinfo=UTC)
    )


@dataclass(frozen=True)
class _Kind:
    value: str = "bars"


@dataclass
class _ReleaseRow:
    release_id: str
    release_checksum: str = "cs"
    dataset_kind: _Kind = field(default_factory=_Kind)


class _FakeSession:
    """artifact 与 background_jobs 两条查询路径的假 session。"""

    def __init__(self, *, artifact: Any) -> None:
        self._artifact = artifact

    async def execute(self, stmt: Any) -> Any:
        result = MagicMock()
        scalars = MagicMock()
        scalars.first.return_value = self._artifact
        result.scalars.return_value = scalars
        result.scalar_one_or_none.return_value = None
        return result

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        return None

    def begin_nested(self) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    async def flush(self) -> None:
        return None


@contextmanager
def _patch_release_get(releases: list[_ReleaseRow]):
    """把 ``ResearchDatasetReleaseRepository.get`` 换成查表的假实现。"""

    by_id = {r.release_id: r for r in releases}

    async def _fake_get(self, release_id: str) -> Any:
        return by_id.get(release_id)

    with patch.object(ResearchDatasetReleaseRepository, "get", _fake_get):
        yield


def _make_app(
    *,
    sandbox_enabled: bool = True,
    write: bool = True,
    artifact: Any = _ArtifactRow(),
) -> McpAppContext:
    settings = Settings(
        research_sandbox_enabled=sandbox_enabled,
        research_sandbox_workspace_root="unused",
    )
    session = _FakeSession(artifact=artifact)
    # maker() 返回 cm;async with 走 cm.return_value.__aenter__(对齐 #215 测试模式)
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    maker = cm
    return McpAppContext(
        settings=settings,
        session_maker=maker,
        audit=AuditRecorder(),
        write_tools_enabled=write,
        engine=MagicMock(),
        feature_snapshot_jobs=MagicMock(),
    )


_ARGS: dict[str, Any] = {
    "kind": "factor",
    "name": "mom20",
    "dataset_release_ids": ["DR-bars"],
    "decision_at": "2024-06-03T15:00:00+08:00",
}


class TestRunEnqueueGuards:
    async def test_readonly_mode_denied(self) -> None:
        app = _make_app(write=False)
        env = await research_code_run.run_enqueue(app, **_ARGS)
        assert env.status == "denied"
        assert env.error is not None
        assert "只读" in env.error.message

    async def test_sandbox_disabled(self) -> None:
        app = _make_app(sandbox_enabled=False)
        env = await research_code_run.run_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "research_sandbox_enabled" in env.error.message

    async def test_kind_must_be_factor(self) -> None:
        app = _make_app()
        env = await research_code_run.run_enqueue(
            app, **{**_ARGS, "kind": "strategy"}
        )
        assert env.error is not None
        assert "factor" in env.error.message

    async def test_naive_decision_at(self) -> None:
        app = _make_app()
        env = await research_code_run.run_enqueue(
            app, **{**_ARGS, "decision_at": "2024-06-03T15:00:00"}
        )
        assert env.error is not None
        assert "时区" in env.error.message

    async def test_no_active_artifact(self) -> None:
        app = _make_app(artifact=None)
        with _patch_release_get([_ReleaseRow("DR-bars")]):
            env = await research_code_run.run_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "active" in env.error.message

    async def test_commit_not_active(self) -> None:
        app = _make_app()
        with _patch_release_get([_ReleaseRow("DR-bars")]):
            env = await research_code_run.run_enqueue(
                app, **{**_ARGS, "commit": "b" * 40}
            )
        assert env.error is not None
        assert "active 引用" in env.error.message

    async def test_release_missing(self) -> None:
        app = _make_app()
        with _patch_release_get([]):
            env = await research_code_run.run_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "不存在" in env.error.message

    async def test_release_without_bars_kind(self) -> None:
        app = _make_app()
        with _patch_release_get(
            [_ReleaseRow("DR-fin", dataset_kind=_Kind("daily_metrics"))]
        ):
            env = await research_code_run.run_enqueue(
                app, **{**_ARGS, "dataset_release_ids": ["DR-fin"]}
            )
        assert env.error is not None
        assert "bars" in env.error.message

    async def test_enqueue_ok(self) -> None:
        app = _make_app()
        with _patch_release_get(
            [
                _ReleaseRow("DR-bars"),
                _ReleaseRow("DR-fin", dataset_kind=_Kind("financial_indicators")),
            ]
        ):
            env = await research_code_run.run_enqueue(
                app,
                **{**_ARGS, "dataset_release_ids": ["DR-bars", "DR-fin"]},
            )
        assert env.status == "ok", env.error
        assert env.data["job_id"].startswith("BJ-")
        assert env.data["kind"] == "research_code_run"
        assert env.data["created"] is True
