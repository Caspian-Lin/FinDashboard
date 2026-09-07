"""``finboard_factor_series_build`` / ``finboard_factor_series_get`` MCP 工具单测。

mock session + patch 覆盖秒级失败分支与缓存命中/入队/查询视图:只读模式 /
沙箱未启用 / 无 active 产物 / 未晋级 / 指定 commit 非 active / 发布缺失 /
series_key 命中 unchanged(不建 job)/ 正常入队 / get summary 不含 values /
detail 含全量 / 未知视图。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from finboard_app.config import Settings
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import factor_series
from finboard_persistence import (
    BackgroundJobRepository,
    FactorSeriesRecord,
    FactorSeriesRepository,
    ResearchCodeArtifactRepository,
    ResearchDatasetReleaseRepository,
)

_COMMIT = "c" * 40


@dataclass
class _ArtifactRow:
    artifact_id: str = "RC-1"
    kind: str = "factor"
    name: str = "mom20"
    commit: str = _COMMIT
    path: str = "factors/mom20"
    checksum: str = "deadbeef"
    status: str = "active"
    promotion_status: str = "passed"
    created_by: str = "agent:mcp"
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 29, tzinfo=UTC)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 29, tzinfo=UTC)
    )


class _FakeSession:
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

    async def flush(self) -> None:
        return None

    def begin_nested(self) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


@contextmanager
def _patch_repos(
    *,
    releases: list[str],
    cached: FactorSeriesRecord | None,
    artifact: Any = _ArtifactRow(),
):
    by_id = {rid: MagicMock(release_id=rid) for rid in releases}

    async def _fake_release_get(self: Any, release_id: str) -> Any:
        return by_id.get(release_id)

    async def _fake_find_matching(self: Any, **kwargs: Any) -> Any:
        return cached

    with (
        patch.object(ResearchDatasetReleaseRepository, "get", _fake_release_get),
        patch.object(
            FactorSeriesRepository,
            "find_matching",
            _fake_find_matching,
        ),
        patch.object(ResearchCodeArtifactRepository, "get_active", _fake_active(artifact)),
    ):
        yield


def _fake_active(artifact: Any):
    async def _get_active(self: Any, *, kind: str, name: str) -> Any:
        if artifact is not None and artifact.kind == kind and artifact.name == name:
            return artifact
        return None

    return _get_active


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
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=settings,
        session_maker=cm,
        audit=AuditRecorder(),
        write_tools_enabled=write,
        engine=MagicMock(),
        feature_snapshot_jobs=MagicMock(),
    )


_ARGS: dict[str, Any] = {
    "name": "mom20",
    "release_id": "DR-bars-1",
    "window_start": "2022-01-01",
    "window_end": "2023-06-30",
    "dataset_release_ids": ["DR-fin-1"],
}


class TestBuildEnqueueGuards:
    async def test_readonly_mode_denied(self) -> None:
        app = _make_app(write=False)
        env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.status == "denied"
        assert env.error is not None
        assert "只读" in env.error.message

    async def test_sandbox_disabled(self) -> None:
        app = _make_app(sandbox_enabled=False)
        env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "research_sandbox_enabled" in env.error.message

    async def test_window_reversed(self) -> None:
        app = _make_app()
        env = await factor_series.build_enqueue(
            app, **{**_ARGS, "window_end": "2021-01-01"}
        )
        assert env.error is not None
        assert "window_end" in env.error.message

    async def test_bad_date(self) -> None:
        app = _make_app()
        env = await factor_series.build_enqueue(
            app, **{**_ARGS, "window_start": "2022/01/01"}
        )
        assert env.error is not None
        assert "ISO" in env.error.message

    async def test_release_in_joint_set_rejected(self) -> None:
        app = _make_app()
        env = await factor_series.build_enqueue(
            app, **{**_ARGS, "dataset_release_ids": ["DR-bars-1"]}
        )
        assert env.error is not None
        assert "联合集" in env.error.message

    async def test_no_active_artifact(self) -> None:
        app = _make_app(artifact=None)
        with _patch_repos(
            releases=["DR-bars-1", "DR-fin-1"], cached=None, artifact=None
        ):
            env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "active" in env.error.message

    async def test_unpromoted_artifact_rejected(self) -> None:
        app = _make_app(artifact=_ArtifactRow(promotion_status="pending"))
        with _patch_repos(
            releases=["DR-bars-1", "DR-fin-1"],
            cached=None,
            artifact=_ArtifactRow(promotion_status="pending"),
        ):
            env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "晋级" in env.error.message

    async def test_commit_not_active(self) -> None:
        app = _make_app()
        with _patch_repos(releases=["DR-bars-1", "DR-fin-1"], cached=None):
            env = await factor_series.build_enqueue(
                app, **{**_ARGS, "commit": "d" * 40}
            )
        assert env.error is not None
        assert "active 引用" in env.error.message

    async def test_release_missing(self) -> None:
        app = _make_app()
        with _patch_repos(releases=[], cached=None):
            env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.error is not None
        assert "不存在" in env.error.message


class TestBuildEnqueueAndCache:
    async def test_cache_hit_returns_unchanged_without_job(self) -> None:
        cached = _cached_record()
        app = _make_app()
        with patch.object(
            BackgroundJobRepository, "create_or_get"
        ) as create:
            with _patch_repos(
                releases=["DR-bars-1", "DR-fin-1"], cached=cached
            ):
                env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.status == "ok", env.error
        assert env.data["unchanged"] is True
        assert env.data["series_id"] == cached.series_id
        assert env.data["content_checksum"] == cached.content_checksum
        create.assert_not_called()  # 缓存命中不建 job

    async def test_enqueue_creates_job(self) -> None:
        @dataclass
        class _JobRow:
            job_id = "BJ-fs-1"
            status = "queued"

        async def _create_or_get(self: Any, **kwargs: Any) -> tuple[Any, bool]:
            return _JobRow(), True

        app = _make_app()
        with patch.object(
            BackgroundJobRepository,
            "create_or_get",
            _create_or_get,
        ):
            with _patch_repos(releases=["DR-bars-1", "DR-fin-1"], cached=None):
                env = await factor_series.build_enqueue(app, **_ARGS)
        assert env.status == "ok", env.error
        assert env.data["unchanged"] is False
        assert env.data["job_id"] == "BJ-fs-1"
        assert env.data["kind"] == "factor_series_build"


def _cached_record() -> FactorSeriesRecord:
    return FactorSeriesRecord.build(
        code_artifact="mom20",
        code_commit=_COMMIT,
        kind="factor",
        release_id="DR-bars-1",
        dataset_release_ids=("DR-fin-1",),
        params={},
        window_start=date(2022, 1, 1),
        window_end=date(2023, 6, 30),
        dates=[date(2024, 1, 31)],
        values={"2024-01-31": {"600000.SH": 1.0}},
    )


class TestSeriesGet:
    async def test_get_summary_excludes_values(self) -> None:
        record = _cached_record()
        app = _make_app()
        with patch.object(
            FactorSeriesRepository,
            "get",
            _fake_get(record),
        ):
            env = await factor_series.series_get(app, record.series_id)
        assert env.status == "ok", env.error
        assert env.data["series_id"] == record.series_id
        assert env.data["factor_name"] == "u_mom20"
        assert env.data["date_count"] == 1
        assert "values" not in env.data
        assert "dates" not in env.data

    async def test_get_detail_includes_values(self) -> None:
        record = _cached_record()
        app = _make_app()
        with patch.object(
            FactorSeriesRepository,
            "get",
            _fake_get(record),
        ):
            env = await factor_series.series_get(
                app, record.series_id, view="detail"
            )
        assert env.status == "ok", env.error
        assert env.data["dates"] == ["2024-01-31"]
        assert env.data["values"] == {"2024-01-31": {"600000.SH": 1.0}}

    async def test_get_unknown_view(self) -> None:
        env = await factor_series.series_get(_make_app(), "FS-x", view="full")
        assert env.status == "denied" or env.error is not None

    async def test_get_not_found(self) -> None:
        app = _make_app()
        with patch.object(
            FactorSeriesRepository,
            "get",
            _fake_get(None),
        ):
            env = await factor_series.series_get(app, "FS-missing123456")
        assert env.error is not None
        assert "不存在" in env.error.message


def _fake_get(record: Any):
    async def _get(self: Any, series_id: str) -> Any:
        return record

    return _get
