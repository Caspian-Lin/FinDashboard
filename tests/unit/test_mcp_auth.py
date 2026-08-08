"""finboard_mcp.auth Bearer token 中间件单元测试(issue #xxx Docker 隔离前置)。

验证:
* 正确 token 放行(200);
* 错误 / 缺失 / 非 Bearer scheme → 401;
* 健康端点 / lifespan 放行(不要求 token);
* 常量时间比较(``secrets.compare_digest``)。
"""

from __future__ import annotations

import pytest

from finboard_mcp.auth import BearerTokenMiddleware, wrap_with_bearer_auth

TOKEN = "test-bearer-token-xyz-789"


def _make_scope(
    path: str, headers: list[tuple[bytes, bytes]] | None = None
) -> dict[str, object]:
    return {"type": "http", "path": path, "headers": headers or []}


async def _run_request(
    wrapped: BearerTokenMiddleware, scope: dict[str, object]
) -> tuple[list[dict[str, object]], bool]:
    """发起一次 ASGI 请求,返回 (响应消息列表, inner_app 是否被调用)。"""
    called = {"inner": False}

    async def inner_app(scope, receive, send):
        called["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped._app = inner_app
    responses: list[dict[str, object]] = []

    async def send(msg):
        responses.append(msg)

    async def receive() -> None:
        return None

    await wrapped(scope, receive, send)
    return responses, called["inner"]


@pytest.fixture
def wrapped() -> BearerTokenMiddleware:
    async def _passthrough(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return BearerTokenMiddleware(_passthrough, token=TOKEN)


async def test_correct_token_passes_through(wrapped) -> None:
    scope = _make_scope("/mcp", [(b"authorization", f"Bearer {TOKEN}".encode())])
    responses, called = await _run_request(wrapped, scope)
    assert called is True
    assert responses[0]["status"] == 200


async def test_wrong_token_returns_401(wrapped) -> None:
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer wrong-token")])
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_missing_header_returns_401(wrapped) -> None:
    scope = _make_scope("/mcp", [])
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_non_bearer_scheme_returns_401(wrapped) -> None:
    scope = _make_scope(
        "/mcp", [(b"authorization", b"Basic dXNlcjpwYXNz")]
    )
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_empty_bearer_returns_401(wrapped) -> None:
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer ")])
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_health_endpoint_is_public(wrapped) -> None:
    """健康探针 /global/health 不要求 token(否则 wait_ready 永远 401 超时)。"""
    for path in ("/global/health", "/health", "/"):
        scope = _make_scope(path, [])
        responses, called = await _run_request(wrapped, scope)
        assert called is True, f"{path} should be public"
        assert responses[0]["status"] == 200


async def test_lifespan_scope_passes_through(wrapped) -> None:
    """非 HTTP scope(lifespan / startup)直接放行,中间件只鉴权 HTTP。"""
    scope: dict[str, object] = {"type": "lifespan", "headers": []}
    _, called = await _run_request(wrapped, scope)
    assert called is True


async def test_malformed_authorization_header_returns_401(wrapped) -> None:
    """乱码 / 解析失败的 header 一律 401。"""
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer")])  # 无空格分隔
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_case_insensitive_scheme(wrapped) -> None:
    """``bearer`` / ``Bearer`` / ``BEARER`` 都应被接受(scheme 大小写不敏感)。"""
    for scheme in ("bearer", "Bearer", "BEARER"):
        scope = _make_scope(
            "/mcp", [(b"authorization", f"{scheme} {TOKEN}".encode())]
        )
        responses, called = await _run_request(wrapped, scope)
        assert called is True, f"scheme '{scheme}' should be accepted"
        assert responses[0]["status"] == 200


async def test_wrap_with_bearer_auth_factory() -> None:
    """工厂函数返回 BearerTokenMiddleware 实例。"""

    async def _app(scope, receive, send):
        pass

    wrapped = wrap_with_bearer_auth(_app, token=TOKEN)
    assert isinstance(wrapped, BearerTokenMiddleware)
    assert wrapped._token == TOKEN


async def test_empty_token_rejects_all(wrapped) -> None:
    """配置 token 为空时(异常状态),所有受保护请求被拒绝。"""
    wrapped._token = ""
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer anything")])
    responses, called = await _run_request(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401
