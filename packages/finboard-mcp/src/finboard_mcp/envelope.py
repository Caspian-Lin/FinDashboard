"""MCP 工具统一响应信封与错误契约。

所有 FinBoard MCP 工具返回 :class:`ToolEnvelope`,保证调用方拿到统一的:

* ``operation_id`` —— 每次调用的唯一操作 ID,便于审计与排查;
* ``status`` —— ``ok`` / ``denied`` / ``error`` / ``pending_approval``;
* ``data`` —— 成功时的结构化产物;
* ``error`` —— 失败时的结构化错误(``kind`` + 是否可重试);
* ``provenance`` —— 来源元数据(对 AI 工具,来自 ``Provenance``);
* ``idempotency_key`` —— 写操作的幂等键(冲突检测用);
* ``message`` —— 人类可读说明。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

ToolStatus = Literal["ok", "denied", "error", "pending_approval"]

ErrorKind = Literal[
    "permission_denied",
    "not_found",
    "timeout",
    "conflict",
    "degraded",
    "invalid_argument",
    "cancelled",
    "unavailable",
]


@dataclass(frozen=True)
class ToolError:
    """结构化错误。

    ``kind`` 是机器可读的错误分类,``retryable`` 提示调用方是否可重试。
    """

    kind: ErrorKind
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class ToolEnvelope:
    """工具统一响应信封。"""

    operation_id: str
    status: ToolStatus
    data: Any = None
    error: ToolError | None = None
    provenance: Mapping[str, Any] | None = None
    idempotency_key: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ToolData:
    """工具 handler 的结构化返回 —— 数据 + 可选来源元数据。

    handler 既可以返回裸数据(等价于 ``ToolData(data=...)``),也可以返回
    带来源的 :class:`ToolData`,由 :func:`run_tool` 提升到信封的 provenance 层。
    """

    data: Any = None
    provenance: Mapping[str, Any] | None = None


def new_operation_id() -> str:
    """生成全局唯一操作 ID(前缀 ``OP-``,便于在审计日志中检索)。"""
    return f"OP-{uuid.uuid4().hex[:12]}"


def ok(
    data: Any = None,
    *,
    message: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    operation_id: str | None = None,
) -> ToolEnvelope:
    return ToolEnvelope(
        operation_id=operation_id or new_operation_id(),
        status="ok",
        data=data,
        message=message,
        provenance=provenance,
        idempotency_key=idempotency_key,
    )


def denied(
    kind: ErrorKind,
    message: str,
    *,
    operation_id: str | None = None,
) -> ToolEnvelope:
    return ToolEnvelope(
        operation_id=operation_id or new_operation_id(),
        status="denied",
        error=ToolError(kind=kind, message=message, retryable=False),
        message=message,
    )


def error(
    kind: ErrorKind,
    message: str,
    *,
    retryable: bool = False,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> ToolEnvelope:
    return ToolEnvelope(
        operation_id=operation_id or new_operation_id(),
        status="error",
        error=ToolError(kind=kind, message=message, retryable=retryable),
        message=message,
        idempotency_key=idempotency_key,
    )


def pending_approval(
    approval_ref: str,
    *,
    message: str,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> ToolEnvelope:
    """写操作审批门:工具不直接执行,返回待审批目标供人工确认。"""
    return ToolEnvelope(
        operation_id=operation_id or new_operation_id(),
        status="pending_approval",
        data={"approval_ref": approval_ref, "requires_human_confirmation": True},
        message=message,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "ErrorKind",
    "ToolData",
    "ToolEnvelope",
    "ToolError",
    "denied",
    "error",
    "new_operation_id",
    "ok",
    "pending_approval",
]
