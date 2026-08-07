"""OpenCode 会话关联与事件持久化仓储(issue #109)。

Repository 模式与交易域一致:不控制事务边界,commit 由调用方决定。
不写入实盘 ``orders`` / ``fills`` / ``positions`` / ``audit_logs``。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_opencode.schemas import (
    AgentEvent,
    ConversationRecord,
    ConversationStatus,
)
from finboard_persistence.models import AgentConversationModel, AgentEventModel

# ---------------------------------------------------------------------------
# model <-> record
# ---------------------------------------------------------------------------


def _conversation_to_model(record: ConversationRecord) -> AgentConversationModel:
    return AgentConversationModel(
        conversation_id=record.conversation_id,
        opencode_session_id=record.opencode_session_id,
        agent_run_id=record.agent_run_id,
        title=record.title,
        status=record.status.value,
        agent_name=record.agent_name,
        model_ref=record.model_ref,
        last_event_seq=record.last_event_seq,
    )


def _model_to_conversation(model: AgentConversationModel) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=model.conversation_id,
        opencode_session_id=model.opencode_session_id,
        agent_run_id=model.agent_run_id,
        title=model.title,
        status=ConversationStatus(model.status),
        agent_name=model.agent_name,
        model_ref=model.model_ref,
        last_event_seq=model.last_event_seq,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _event_to_model(event: AgentEvent) -> AgentEventModel:
    return AgentEventModel(
        conversation_id=event.conversation_id,
        event_seq=event.event_seq,
        event_type=event.event_type,
        role=event.role,
        payload=event.payload,
        timestamp=event.timestamp,
    )


def _model_to_event(model: AgentEventModel) -> AgentEvent:
    return AgentEvent(
        conversation_id=model.conversation_id,
        event_seq=model.event_seq,
        event_type=model.event_type,
        role=model.role,
        payload=model.payload,
        timestamp=model.timestamp,
    )


# ---------------------------------------------------------------------------
# AgentConversationRepository
# ---------------------------------------------------------------------------


class AgentConversationRepository:
    """:class:`ConversationRecord` 持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, record: ConversationRecord) -> None:
        existing = await self._get_model(record.conversation_id)
        if existing is None:
            self._session.add(_conversation_to_model(record))
        else:
            existing.opencode_session_id = record.opencode_session_id
            existing.agent_run_id = record.agent_run_id
            existing.title = record.title
            existing.status = record.status.value
            existing.agent_name = record.agent_name
            existing.model_ref = record.model_ref
            existing.last_event_seq = record.last_event_seq
            existing.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        model = await self._get_model(conversation_id)
        return _model_to_conversation(model) if model else None

    async def get_by_session(self, opencode_session_id: str) -> ConversationRecord | None:
        stmt = select(AgentConversationModel).where(
            AgentConversationModel.opencode_session_id == opencode_session_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_conversation(model) if model else None

    async def list_recent(self, limit: int = 50) -> list[ConversationRecord]:
        stmt = (
            select(AgentConversationModel)
            .order_by(AgentConversationModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_model_to_conversation(m) for m in result.scalars()]

    async def list_by_status(self, status: ConversationStatus) -> list[ConversationRecord]:
        stmt = (
            select(AgentConversationModel)
            .where(AgentConversationModel.status == status.value)
            .order_by(AgentConversationModel.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return [_model_to_conversation(m) for m in result.scalars()]

    async def update_status(
        self, conversation_id: str, status: ConversationStatus
    ) -> None:
        model = await self._get_model(conversation_id)
        if model is None:
            return
        model.status = status.value
        model.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def update_event_seq(self, conversation_id: str, seq: int) -> None:
        model = await self._get_model(conversation_id)
        if model is None:
            return
        if seq > model.last_event_seq:
            model.last_event_seq = seq
            model.updated_at = datetime.now(UTC)
            await self._session.flush()

    async def _get_model(self, conversation_id: str) -> AgentConversationModel | None:
        stmt = select(AgentConversationModel).where(
            AgentConversationModel.conversation_id == conversation_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# AgentEventRepository
# ---------------------------------------------------------------------------


class AgentEventRepository:
    """OpenCode 会话关键事件持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AgentEvent) -> None:
        self._session.add(_event_to_model(event))
        await self._session.flush()

    async def append_many(self, events: list[AgentEvent]) -> None:
        for event in events:
            self._session.add(_event_to_model(event))
        if events:
            await self._session.flush()

    async def list_by_conversation(
        self, conversation_id: str, *, after_seq: int = 0
    ) -> list[AgentEvent]:
        stmt = (
            select(AgentEventModel)
            .where(
                AgentEventModel.conversation_id == conversation_id,
                AgentEventModel.event_seq > after_seq,
            )
            .order_by(AgentEventModel.event_seq.asc())
        )
        result = await self._session.execute(stmt)
        return [_model_to_event(m) for m in result.scalars()]
