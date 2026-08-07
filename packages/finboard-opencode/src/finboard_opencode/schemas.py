"""OpenCode 研究运行时会话关联的领域类型。

``conversation_id`` 由 FinBoard 本地生成,关联到 OpenCode ``session_id`` 与
可选的研究运行 ``agent_run_id``。``ConversationStatus`` 描述会话生命周期;
``AgentEvent`` 是从 OpenCode durable 事件流投影出的关键事件。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

CONVERSATION_ID_PREFIX = "CONV"


class ConversationStatus(StrEnum):
    """研究会话生命周期状态。"""

    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    #: OpenCode session 已不存在 / 被外部删除。
    ORPHANED = "orphaned"


def generate_conversation_id() -> str:
    """生成全局唯一的 ``conversation_id``。

    格式: ``CONV-<YYYYMMDD-UTC>-<16 hex>``
    """
    today = datetime.now(UTC).strftime("%Y%m%d")
    suffix = secrets.token_hex(8)
    return f"{CONVERSATION_ID_PREFIX}-{today}-{suffix}"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """从 OpenCode 事件流投影出的关键事件。"""

    conversation_id: str
    event_seq: int
    event_type: str
    role: str | None
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """研究会话关联记录(领域对象)。"""

    conversation_id: str
    opencode_session_id: str
    agent_run_id: str | None
    title: str | None
    status: ConversationStatus
    agent_name: str
    model_ref: str | None
    last_event_seq: int
    created_at: datetime
    updated_at: datetime
