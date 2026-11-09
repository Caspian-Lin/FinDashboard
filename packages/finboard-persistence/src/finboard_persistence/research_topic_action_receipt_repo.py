"""Per-request receipt ledger for topic actions (#516).

A topic link is unique on ``(topic, entity_type, entity_id)``, so the link row
cannot anchor a second action against the same artifact. This repository owns the
append-only ``research_topic_action_receipts`` ledger keyed by
``(topic_id, operation_id)``: the application service checks it **before**
executing an action and inserts it in the same transaction as the artifact/job,
the link and the audit event. A replayed operation therefore returns the stored
result instead of executing again (no duplicate trial, no extra budget, no
second sandbox job).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchTopicActionReceiptModel


class ResearchTopicActionReceiptConflictError(RuntimeError):
    """``(topic_id, operation_id)`` already exists (concurrent replay)."""


class ResearchTopicActionReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, topic_id: str, operation_id: str
    ) -> ResearchTopicActionReceiptModel | None:
        stmt = select(ResearchTopicActionReceiptModel).where(
            ResearchTopicActionReceiptModel.topic_id == topic_id,
            ResearchTopicActionReceiptModel.operation_id == operation_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        topic_id: str,
        operation_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        result_code: str,
        created_by: str,
        job_id: str | None = None,
        result_summary: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> ResearchTopicActionReceiptModel:
        row = ResearchTopicActionReceiptModel(
            topic_id=topic_id,
            operation_id=operation_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            job_id=job_id,
            result_code=result_code,
            result_summary=dict(result_summary or {}),
            created_by=created_by,
            created_at=created_at or datetime.now(UTC),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ResearchTopicActionReceiptConflictError(
                "operation_id 已绑定该课题的其他动作回执"
            ) from exc
        return row


__all__ = [
    "ResearchTopicActionReceiptConflictError",
    "ResearchTopicActionReceiptRepository",
]
