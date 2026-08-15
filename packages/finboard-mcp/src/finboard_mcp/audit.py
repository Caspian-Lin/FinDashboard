"""MCP 工具调用审计。

每次工具调用都记录一条 :class:`AuditRecord`,通过 structlog 以结构化日志输出
(便于日志聚合系统采集),并在内存保留副本供测试断言。

持久化(issue #157,``mcp_audit_persist`` 接线):``FINBOARD_MCP_AUDIT_PERSIST=true``
时,``AuditRecorder`` 额外把记录追加到独立的 ``mcp_audit_events`` 表(只追加,
重启不丢);默认关闭。持久化失败只记 warning,绝不影响工具调用本身。

安全要求(详见 ``AGENTS.md``):

* 不记录 API Key / 原始凭证 / 未脱敏思考内容;
* 入参进入审计前必须经 :func:`summarize_arguments` 脱敏与截断。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from finboard_backtest.factor_research.sanitizer import sanitize_prompt

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MAX_ARG_LEN = 120


def summarize_arguments(
    arguments: Mapping[str, Any],
    *,
    sensitive: tuple[str, ...] = (),
) -> dict[str, Any]:
    """把入参脱敏、截断后作为审计摘要。

    * 所有值先经 :func:`sanitize_prompt` 抹掉 ``sk-*`` / 密码 / token 等凭证;
    * 敏感参数(如 ``prompt``)或超长值截断到 ``_MAX_ARG_LEN``;
    * ``None`` 值跳过,避免审计噪声。
    """
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        text = sanitize_prompt(str(value))
        if key in sensitive or len(text) > _MAX_ARG_LEN:
            text = text[:_MAX_ARG_LEN]
        summary[key] = text
    return summary


@dataclass(frozen=True)
class AuditRecord:
    """单次工具调用的审计记录。"""

    operation_id: str
    tool_name: str
    arguments_summary: dict[str, Any]
    status: str
    latency_ms: int
    error_kind: str | None
    caller: str | None
    recorded_at: str


class AuditRecorder:
    """审计记录器 —— 内存副本 + structlog 结构化输出 + 可选 DB 持久化。

    ``mcp_audit_persist=true`` 时传入 ``session_maker``,每条记录追加到
    ``mcp_audit_events`` 表(独立事务);持久化失败只记 warning,不影响工具调用。
    不复用 ``ai_audit_events``(该表 event_type 受 #84 的 AuditEventType 枚举约束,
    MCP 工具调用不属于 AI 假设审批域)。
    """

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._records: list[AuditRecord] = []
        self._session_maker = session_maker
        self._log = structlog.get_logger("finboard.mcp.audit")

    async def record(self, record: AuditRecord) -> None:
        self._records.append(record)
        self._log.info(
            "mcp.tool_called",
            operation_id=record.operation_id,
            tool=record.tool_name,
            status=record.status,
            latency_ms=record.latency_ms,
            error_kind=record.error_kind,
            caller=record.caller,
            arguments=record.arguments_summary,
        )
        session_maker = self._session_maker
        if session_maker is not None:
            await self._persist(record, session_maker)

    async def _persist(
        self,
        record: AuditRecord,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """把记录追加到 ``mcp_audit_events``(独立 session + commit;失败降级)。"""
        from finboard_persistence import McpAuditRepository

        try:
            async with session_maker() as session:
                repo = McpAuditRepository(session)
                await repo.append(
                    operation_id=record.operation_id,
                    tool_name=record.tool_name,
                    status=record.status,
                    latency_ms=record.latency_ms,
                    error_kind=record.error_kind,
                    caller=record.caller,
                    arguments_summary=dict(record.arguments_summary),
                    recorded_at=datetime.fromisoformat(record.recorded_at),
                )
                await session.commit()
        except Exception as exc:  # 持久化失败绝不影响工具调用
            self._log.warning(
                "mcp.audit_persist_failed",
                operation_id=record.operation_id,
                tool=record.tool_name,
                error=str(exc)[:200],
            )

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def reset(self) -> None:
        self._records.clear()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AuditRecord",
    "AuditRecorder",
    "now_iso",
    "summarize_arguments",
]
