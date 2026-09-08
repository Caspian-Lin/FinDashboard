"""issue #373:任务中心服务端分页(X-Total-Count / offset)与诊断重放 REST 服务化。

路由契约单测(repo 走 mock,不触库);PG 层 offset/count 语义与门控单一
事实源分别在 ``test_background_job_persistence.py`` 与
``test_issue_383_flamegraph_gate.py`` 覆盖。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import job_flamegraph as fl
from finboard_api.routes import job_flamegraph_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(job_flamegraph_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def _job_row(kind: str = "backtest_run", status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(job_id="BJ-DIAG000000000001", kind=kind, status=status)


@pytest.fixture
def profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "job_profiles"
    root.mkdir()
    monkeypatch.setattr(fl, "PROFILE_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def clear_handles() -> None:
    fl._ACTIVE_REPLAYS.clear()


def _make_session_dir(root: Path, job_id: str, stamp: str) -> Path:
    d = root / f"{job_id}-{stamp}"
    d.mkdir()
    return d


# ------------------------------------------------- 分页契约(X-Total-Count/offset)


def test_list_offset_and_total_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finboard_api.routes import jobs_router
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    app = cast(FastAPI, client.app)
    app.include_router(jobs_router)
    list_mock = AsyncMock(return_value=[])
    count_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(BackgroundJobRepository, "list_recent", list_mock)
    monkeypatch.setattr(BackgroundJobRepository, "count_recent", count_mock)

    response = client.get("/api/jobs", params={"limit": 10, "offset": 30})
    assert response.status_code == 200
    assert response.headers["x-total-count"] == "42"
    assert list_mock.call_args.kwargs["offset"] == 30
    assert count_mock.call_args.kwargs == {
        k: v
        for k, v in list_mock.call_args.kwargs.items()
        if k not in {"limit", "offset"}
    }


def test_list_offset_rejected_negative(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finboard_api.routes import jobs_router
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    cast(FastAPI, client.app).include_router(jobs_router)
    monkeypatch.setattr(
        BackgroundJobRepository, "list_recent", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        BackgroundJobRepository, "count_recent", AsyncMock(return_value=0)
    )
    assert client.get("/api/jobs", params={"offset": -1}).status_code == 422


# ------------------------------------------------------- 火焰图:能力表 / 列表


def test_flamegraph_meta(client: TestClient) -> None:
    response = client.get("/api/jobs/flamegraph/meta")
    assert response.status_code == 200
    payload = response.json()
    assert "backtest_run" in payload["replayable_kinds"]
    assert "echo" in payload["rejected_kinds"]


def test_session_list_statuses(
    client: TestClient, profile_root: Path
) -> None:
    job_id = "BJ-DIAG000000000001"
    done = _make_session_dir(profile_root, job_id, "20260908-120000")
    (done / "meta.json").write_text("{}", encoding="utf-8")
    (done / "flamegraph.svg").write_text("<svg/>", encoding="utf-8")
    orphan = _make_session_dir(profile_root, job_id, "20260908-110000")
    (orphan / "meta.json").write_text("{}", encoding="utf-8")
    # 非法目录名(不匹配会话形态)不可见于列表,更不可按路径读取。
    (profile_root / f"{job_id}-garbage").mkdir()

    response = client.get(f"/api/jobs/{job_id}/flamegraph")
    assert response.status_code == 200
    sessions = response.json()
    assert [s["session_id"] for s in sessions] == [
        f"{job_id}-20260908-120000",
        f"{job_id}-20260908-110000",
    ]
    assert sessions[0]["status"] == "done"
    assert sessions[1]["status"] == "orphaned"


def test_svg_endpoint_serves_and_validates_path(
    client: TestClient, profile_root: Path
) -> None:
    job_id = "BJ-DIAG000000000001"
    d = _make_session_dir(profile_root, job_id, "20260908-120000")
    (d / "flamegraph.svg").write_text("<svg/>", encoding="utf-8")
    ok = client.get(f"/api/jobs/{job_id}/flamegraph/{d.name}/flamegraph.svg")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/svg+xml")
    # 未生成 svg 的会话 404;目录穿越形态一律 404。
    empty = _make_session_dir(profile_root, job_id, "20260908-110000")
    assert (
        client.get(f"/api/jobs/{job_id}/flamegraph/{empty.name}/flamegraph.svg").status_code
        == 404
    )
    traversal = f"../{profile_root.name}-20260908-120000"
    assert (
        client.get(
            f"/api/jobs/{job_id}/flamegraph/{traversal}/flamegraph.svg"
        ).status_code
        == 404
    )


# ------------------------------------------------------- 火焰图:触发门控


def test_start_rejects_unknown_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    monkeypatch.setattr(
        BackgroundJobRepository, "get", AsyncMock(return_value=None)
    )
    assert client.post("/api/jobs/BJ-NOPE/flamegraph").status_code == 404


@pytest.mark.parametrize(
    ("kind", "status"),
    [("echo", "succeeded"), ("backtest_run", "running"), ("dataset_publish", "failed")],
)
def test_start_gate_rejections(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    profile_root: Path,
    kind: str,
    status: str,
) -> None:
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    monkeypatch.setattr(
        BackgroundJobRepository, "get", AsyncMock(return_value=_job_row(kind, status))
    )
    response = client.post("/api/jobs/BJ-DIAG000000000001/flamegraph")
    assert response.status_code == 422
    assert response.json()["detail"]


def test_start_py_spy_missing_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, profile_root: Path
) -> None:
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    monkeypatch.setattr(
        BackgroundJobRepository,
        "get",
        AsyncMock(return_value=_job_row()),
    )
    monkeypatch.setattr(fl, "resolve_py_spy", lambda: None)
    response = client.post("/api/jobs/BJ-DIAG000000000001/flamegraph")
    assert response.status_code == 503


def test_start_spawns_detached_replay(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    profile_root: Path,
) -> None:
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    monkeypatch.setattr(
        BackgroundJobRepository,
        "get",
        AsyncMock(return_value=_job_row()),
    )
    monkeypatch.setattr(fl, "resolve_py_spy", lambda: "py-spy-fake")
    spawned: list[list[str]] = []
    handle = cast(
        "subprocess.Popen[bytes]", SimpleNamespace(poll=lambda: None)
    )

    def _fake_spawn(cmd: list[str], log_path: Path) -> object:
        spawned.append(cmd)
        assert log_path.name == "replay.log"
        return handle

    monkeypatch.setattr(fl, "_spawn_detached", _fake_spawn)
    response = client.post("/api/jobs/BJ-DIAG000000000001/flamegraph")
    assert response.status_code == 202
    payload = response.json()["session"]
    assert payload["status"] == "running"
    session_id = payload["session_id"]
    session_dir = profile_root / session_id
    assert session_dir.is_dir()
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "backtest_run"
    assert "backtest_runs" in meta["replay_side_effect"]
    cmd = spawned[0]
    assert cmd[0] == "py-spy-fake"
    assert "job-replay-exec" in cmd
    assert "BJ-DIAG000000000001" in cmd
    assert str(session_dir.resolve()) in cmd
    assert fl._ACTIVE_REPLAYS[session_id] is handle


def test_start_conflicts_with_running_session(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    profile_root: Path,
) -> None:
    from finboard_persistence.background_job_repo import BackgroundJobRepository

    job_id = "BJ-DIAG000000000001"
    monkeypatch.setattr(
        BackgroundJobRepository, "get", AsyncMock(return_value=_job_row())
    )
    d = _make_session_dir(profile_root, job_id, "20260908-120000")
    fl._ACTIVE_REPLAYS[d.name] = cast(
        "subprocess.Popen[bytes]", SimpleNamespace(poll=lambda: None)
    )
    response = client.post(f"/api/jobs/{job_id}/flamegraph")
    assert response.status_code == 409
    assert "已有诊断在运行" in response.json()["detail"]
