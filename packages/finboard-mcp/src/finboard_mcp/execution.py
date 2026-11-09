"""MCP 工具统一执行器。

每个工具函数的函数体只关心「做什么」(返回 data 或抛异常),横切关注点 ——
作用域强制(#517)、计时、审计、脱敏摘要、异常到信封的映射 —— 统一由
:func:`run_tool` 处理。

作用域强制(issue #517):

* 身份来自显式 ``identity`` 参数 → ``contextvars``(工具入口
  ``app_context(ctx)`` 写入)→ 进程级 stdio 默认身份;
* ``TOOL_SCOPES`` 查不到工具条目 = 契约错误 = **拒绝**(fail-closed),不会
  「未声明即放行」;``"*"`` 仅由旧 ``FINBOARD_MCP_AUTH_TOKEN`` 合成;
* ``FINBOARD_MCP_READONLY_ONLY`` 兼容期:任何需要写作用域的工具一律拒绝;
* 只有 server lifespan / ``__main__`` 装配了策略后才强制。**未装配的进程内
  调用不设作用域边界,仅供测试直接调用 helper**;生产入口(stdio / HTTP /
  内嵌)启动时装配并在装配失败时具名退出。

异常映射策略:

* :class:`McpToolError` —— 工具自行精确声明的业务错误,直接采用其字段;
* :class:`asyncio.TimeoutError` → ``error(timeout, retryable=True)``;
* :class:`LookupError` / :class:`KeyError` → ``error(not_found)``;
* :class:`ValueError` → ``error(invalid_argument)``;
* 其它异常 → ``error(unavailable)``,避免向调用方泄露内部栈。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from finboard_mcp.audit import AuditRecord, AuditRecorder, now_iso, summarize_arguments
from finboard_mcp.envelope import (
    ErrorKind,
    ToolData,
    ToolEnvelope,
    denied,
    error,
    new_operation_id,
    ok,
)
from finboard_mcp.identity import McpClientIdentity, current_identity
from finboard_mcp.scopes import (
    identity_has_scopes,
    required_scopes_for,
    scope_policy,
)

Handler = Callable[[], Awaitable[Any]]

_logger = logging.getLogger("finboard.mcp.execution")


class McpToolError(Exception):
    """工具主动声明的业务错误,携带结构化分类与可重试标志。"""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


async def run_tool(
    *,
    audit: AuditRecorder,
    tool_name: str,
    arguments: dict[str, Any],
    handler: Handler,
    sensitive: tuple[str, ...] = (),
    caller: str | None = None,
    idempotency_key: str | None = None,
    identity: McpClientIdentity | None = None,
) -> ToolEnvelope:
    """执行一个工具,统一作用域强制 / 审计与异常映射,返回 :class:`ToolEnvelope`。"""
    operation_id = new_operation_id()
    summary = summarize_arguments(arguments, sensitive=sensitive)
    policy = scope_policy()
    effective_identity = identity or current_identity() or policy.default_identity
    audit_caller = caller or (
        effective_identity.client_id if effective_identity is not None else None
    )

    if policy.enforce:
        required = required_scopes_for(tool_name)
        if required is None:
            # 新增工具未登记 TOOL_SCOPES:契约错误,拒绝而不是放行。
            _logger.error(
                "mcp.scope_contract_missing tool_name=%s detail=%s",
                tool_name,
                "tool 未在 finboard_mcp.scopes.TOOL_SCOPES 登记,按拒绝处理",
            )
            await _record_denied(
                audit,
                operation_id=operation_id,
                tool_name=tool_name,
                summary=summary,
                caller=audit_caller,
                message="工具未登记作用域,已按默认拒绝处理(契约错误)",
            )
            return denied(
                "permission_denied",
                "工具未登记作用域,已按默认拒绝处理(契约错误)",
                operation_id=operation_id,
            )
        if policy.readonly_only:
            # 兼容期开关:无论身份是否全权,写作用域工具一律拒绝。
            permitted = required <= frozenset({"research.read"})
        else:
            permitted = identity_has_scopes(effective_identity, required)
        if not permitted:
            if policy.readonly_only:
                message = (
                    "MCP 处于只读兼容模式(FINBOARD_MCP_READONLY_ONLY),写作用域工具被拒绝"
                )
            else:
                granted = sorted(effective_identity.scopes) if effective_identity else []
                message = (
                    f"客户端缺少所需作用域:{', '.join(sorted(required))}"
                    f"(已授予:{', '.join(granted) or '无'})"
                )
            await _record_denied(
                audit,
                operation_id=operation_id,
                tool_name=tool_name,
                summary=summary,
                caller=audit_caller,
                message=message,
            )
            return denied("permission_denied", message, operation_id=operation_id)

    started = time.monotonic()
    try:
        result = await handler()
    except BaseException as exc:
        status, kind, message, retryable = _map_exception(exc)
        latency_ms = int((time.monotonic() - started) * 1000)
        await audit.record(
            AuditRecord(
                operation_id=operation_id,
                tool_name=tool_name,
                arguments_summary=summary,
                status=status,
                latency_ms=latency_ms,
                error_kind=kind,
                caller=audit_caller,
                recorded_at=now_iso(),
            )
        )
        if status == "denied":
            return denied(kind, message, operation_id=operation_id)
        return error(
            kind,
            message,
            retryable=retryable,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

    provenance, data = _unpack(result)
    latency_ms = int((time.monotonic() - started) * 1000)
    await audit.record(
        AuditRecord(
            operation_id=operation_id,
            tool_name=tool_name,
            arguments_summary=summary,
            status="ok",
            latency_ms=latency_ms,
            error_kind=None,
            caller=audit_caller,
            recorded_at=now_iso(),
        )
    )
    return ok(
        data,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        provenance=provenance,
    )


async def _record_denied(
    audit: AuditRecorder,
    *,
    operation_id: str,
    tool_name: str,
    summary: dict[str, Any],
    caller: str | None,
    message: str,
) -> None:
    """作用域拒绝也进审计(与 handler 内 permission_denied 同一信封语义)。"""
    await audit.record(
        AuditRecord(
            operation_id=operation_id,
            tool_name=tool_name,
            arguments_summary=summary,
            status="denied",
            latency_ms=0,
            error_kind="permission_denied",
            caller=caller,
            recorded_at=now_iso(),
        )
    )
    _logger.warning(
        "mcp.scope_denied tool_name=%s caller=%s detail=%s", tool_name, caller, message
    )


def _unpack(result: Any) -> tuple[Mapping[str, Any] | None, Any]:
    """handler 返回 :class:`ToolData` 时拆出来源,裸数据则直接返回。"""
    if isinstance(result, ToolData):
        return result.provenance, result.data
    return None, result


def _map_exception(exc: BaseException) -> tuple[str, ErrorKind, str, bool]:
    """把异常映射为 ``(status, kind, message, retryable)``。"""
    if isinstance(exc, McpToolError):
        status = "denied" if exc.kind == "permission_denied" else "error"
        return status, exc.kind, exc.message, exc.retryable
    if isinstance(exc, asyncio.TimeoutError):
        return "error", "timeout", "工具执行超时", True
    if isinstance(exc, LookupError | KeyError):
        return "error", "not_found", str(exc) or "未找到对应资源", False
    if isinstance(exc, ValueError):
        return "error", "invalid_argument", str(exc) or "参数非法", False
    return "error", "unavailable", f"工具不可用: {exc}", False


__all__ = ["McpToolError", "run_tool"]
