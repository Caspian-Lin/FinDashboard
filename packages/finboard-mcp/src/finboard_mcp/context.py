"""MCP server 生命周期上下文与组装根。

MCP server 是独立进程,**不依赖 ``TradingKernel`` / broker**(那是交易域)。
这里只构建研究域所需的依赖:

* ``AsyncEngine`` + ``session_maker`` —— 复用 ``finboard_persistence`` 工厂,
  每个工具调用开独立 ``AsyncSession``;
* ``ResearchAssistant`` —— 复用 ``build_llm_provider``,配置不全时降级 fake;
* ``AuditRecorder`` —— 工具调用审计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finboard_app.config import Settings, load_settings
from finboard_app.llm_factory import build_llm_provider
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.factor_research.provider import LLMProvider
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
    research_assistant: ResearchAssistant
    audit: AuditRecorder
    write_tools_enabled: bool
    engine: AsyncEngine
    provider: LLMProvider
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

    try:
        provider = build_llm_provider(settings)
    except ValueError as exc:
        log.warning("mcp.llm_provider_fallback", error=str(exc))
        provider = FakeLLMProvider()

    context = McpAppContext(
        settings=settings,
        session_maker=smaker,
        research_assistant=ResearchAssistant(provider),
        # #157:mcp_audit_persist=true 时审计记录追加到 mcp_audit_events 表
        #(独立 session + commit,失败只记 warning);默认仅 structlog + 内存副本。
        audit=AuditRecorder(
            session_maker=smaker if settings.mcp_audit_persist else None
        ),
        write_tools_enabled=not settings.mcp_readonly_only,
        engine=engine,
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )

    log.info(
        "mcp.starting",
        readonly_only=settings.mcp_readonly_only,
        audit_persist=settings.mcp_audit_persist,
        provider=provider.provider_name(),
    )
    try:
        yield context
    finally:
        if hasattr(provider, "close"):
            provider.close()
        await engine.dispose()
        log.info("mcp.stopped")


__all__ = ["McpAppContext", "app_context", "app_lifespan"]
