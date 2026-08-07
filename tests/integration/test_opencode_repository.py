"""AgentConversationRepository / AgentEventRepository 集成测试(issue #109)。

需要 PostgreSQL(CI 提供)。验证 ORM mapping、CRUD、状态/游标更新。
不写入实盘 orders/fills/positions。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_opencode.repository import (
    AgentConversationRepository,
    AgentEventRepository,
)
from finboard_opencode.schemas import (
    AgentEvent,
    ConversationRecord,
    ConversationStatus,
)

pytestmark = [pytest.mark.integration]


def _record(
    *,
    conversation_id: str = "CONV-test-1",
    status: ConversationStatus = ConversationStatus.ACTIVE,
    last_event_seq: int = 0,
) -> ConversationRecord:
    now = datetime.now(UTC)
    return ConversationRecord(
        conversation_id=conversation_id,
        opencode_session_id=f"sess-{conversation_id}",
        agent_run_id=None,
        title="测试会话",
        status=status,
        agent_name="finboard-researcher",
        model_ref=None,
        last_event_seq=last_event_seq,
        created_at=now,
        updated_at=now,
    )


async def test_conversation_save_and_get(db_session: AsyncSession) -> None:
    repo = AgentConversationRepository(db_session)
    await repo.save(_record())
    fetched = await repo.get("CONV-test-1")

    assert fetched is not None
    assert fetched.opencode_session_id == "sess-CONV-test-1"
    assert fetched.status is ConversationStatus.ACTIVE
    assert fetched.title == "测试会话"


async def test_conversation_get_by_session(db_session: AsyncSession) -> None:
    repo = AgentConversationRepository(db_session)
    await repo.save(_record())
    fetched = await repo.get_by_session("sess-CONV-test-1")

    assert fetched is not None
    assert fetched.conversation_id == "CONV-test-1"


async def test_update_status(db_session: AsyncSession) -> None:
    repo = AgentConversationRepository(db_session)
    await repo.save(_record())
    await repo.update_status("CONV-test-1", ConversationStatus.INTERRUPTED)

    fetched = await repo.get("CONV-test-1")
    assert fetched is not None
    assert fetched.status is ConversationStatus.INTERRUPTED


async def test_update_event_seq_monotonic(db_session: AsyncSession) -> None:
    repo = AgentConversationRepository(db_session)
    await repo.save(_record(last_event_seq=5))

    await repo.update_event_seq("CONV-test-1", 3)  # 更小,不更新
    fetched = await repo.get("CONV-test-1")
    assert fetched is not None
    assert fetched.last_event_seq == 5

    await repo.update_event_seq("CONV-test-1", 10)
    fetched = await repo.get("CONV-test-1")
    assert fetched is not None
    assert fetched.last_event_seq == 10


async def test_list_by_status(db_session: AsyncSession) -> None:
    repo = AgentConversationRepository(db_session)
    await repo.save(_record(conversation_id="CONV-a", status=ConversationStatus.ACTIVE))
    await repo.save(
        _record(conversation_id="CONV-b", status=ConversationStatus.INTERRUPTED)
    )

    active = await repo.list_by_status(ConversationStatus.ACTIVE)
    assert len(active) == 1
    assert active[0].conversation_id == "CONV-a"


async def test_event_append_and_list(db_session: AsyncSession) -> None:
    conv_repo = AgentConversationRepository(db_session)
    event_repo = AgentEventRepository(db_session)
    await conv_repo.save(_record())

    await event_repo.append(
        AgentEvent(
            conversation_id="CONV-test-1",
            event_seq=1,
            event_type="tool.call",
            role="assistant",
            payload={"name": "finboard.run.list"},
            timestamp=datetime.now(UTC),
        )
    )
    await event_repo.append(
        AgentEvent(
            conversation_id="CONV-test-1",
            event_seq=2,
            event_type="message.completed",
            role="assistant",
            payload={"text": "done"},
            timestamp=datetime.now(UTC),
        )
    )

    all_events = await event_repo.list_by_conversation("CONV-test-1")
    assert len(all_events) == 2

    after_one = await event_repo.list_by_conversation("CONV-test-1", after_seq=1)
    assert len(after_one) == 1
    assert after_one[0].event_type == "message.completed"


async def test_event_unique_constraint(db_session: AsyncSession) -> None:
    """同一 conversation + event_seq 唯一约束。"""
    conv_repo = AgentConversationRepository(db_session)
    event_repo = AgentEventRepository(db_session)
    await conv_repo.save(_record())

    await event_repo.append(
        AgentEvent(
            conversation_id="CONV-test-1",
            event_seq=1,
            event_type="message",
            role=None,
            payload={},
            timestamp=datetime.now(UTC),
        )
    )
    await db_session.flush()

    # 重复 seq 应触发唯一约束(repository.append 内部 flush 时抛出)
    event_repo_duplicate = AgentEventRepository(db_session)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await event_repo_duplicate.append(
            AgentEvent(
                conversation_id="CONV-test-1",
                event_seq=1,
                event_type="message",
                role=None,
                payload={},
                timestamp=datetime.now(UTC),
            )
        )
