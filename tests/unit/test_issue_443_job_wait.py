"""``finboard_job_wait`` 有界阻塞等待工具测试(issue #443)。

用桩 repo(monkeypatch ``BackgroundJobRepository.get``,按 job_id 返回预设
状态序列,末位状态保持)验证:

* 已终态 job 立即返回(completed=true,单次查询零等待);
* 运行中 job 阻塞轮询至终态(poll 夹紧下限 1s);
* 超时未终态返回当前快照 + completed=false,**不抛错**;
* 不存在 job → not_found(与 job_get 同口径);
* 并发两个 wait 互不干扰(无共享可变状态);
* 参数夹紧:timeout ≤ 300、poll ≥ 1(纯函数断言 + 轮询风暴行为断言);
* 纯读:session 无 commit/rollback,repo 写方法零调用。

无需 DB。
"""

from __future__ import annotations

import asyncio
import time
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
from finboard_mcp.tools import jobs as job_tools
from finboard_persistence.background_job_repo import BackgroundJobRepository


def _job_row(job_id: str, *, status: str) -> Any:
    """构造 duck-typed BackgroundJobModel 行(满足 JobOut.model_validate)。"""

    now = datetime(2026, 9, 11, tzinfo=UTC)
    return SimpleNamespace(
        job_id=job_id,
        kind="echo",
        queue="default",
        status=status,
        priority=0,
        payload={"msg": "hi"},
        payload_checksum="abc123",
        idempotency_key="idem-key-443",
        progress_total=0,
        progress_done=0,
        phase=None,
        result_ref=None,
        error_code=None,
        error_summary=None,
        attempt=0,
        max_attempts=3,
        worker_id=None,
        heartbeat_at=None,
        lease_until=None,
        requested_by="tester",
        created_at=now,
        started_at=None,
        finished_at=None,
        archived_at=None,
        updated_at=now,
    )


