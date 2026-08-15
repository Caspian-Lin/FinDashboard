"""OpenCodeRuntimeClient 单元测试(issue #109)。

用 ``httpx.MockTransport`` 拦截请求,验证 HTTP 调用、SSE 解析与错误处理。
不连接真实 OpenCode server。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from finboard_opencode.runtime import (
    OpenCodeRuntimeClient,
    OpenCodeRuntimeError,
    OpenCodeUnavailableError,
)


@pytest.fixture
def make_client():
    """构造一个注入 MockTransport 的 client。"""

    def _make(handler) -> OpenCodeRuntimeClient:
        client = OpenCodeRuntimeClient(base_url="http://test")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test"
        )
        return client

    return _make


async def test_health_ok(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/global/health"
        return httpx.Response(200, json={"healthy": True, "version": "0.1.0"})

    client = make_client(handler)
    data = await client.health()
    assert data["healthy"] is True
    await client.aclose()


async def test_create_session(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"agent": "finboard-researcher", "title": "test"}
        return httpx.Response(200, json={"data": {"id": "sess-abc", "title": "test"}})

    client = make_client(handler)
    session = await client.create_session(agent="finboard-researcher", title="test")
    assert session["id"] == "sess-abc"
    await client.aclose()


async def test_prompt(make_client) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "msg-1"}})

    client = make_client(handler)
    result = await client.prompt("sess-1", prompt_text="hello", wait=False)
    assert captured["path"] == "/api/session/sess-1/prompt"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["resume"] is False
    assert result["id"] == "msg-1"
    await client.aclose()


async def test_interrupt(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/sess-1/interrupt"
        return httpx.Response(204)

    client = make_client(handler)
    await client.interrupt("sess-1")  # 不抛异常即通过
    await client.aclose()


async def test_http_error_raises_runtime_error(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = make_client(handler)
    with pytest.raises(OpenCodeRuntimeError):
        await client.create_session()
    await client.aclose()


async def test_connect_error_raises_unavailable(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(OpenCodeUnavailableError):
        await client.create_session()
    await client.aclose()


async def test_subscribe_events_parses_sse(make_client) -> None:
    sse = (
        b'event: message\n'
        b'data: {"seq": 1, "type": "message", "role": "user"}\n'
        b'\n'
        b'event: tool.call\n'
        b'data: {"seq": 2, "type": "tool.call", "name": "finboard.run.list"}\n'
        b'\n'
        b'data: {"seq": 3, "type": "message.part"}\n'
        b'\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/sess-1/event"
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    events = []
    async for event in client.subscribe_events("sess-1", after_seq=0):
        events.append(event)

    assert len(events) == 3
    assert events[0]["seq"] == 1
    assert events[0]["event"] == "message"
    assert events[1]["seq"] == 2
    assert events[1]["name"] == "finboard.run.list"
    assert events[2]["seq"] == 3
    await client.aclose()


async def test_subscribe_events_after_param(make_client) -> None:
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params["after"] = request.url.params.get("after")
        return httpx.Response(200, content=b"")

    client = make_client(handler)
    async for _ in client.subscribe_events("sess-1", after_seq=42):
        pass
    assert captured_params["after"] == "42"
    await client.aclose()


async def test_subscribe_events_connect_error(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dropped")

    client = make_client(handler)
    with pytest.raises(OpenCodeUnavailableError):
        async for _ in client.subscribe_events("sess-1"):
            pass
    await client.aclose()


async def test_list_messages(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "m1"}, {"id": "m2"}]},
        )

    client = make_client(handler)
    messages = await client.list_messages("sess-1")
    assert len(messages) == 2
    await client.aclose()


async def test_get_session(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/sess-1"
        return httpx.Response(200, json={"data": {"id": "sess-1", "title": "t"}})

    client = make_client(handler)
    session = await client.get_session("sess-1")
    assert session["id"] == "sess-1"
    await client.aclose()


async def test_session_history_with_replay_cursor(make_client) -> None:
    """#112:Agent Run 回放 —— 从 after 游标续取会话历史(断点重放)。

    #121 重构后 OpenCode 自身管理会话/历史,FinBoard 不再维护会话投影表;
    ``session_history(after=...)`` 与 SSE 事件流(``after_seq`` 游标)是回放原语。
    """
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/sess-1/history"
        captured_params["after"] = request.url.params.get("after")
        return httpx.Response(
            200,
            json={
                "data": {
                    "session_id": "sess-1",
                    "events": [{"seq": 3, "type": "message", "role": "user"}],
                    "cursor": 3,
                }
            },
        )

    client = make_client(handler)
    history = await client.session_history("sess-1", after=3)
    assert captured_params["after"] == "3"
    assert history["data"]["events"][0]["seq"] == 3
    await client.aclose()


async def test_session_history_without_cursor(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "after" not in request.url.params
        return httpx.Response(200, json={"data": {"events": []}})

    client = make_client(handler)
    history = await client.session_history("sess-1")
    assert history["data"]["events"] == []
    await client.aclose()


async def test_no_auth_by_default() -> None:
    """#157 移除 basic auth 后,请求不带 Authorization 头(单用户明文 URL 场景)。

    web 容器模式(4097)与外部 serve 模式(4096)均无鉴权;宿主机侧 127.0.0.1
    绑定是唯一网络边界。
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": {"id": "sess-1"}})

    client = OpenCodeRuntimeClient(base_url="http://test")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    await client.create_session()
    assert captured["authorization"] == ""
    await client.aclose()
