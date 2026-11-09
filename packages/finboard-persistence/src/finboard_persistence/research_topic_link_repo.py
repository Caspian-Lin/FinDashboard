"""Immutable topic -> research artifact links (#515).

The repository owns only the append-only link ledger. It never writes run or job
state: live status is derived by joining ``research_runs`` / ``background_jobs``
in :meth:`ResearchTopicLinkRepository.list_runs_for_topic` (see the #515 plan
§6.1). Insert conflicts are surfaced as a persistence error and mapped to the
unified research conflict family by the application service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    BackgroundJobModel,
    ResearchRunModel,
    ResearchTopicLinkModel,
    ResearchTopicModel,
)


class ResearchTopicLinkConflictError(RuntimeError):
    """A link uniqueness rule decided against the writer (operation/topic)."""


@dataclass(slots=True)
class TopicRunJoin:
    """A link joined with its run and background job (read-side projection)."""

    link: ResearchTopicLinkModel
    run: ResearchRunModel | None
    job: BackgroundJobModel | None


@dataclass(slots=True)
class RunTopicJoin:
    """A link joined with the topic that references the run (reverse lookup)."""

    link: ResearchTopicLinkModel
    topic: ResearchTopicModel


class ResearchTopicLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_operation_id(
        self, operation_id: str
    ) -> ResearchTopicLinkModel | None:
        stmt = select(ResearchTopicLinkModel).where(
            ResearchTopicLinkModel.operation_id == operation_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get(
        self, topic_id: str, entity_type: str, entity_id: str
    ) -> ResearchTopicLinkModel | None:
        stmt = select(ResearchTopicLinkModel).where(
            ResearchTopicLinkModel.topic_id == topic_id,
            ResearchTopicLinkModel.entity_type == entity_type,
            ResearchTopicLinkModel.entity_id == entity_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_or_get(
        self,
        *,
        topic_id: str,
        goal_version: str,
        entity_type: str,
        entity_id: str,
        created_by: str,
        job_id: str | None = None,
        operation_id: str | None = None,
        entry_id: str | None = None,
    ) -> tuple[ResearchTopicLinkModel, bool]:
        """Return ``(link, created)``; existing links are immutable and reused.

        ``operation_id`` binds a request to its original link even when a client
        lost the response and generated a different run idempotency key. A link
        already present for ``(topic, entity_type, entity_id)`` wins over a new
        operation id: the reference is the fact, the request key is metadata.
        """

        if operation_id is not None:
            bound = await self.get_by_operation_id(operation_id)
            if bound is not None:
                if (
                    bound.topic_id != topic_id
                    or bound.entity_type != entity_type
                    or bound.entity_id != entity_id
                ):
                    raise ResearchTopicLinkConflictError(
                        "operation_id 已绑定其他课题或产物: "
                        f"{bound.topic_id} / {bound.entity_type} / {bound.entity_id}"
                    )
                return bound, False
        existing = await self.get(topic_id, entity_type, entity_id)
        if existing is not None:
            return existing, False
        row = ResearchTopicLinkModel(
            topic_id=topic_id,
            goal_version=goal_version,
            entry_id=entry_id,
            entity_type=entity_type,
            entity_id=entity_id,
            job_id=job_id,
            operation_id=operation_id,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ResearchTopicLinkConflictError(
                "课题链接唯一约束冲突(operation_id 或 topic/entity 已存在)"
            ) from exc
        return row, True

    async def list_runs_for_topic(
        self,
        topic_id: str,
        *,
        limit: int,
        cursor: int | None = None,
    ) -> list[TopicRunJoin]:
        """Links for ``topic_id`` joined with run/job, newest first.

        At most ``limit + 1`` rows are returned so the caller can derive
        ``has_more``; the link table stays the only authority for membership.
        """

        stmt = (
            select(ResearchTopicLinkModel, ResearchRunModel, BackgroundJobModel)
            .outerjoin(
                ResearchRunModel,
                and_(
                    ResearchRunModel.run_id == ResearchTopicLinkModel.entity_id,
                    ResearchTopicLinkModel.entity_type == "research_run",
                ),
            )
            .outerjoin(
                BackgroundJobModel,
                BackgroundJobModel.job_id == ResearchTopicLinkModel.job_id,
            )
            .where(
                ResearchTopicLinkModel.topic_id == topic_id,
                ResearchTopicLinkModel.entity_type == "research_run",
            )
            .order_by(ResearchTopicLinkModel.id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            stmt = stmt.where(ResearchTopicLinkModel.id < cursor)
        stmt = stmt.execution_options(populate_existing=True)
        rows = (await self.session.execute(stmt)).all()
        return [
            TopicRunJoin(link=link, run=run, job=job) for link, run, job in rows
        ]

    async def get_run_link(
        self, topic_id: str, run_id: str
    ) -> TopicRunJoin | None:
        stmt = (
            select(ResearchTopicLinkModel, ResearchRunModel, BackgroundJobModel)
            .outerjoin(
                ResearchRunModel,
                ResearchRunModel.run_id == ResearchTopicLinkModel.entity_id,
            )
            .outerjoin(
                BackgroundJobModel,
                BackgroundJobModel.job_id == ResearchTopicLinkModel.job_id,
            )
            .where(
                ResearchTopicLinkModel.topic_id == topic_id,
                ResearchTopicLinkModel.entity_type == "research_run",
                ResearchTopicLinkModel.entity_id == run_id,
            )
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        link, run, job = row
        return TopicRunJoin(link=link, run=run, job=job)

    async def list_topics_for_run(
        self, run_id: str, *, limit: int = 50
    ) -> list[RunTopicJoin]:
        stmt = (
            select(ResearchTopicLinkModel, ResearchTopicModel)
            .outerjoin(
                ResearchTopicModel,
                ResearchTopicModel.topic_id == ResearchTopicLinkModel.topic_id,
            )
            .where(
                ResearchTopicLinkModel.entity_type == "research_run",
                ResearchTopicLinkModel.entity_id == run_id,
            )
            .order_by(ResearchTopicLinkModel.id.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            RunTopicJoin(link=link, topic=topic)
            for link, topic in rows
            if topic is not None
        ]


__all__ = [
    "ResearchTopicLinkConflictError",
    "ResearchTopicLinkRepository",
    "RunTopicJoin",
    "TopicRunJoin",
]