def _session_maker() -> async_sessionmaker[AsyncSession]:
    """mock session_maker(与 test_mcp_tools_jobs.py 同模式,可查 commit 计数)。"""

    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _get_session(app: McpAppContext) -> AsyncMock:
    """从 mock session_maker 提取内部 session 对象。"""

    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _make_app() -> McpAppContext:
    return McpAppContext(
        settings=Settings(),
        session_maker=_session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _patch_get(
    monkeypatch: pytest.MonkeyPatch, sequences: dict[str, list[str]]
) -> dict[str, int]:
    """按 job_id 返回预设状态序列(末位状态保持);返回调用计数 dict。

    序列外的 job_id 返回 None(→ not_found 路径)。
    """

    calls: dict[str, int] = {}

    async def _get(self: Any, jid: str) -> Any:
        calls[jid] = calls.get(jid, 0) + 1
        seq = sequences.get(jid)
        if seq is None:
            return None
        index = min(calls[jid] - 1, len(seq) - 1)
        return _job_row(jid, status=seq[index])

    monkeypatch.setattr(BackgroundJobRepository, "get", _get)
    return calls


def _guard_write_methods(monkeypatch: pytest.MonkeyPatch, recorded: list[str]) -> None:
    """把 repo 全部写方法替换为哨兵:被调用即记名(纯读断言用)。"""

    def _sentinel(name: str) -> Any:
        async def _record(self: Any, *args: Any, **kw: Any) -> Any:
            recorded.append(name)
            return None

        return _record

    for name in (
        "create_or_get",
        "request_cancel",
        "archive",
        "archive_bulk",
        "unarchive",
        "finish",
        "transition",
    ):
        monkeypatch.setattr(BackgroundJobRepository, name, _sentinel(name))


# ---------------------------------------------------------------------------
# 参数夹紧(纯函数)
# ---------------------------------------------------------------------------


class TestJobWaitClamp:
    def test_timeout_above_300_clamped(self) -> None:
        assert job_tools._clamp_wait_params(10_000, 2) == (300, 2)

    def test_poll_below_1_clamped(self) -> None:
        assert job_tools._clamp_wait_params(60, 0) == (60, 1)

    def test_negative_timeout_floored_to_1(self) -> None:
        assert job_tools._clamp_wait_params(-5, -1) == (1, 1)

    def test_defaults_pass_through(self) -> None:
        assert job_tools._clamp_wait_params(60, 2) == (60, 2)


# ---------------------------------------------------------------------------
# finboard.job.wait
# ---------------------------------------------------------------------------


class TestJobWait:
    async def test_terminal_job_returns_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_get(monkeypatch, {"BJ-W1": ["succeeded"]})
        app = _make_app()
        # 预热 _job_out 内部的延迟 import(finboard_api 首次加载 >1s),
        # 否则首窗 waited_seconds 被加载成本污染,「零等待」断言不确定。
        job_tools._job_out(_job_row("BJ-WARM", status="succeeded"))
        env = await job_tools.job_wait(app, "BJ-W1")
        assert env.status == "ok"
        data = env.data
        assert data["completed"] is True
        assert data["status"] == "succeeded"
        assert data["waited_seconds"] == 0
        # finboard_job_get(view=summary) 同形状:剥离 payload,附指纹。
        assert "payload" not in data
        assert data["data_hash"]
        assert data["run_status"] is None
        # 已终态:单次查询,零等待。
        assert calls["BJ-W1"] == 1

    async def test_running_job_waits_until_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_get(
            monkeypatch, {"BJ-W2": ["running", "running", "succeeded"]}
        )
        app = _make_app()
        env = await job_tools.job_wait(
            app, "BJ-W2", timeout_seconds=60, poll_interval_seconds=1
        )
        assert env.status == "ok"
        data = env.data
        assert data["completed"] is True
        assert data["status"] == "succeeded"
        assert data["waited_seconds"] >= 1
        assert calls["BJ-W2"] == 3

    async def test_timeout_returns_snapshot_not_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_get(monkeypatch, {"BJ-W3": ["running"]})
        app = _make_app()
        started = time.monotonic()
        env = await job_tools.job_wait(
            app, "BJ-W3", timeout_seconds=1, poll_interval_seconds=1
        )
        elapsed = time.monotonic() - started
        # 超时不抛错:ok 信封 + completed=false,调用方可续期再等。
        assert env.status == "ok"
        data = env.data
        assert data["completed"] is False
        assert data["status"] == "running"
        assert data["waited_seconds"] >= 1
        assert elapsed < 10
        assert calls["BJ-W3"] <= 4

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_get(monkeypatch, {})
        app = _make_app()
        env = await job_tools.job_wait(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_poll_clamp_prevents_storm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """poll=0 夹到 1:2s 窗口内查询次数有界(sleep(0) 忙轮询会是上千次)。"""
        calls = _patch_get(monkeypatch, {"BJ-W4": ["running"]})
        app = _make_app()
        env = await job_tools.job_wait(
            app, "BJ-W4", timeout_seconds=2, poll_interval_seconds=0
        )
        assert env.status == "ok"
        assert env.data["completed"] is False
        assert calls["BJ-W4"] <= 5

    async def test_concurrent_waits_are_independent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """并发两个 wait 互不干扰:各自按自己的序列与终态返回。"""
        _patch_get(
            monkeypatch,
            {"BJ-A": ["running", "succeeded"], "BJ-B": ["running", "failed"]},
        )
        app = _make_app()
        env_a, env_b = await asyncio.gather(
            job_tools.job_wait(
                app, "BJ-A", timeout_seconds=30, poll_interval_seconds=1
            ),
            job_tools.job_wait(
                app, "BJ-B", timeout_seconds=30, poll_interval_seconds=1
            ),
        )
        assert env_a.status == "ok"
        assert env_b.status == "ok"
        assert env_a.data["job_id"] == "BJ-A"
        assert env_a.data["status"] == "succeeded"
        assert env_a.data["completed"] is True
        assert env_b.data["job_id"] == "BJ-B"
        assert env_b.data["status"] == "failed"
        assert env_b.data["completed"] is True

    async def test_pure_read_no_write_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """纯读:session 无 commit/rollback,repo 写方法零调用。"""
        writes: list[str] = []
        _guard_write_methods(monkeypatch, writes)
        _patch_get(monkeypatch, {"BJ-W5": ["succeeded"]})
        app = _make_app()
        env = await job_tools.job_wait(app, "BJ-W5")
        assert env.status == "ok"
        session = _get_session(app)
        assert session.commit.await_count == 0
        assert session.rollback.await_count == 0
        assert writes == []
