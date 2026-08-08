"""OpenCode Web 访问凭证签发(issue #118)。

FinBoard 网关是 OpenCode Web 的**控制面**:不直接向浏览器暴露未授权的 OpenCode 端口,
而是为已授权的 ``conversation`` 签发访问凭证(OpenCode Web 根 URL + basic auth)。
前端拿到凭证后用 iframe 跨源嵌入 OpenCode Web。

授权校验:``conversation`` 必须存在且状态为 ``ACTIVE``(由路由层查 DB 后传入本模块)。
单用户场景下"归属"退化为存在性 + 状态;多用户场景需在路由层叠加 owner 校验。

红线:本模块只签发研究运行时访问凭证,不触及实盘订单 / 持仓 / 风控。
"""

from __future__ import annotations

from dataclasses import dataclass

from finboard_opencode.process_manager import OpenCodeProcessManager
from finboard_opencode.schemas import ConversationRecord, ConversationStatus

#: 密码脱敏掩码(用于 ``/status`` 等不需要明文密码的视图)。
_REDACTED = "***"


@dataclass(frozen=True, slots=True)
class OpenCodeAccessInfo:
    """已授权会话的 OpenCode Web 访问凭证。

    前端 iframe 用 ``web_url`` + ``username``/``password`` 构造 basic auth URL:
    ``http://<username>:<password>@<host>:<port>/``。
    """

    conversation_id: str
    opencode_session_id: str
    web_url: str
    username: str
    password: str
    agent_name: str


class AccessNotAuthorizedError(PermissionError):
    """会话未授权访问 OpenCode Web(不存在 / 非 ACTIVE 状态)。"""

    def __init__(self, conversation_id: str, reason: str) -> None:
        super().__init__(
            f"conversation {conversation_id} not authorized for opencode access: {reason}"
        )
        self.conversation_id = conversation_id
        self.reason = reason


def _authorize(record: ConversationRecord | None, conversation_id: str) -> ConversationRecord:
    """校验会话授权:必须存在且 ACTIVE。"""
    if record is None:
        raise AccessNotAuthorizedError(conversation_id, "conversation not found")
    if record.status != ConversationStatus.ACTIVE:
        raise AccessNotAuthorizedError(conversation_id, f"status={record.status.value}")
    return record


class AccessCredentialIssuer:
    """为已授权会话签发 OpenCode Web 访问凭证。

    从 :class:`OpenCodeProcessManager` 读取 base_url + basic auth 密码,叠加会话信息
    构造 :class:`OpenCodeAccessInfo`。授权校验由 :func:`_authorize` 完成。
    """

    def __init__(
        self,
        process_manager: OpenCodeProcessManager,
        *,
        agent_name: str = "finboard-researcher",
    ) -> None:
        self._manager = process_manager
        self._agent_name = agent_name

    def issue(
        self, record: ConversationRecord | None, *, conversation_id: str
    ) -> OpenCodeAccessInfo:
        """校验并签发访问凭证。

        调用方(路由层)负责查 DB 拿到 ``record`` 后传入;``conversation_id`` 单独传
        是为了 record 为 None 时仍能报出具体会话。
        """
        authorized = _authorize(record, conversation_id)
        config = self._manager.config
        return OpenCodeAccessInfo(
            conversation_id=authorized.conversation_id,
            opencode_session_id=authorized.opencode_session_id,
            web_url=config.base_url,
            username=config.username,
            password=self._manager.effective_password,
            agent_name=self._agent_name,
        )

    @staticmethod
    def redact(info: OpenCodeAccessInfo) -> dict[str, str | None]:
        """返回脱敏视图(用于日志 / ``/status`` 等非凭证下发场景)。"""
        return {
            "conversation_id": info.conversation_id,
            "opencode_session_id": info.opencode_session_id,
            "web_url": info.web_url,
            "username": info.username,
            "password": _REDACTED,
            "agent_name": info.agent_name,
        }
