"""Append-only research rounds and versioned goals, no evidence mutations."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchTopicEntryModel, ResearchTopicModel

TOPIC_STATUSES = ("draft", "active", "paused", "closed")


def checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop internal markers before embedding a topic snapshot in a public entry."""
    return {k: v for k, v in payload.items() if k != "created_as"}


class ResearchTopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self,
        topic_id: str,
        *,
        lock: bool = False,
        populate_existing: bool = False,
    ) -> ResearchTopicModel:
        stmt = select(ResearchTopicModel).where(ResearchTopicModel.topic_id == topic_id)
        # A row lock in a concurrent writer must refresh the identity map; callers
        # may force the same refresh without a lock (read-modify-write helpers).
        if populate_existing or lock:
            stmt = stmt.execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise LookupError(f"研究课题不存在: {topic_id}")
        return row

    async def find_by_idempotency_key(self, key: str) -> ResearchTopicModel | None:
        stmt = select(ResearchTopicModel).where(ResearchTopicModel.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def goal(self, topic_id: str, version: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT payload::jsonb->'topic'->'goal' FROM research_topic_entries WHERE topic_id=:topic_id AND payload::jsonb->>'entry_type'='goal' AND payload::jsonb->'topic'->'goal'->>'version'=:version ORDER BY id LIMIT 1"
                ),
                {"topic_id": topic_id, "version": version},
            )
        ).scalar_one_or_none()
        return row

    async def create(
        self,
        payload: dict[str, Any],
        actor: str,
        *,
        status: str = "active",
        idempotency_key: str | None = None,
    ) -> ResearchTopicModel:
        if status not in TOPIC_STATUSES:
            raise ValueError(f"未知课题状态: {status}")
        row = ResearchTopicModel(
            topic_id=f"RT-{uuid.uuid4().hex[:24]}",
            payload=payload,
            created_by=actor,
            revision=1,
            status=status,
            idempotency_key=idempotency_key,
        )
        self.session.add(row)
        await self.session.flush()
        await self.append(
            row.topic_id,
            "initial-draft" if status == "draft" else "initial-goal",
            {
                "entry_type": "draft" if status == "draft" else "goal",
                "topic_revision": 1,
                "topic": public_payload(payload),
            },
            actor,
        )
        return row

    async def update(
        self, topic_id: str, payload: dict[str, Any], revision: int, actor: str
    ) -> ResearchTopicModel:
        row = await self.get(topic_id, lock=True)
        if row.revision != revision:
            raise ValueError("课题版本已变化,请重新加载后续接")
        goal = payload.get("goal")
        old_goal = await self.goal(topic_id, goal["version"]) if goal else None
        if old_goal is not None and old_goal != goal:
            raise ValueError("目标内容变更须使用新目标版本,历史版本不可改写")
        # Status changes are lifecycle transitions only; PUT never writes the
        # column. Keep payload.status in sync with the authoritative column.
        payload = {**payload, "status": row.status}
        row.revision += 1
        row.payload = payload
        await self.append(
            topic_id,
            f"goal-{row.revision}",
            {"entry_type": "goal", "topic_revision": row.revision, "topic": public_payload(payload)},
            actor,
        )
        await self.session.flush()
        return row

    async def apply_transition(
        self,
        row: ResearchTopicModel,
        *,
        target: str,
        action: str,
        reason: str,
        actor: str,
        payload_updates: dict[str, Any] | None = None,
    ) -> ResearchTopicModel:
        """Bump the revision, write status to both places and append the audit entry."""
        if target not in TOPIC_STATUSES:
            raise ValueError(f"未知课题状态: {target}")
        source = row.status
        payload = dict(row.payload)
        if payload_updates:
            payload.update(payload_updates)
        payload["status"] = target
        row.revision += 1
        row.payload = payload
        row.status = target
        await self.append(
            row.topic_id,
            f"lifecycle-{action}-{row.revision}",
            {
                "entry_type": "lifecycle",
                "action": action,
                "from": source,
                "to": target,
                "reason": reason,
                "actor": actor,
                "at": utcnow().isoformat(),
                "topic_revision": row.revision,
            },
            actor,
        )
        await self.session.flush()
        return row

    async def append(
        self, topic_id: str, key: str, payload: dict[str, Any], actor: str
    ) -> ResearchTopicEntryModel:
        # Topic lock serializes same-key writers without aborting their transaction.
        topic = await self.get(topic_id, lock=True)
        topic.updated_at = utcnow()
        if (
            payload.get("entry_type") in {"round", "evidence"}
            and await self.goal(topic_id, payload["goal_version"]) is None
        ):
            raise ValueError("本轮目标版本须引用课题已有目标记录")
        stmt = select(ResearchTopicEntryModel).where(
            ResearchTopicEntryModel.topic_id == topic_id,
            ResearchTopicEntryModel.idempotency_key == key,
        )
        old = (await self.session.execute(stmt)).scalar_one_or_none()
        digest = checksum(payload)
        if old is not None:
            if old.checksum != digest:
                raise ValueError("同一幂等键对应不同轮次内容")
            return old
        row = ResearchTopicEntryModel(
            entry_id=f"RE-{uuid.uuid4().hex[:24]}",
            topic_id=topic_id,
            idempotency_key=key,
            payload=payload,
            checksum=digest,
            created_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_topics(
        self,
        limit: int,
        offset: int,
        *,
        status: str | None = None,
        sort: str = "updated",
    ) -> list[ResearchTopicModel]:
        stmt = select(ResearchTopicModel)
        if status:
            stmt = stmt.where(ResearchTopicModel.status == status)
        if sort == "created":
            stmt = stmt.order_by(ResearchTopicModel.id.desc())
        else:
            stmt = stmt.order_by(
                ResearchTopicModel.updated_at.desc(), ResearchTopicModel.id.desc()
            )
        stmt = stmt.offset(offset).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def entries(
        self, topic_id: str, limit: int, offset: int
    ) -> list[ResearchTopicEntryModel]:
        await self.get(topic_id)
        stmt = (
            select(ResearchTopicEntryModel)
            .where(ResearchTopicEntryModel.topic_id == topic_id)
            .order_by(ResearchTopicEntryModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def entry(self, topic_id: str, entry_id: str) -> ResearchTopicEntryModel | None:
        stmt = select(ResearchTopicEntryModel).where(
            ResearchTopicEntryModel.topic_id == topic_id,
            ResearchTopicEntryModel.entry_id == entry_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_lifecycle(self, topic_id: str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    text(
                        "SELECT entry_id, payload, checksum, created_by, created_at FROM research_topic_entries WHERE topic_id=:topic_id AND payload::jsonb->>'entry_type'='lifecycle' ORDER BY id DESC LIMIT 1"
                    ),
                    {"topic_id": topic_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    async def recent_rounds(self, topic_id: str, limit: int) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    text(
                        "SELECT entry_id, payload, checksum, created_by, created_at FROM research_topic_entries WHERE topic_id=:topic_id AND payload::jsonb->>'entry_type' IN ('round','evidence') ORDER BY id DESC LIMIT :limit"
                    ),
                    {"topic_id": topic_id, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    async def count_rounds(self, topic_id: str) -> int:
        value = (
            await self.session.execute(
                text(
                    "SELECT count(*) FROM research_topic_entries WHERE topic_id=:topic_id AND payload::jsonb->>'entry_type' IN ('round','evidence')"
                ),
                {"topic_id": topic_id},
            )
        ).scalar_one()
        return int(value)

    async def branches(self, topic_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    text(
                        "SELECT payload::jsonb->>'branch' AS branch, count(*) AS count,"
                        " max(created_at) AS last_at,"
                        " (array_agg(payload::jsonb->>'outcome' ORDER BY created_at DESC, id DESC))[1]"
                        " AS last_outcome"
                        " FROM research_topic_entries"
                        " WHERE topic_id=:topic_id"
                        " AND payload::jsonb->>'entry_type' IN ('round','evidence')"
                        " AND coalesce(payload::jsonb->>'branch','') <> ''"
                        " GROUP BY 1 ORDER BY max(created_at) DESC, 1 LIMIT :limit OFFSET :offset"
                    ),
                    {"topic_id": topic_id, "limit": limit + 1, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]
