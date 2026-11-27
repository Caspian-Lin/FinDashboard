"""HTTP 传输的 Bearer 凭据鉴权中间件(issue #517 按客户端授权)。

``finboard_mcp`` 切到 ``streamable-http`` / ``sse`` 传输后,若绑定非 loopback
会暴露到局域网。本模块提供纯 ASGI 中间件:

* 从 ``Authorization: Bearer <token>`` 解析候选 token;
* 先查 ``mcp_clients``(按 ``sha256(token)`` 摘要),校验未吊销 / 未过期;
* 兼容 ``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token —— 合成 ``MC-legacy`` /
  ``scopes=["*"]``,启动时打印一行 deprecation warning(下一版本移除);
* 未命中 / 已吊销 / 已过期一律 401,响应体完全相同,不泄露客户端是否存在;
* 命中后把 :class:`~finboard_mcp.identity.McpClientIdentity` 写入
  ``scope["state"]["mcp_identity"]``,工具入口 ``app_context(ctx)`` 读取并注入
  contextvar,``run_tool`` 据此强制作用域。

设计要点:

- **stdio 模式不走这里** —— 本机子进程接入无 HTTP 请求,身份由
  ``FINBOARD_MCP_STDIO_SCOPES`` 决定(``__main__`` 装配)。
- **不记录 token 内容** —— 敏感字段不进日志 / 审计 / Provenance(AGENTS.md 红线)。
- **常量时间比较** —— ``secrets.compare_digest`` 防时序攻击;无匹配行时也做一次
  哑比较,避免用响应时间探测客户端是否存在。
- **健康探针放行** —— ``/global/health`` 等 ping 端点不要求 token(否则健康探测
  会被 401 挡住)。

红线:本模块只做传输层鉴权与身份解析,不改变工具权限矩阵(研究写操作自主执行 /
实盘能力永久不注册)。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_mcp.identity import (
    LEGACY_CLIENT_ID,
    McpClientIdentity,
    hash_client_token,
)
from finboard_persistence.mcp_client_repo import McpClientRepository

_logger = logging.getLogger("finboard.mcp.auth")

#: 不要求 Bearer token 的路径前缀(健康探针 / 根路径 ping)。
_PUBLIC_PATHS: frozenset[str] = frozenset({"/global/health", "/health", "/"})

#: 无匹配行时用于常量时间哑比较的假摘要(长度与 sha256 hex 一致)。
_DUMMY_TOKEN_HASH = "0" * 64

#: ``last_used_at`` 的最小更新间隔,避免每个 HTTP 请求都写库。
_TOUCH_INTERVAL = timedelta(seconds=60)

#: 身份解析器:token → 身份(未命中返回 None)。
IdentityResolver = Callable[[str], Awaitable[McpClientIdentity | None]]

#: ASGI 3 元组:(scope, receive, send)。
_ASGIScope = dict[str, Any]
_ASGIReceive = Callable[[], Awaitable[object]]
_ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


def legacy_identity() -> McpClientIdentity:
    """``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 合成的兼容身份。"""
    return McpClientIdentity(
        client_id=LEGACY_CLIENT_ID,
        name="legacy-env-token",
        scopes=frozenset({"*"}),
    )


def warn_legacy_auth_token() -> None:
    """启动时打印一行 deprecation warning(不记录 token 内容)。"""
    _logger.warning(
        "FINBOARD_MCP_AUTH_TOKEN 是旧共享 token(全权、不可按客户端撤销),"
        "已进入弃用期;请改用 `finboard mcp client create` 签发按客户端凭据,"
        "下一版本将移除该环境变量。"
    )


class DatabaseIdentityResolver:
    """按 ``mcp_clients.token_hash`` 解析 Bearer token 的身份。

    每次解析开独立短会话(与工具调用的 session 分离);``last_used_at`` 最多
    每 :data:`_TOUCH_INTERVAL` 更新一次。数据库不可达 / 查询失败一律返回
    ``None``(fail-closed),由中间件回 401,不向调用方泄露内部错误。
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        touch_interval: timedelta = _TOUCH_INTERVAL,
    ) -> None:
        self._session_maker = session_maker
        self._touch_interval = touch_interval

    async def __call__(self, token: str) -> McpClientIdentity | None:
        digest = hash_client_token(token)
        try:
            async with self._session_maker() as session:
                repo = McpClientRepository(session)
                row = await repo.get_by_token_hash(digest)
                if row is None:
                    # 常量时间哑比较:让「不存在」与「摘要不匹配」耗时一致。
                    secrets.compare_digest(digest, _DUMMY_TOKEN_HASH)
                    return None
                if not secrets.compare_digest(row.token_hash, digest):
                    return None
                now = datetime.now(UTC)
                if row.revoked_at is not None:
                    return None
                if row.expires_at is not None and row.expires_at <= now:
                    return None
                if row.last_used_at is None or now - row.last_used_at >= self._touch_interval:
                    await repo.touch_last_used(row.client_id, when=now)
                    await session.commit()
                return McpClientIdentity(
                    client_id=row.client_id,
                    name=row.name,
                    scopes=frozenset(row.scopes),
                )
        except Exception:
            _logger.exception("mcp.auth.identity_resolver_failed")
            return None


