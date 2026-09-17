"""OpenCode 运行时 HTTP/SSE 客户端(issue #109)。

连接 ``opencode serve`` 启动的 headless HTTP server,提供会话创建、消息发送、
SSE 事件订阅(支持 ``after`` 断线续传)与中断。

端点基于 OpenCode v2 API(experimental,``/api`` 前缀);真机验证时若端点形状
有偏差,调整 ``api_prefix`` 或对应路径即可(#121 重构后本客户端直接供
FinBoard 网关与 access 签发使用,不再有上层 ConversationService)。

红线:本客户端只与 OpenCode 研究运行时交互,不连接实盘 broker / 账户 / 订单。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx


class OpenCodeRuntimeError(RuntimeError):
    """OpenCode 运行时调用失败。"""


class OpenCodeUnavailableError(OpenCodeRuntimeError):
    """OpenCode server 不可达 / 连接失败。"""


def _envelope_data(data: dict[str, Any]) -> dict[str, Any]:
    """OpenCode v2 把资源包在 ``{"data": ...}`` 里;v1 直接返回资源。"""
    inner = data.get("data", data)
    return inner if isinstance(inner, dict) else data


class OpenCodeRuntimeClient:
    """``opencode serve`` 的 async HTTP + SSE 客户端。

    生命周期:调用方负责 ``await aclose()``(通常由 app lifespan 管理)。
    所有方法在 HTTP 错误时抛 :class:`OpenCodeRuntimeError`;连接失败降级为
    :class:`OpenCodeUnavailableError`,上层可据此将会话标记为 ORPHANED。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        *,
        api_prefix: str = "/api",
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._prefix = api_prefix.rstrip("/")
        # #157 移除 OpenCode Web basic auth(单用户明文 URL 决策)后,两种部署
        # 形态(web 容器 4097 / 外部 serve 4096)都是无鉴权直连;宿主机侧
        # 127.0.0.1 绑定是唯一网络边界。
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers=headers or {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenCodeRuntimeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._prefix}{path}"

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._client.request(
                method, self._url(path), json=json_body, params=params
            )
        except httpx.HTTPError as exc:
            raise OpenCodeUnavailableError(str(exc)) from exc
        if resp.status_code >= 400:
            raise OpenCodeRuntimeError(
                f"OpenCode {method} {path} -> {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return cast(dict[str, Any], resp.json())

    # ------------------------------------------------------------------
    # health / session
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """服务健康与版本(``GET /global/health``)。"""
        try:
            resp = await self._client.get("/global/health")
        except httpx.HTTPError as exc:
            raise OpenCodeUnavailableError(str(exc)) from exc
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    async def list_sessions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/session")
        items = data.get("data", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    async def create_session(
        self,
        *,
        agent: str | None = None,
        title: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if agent is not None:
            body["agent"] = agent
        if title is not None:
            body["title"] = title
        if model is not None:
            body["model"] = model
        data = await self._request("POST", "/session", json_body=body)
        return _envelope_data(data)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/session/{session_id}")
        return _envelope_data(data)

    # ------------------------------------------------------------------
    # message / prompt
    # ------------------------------------------------------------------

    async def prompt(
        self,
        session_id: str,
        *,
        prompt_text: str,
        agent: str | None = None,
        model: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": prompt_text}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
        if not wait:
            body["resume"] = False
        data = await self._request(
            "POST", f"/session/{session_id}/prompt", json_body=body
        )
        return _envelope_data(data)

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/session/{session_id}/message")
        items = data.get("data", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    # ------------------------------------------------------------------
    # events / interrupt
    # ------------------------------------------------------------------

    async def interrupt(self, session_id: str) -> None:
        await self._request("POST", f"/session/{session_id}/interrupt")

    async def abort(self, session_id: str) -> None:
        """中止运行中的会话(v1 兼容端点)。"""
        await self._request("POST", f"/session/{session_id}/abort")

    async def session_history(
        self, session_id: str, *, after: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        return await self._request(
            "GET", f"/session/{session_id}/history", params=params or None
        )

    async def subscribe_events(
        self, session_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅会话 durable 事件流(SSE)。

        从 ``after_seq`` 之后开始重放已完成事件,然后继续推送新事件。
        调用方在处理每个事件后应持久化其 ``seq`` 作为断线续传游标。
        连接断开时迭代正常结束,上层负责从 ``last_event_seq`` 重新订阅。
        """
        params = {"after": after_seq} if after_seq else {}
        url = self._url(f"/session/{session_id}/event")
        try:
            async with self._client.stream("GET", url, params=params) as resp:
                if resp.status_code >= 400:
                    raise OpenCodeRuntimeError(
                        f"OpenCode event stream -> {resp.status_code}"
                    )
                event_type: str | None = None
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                payload = {"raw": raw}
                            if event_type:
                                payload.setdefault("event", event_type)
                            yield payload
                        event_type = None
                        data_lines = []
        except httpx.HTTPError as exc:
            raise OpenCodeUnavailableError(str(exc)) from exc
