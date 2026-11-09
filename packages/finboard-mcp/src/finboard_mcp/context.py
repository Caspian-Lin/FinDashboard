"""MCP server 生命周期上下文与组装根。

MCP server 是独立进程,**不依赖 ``TradingKernel`` / broker**(那是交易域)。
这里只构建研究域所需的依赖:

* ``AsyncEngine`` + ``session_maker`` —— 复用 ``finboard_persistence`` 工厂,
  每个工具调用开独立 ``AsyncSession``;
* ``AuditRecorder`` —— 工具调用审计;
* 进程级作用域策略(#517) —— 启动时装配 ``readonly_only`` 与 stdio 默认身份。

身份如何到达工具(issue #517):HTTP 请求的身份由
:mod:`finboard_mcp.auth` 中间件写入 ``scope["state"]["mcp_identity"]``;每个
工具函数入口都调用 :func:`app_context`,它从 MCP ``request_context.request``
取出该身份并写入 ``contextvars``,``run_tool`` 在唯一漏斗读取。stdio / 进程内
调用没有 HTTP 请求,回退到 ``FINBOARD_MCP_STDIO_SCOPES`` 默认身份。

Issue #160 起 FinBoard 不再内置任何 LLM 调用(``ResearchAssistant`` 退役),
AI 能力由 OpenCode 研究运行时承担,MCP 只暴露研究/数据域工具。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finboard_app.config import Settings, load_settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.auth import warn_legacy_auth_token
from finboard_mcp.identity import (
    STDIO_CLIENT_ID,
    McpClientIdentity,
    current_identity,
    set_current_identity,
)
from finboard_mcp.scopes import (
    arm_scope_enforcement_preserving_identity,
    parse_scopes,
    require_scope_enforcement_armed,
    reset_scope_policy,
    scope_policy,
)
from finboard_persistence import create_async_engine, session_factory

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.context import Context


def build_stdio_identity(scopes_raw: str) -> McpClientIdentity:
    """构造 stdio 本机子进程身份(默认 ``research.read``,需要写时显式配置)。"""
    scopes = parse_scopes(scopes_raw or "research.read")
    return McpClientIdentity(
        client_id=STDIO_CLIENT_ID,
        name="stdio",
        scopes=frozenset(scopes),
    )


@dataclass
class McpAppContext:
    """lifespan 持有的进程级依赖集合。"""

    settings: Settings
    session_maker: async_sessionmaker[AsyncSession]
    audit: AuditRecorder
    write_tools_enabled: bool
    engine: AsyncEngine
    feature_snapshot_jobs: FeatureSnapshotJobManager

    @property
    def identity(self) -> McpClientIdentity | None:
        """当前工具调用的客户端身份(#517;未解析返回 ``None``)。"""
        return current_identity() or scope_policy().default_identity


def _identity_from_request(ctx: Context) -> McpClientIdentity | None:
    """从 HTTP 请求的 ``scope["state"]`` 读取 ASGI 中间件写入的身份。"""
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return None
    state = getattr(request, "state", None)
    if state is None:
        return None
    identity = getattr(state, "mcp_identity", None)
    return identity if isinstance(identity, McpClientIdentity) else None


def app_context(ctx: Context) -> McpAppContext:
    """从 MCP ``Context`` 取出 lifespan 组装的依赖,并绑定本次调用的身份。

    每次工具调用都会执行(148 个工具全部经此入口),因此 contextvar 总是被
    覆盖,不会残留上一次调用的身份:

    * HTTP 请求(``request_context.request`` 存在):只信任中间件写入
      ``request.state.mcp_identity`` 的身份;缺失即写 ``None`` → 作用域检查
      直接拒绝(fail-closed,不回退进程默认身份);
    * stdio / 进程内(``request is None``):使用进程级默认身份
      (``FINBOARD_MCP_STDIO_SCOPES``),未配置则 ``None`` → 拒绝。
    """
    app = cast(McpAppContext, ctx.request_context.lifespan_context)
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        identity = _identity_from_request(ctx)
    else:
        identity = scope_policy().default_identity
    # 不 reset:身份在工具调用任务内有效,下一调用会再次覆盖;HTTP session
    # 任务与客户端一一对应,stdio / 进程内为固定默认身份。
    set_current_identity(identity)
    return app


@asynccontextmanager
async def app_lifespan(_server: MCPServer) -> AsyncIterator[McpAppContext]:
    settings = load_settings()
    log = structlog.get_logger("finboard.mcp.lifespan")

    # #517:启动即装配作用域强制。默认身份(stdio)由 `__main__` 在启动前设置;
    # lifespan 只补 readonly 开关并保留它,绝不从 FINBOARD_MCP_STDIO_SCOPES
    # 推导 HTTP 默认身份(HTTP 无身份必须拒绝)。未装配状态只用于直接调用
    # helper 的单元测试。
    arm_scope_enforcement_preserving_identity(
        readonly_only=settings.mcp_readonly_only
    )
    require_scope_enforcement_armed()
    if settings.mcp_readonly_only:
        log.warning(
            "mcp.readonly_only_deprecated",
            message=(
                "FINBOARD_MCP_READONLY_ONLY 已弃用:等价于所有客户端只保留 "
                "research.read 作用域,下一版本移除;请改用 `finboard mcp client "
                "create --preset readonly`。"
            ),
        )
    if settings.mcp_transport != "stdio" and settings.mcp_auth_token:
        warn_legacy_auth_token()

    engine = create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    smaker = session_factory(engine)

    context = McpAppContext(
        settings=settings,
        session_maker=smaker,
        # #157:mcp_audit_persist=true 时审计记录追加到 mcp_audit_events 表
        #(独立 session + commit,失败只记 warning);默认仅 structlog + 内存副本。
        audit=AuditRecorder(
            session_maker=smaker if settings.mcp_audit_persist else None
        ),
        write_tools_enabled=not settings.mcp_readonly_only,
        engine=engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )

    default_identity = scope_policy().default_identity
    log.info(
        "mcp.starting",
        readonly_only=settings.mcp_readonly_only,
        audit_persist=settings.mcp_audit_persist,
        transport=settings.mcp_transport,
        default_identity=(
            default_identity.client_id if default_identity is not None else None
        ),
    )
    try:
        yield context
    finally:
        await engine.dispose()
        reset_scope_policy()
        log.info("mcp.stopped")


__all__ = [
    "McpAppContext",
    "app_context",
    "app_lifespan",
    "build_stdio_identity",
]
