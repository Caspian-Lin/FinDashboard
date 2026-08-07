"""ConversationService 单元测试(issue #109)。

用内存 fake runtime + fake repos 验证会话关联、SSE 事件投影与断线恢复逻辑。
不连接真实 OpenCode server / DB。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from finboard_opencode.conversation import ConversationService
from finboard_opencode.runtime import (
    OpenCodeRuntimeError,
    OpenCodeUnavailableError,
)
from finboard_opencode.schemas import (
    AgentEvent,
    ConversationRecord,
    ConversationStatus,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConvRepo:
    def __init__(self) -> None:
        self._store: dict[str, ConversationRecord] = {}

    async def save(self, record: ConversationRecord) -> None:
        self._store[record.conversation_id] = record

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        return self._store.get(conversation_id)

    async def list_recent(self, limit: int = 50) -> list[ConversationRecord]:
        items = sorted(self._store.values(), key=lambda r: r.created_at, reverse=True)
        return items[:limit]

    async def list_by_status(
        self, status: ConversationStatus
    ) -> list[ConversationRecord]:
        return [r for r in self._store.values() if r.status == status]

    async def update_status(
        self, conversation_id: str, status: ConversationStatus
    ) -> None:
        record = self._store.get(conversation_id)
        if record:
            self._store[conversation_id] = replace(record, status=status)

    async def update_event_seq(self, conversation_id: str, seq: int) -> None:
        record = self._store.get(conversation_id)
        if record and seq > record.last_event_seq:
            self._store[conversation_id] = replace(record, last_event_seq=seq)


class FakeEventRepo:
    def __init__(self) -> None:
        self._events: list[AgentEvent] = []

    async def append(self, event: AgentEvent) -> None:
        self._events.append(event)

    async def append_many(self, events: list[AgentEvent]) -> None:
        self._events.extend(events)

    async def list_by_conversation(
        self, conversation_id: str, *, after_seq: int = 0
    ) -> list[AgentEvent]:
        return [
            e
            for e in self._events
            if e.conversation_id == conversation_id and e.event_seq > after_seq
        ]


class FakeRuntime:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.interrupted: list[str] = []
        self.aborted: list[str] = []
        self._drop_on_subscribe = False

    async def create_session(
        self, *, agent=None, title=None, model=None
    ) -> dict[str, Any]:
        sid = f"sess-{len(self.sessions) + 1}"
        self.sessions[sid] = {"id": sid, "title": title or "", "model": model}
        return self.sessions[sid]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            raise OpenCodeRuntimeError("not found")
        return self.sessions[session_id]

    async def prompt(
        self, session_id, *, prompt_text, agent=None, model=None, wait=True
    ) -> dict[str, Any]:
        return {"id": "msg-1", "session_id": session_id}

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        return []

    async def interrupt(self, session_id: str) -> None:
        self.interrupted.append(session_id)

    async def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)

    async def subscribe_events(
        self, session_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        if self._drop_on_subscribe:
            raise OpenCodeUnavailableError("connection dropped")
        for ev in self.events.get(session_id, []):
            if ev.get("seq", 0) > after_seq:
                yield ev


def _make_service() -> tuple[ConversationService, FakeRuntime, FakeConvRepo, FakeEventRepo]:
    runtime = FakeRuntime()
    conv = FakeConvRepo()
    events = FakeEventRepo()
    service = ConversationService(runtime, conv, events)  # type: ignore[arg-type]
    return service, runtime, conv, events


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


async def test_start_conversation_persists_record() -> None:
    service, _runtime, conv, _ = _make_service()
    record = await service.start_conversation(title="因子分析", agent_run_id="RR-1")

    assert record.conversation_id.startswith("CONV-")
    assert record.opencode_session_id == "sess-1"
    assert record.agent_run_id == "RR-1"
    assert record.status is ConversationStatus.ACTIVE
    assert record.agent_name == "finboard-researcher"
    assert record.last_event_seq == 0
    stored = await conv.get(record.conversation_id)
    assert stored is not None
    assert stored.opencode_session_id == "sess-1"


async def test_subscribe_projects_key_events_and_advances_cursor() -> None:
    service, runtime, conv, events = _make_service()
    record = await service.start_conversation()
    runtime.events[record.opencode_session_id] = [
        {"seq": 1, "type": "message.part", "text": "tok"},
        {"seq": 2, "type": "tool.call", "name": "finboard.run.list"},
        {"seq": 3, "type": "message.completed", "role": "assistant"},
    ]
    yielded = [ev async for ev in service.subscribe(record.conversation_id)]

    assert len(yielded) == 2
    assert yielded[0].event_type == "tool.call"
    assert yielded[1].event_type == "message.completed"
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.last_event_seq == 3
    stored = await events.list_by_conversation(record.conversation_id)
    assert len(stored) == 2


async def test_subscribe_skips_events_without_seq() -> None:
    service, runtime, conv, _ = _make_service()
    record = await service.start_conversation()
    runtime.events[record.opencode_session_id] = [
        {"type": "noise"},
        {"seq": 5, "type": "error", "message": "boom"},
    ]
    yielded = [ev async for ev in service.subscribe(record.conversation_id)]

    assert len(yielded) == 1
    assert yielded[0].event_seq == 5
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.last_event_seq == 5


async def test_subscribe_disconnect_marks_interrupted() -> None:
    service, runtime, conv, _ = _make_service()
    record = await service.start_conversation()
    runtime._drop_on_subscribe = True

    yielded = [ev async for ev in service.subscribe(record.conversation_id)]
    assert yielded == []
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.status is ConversationStatus.INTERRUPTED


async def test_interrupt_calls_runtime_and_updates_status() -> None:
    service, runtime, conv, _ = _make_service()
    record = await service.start_conversation()
    await service.interrupt(record.conversation_id)

    assert runtime.interrupted == [record.opencode_session_id]
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.status is ConversationStatus.INTERRUPTED


async def test_replay_events_reads_local_store() -> None:
    service, _runtime, _, events = _make_service()
    record = await service.start_conversation()
    await events.append(
        AgentEvent(
            conversation_id=record.conversation_id,
            event_seq=1,
            event_type="tool.call",
            role="assistant",
            payload={"name": "finboard.run.list"},
            timestamp=datetime.now(UTC),
        )
    )
    replayed = await service.replay_events(record.conversation_id)
    assert len(replayed) == 1
    assert replayed[0].event_type == "tool.call"


async def test_reconcile_orphaned_marks_missing_sessions() -> None:
    service, runtime, conv, _ = _make_service()
    record = await service.start_conversation()
    runtime.sessions.pop(record.opencode_session_id)
    orphaned = await service.reconcile_orphaned()

    assert orphaned == 1
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.status is ConversationStatus.ORPHANED


async def test_reconcile_orphaned_skips_when_unavailable() -> None:
    service, runtime, conv, _ = _make_service()
    record = await service.start_conversation()
    original = runtime.get_session

    async def _unavailable(session_id: str) -> dict[str, Any]:
        raise OpenCodeUnavailableError("down")

    runtime.get_session = _unavailable  # type: ignore[method-assign]
    orphaned = await service.reconcile_orphaned()
    runtime.get_session = original  # type: ignore[method-assign]

    assert orphaned == 0
    updated = await conv.get(record.conversation_id)
    assert updated is not None
    assert updated.status is ConversationStatus.ACTIVE


async def test_send_prompt_passes_through() -> None:
    service, _runtime, _, _ = _make_service()
    record = await service.start_conversation()
    result = await service.send_prompt(record.conversation_id, "分析沪深300")
    assert result["session_id"] == record.opencode_session_id
