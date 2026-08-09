"""OpenCode Web 访问凭证签发(issue #118 / 重构 #121)。

FinBoard 网关是 OpenCode Web 的**控制面**:不直接向浏览器暴露未授权的 OpenCode 端口,
而是在网关启用时为前端 iframe 签发访问凭证(OpenCode Web 根 URL + basic auth)。
前端拿到凭证后用 iframe 跨源嵌入 OpenCode Web。

重构背景(#121):FinBoard 不再维护独立研究会话投影层(``agent_conversations``/
``agent_events``)。OpenCode 自身管理会话/历史/恢复,FinBoard 只负责隔离实例的进程
托管与访问凭证签发。``/access`` 不再绑定 ``conversation_id``,网关启用即签发。

红线:本模块只签发研究运行时访问凭证,不触及实盘订单 / 持仓 / 风控。
"""

from __future__ import annotations

from dataclasses import dataclass

from finboard_opencode.process_manager import OpenCodeProcessManager

#: 密码脱敏掩码(用于 ``/status`` 等不需要明文密码的视图)。
_REDACTED = "***"


@dataclass(frozen=True, slots=True)
class OpenCodeAccessInfo:
    """OpenCode Web 访问凭证。

    前端 iframe 用 ``web_url`` + ``username``/``password`` 构造 basic auth URL:
    ``http://<username>:<password>@<host>:<port>/``。
    """

    web_url: str
    username: str
    password: str
    agent_name: str


class AccessCredentialIssuer:
    """签发 OpenCode Web 访问凭证。

    从 :class:`OpenCodeProcessManager` 读取 base_url + basic auth 密码,构造
    :class:`OpenCodeAccessInfo`。网关启用即可签发,不再绑定 conversation。
    """

    def __init__(
        self,
        process_manager: OpenCodeProcessManager,
        *,
        agent_name: str = "finboard-researcher",
    ) -> None:
        self._manager = process_manager
        self._agent_name = agent_name

    def issue_default(self) -> OpenCodeAccessInfo:
        """签发默认访问凭证(不查 conversation,#121 重构后唯一签发路径)。"""
        config = self._manager.config
        return OpenCodeAccessInfo(
            web_url=config.base_url,
            username=config.username,
            password=self._manager.effective_password,
            agent_name=self._agent_name,
        )

    @staticmethod
    def redact(info: OpenCodeAccessInfo) -> dict[str, str | None]:
        """返回脱敏视图(用于日志 / ``/status`` 等非凭证下发场景)。"""
        return {
            "web_url": info.web_url,
            "username": info.username,
            "password": _REDACTED,
            "agent_name": info.agent_name,
        }
