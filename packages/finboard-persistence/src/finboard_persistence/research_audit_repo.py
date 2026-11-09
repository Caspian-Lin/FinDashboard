"""研究操作审计事件仓储(issue #518,``research_audit_events`` 只追加)。

写入语义由 :mod:`finboard_app.research_audit` 的 recorder 决定:

* 成功路径 ``record(session, event)`` —— 与业务写入共用调用方 session,
  **本仓储不 commit**;审计插入失败会让整个业务事务回滚(issue #518 明确要求);
* 拒绝/失败路径 ``record_detached(event)`` —— recorder 用独立会话写入并 commit。

查询只返回脱敏摘要与 digest,不返回原始参数(原始参数从未入库)。
无外键、无级联;不触碰实盘 ``audit_logs``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchAuditEventModel

#: 单页上限(读 API 与 MCP 工具同源,避免无界扫描)。
MAX_AUDIT_PAGE = 200


class ResearchAuditRepository:
    """``research_audit_events`` 只追加仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        audit_id: str,
        occurred_at: datetime,
        actor: str,
        actor_type: str,
        source: str,
        operation: str,
        outcome: str,
        params_digest: str,
        client_id: str | None = None,
        topic_id: str | None = None,
        entry_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        result_code: str | None = None,
        correlation: Mapping[str, str] | None = None,
        params_summary: Mapping[str, Any] | None = None,
        error_summary: str | None = None,
        idempotency_key: str | None = None,
        trust: str = "server_observed",
    ) -> ResearchAuditEventModel:
        """插入一条审计行并 flush(不 commit,事务所有权归调用方)。"""

        model = ResearchAuditEventModel(
            audit_id=audit_id,
            occurred_at=occurred_at,
            actor=actor,
            actor_type=actor_type,
            source=source,
            client_id=client_id,
            topic_id=topic_id,
            entry_id=entry_id,
            operation=operation,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            result_code=result_code,
            correlation=dict(correlation or {}),
            params_summary=dict(params_summary or {}),
            params_digest=params_digest,
            error_summary=error_summary,
            idempotency_key=idempotency_key,
            trust=trust,
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def get(self, audit_id: str) -> ResearchAuditEventModel | None:
        return (
            await self._session.execute(
                select(ResearchAuditEventModel).where(
                    ResearchAuditEventModel.audit_id == audit_id
                )
            )
        ).scalar_one_or_none()

    async def find_client_event(
        self, *, client_id: str, client_event_id: str
    ) -> ResearchAuditEventModel | None:
        """按 ``(client_id, client_event_id)`` 查找已上报的客户端声明事件。

        ``client_event_id`` 落在 ``idempotency_key`` 列(数据字典见
        ``docs/research/audit-operations.md``),与部分唯一索引同源。
        """

        return (
            await self._session.execute(
                select(ResearchAuditEventModel).where(
                    ResearchAuditEventModel.client_id == client_id,
                    ResearchAuditEventModel.idempotency_key == client_event_id,
                    ResearchAuditEventModel.trust == "client_declared",
                )
            )
        ).scalar_one_or_none()

    async def list_events(
        self,
        *,
        actor: str | None = None,
        client_id: str | None = None,
        source: str | None = None,
        topic_id: str | None = None,
        entry_id: str | None = None,
        entity_id: str | None = None,
        operation: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: tuple[datetime, int] | None = None,
        limit: int = 100,
    ) -> list[ResearchAuditEventModel]:
        """按 ``(occurred_at, id)`` 倒序读取一页,返回 ``limit + 1`` 行内的一页。

        cursor 是上一页最后一行的 ``(occurred_at, id)``;调用方用
        ``limit`` 截断并自行判断 ``has_more``。
        """

        bounded = max(1, min(limit, MAX_AUDIT_PAGE))
        stmt = select(ResearchAuditEventModel).order_by(
            ResearchAuditEventModel.occurred_at.desc(),
            ResearchAuditEventModel.id.desc(),
        )
        if actor:
            stmt = stmt.where(ResearchAuditEventModel.actor == actor)
        if client_id:
            stmt = stmt.where(ResearchAuditEventModel.client_id == client_id)
        if source:
            stmt = stmt.where(ResearchAuditEventModel.source == source)
        if topic_id:
            stmt = stmt.where(ResearchAuditEventModel.topic_id == topic_id)
        if entry_id:
            stmt = stmt.where(ResearchAuditEventModel.entry_id == entry_id)
        if entity_id:
            stmt = stmt.where(ResearchAuditEventModel.entity_id == entity_id)
        if operation:
            stmt = stmt.where(ResearchAuditEventModel.operation == operation)
        if outcome:
            stmt = stmt.where(ResearchAuditEventModel.outcome == outcome)
        if since is not None:
            stmt = stmt.where(ResearchAuditEventModel.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(ResearchAuditEventModel.occurred_at <= until)
        if cursor is not None:
            occurred_at, row_id = cursor
            stmt = stmt.where(
                or_(
                    ResearchAuditEventModel.occurred_at < occurred_at,
                    (
                        (ResearchAuditEventModel.occurred_at == occurred_at)
                        & (ResearchAuditEventModel.id < row_id)
                    ),
                )
            )
        stmt = stmt.limit(bounded + 1)
        return list((await self._session.execute(stmt)).scalars())

    async def coverage(self) -> dict[str, Any]:
        """历史覆盖声明:最早一条记录、按信任级别的最早记录与总数。

        迁移(2026-11-09)之前的写操作根本没有行,读取方必须显示
        「此前无审计记录」而非「无操作」。
        """

        total = int(
            (
                await self._session.execute(
                    select(func.count(ResearchAuditEventModel.id))
                )
            ).scalar_one()
        )
        earliest = await self._session.scalar(
            select(func.min(ResearchAuditEventModel.occurred_at))
        )
        latest = await self._session.scalar(
            select(func.max(ResearchAuditEventModel.occurred_at))
        )
        by_trust: dict[str, str | None] = {}
        for trust in ("server_observed", "client_declared"):
            by_trust[trust] = await self._session.scalar(
                select(func.min(ResearchAuditEventModel.occurred_at)).where(
                    ResearchAuditEventModel.trust == trust
                )
            )
        return {
            "from": earliest,
            "to": latest,
            "total": total,
            "server_observed_from": by_trust["server_observed"],
            "client_declared_from": by_trust["client_declared"],
        }

    async def purge_before(
        self, before: datetime, *, dry_run: bool = True
    ) -> int:
        """删除 ``occurred_at < before`` 的行(研究域专用手动保留策略)。

        v1 不自动清理:只有 ``finboard research audit purge`` 显式调用,
        ``dry_run=True`` 只统计条数。返回受影响行数。
        """

        condition = ResearchAuditEventModel.occurred_at < before
        if dry_run:
            result = await self._session.execute(
                select(func.count(ResearchAuditEventModel.id)).where(condition)
            )
            return int(result.scalar_one())
        deleted = await self._session.execute(
            delete(ResearchAuditEventModel).where(condition)
        )
        await self._session.flush()
        return int(getattr(deleted, "rowcount", 0) or 0)

    async def distinct_operations(self) -> Sequence[str]:
        """已出现过的操作名(运维排查用;有界读取不暴露给外部工具)。"""

        result = await self._session.execute(
            select(ResearchAuditEventModel.operation)
            .distinct()
            .order_by(ResearchAuditEventModel.operation)
        )
        return [str(value) for value in result.scalars()]


__all__ = ["MAX_AUDIT_PAGE", "ResearchAuditRepository"]
