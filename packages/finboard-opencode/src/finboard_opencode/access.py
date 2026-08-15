"""OpenCode Web 访问信息签发(issue #118 / 重构 #121 / #157 移除 basic auth)。

FinBoard 网关是 OpenCode Web 的**控制面**:管理容器生命周期并向前端签发访问信息。
#157 用户决策:当前为单一用户模型,OpenCode Web **不启用 basic auth**,直接使用
明文 ``http://127.0.0.1:{port}`` URL;宿主机侧 127.0.0.1 绑定是唯一网络边界。
前端 iframe 与「新窗口打开」共用同一 URL,不再存在凭证分发链路。

重构背景(#121):FinBoard 不再维护独立研究会话投影层(``agent_conversations``/
``agent_events``)。OpenCode 自身管理会话/历史/恢复,FinBoard 只负责隔离实例的进程
托管与访问信息签发。``/access`` 不绑定 ``conversation_id``,网关启用即签发。

红线:本模块只签发研究运行时访问信息,不触及实盘订单 / 持仓 / 风控。
"""

from __future__ import annotations

from dataclasses import dataclass

from finboard_opencode.process_manager import OpenCodeProcessManager


@dataclass(frozen=True, slots=True)
class OpenCodeAccessInfo:
    """OpenCode Web 访问信息(明文 URL,无凭证;#157 单用户模型)。

    前端 iframe / 新窗口直接使用 ``web_url``(+ ``?directory=/workspace`` 查询参数,
    见 ``web/src/lib/opencode-url.ts``)。
    """

    web_url: str
    agent_name: str


class AccessIssuer:
    """签发 OpenCode Web 访问信息。

    从 :class:`OpenCodeProcessManager` 读取 base_url,构造 :class:`OpenCodeAccessInfo`。
    网关启用即可签发,不绑定 conversation(#121),不再生成 / 分发任何凭证(#157)。
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
        """签发默认访问信息(明文 URL,#121 重构后唯一签发路径)。"""
        return OpenCodeAccessInfo(
            web_url=self._manager.config.base_url,
            agent_name=self._agent_name,
        )


__all__ = ["AccessIssuer", "OpenCodeAccessInfo"]
