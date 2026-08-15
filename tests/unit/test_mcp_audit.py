"""审计:入参脱敏摘要与审计记录器(#157 增加持久化接线)。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_mcp.audit import AuditRecord, AuditRecorder, now_iso, summarize_arguments


class TestSummarizeArguments:
    def test_redacts_api_keys(self) -> None:
        # sk- 正则要求 sk- 后至少 20 位字母数字
        secret = "sk-" + "a" * 24
        summary = summarize_arguments({"prompt": f"我的 key 是 {secret}"})
        assert secret not in summary["prompt"]

    def test_redacts_passwords(self) -> None:
        summary = summarize_arguments({"prompt": "password=hunter2"})
        assert "hunter2" not in summary["prompt"]

    def test_truncates_long_values(self) -> None:
        summary = summarize_arguments({"prompt": "X" * 500})
        assert len(summary["prompt"]) <= 120

    def test_sensitive_short_value_kept(self) -> None:
        summary = summarize_arguments({"prompt": "短问题"}, sensitive=("prompt",))
        assert summary["prompt"] == "短问题"

    def test_skips_none(self) -> None:
        summary = summarize_arguments({"a": None, "b": 1})
        assert "a" not in summary
        assert summary["b"] == "1"

    def test_preserves_argument_keys(self) -> None:
        summary = summarize_arguments({"limit": 10, "run_id": "RR-1"})
        assert set(summary.keys()) == {"limit", "run_id"}


class _FakeSession:
    """最小 AsyncSession 桩:记录 add 的模型,commit 计数。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.fail_on_commit = False

    def add(self, model: Any) -> None:
        self.added.append(model)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        if self.fail_on_commit:
            raise RuntimeError("db down")
        self.commits += 1


class _FakeSessionMaker:
    """async_sessionmaker 桩:每次调用返回新 session(async context manager)。"""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSessionContext:
        session = _FakeSession()
        self.sessions.append(session)
        return _FakeSessionContext(session)


class _FakeSessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestAuditRecorder:
    def _record(self, **overrides: object) -> AuditRecord:
        defaults: dict[str, object] = {
            "operation_id": "OP-1",
            "tool_name": "finboard.run.list",
            "arguments_summary": {"limit": "3"},
            "status": "ok",
            "latency_ms": 5,
            "error_kind": None,
            "caller": None,
            "recorded_at": now_iso(),
        }
        defaults.update(overrides)
        return AuditRecord(**defaults)  # type: ignore[arg-type]

    async def test_record_appends(self) -> None:
        recorder = AuditRecorder()
        await recorder.record(self._record())
        assert len(recorder.records) == 1
        assert recorder.records[0].tool_name == "finboard.run.list"

    async def test_records_returns_copy(self) -> None:
        recorder = AuditRecorder()
        await recorder.record(self._record())
        snapshot = recorder.records
        await recorder.record(self._record(operation_id="OP-2"))
        assert len(snapshot) == 1  # 快照不受后续追加影响

    async def test_reset_clears(self) -> None:
        recorder = AuditRecorder()
        await recorder.record(self._record())
        recorder.reset()
        assert recorder.records == []

    async def test_denied_status_recorded(self) -> None:
        recorder = AuditRecorder()
        await recorder.record(self._record(status="denied", error_kind="permission_denied"))
        assert recorder.records[0].status == "denied"
        assert recorder.records[0].error_kind == "permission_denied"

    async def test_persist_off_by_default(self) -> None:
        """默认不持久化(内存 + structlog);session_maker 为 None。"""
        recorder = AuditRecorder()
        await recorder.record(self._record())
        assert recorder._session_maker is None

    async def test_persist_appends_row_when_enabled(self) -> None:
        """#157:mcp_audit_persist 开启时,记录追加到 mcp_audit_events(独立事务)。"""
        maker = _FakeSessionMaker()
        recorder = AuditRecorder(
            session_maker=cast("async_sessionmaker[AsyncSession]", maker)
        )
        await recorder.record(
            self._record(caller="agent:mcp", error_kind=None)
        )
        assert len(maker.sessions) == 1
        session = maker.sessions[0]
        assert session.commits == 1
        assert len(session.added) == 1
        row = session.added[0]
        assert row.operation_id == "OP-1"
        assert row.tool_name == "finboard.run.list"
        assert row.status == "ok"
        assert row.caller == "agent:mcp"
        assert row.arguments_summary == {"limit": "3"}
        # 内存副本照常保留。
        assert len(recorder.records) == 1

    async def test_persist_failure_never_breaks_recording(self) -> None:
        """持久化失败只降级为 warning,不影响内存副本 / 工具调用。"""

        class _FailingMaker(_FakeSessionMaker):
            def __call__(self) -> _FakeSessionContext:
                super().__call__()
                self.sessions[-1].fail_on_commit = True
                return _FakeSessionContext(self.sessions[-1])

        recorder = AuditRecorder(
            session_maker=cast("async_sessionmaker[AsyncSession]", _FailingMaker())
        )
        await recorder.record(self._record())
        # 内存副本照常保留(commit 失败被吞掉,只记 warning)。
        assert len(recorder.records) == 1
        assert recorder.records[0].operation_id == "OP-1"


@pytest.mark.unit
class TestNowIso:
    def test_returns_iso_string(self) -> None:
        assert isinstance(now_iso(), str)
        assert "T" in now_iso()
