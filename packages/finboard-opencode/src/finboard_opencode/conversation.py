"""OpenCode 会话关联服务(issue #109)。

编排 :class:`OpenCodeRuntimeClient`(运行时)、:class:`AgentConversationRepository`
(会话关联)与 :class:`AgentEventRepository`(事件持久化),提供:

* 创建研究会话并关联到 ``conversation_id`` / ``agent_run_id``;
* 从 ``last_event_seq`` 续订 SSE 事件流(断线恢复);
* 投影关键事件(message 完成 / 工具调用 / 工具结果 / 错误 / 状态)并持久化;
* 中断 / 中止会话;
* 历史回放(本地持久化的事件 + OpenCode history 兜底)。

红线:OpenCode 是受控研究运行时,本服务不触碰实盘订单 / 持仓 / 风控。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog

from finboard_opencode.repository import (
    AgentConversationRepository,
    AgentEventRepository,
)
from finboard_opencode.runtime import (
    OpenCodeRuntimeClient,
    OpenCodeRuntimeError,
    OpenCodeUnavailableError,
)
from finboard_opencode.schemas import (
    AgentEvent,
    ConversationRecord,
    ConversationStatus,
    generate_conversation_id,
)

#: 持久化到 ``agent_events`` 的关键事件类型白名单。
#: token 级增量(message.part 流式片段)不落库,只推进 ``last_event_seq`` 游标。
KEY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "message",
        "message.updated",
        "message.completed",
        "message.removed",
        "tool",
        "tool.call",
        "tool.result",
        "tool.output",
        "error",
        "session.state",
        "session.updated",
        "agent.switched",
        "abort",
        "interrupt",
    }
)


def _extract_seq(raw: dict[str, Any]) -> int | None:
    for key in ("seq", "sequence", "sequenceNumber", "id"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _extract_type(raw: dict[str, Any]) -> str:
    return str(raw.get("type") or raw.get("event") or "unknown")


def _extract_role(raw: dict[str, Any], event_type: str) -> str | None:
    props = raw.get("properties")
    if isinstance(props, dict):
        role = props.get("role")
        if isinstance(role, str):
            return role
        msg = props.get("message")
        if isinstance(msg, dict):
            msg_role = msg.get("role")
            if isinstance(msg_role, str):
                return msg_role
    if "user" in event_type:
        return "user"
    if "assistant" in event_type or "tool" in event_type:
        return "assistant"
    role_value: object = raw.get("role")
    if isinstance(role_value, str):
        return role_value
    return None


def _is_key_event(event_type: str) -> bool:
    return event_type in KEY_EVENT_TYPES


class ConversationService:
    """编排 OpenCode 运行时与会话关联持久化。"""

    def __init__(
        self,
        runtime: OpenCodeRuntimeClient,
        conversation_repo: AgentConversationRepository,
        event_repo: AgentEventRepository,
        *,
        default_agent: str = "finboard-researcher",
    ) -> None:
        self._runtime = runtime
        self._conv = conversation_repo
        self._events = event_repo
        self._default_agent = default_agent
        self._log = structlog.get_logger("finboard_opencode.conversation")

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def start_conversation(
        self,
        *,
        title: str | None = None,
        agent_run_id: str | None = None,
        agent: str | None = None,
        model: str | None = None,
    ) -> ConversationRecord:
        """创建 OpenCode session 并持久化会话关联。"""
        agent_name = agent or self._default_agent
        session = await self._runtime.create_session(
            agent=agent_name, title=title, model=model
        )
        session_id = str(session.get("id") or session.get("sessionID") or "")
        if not session_id:
            raise OpenCodeRuntimeError("OpenCode create_session 未返回 session id")
        model_ref = model or (
            str(session["model"]) if isinstance(session.get("model"), str) else None
        )
        record = ConversationRecord(
            conversation_id=generate_conversation_id(),
            opencode_session_id=session_id,
            agent_run_id=agent_run_id,
            title=title or str(session.get("title") or ""),
            status=ConversationStatus.ACTIVE,
            agent_name=agent_name,
            model_ref=model_ref,
            last_event_seq=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await self._conv.save(record)
        self._log.info(
            "conversation.started",
            conversation_id=record.conversation_id,
            opencode_session_id=session_id,
            agent_run_id=agent_run_id,
            agent=agent_name,
        )
        return record

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        return await self._conv.get(conversation_id)

    async def list_conversations(self, *, limit: int = 50) -> list[ConversationRecord]:
        return await self._conv.list_recent(limit=limit)

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------

    async def send_prompt(
        self,
        conversation_id: str,
        prompt_text: str,
        *,
        wait: bool = True,
    ) -> dict[str, Any]:
        record = await self._require_active(conversation_id)
        return await self._runtime.prompt(
            record.opencode_session_id, prompt_text=prompt_text, wait=wait
        )

    async def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        record = await self._conv.get(conversation_id)
        if record is None:
            return []
        return await self._runtime.list_messages(record.opencode_session_id)

    # ------------------------------------------------------------------
    # 事件订阅与断线恢复
    # ------------------------------------------------------------------

    async def subscribe(
        self, conversation_id: str
    ) -> AsyncIterator[AgentEvent]:
        """从 ``last_event_seq`` 续订 SSE 事件流。

        每个 raw 事件都推进 ``last_event_seq`` 游标(保证断线后不重复);
        关键事件额外投影到 ``agent_events`` 并 yield 给调用方。
        连接断开时迭代正常结束 —— 调用方再次 subscribe 即可从游标恢复。
        """
        record = await self._require_active(conversation_id)
        after = record.last_event_seq
        self._log.info(
            "conversation.subscribe",
            conversation_id=conversation_id,
            after_seq=after,
        )
        try:
            async for raw in self._runtime.subscribe_events(
                record.opencode_session_id, after_seq=after
            ):
                seq = _extract_seq(raw)
                if seq is None:
                    continue
                await self._conv.update_event_seq(conversation_id, seq)
                event_type = _extract_type(raw)
                if not _is_key_event(event_type):
                    continue
                event = AgentEvent(
                    conversation_id=conversation_id,
                    event_seq=seq,
                    event_type=event_type,
                    role=_extract_role(raw, event_type),
                    payload=raw,
                    timestamp=datetime.now(UTC),
                )
                await self._events.append(event)
                yield event
        except OpenCodeUnavailableError as exc:
            self._log.warning(
                "conversation.subscribe.disconnected",
                conversation_id=conversation_id,
                error=str(exc),
            )
            await self._conv.update_status(
                conversation_id, ConversationStatus.INTERRUPTED
            )

    async def replay_events(
        self, conversation_id: str, *, after_seq: int = 0
    ) -> list[AgentEvent]:
        """从本地持久化的事件回放(无需 OpenCode 在线)。"""
        return await self._events.list_by_conversation(
            conversation_id, after_seq=after_seq
        )

    # ------------------------------------------------------------------
    # 中断 / 中止
    # ------------------------------------------------------------------

    async def interrupt(self, conversation_id: str) -> None:
        record = await self._require_active(conversation_id)
        await self._runtime.interrupt(record.opencode_session_id)
        await self._conv.update_status(
            conversation_id, ConversationStatus.INTERRUPTED
        )

    async def abort(self, conversation_id: str) -> None:
        record = await self._require_active(conversation_id)
        await self._runtime.abort(record.opencode_session_id)
        await self._conv.update_status(
            conversation_id, ConversationStatus.INTERRUPTED
        )

    async def mark_completed(self, conversation_id: str) -> None:
        await self._conv.update_status(
            conversation_id, ConversationStatus.COMPLETED
        )

    async def mark_failed(self, conversation_id: str, reason: str) -> None:
        self._log.warning(
            "conversation.failed",
            conversation_id=conversation_id,
            reason=reason,
        )
        await self._conv.update_status(conversation_id, ConversationStatus.FAILED)

    # ------------------------------------------------------------------
    # 孤儿恢复:检查 ACTIVE/INTERRUPTED 会话的 opencode session 是否还在
    # ------------------------------------------------------------------

    async def reconcile_orphaned(self) -> int:
        """检查中断/活跃会话的 OpenCode session 是否仍然存在。

        返回被标记为 ORPHANED 的会话数。OpenCode 不可达时不做任何变更
        (避免误判)。
        """
        orphaned = 0
        for status in (ConversationStatus.ACTIVE, ConversationStatus.INTERRUPTED):
            for record in await self._conv.list_by_status(status):
                try:
                    await self._runtime.get_session(record.opencode_session_id)
                except OpenCodeUnavailableError:
                    return orphaned
                except OpenCodeRuntimeError:
                    await self._conv.update_status(
                        record.conversation_id, ConversationStatus.ORPHANED
                    )
                    orphaned += 1
        return orphaned

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require_active(self, conversation_id: str) -> ConversationRecord:
        record = await self._conv.get(conversation_id)
        if record is None:
            raise ConversationNotFoundError(conversation_id)
        return record


class ConversationNotFoundError(LookupError):
    """会话不存在。"""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"conversation not found: {conversation_id}")
        self.conversation_id = conversation_id
