"""run_tool 统一执行器:成功路径、来源提升与异常到信封的映射。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from finboard_mcp.audit import AuditRecorder
from finboard_mcp.envelope import ToolData, ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool


def _returning(value: object) -> Callable[[], Awaitable[object]]:
    async def handler() -> object:
        return value

    return handler


def _raising(exc: BaseException) -> Callable[[], Awaitable[None]]:
    async def handler() -> None:
        raise exc

    return handler


async def _run(handler: Callable[[], Awaitable[Any]]) -> ToolEnvelope:
    return await run_tool(
        audit=AuditRecorder(),
        tool_name="t",
        arguments={"k": "v"},
        handler=handler,
    )


class TestSuccessPath:
    async def test_ok(self) -> None:
        env = await _run(_returning({"x": 1}))
        assert env is not None
        assert env.status == "ok"
        assert env.data == {"x": 1}

    async def test_tooldata_lifts_provenance(self) -> None:
        env = await _run(_returning(ToolData(data=1, provenance={"k": 1})))
        assert env is not None
        assert env.data == 1
        assert env.provenance == {"k": 1}


class TestExceptionMapping:
    async def test_timeout_is_retryable(self) -> None:
        env = await _run(_raising(TimeoutError()))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "timeout"
        assert env.error.retryable is True

    async def test_lookup_error_maps_not_found(self) -> None:
        env = await _run(_raising(KeyError("RR-1")))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_value_error_maps_invalid_argument(self) -> None:
        env = await _run(_raising(ValueError("参数错")))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_mcp_tool_error_conflict(self) -> None:
        env = await _run(_raising(McpToolError("conflict", "冲突")))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_mcp_tool_error_permission_denied(self) -> None:
        env = await _run(_raising(McpToolError("permission_denied", "拒绝")))
        assert env.status == "denied"

    async def test_generic_maps_unavailable(self) -> None:
        env = await _run(_raising(RuntimeError("boom")))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "unavailable"
        assert env.error.retryable is False


class TestAuditIntegration:
    async def test_ok_records_audit(self) -> None:
        audit = AuditRecorder()
        await run_tool(
            audit=audit,
            tool_name="finboard.run.list",
            arguments={"limit": 5},
            handler=_returning([]),
        )
        assert len(audit.records) == 1
        assert audit.records[0].status == "ok"
        assert audit.records[0].tool_name == "finboard.run.list"

    async def test_sensitive_argument_redacted_in_audit(self) -> None:
        secret = "sk-" + "b" * 24
        audit = AuditRecorder()
        await run_tool(
            audit=audit,
            tool_name="finboard.ai.ask",
            arguments={"prompt": f"我的 key 是 {secret}"},
            handler=_raising(McpToolError("permission_denied", "x")),
            sensitive=("prompt",),
        )
        recorded_prompt = audit.records[0].arguments_summary["prompt"]
        assert secret not in recorded_prompt
        assert audit.records[0].status == "denied"
