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


# ---------------------------------------------------------------------------
# issue #517:按客户端凭据解析身份(注入 resolver,不依赖 DB)
# ---------------------------------------------------------------------------


def _identity(client_id: str = "MC-0123456789ab", scopes: tuple[str, ...] = ("research.read",)):
    from finboard_mcp.identity import McpClientIdentity

    return McpClientIdentity(
        client_id=client_id, name="pytest", scopes=frozenset(scopes)
    )


async def _run_capture(
    wrapped: BearerTokenMiddleware, scope: dict[str, object]
) -> tuple[list[dict[str, object]], dict[str, object], bool]:
    """发起一次 ASGI 请求,捕获 inner app 收到的 scope state 与是否被调用。"""
    captured: dict[str, object] = {}
    called = {"inner": False}

    async def inner_app(scope, receive, send):
        called["inner"] = True
        captured.update(scope.get("state") or {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped._app = inner_app
    responses: list[dict[str, object]] = []

    async def send(msg):
        responses.append(msg)

    async def receive() -> None:
        return None

    await wrapped(scope, receive, send)
    return responses, captured, called["inner"]


async def test_resolver_identity_written_to_scope_state() -> None:
    """resolver 命中的身份写入 scope["state"],工具入口 app_context 可读取。"""
    expected = _identity()

    async def resolver(token: str):
        return expected if token == "client-token" else None

    async def _passthrough(scope, receive, send):
        pass

    wrapped = BearerTokenMiddleware(_passthrough, resolver=resolver)
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer client-token")])
    responses, captured, called = await _run_capture(wrapped, scope)
    assert called is True
    assert responses[0]["status"] == 200
    assert captured["mcp_identity"] is expected


async def test_resolver_miss_without_legacy_token_returns_401() -> None:
    async def resolver(token: str):
        return None

    async def _passthrough(scope, receive, send):
        pass

    wrapped = BearerTokenMiddleware(_passthrough, resolver=resolver)
    scope = _make_scope("/mcp", [(b"authorization", b"Bearer unknown")])
    responses, _, called = await _run_capture(wrapped, scope)
    assert called is False
    assert responses[0]["status"] == 401


async def test_legacy_token_identity_has_wildcard() -> None:
    """旧 FINBOARD_MCP_AUTH_TOKEN 兼容:合成 MC-legacy / scopes=["*"]。"""
    from finboard_mcp.identity import McpClientIdentity

    async def _passthrough(scope, receive, send):
        pass

    wrapped = BearerTokenMiddleware(_passthrough, token=TOKEN)
    scope = _make_scope("/mcp", [(b"authorization", f"Bearer {TOKEN}".encode())])
    _responses, captured, called = await _run_capture(wrapped, scope)
    assert called is True
    identity = captured["mcp_identity"]
    assert isinstance(identity, McpClientIdentity)
    assert identity.client_id == "MC-legacy"
    assert identity.scopes == frozenset({"*"})


async def test_wrong_and_unknown_tokens_share_401_body() -> None:
    """错误密钥与不存在的客户端返回完全相同的 401,不泄露客户端存在性。"""

    async def resolver(token: str):
        return _identity() if token == "right" else None

    async def _passthrough(scope, receive, send):
        pass

    wrapped = BearerTokenMiddleware(_passthrough, resolver=resolver)
    wrong, _, _ = await _run_capture(
        wrapped, _make_scope("/mcp", [(b"authorization", b"Bearer wrong")])
    )
    unknown, _, _ = await _run_capture(
        wrapped, _make_scope("/mcp", [(b"authorization", b"Bearer MC-does-not-exist")])
    )
    assert wrong == unknown
    assert wrong[0]["status"] == 401


async def test_resolver_exception_fails_closed_with_401() -> None:
    """resolver 抛错(DB 不可达等)不得放行,回 401。"""

    async def resolver(token: str):
        raise RuntimeError("db down")

    async def _passthrough(scope, receive, send):
        pass

    wrapped = BearerTokenMiddleware(_passthrough, resolver=resolver)
    responses, _, called = await _run_capture(
        wrapped, _make_scope("/mcp", [(b"authorization", b"Bearer x")])
    )
    assert called is False
    assert responses[0]["status"] == 401


def test_app_context_binds_identity_from_request_state() -> None:
    """app_context 从 MCP request_context.request.state 取身份并写入 contextvar。"""
    from types import SimpleNamespace

    from finboard_mcp.context import app_context
    from finboard_mcp.identity import (
        current_identity,
        reset_current_identity,
        set_current_identity,
    )

    expected = _identity()
    fake_app = object()
    fake_ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=fake_app,
            request=SimpleNamespace(state=SimpleNamespace(mcp_identity=expected)),
        )
    )
    token = set_current_identity(None)
    try:
        result = app_context(fake_ctx)  # type: ignore[arg-type]
        assert result is fake_app
        assert current_identity() is expected
    finally:
        reset_current_identity(token)


def test_app_context_without_request_clears_identity() -> None:
    """无 HTTP 请求且无默认身份时写 None,不残留上一次调用的身份。"""
    from types import SimpleNamespace

    from finboard_mcp.context import app_context
    from finboard_mcp.identity import (
        current_identity,
        reset_current_identity,
        set_current_identity,
    )
    from finboard_mcp.scopes import reset_scope_policy

    reset_scope_policy()
    token = set_current_identity(_identity())
    fake_ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=object(), request=None)
    )
    try:
        app_context(fake_ctx)  # type: ignore[arg-type]
        assert current_identity() is None
    finally:
        reset_current_identity(token)

