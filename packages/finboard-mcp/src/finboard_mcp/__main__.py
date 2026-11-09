"""FinBoard MCP server CLI 入口(issue #517 独立研究入口)。

默认以 stdio 传输启动(供本地 host 以子进程方式接入)。设置
``FINBOARD_MCP_TRANSPORT=streamable-http`` 可切换为 HTTP 传输 —— 此时必须至少
有一种服务端凭据(``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 或 ``mcp_clients``
表里的有效客户端),否则具名拒绝启动(防裸奔)。

启动门禁(issue #517):

* ``FINBOARD_MCP_ENABLED=false`` 显式关闭 → 打印具名原因,退出码 2
  (未设置时默认启用);
* HTTP 非 loopback 绑定 → 必须 ``FINBOARD_MCP_ALLOW_REMOTE=true``,并打印
  TLS / 反向代理要求警告;
* HTTP 无凭据(无 env token 且无有效客户端)→ 具名拒绝;
* stdio 默认作用域 ``FINBOARD_MCP_STDIO_SCOPES``(默认 ``research.read``),
  需要写操作必须显式配置(收紧行为变更,PR 已声明)。

独立启动不读取任何 OpenCode 环境变量;仅需 ``FINBOARD_DB_URL``(工具执行用)。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.config import Settings, load_settings
from finboard_mcp.auth import (
    DatabaseIdentityResolver,
    warn_legacy_auth_token,
    wrap_with_bearer_auth,
)
from finboard_mcp.context import build_stdio_identity
from finboard_mcp.server import build_mcp_server
from finboard_persistence import create_async_engine, session_factory
from finboard_persistence.mcp_client_repo import McpClientRepository

_logger = structlog.get_logger("finboard.mcp.startup")

_ASGIScope = dict[str, Any]
_ASGIReceive = Callable[[], Awaitable[object]]
_ASGISend = Callable[[dict[str, Any]], Awaitable[None]]

#: 退出码:配置拒绝(与 Python 未捕获异常 / uvicorn 启动失败区分)。
_EXIT_CONFIG = 2


def main() -> None:
    _configure_windows_asyncio()
    settings = load_settings()
    if not settings.mcp_enabled:
        _exit_named(
            "FINBOARD_MCP_ENABLED=false:FinBoard MCP server 未启用。"
            "设置 FINBOARD_MCP_ENABLED=true 后重试。"
        )
    mcp = build_mcp_server()
    # stdio 传输:本机子进程接入,无 HTTP 请求,身份由配置的默认作用域决定。
    if settings.mcp_transport == "stdio":
        identity = build_stdio_identity(settings.mcp_stdio_scopes)
        _logger.warning(
            "mcp.stdio_scopes",
            scopes=sorted(identity.scopes),
            message=(
                "stdio 不再等价全权:默认作用域已收紧,写操作需显式设置 "
                "FINBOARD_MCP_STDIO_SCOPES(如 research.read,research.write,research.run)"
            ),
        )
        mcp.run(transport="stdio")
        return
    _run_http(mcp, settings)


def _configure_windows_asyncio() -> None:
    """Windows 上 psycopg 异步连接要求 SelectorEventLoop(与 finboard CLI 一致)。"""
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run_http(mcp: Any, settings: Settings) -> None:
    """以 HTTP 传输启动,在外层包裹按客户端凭据鉴权中间件后交给 uvicorn。"""
    transport = settings.mcp_transport
    legacy_token = settings.mcp_auth_token
    host = settings.mcp_host
    port = settings.mcp_port

    # 网络边界:非 loopback 绑定必须显式放行,并提示 TLS / 反向代理要求。
    if not _is_loopback_host(host):
        if not settings.mcp_allow_remote:
            _exit_named(
                f"FINBOARD_MCP_HOST={host} 是非 loopback 绑定,已拒绝启动。"
                "外部访问必须显式设置 FINBOARD_MCP_ALLOW_REMOTE=true,并在前置 "
                "TLS 反向代理后暴露;stdio / 127.0.0.1 不需要该开关。"
            )
        _logger.warning(
            "mcp.remote_bind",
            host=host,
            port=port,
            message=(
                "MCP 绑定非 loopback:必须置于 TLS 反向代理之后,并限制来源网络;"
                "MCP 自身不终结 TLS,也不提供多用户隔离。"
            ),
        )
    if legacy_token:
        warn_legacy_auth_token()

    engine = create_async_engine(
        settings.db_url,
        pool_size=1,
        max_overflow=2,
    )
    smaker = session_factory(engine)
    try:
        if not legacy_token and not asyncio.run(
            _has_active_clients(settings.db_url)
        ):
            _exit_named(
                "HTTP 传输没有可用凭据:FINBOARD_MCP_AUTH_TOKEN 未设置,且 "
                "mcp_clients 表中没有未吊销/未过期的客户端。先运行 "
                "`finboard mcp client create --name <n> --preset readonly`,"
                "或设置 FINBOARD_MCP_AUTH_TOKEN(旧共享 token,已弃用)。"
            )
        if transport == "streamable-http":
            starlette_app = mcp.streamable_http_app(host=host)
        elif transport == "sse":
            starlette_app = mcp.sse_app(host=host)
        else:  # pragma: no cover — Settings Literal 已限定
            _exit_named(f"未支持的 mcp_transport: {transport}")
        resolver = DatabaseIdentityResolver(smaker)
        wrapped = wrap_with_bearer_auth(
            starlette_app, token=legacy_token, resolver=resolver
        )
        app = _dispose_engine_on_shutdown(wrapped, engine)
        _logger.info(
            "mcp.http_starting",
            host=host,
            port=port,
            transport=transport,
            legacy_token=bool(legacy_token),
        )
        uvicorn.run(app, host=host, port=port)
    finally:
        # uvicorn 正常返回时事件循环已关闭;dispose 由 lifespan 包装兜底,
        # 这里只在从未进入 lifespan(启动即失败)时安全回收。
        _dispose_if_unused(engine)


async def _has_active_clients(db_url: str) -> bool:
    """用一次性引擎做启动凭据检查(不把连接池带到 uvicorn 的事件循环)。"""
    check_engine = create_async_engine(db_url, pool_size=1, max_overflow=0)
    try:
        async with session_factory(check_engine)() as session:
            return await McpClientRepository(session).count_active() > 0
    finally:
        await check_engine.dispose()


def _dispose_engine_on_shutdown(app: Any, engine: AsyncEngine) -> Any:
    """ASGI 包装:在 ``lifespan.shutdown.complete`` 后释放鉴权引擎连接池。"""

    async def _wrapped(
        scope: _ASGIScope, receive: _ASGIReceive, send: _ASGISend
    ) -> None:
        if scope.get("type") != "lifespan":
            await app(scope, receive, send)
            return

        async def _send(message: dict[str, Any]) -> None:
            await send(message)
            if message.get("type") == "lifespan.shutdown.complete":
                await engine.dispose()

        await app(scope, receive, _send)

    return _wrapped


def _dispose_if_unused(engine: AsyncEngine) -> None:
    """启动失败时同步回收引擎(uvicorn 未跑起来 → 没有绑定旧事件循环的连接)。"""
    with contextlib.suppress(Exception):
        asyncio.run(engine.dispose())


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return (
        normalized in {"localhost", "::1"}
        or normalized == "127.0.0.1"
        or normalized.startswith("127.")
    )


def _exit_named(message: str) -> None:
    """打印具名原因并以退出码 2 结束(配置拒绝,不是崩溃)。"""
    print(f"[finboard-mcp] 启动被拒绝:{message}", file=sys.stderr)
    raise SystemExit(_EXIT_CONFIG)


if __name__ == "__main__":
    main()
