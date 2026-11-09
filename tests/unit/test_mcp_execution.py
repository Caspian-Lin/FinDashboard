"""run_tool 统一执行器:成功路径、来源提升与异常到信封的映射。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

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


# ---------------------------------------------------------------------------
# issue #517:run_tool 唯一漏斗的作用域强制
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_scope_policy() -> Iterator[None]:
    from finboard_mcp.scopes import reset_scope_policy

    reset_scope_policy()
    yield
    reset_scope_policy()


def _spy() -> tuple[Callable[[], Awaitable[Any]], dict[str, bool]]:
    hit = {"called": False}

    async def handler() -> dict[str, bool]:
        hit["called"] = True
        return {"ok": True}

    return handler, hit


def _identity(*scopes: str):
    from finboard_mcp.identity import McpClientIdentity

    return McpClientIdentity(
        client_id="MC-test", name="pytest", scopes=frozenset(scopes)
    )


class TestScopePolicySelfCheck:
    def test_unarmed_raises(self) -> None:
        from finboard_mcp.scopes import (
            require_scope_enforcement_armed,
            reset_scope_policy,
        )

        reset_scope_policy()
        with pytest.raises(RuntimeError, match="未装配"):
            require_scope_enforcement_armed()

    def test_armed_passes(self) -> None:
        from finboard_mcp.scopes import (
            arm_scope_enforcement,
            require_scope_enforcement_armed,
        )

        arm_scope_enforcement(readonly_only=False)
        require_scope_enforcement_armed()  # 不抛异常


class TestScopeEnforcement:
    async def test_missing_identity_denied_when_enforced(self) -> None:
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=False)
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.list",
            arguments={},
            handler=handler,
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"
        assert hit["called"] is False

    async def test_read_scope_allows_read_tool(self) -> None:
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=False)
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.list",
            arguments={},
            handler=handler,
            identity=_identity("research.read"),
        )
        assert env.status == "ok"
        assert hit["called"] is True

    async def test_readonly_identity_denied_on_write_tool(self) -> None:
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=False)
        handler, hit = _spy()
        audit = AuditRecorder()
        env = await run_tool(
            audit=audit,
            tool_name="finboard.run.queue",
            arguments={"strategy_id": "x"},
            handler=handler,
            identity=_identity("research.read"),
        )
        assert env.status == "denied"
        assert hit["called"] is False
        # 越权也进审计,且 caller 是客户端 ID。
        assert audit.records[0].status == "denied"
        assert audit.records[0].caller == "MC-test"

    async def test_wildcard_identity_allows_write(self) -> None:
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=False)
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.queue",
            arguments={},
            handler=handler,
            identity=_identity("*"),
        )
        assert env.status == "ok"
        assert hit["called"] is True

    async def test_unknown_tool_entry_fails_closed_even_for_wildcard(self) -> None:
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=False)
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.unregistered_tool",
            arguments={},
            handler=handler,
            identity=_identity("*"),
        )
        assert env.status == "denied"
        assert hit["called"] is False

    async def test_readonly_only_flag_denies_writes_for_wildcard(self) -> None:
        """FINBOARD_MCP_READONLY_ONLY 兼容期:全权身份也只能读。"""
        from finboard_mcp.scopes import arm_scope_enforcement

        arm_scope_enforcement(readonly_only=True)
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.queue",
            arguments={},
            handler=handler,
            identity=_identity("*"),
        )
        assert env.status == "denied"
        assert hit["called"] is False

        read_handler, read_hit = _spy()
        read_env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.list",
            arguments={},
            handler=read_handler,
            identity=_identity("*"),
        )
        assert read_env.status == "ok"
        assert read_hit["called"] is True

    async def test_unarmed_policy_keeps_direct_helper_behavior(self) -> None:
        """未装配策略(直接调用 helper 的单元测试)保持旧行为。"""
        handler, hit = _spy()
        env = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.run.queue",
            arguments={},
            handler=handler,
        )
        assert env.status == "ok"
        assert hit["called"] is True
