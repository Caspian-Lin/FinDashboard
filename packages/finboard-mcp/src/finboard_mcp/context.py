"""MCP server 生命周期上下文与组装根。

MCP server 是独立进程,**不依赖 ``TradingKernel`` / broker**(那是交易域)。
这里只构建研究域所需的依赖:

* ``AsyncEngine`` + ``session_maker`` —— 复用 ``finboard_persistence`` 工厂,
  每个工具调用开独立 ``AsyncSession``;
* ``AuditRecorder`` —— 工具调用审计。

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
from finboard_persistence import create_async_engine, session_factory

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.context import Context


@dataclass
class McpAppContext:
    """lifespan 持有的进程级依赖集合。"""

    settings: Settings
    session_maker: async_sessionmaker[AsyncSession]
    audit: AuditRecorder
    write_tools_enabled: bool
    engine: AsyncEngine
    feature_snapshot_jobs: FeatureSnapshotJobManager


def app_context(ctx: Context) -> McpAppContext:
    """从 MCP ``Context`` 取出 lifespan 组装的依赖集合。"""
    return cast(McpAppContext, ctx.request_context.lifespan_context)


@asynccontextmanager
async def app_lifespan(_server: MCPServer) -> AsyncIterator[McpAppContext]:
    settings = load_settings()
    log = structlog.get_logger("finboard.mcp.lifespan")

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

    log.info(
        "mcp.starting",
        readonly_only=settings.mcp_readonly_only,
        audit_persist=settings.mcp_audit_persist,
    )
    try:
        yield context
    finally:
        await engine.dispose()
        log.info("mcp.stopped")


__all__ = ["McpAppContext", "app_context", "app_lifespan"]
