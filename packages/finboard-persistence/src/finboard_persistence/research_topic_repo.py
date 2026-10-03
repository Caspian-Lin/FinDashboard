"""Append-only research rounds and versioned goals, no evidence mutations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchTopicEntryModel, ResearchTopicModel


def checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ResearchTopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, topic_id: str, *, lock: bool = False) -> ResearchTopicModel:
        stmt = select(ResearchTopicModel).where(ResearchTopicModel.topic_id == topic_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise LookupError(f"研究课题不存在: {topic_id}")
        return row

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

    async def create(self, payload: dict[str, Any], actor: str) -> ResearchTopicModel:
        row = ResearchTopicModel(
            topic_id=f"RT-{uuid.uuid4().hex[:24]}", payload=payload, created_by=actor, revision=1
        )
        self.session.add(row)
        await self.session.flush()
        await self.append(
            row.topic_id,
            "initial-goal",
            {"entry_type": "goal", "topic_revision": 1, "topic": payload},
            actor,
        )
        return row

    async def update(
        self, topic_id: str, payload: dict[str, Any], revision: int, actor: str
    ) -> ResearchTopicModel:
        row = await self.get(topic_id, lock=True)
        if row.revision != revision:
            raise ValueError("课题版本已变化,请重新加载后续接")
        old_goal = await self.goal(topic_id, payload["goal"]["version"])
        if old_goal is not None and old_goal != payload["goal"]:
            raise ValueError("目标内容变更须使用新目标版本,历史版本不可改写")
        row.revision += 1
        row.payload = payload
        await self.append(
            topic_id,
            f"goal-{row.revision}",
            {"entry_type": "goal", "topic_revision": row.revision, "topic": payload},
            actor,
        )
        await self.session.flush()
        return row

    async def append(
        self, topic_id: str, key: str, payload: dict[str, Any], actor: str
    ) -> ResearchTopicEntryModel:
        # Topic lock serializes same-key writers without aborting their transaction.
        await self.get(topic_id, lock=True)
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

    async def list_topics(self, limit: int, offset: int) -> list[ResearchTopicModel]:
        stmt = (
            select(ResearchTopicModel)
            .order_by(ResearchTopicModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
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