class BearerTokenMiddleware:
    """ASGI3 中间件:解析 Bearer token → 客户端身份,失败 401。

    用法::

        wrapped = BearerTokenMiddleware(starlette_app, resolver=DatabaseIdentityResolver(sm))
        uvicorn.run(wrapped, ...)

    ``starlette`` 的 ``Starlette`` 实例本身实现了 ASGI3 ``__call__``,因此可以
    直接包裹。``token`` 参数保留为旧共享 token 兼容路径(``_token`` 属性供既有
    单测访问)。
    """

    def __init__(
        self,
        app: Callable[[_ASGIScope, _ASGIReceive, _ASGISend], Any],
        *,
        token: str = "",
        resolver: IdentityResolver | None = None,
        public_paths: Iterable[str] = _PUBLIC_PATHS,
    ) -> None:
        self._app = app
        self._token = token
        self._resolver = resolver
        self._public_paths = frozenset(public_paths)

    async def __call__(
        self, scope: _ASGIScope, receive: _ASGIReceive, send: _ASGISend
    ) -> None:
        if scope.get("type") != "http":
            # 非 HTTP(如 lifespan)直接放行 —— 中间件只鉴权 HTTP 请求。
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self._public_paths:
            await self._app(scope, receive, send)
            return
        identity = await self._resolve_identity(scope)
        if identity is None:
            await self._reject(send)
            return
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):  # pragma: no cover - ASGI 契约保证 dict
            state = {}
            scope["state"] = state
        state["mcp_identity"] = identity
        await self._app(scope, receive, send)

    async def _resolve_identity(self, scope: _ASGIScope) -> McpClientIdentity | None:
        candidate = _bearer_token(scope)
        if candidate is None:
            return None
        if self._resolver is not None:
            try:
                identity = await self._resolver(candidate)
            except Exception:  # 解析器异常(DB 不可达等)一律 fail-closed
                _logger.exception("mcp.auth.identity_resolver_failed")
                identity = None
            if identity is not None:
                return identity
        if self._token and secrets.compare_digest(candidate, self._token):
            return legacy_identity()
        return None

    @staticmethod
    async def _reject(send: _ASGISend) -> None:
        """返回 401 Unauthorized(不带 WWW-Authenticate,避免泄露期望 scheme)。"""
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _bearer_token(scope: _ASGIScope) -> str | None:
    """从 ASGI scope 提取 Bearer token;缺失 / 非 Bearer / 空值返回 None。"""
    for name, value in _iter_headers(scope):
        if name == b"authorization":
            return _extract_bearer(value)
    return None


def _iter_headers(scope: _ASGIScope) -> Iterable[tuple[bytes, bytes]]:
    """从 ASGI scope 提取原始 header 二元组(scope["headers"])。"""
    headers = scope.get("headers")
    if not headers:
        return []
    return [(bytes(name), bytes(value)) for name, value in headers]


def _extract_bearer(raw: bytes) -> str | None:
    """从 ``Authorization`` header 值提取 Bearer token;非 Bearer scheme 返回 None。"""
    try:
        text = raw.decode("latin-1")
    except (UnicodeDecodeError, AttributeError):
        return None
    parts = text.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    candidate = parts[1].strip()
    return candidate or None


def wrap_with_bearer_auth(
    app: Any,
    *,
    token: str = "",
    resolver: IdentityResolver | None = None,
    public_paths: Iterable[str] = _PUBLIC_PATHS,
) -> BearerTokenMiddleware:
    """便捷工厂:把任意 ASGI3 app 包成带 Bearer 鉴权的 app。"""
    return BearerTokenMiddleware(
        app, token=token, resolver=resolver, public_paths=public_paths
    )


__all__ = [
    "BearerTokenMiddleware",
    "DatabaseIdentityResolver",
    "IdentityResolver",
    "legacy_identity",
    "warn_legacy_auth_token",
    "wrap_with_bearer_auth",
]
