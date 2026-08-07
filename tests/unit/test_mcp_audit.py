"""审计:入参脱敏摘要与审计记录器。"""

from __future__ import annotations

import pytest

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


@pytest.mark.unit
class TestNowIso:
    def test_returns_iso_string(self) -> None:
        assert isinstance(now_iso(), str)
        assert "T" in now_iso()
