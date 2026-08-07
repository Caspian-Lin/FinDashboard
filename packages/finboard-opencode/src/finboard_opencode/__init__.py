"""finboard-opencode:OpenCode 研究运行时集成(issue #109)。

会话关联(``conversation_id`` ↔ OpenCode ``session_id`` ↔ ``agent_run_id``)、
SSE 事件订阅与断线恢复、关键事件持久化。

红线:OpenCode 是受控研究运行时,不连接实盘 broker / 账户 / 订单 / 持仓 / 风控。
"""

from finboard_opencode.conversation import (
    KEY_EVENT_TYPES,
    ConversationNotFoundError,
    ConversationService,
)
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

__all__ = [
    "KEY_EVENT_TYPES",
    "AgentConversationRepository",
    "AgentEvent",
    "AgentEventRepository",
    "ConversationNotFoundError",
    "ConversationRecord",
    "ConversationService",
    "ConversationStatus",
    "OpenCodeRuntimeClient",
    "OpenCodeRuntimeError",
    "OpenCodeUnavailableError",
    "generate_conversation_id",
]
