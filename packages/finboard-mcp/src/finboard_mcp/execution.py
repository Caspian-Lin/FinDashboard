"""MCP 工具统一执行器。

每个工具函数的函数体只关心「做什么」(返回 data 或抛异常),横切关注点 ——
计时、审计、脱敏摘要、异常到信封的映射 —— 统一由 :func:`run_tool` 处理。

异常映射策略:

* :class:`McpToolError` —— 工具自行精确声明的业务错误,直接采用其字段;
* :class:`asyncio.TimeoutError` → ``error(timeout, retryable=True)``;
* :class:`LookupError` / :class:`KeyError` → ``error(not_found)``;
* :class:`ValueError` → ``error(invalid_argument)``;
* 其它异常 → ``error(unavailable)``,避免向调用方泄露内部栈。
"""

from __future__ import annotations

import asyncio
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

Handler = Callable[[], Awaitable[Any]]


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
) -> ToolEnvelope:
    """执行一个工具,统一审计与异常映射,返回 :class:`ToolEnvelope`。"""
    operation_id = new_operation_id()
    summary = summarize_arguments(arguments, sensitive=sensitive)
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
                caller=caller,
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
            caller=caller,
            recorded_at=now_iso(),
        )
    )
    return ok(
        data,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        provenance=provenance,
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
