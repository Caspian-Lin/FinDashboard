"""HTTP 传输的 Bearer token 鉴权中间件(issue #xxx,Docker 隔离前置)。

``finboard_mcp`` 切到 ``streamable-http`` / ``sse`` 传输后,若绑定 ``0.0.0.0`` 会暴露到
局域网(容器跨 ``host.docker.internal`` 访问必须绑非 loopback)。本模块提供一个纯 ASGI
中间件,校验 ``Authorization: Bearer <token>`` header,校验失败返回 401。

设计要点:
- **stdio 模式不走这里** —— 本机子进程接入无需鉴权,``__main__`` 只在 HTTP 分支包裹。
- **不记录 token 内容** —— 敏感字段不进日志 / 审计 / Provenance(AGENTS.md 红线)。
- **常量时间比较** —— ``secrets.compare_digest`` 防时序攻击。
- **健康探针放行** —— ``/global/health`` 等 ping 端点不要求 token(否则 FinBoard 的
  ``OpenCodeProcessManager.wait_ready`` 健康探测会被 401 挡住)。

红线:本模块只做传输层鉴权,不改变工具权限矩阵(研究写操作自主执行 / 实盘能力永久不注册)。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

#: 不要求 Bearer token 的路径前缀(健康探针 / 根路径 ping)。
_PUBLIC_PATHS: frozenset[str] = frozenset({"/global/health", "/health", "/"})

#: ASGI 3 元组:(scope, receive, send)。
_ASGIScope = dict[str, Any]
_ASGIReceive = Callable[[], Awaitable[object]]
_ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class BearerTokenMiddleware:
    """ASGI3 中间件:校验 ``Authorization: Bearer <token>`` header。

    用法::

        wrapped = BearerTokenMiddleware(starlette_app, token="...")
        uvicorn.run(wrapped, ...)

    ``starlette`` 的 ``Starlette`` 实例本身实现了 ASGI3 ``__call__``,因此可以直接包裹。
    """

    def __init__(
        self,
        app: Callable[[_ASGIScope, _ASGIReceive, _ASGISend], Any],
        *,
        token: str,
        public_paths: Iterable[str] = _PUBLIC_PATHS,
    ) -> None:
        self._app = app
        self._token = token
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
        if self._check_authorization(scope):
            await self._app(scope, receive, send)
            return
        await self._reject(send)

    def _check_authorization(self, scope: _ASGIScope) -> bool:
        """从 ASGI scope 提取 headers 并校验 Bearer token(常量时间)。"""
        token = self._token
        if not token:
            # 配置异常应该在启动时拦截;运行期兜底拒绝。
            return False
        for name, value in _iter_headers(scope):
            if name == b"authorization":
                candidate = _extract_bearer(value)
                # 找到 authorization header:匹配则放行,否则拒绝(不再继续找)。
                return candidate is not None and secrets.compare_digest(candidate, token)
        return False

    @staticmethod
    async def _reject(send: _ASGISend) -> None:
        """返回 401 Unauthorized(不带 WWW-Authenticate,避免明文泄露期望 scheme)。"""
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
    app: Any, *, token: str, public_paths: Iterable[str] = _PUBLIC_PATHS
) -> BearerTokenMiddleware:
    """便捷工厂:把任意 ASGI3 app 包成带 Bearer 鉴权的 app。"""
    return BearerTokenMiddleware(app, token=token, public_paths=public_paths)


__all__ = ["BearerTokenMiddleware", "wrap_with_bearer_auth"]
