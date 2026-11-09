"""MCP 工具层的研究审计适配(issue #518)。

工具层只做两件事:

* 用**服务端身份**构造 :class:`ResearchOperationContext`(客户端 ID 来自
  #517 的凭据解析,``operation_id`` 可选);
* 成功写走 ``record``(与业务写入同 session,不 commit);拒绝/失败走
  ``record_detached``(独立会话,绝不掩盖原始错误)。

真正的业务字段(操作名、实体、关联 ID)由各工具声明 —— 本模块不猜测。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from finboard_app.research_audit import ResearchAuditEvent, research_audit_recorder
from finboard_app.research_operation import ResearchOperationContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finboard_mcp.context import McpAppContext

AuditOutcome = Literal["succeeded", "failed", "rejected", "denied"]


def mcp_context(
    app: McpAppContext, *, operation_id: str | None = None
) -> ResearchOperationContext:
    """当前工具调用的服务端上下文(``source=mcp``,``actor=agent:<client>``)。"""

    identity = getattr(app, "identity", None)
    return ResearchOperationContext.for_mcp(
        client_id=identity.client_id if identity is not None else None,
        operation_id=operation_id,
    )


async def record_success(
    session: AsyncSession,
    *,
    app: McpAppContext,
    operation: str,
    params_summary: Mapping[str, Any],
    correlation: Mapping[str, str] | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    result_code: str | None = None,
    idempotency_key: str | None = None,
    outcome: AuditOutcome = "succeeded",
) -> None:
    """在调用方 session 内记录成功审计(不 commit,失败让业务回滚)。

    ``outcome`` 默认 ``succeeded``;调用方语义是「请求已完成但未产生动作」
    (如对终态任务取消)时显式传 ``rejected``。
    """

    await research_audit_recorder().record(
        session,
        ResearchAuditEvent(
            operation=operation,
            outcome=outcome,
            context=mcp_context(app),
            entity_type=entity_type,
            entity_id=entity_id,
            correlation=dict(correlation or {}),
            result_code=result_code,
            params_summary=dict(params_summary),
            idempotency_key=idempotency_key,
        ),
    )


async def record_failure(
    app: McpAppContext,
    *,
    operation: str,
    params_summary: Mapping[str, Any],
    error: BaseException,
    outcome: AuditOutcome = "rejected",
    result_code: str | None = None,
    correlation: Mapping[str, str] | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """独立会话补记拒绝/失败;自身失败只记日志(recorder 内部吸收)。"""

    await research_audit_recorder().record_detached(
        ResearchAuditEvent(
            operation=operation,
            outcome=outcome,
            context=mcp_context(app),
            entity_type=entity_type,
            entity_id=entity_id,
            correlation=dict(correlation or {}),
            result_code=result_code or type(error).__name__,
            params_summary=dict(params_summary),
            error_summary=str(error),
            idempotency_key=idempotency_key,
        )
    )


__all__ = ["AuditOutcome", "mcp_context", "record_failure", "record_success"]
