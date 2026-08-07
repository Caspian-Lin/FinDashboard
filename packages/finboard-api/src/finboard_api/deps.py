"""FastAPI 依赖注入。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.bootstrap import KernelComponents
from finboard_backtest.factor_research import ResearchAssistant
from finboard_core import TradingKernel
from finboard_opencode import (
    AgentConversationRepository,
    AgentEventRepository,
    ConversationService,
    OpenCodeRuntimeClient,
)
from finboard_shared.identifiers import AccountId


def get_kernel(request: Request) -> TradingKernel:
    return request.app.state.kernel  # type: ignore[no-any-return]


def get_session(request: Request) -> AsyncSession:
    """kernel 共享 session —— 仅用于交易域路由(orders/fills/positions/reconcile)。

    这些路由与 kernel 事件消费者共享同一 session,commit 由各路由显式执行。
    """
    return request.app.state.session  # type: ignore[no-any-return]


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """每请求独立的 session —— 用于数据域路由(instruments/watchlist/backtest)。

    这些表与 kernel 交易域无关,独立 session 避免并发请求竞争同一 AsyncSession。
    """
    smaker = request.app.state.session_maker
    async with smaker() as session:
        yield session


def get_account_id(request: Request) -> AccountId:
    return request.app.state.account_id  # type: ignore[no-any-return]


def get_research_assistant(request: Request) -> ResearchAssistant:
    """AI 研究助手(issue #84)——lifespan 构建的 ResearchAssistant 单例。"""
    return request.app.state.research_assistant  # type: ignore[no-any-return]


def get_components(request: Request) -> KernelComponents:
    return request.app.state.components  # type: ignore[no-any-return]


def get_opencode_runtime(request: Request) -> OpenCodeRuntimeClient | None:
    """OpenCode 运行时客户端(issue #109)——lifespan 构建的单例。

    未启用时返回 ``None``(路由据此返回 503)。
    """
    return getattr(request.app.state, "opencode_runtime", None)


async def get_conversation_service(
    request: Request,
) -> AsyncIterator[ConversationService]:
    """每请求构建 :class:`ConversationService`,共享 runtime 单例。

    管理 DB session 生命周期:请求结束后 commit。
    """
    runtime: OpenCodeRuntimeClient | None = getattr(
        request.app.state, "opencode_runtime", None
    )
    if runtime is None:
        raise HTTPException(status_code=503, detail="opencode runtime disabled")
    default_agent = getattr(
        request.app.state, "opencode_default_agent", "finboard-researcher"
    )
    smaker = request.app.state.session_maker
    async with smaker() as session:
        yield ConversationService(
            runtime,
            AgentConversationRepository(session),
            AgentEventRepository(session),
            default_agent=default_agent,
        )
        await session.commit()
