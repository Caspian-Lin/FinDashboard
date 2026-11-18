"""本机 Docker 研究版的 Web、REST 和 HTTP MCP 入口。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Route

from finboard_api.errors import finboard_error_handler
from finboard_api.routes import (
    backtest_router,
    data_backfill_router,
    data_catalog_router,
    data_router,
    data_scopes_router,
    instruments_router,
    jobs_router,
    portfolio_router,
    research_docs_router,
    research_memories_router,
    research_router,
    research_runs_router,
    strategy_presets_router,
    strategy_specs_router,
    watchlist_router,
)
from finboard_api.routes.research_audit import router as research_audit_router
from finboard_api.routes.research_diagnostics import router as research_diagnostics_router
from finboard_api.routes.research_topic_actions import topic_actions_router
from finboard_api.routes.research_topic_runs import run_links_router, topic_runs_router
from finboard_api.routes.research_topics import explanation_router
from finboard_api.routes.research_topics import router as research_topics_router
from finboard_app.config import load_settings
from finboard_app.logging import setup_logging
from finboard_app.research_audit import (
    ResearchAuditRecorder,
    configure_research_audit_recorder,
    reset_research_audit_recorder,
)
from finboard_data.trading_calendar import install_calendar_store
from finboard_mcp.auth import DatabaseIdentityResolver, wrap_with_bearer_auth
from finboard_mcp.scopes import arm_scope_enforcement
from finboard_mcp.server import build_mcp_server
from finboard_persistence import PgTradingCalendarStore, create_async_engine, session_factory
from finboard_shared.exceptions import FinboardError


def create_app() -> FastAPI:
    settings = load_settings()
    web = Path(os.environ["FINBOARD_RESEARCH_WEB_ROOT"]).resolve(strict=True)
    engine = create_async_engine(settings.db_url)
    sessions = session_factory(engine)
    # 单机分发通过 Compose 的 loopback 端口进入。独立 MCP 入口仍走原远程门禁。
    mcp = build_mcp_server().streamable_http_app(json_response=True, stateless_http=True)
    authenticated_mcp = wrap_with_bearer_auth(
        mcp, resolver=DatabaseIdentityResolver(sessions), public_paths=(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings)
        install_calendar_store(PgTradingCalendarStore(sessions))
        configure_research_audit_recorder(ResearchAuditRecorder(session_maker=sessions))
        arm_scope_enforcement(readonly_only=settings.mcp_readonly_only, default_identity=None)
        try:
            async with mcp.router.lifespan_context(mcp):
                yield
        finally:
            install_calendar_store(None)
            reset_research_audit_recorder()
            await engine.dispose()

    app = FastAPI(title="FinDashboard Research", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_maker = sessions
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_exception_handler(FinboardError, finboard_error_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def same_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != f"http://{request.url.netloc}":
            return JSONResponse({"error": "origin_denied"}, status_code=403)
        return await call_next(request)

    for router in (
        backtest_router, data_backfill_router, data_catalog_router, data_router,
        data_scopes_router, instruments_router, jobs_router, portfolio_router,
        research_docs_router, research_memories_router, research_router, research_runs_router,
        strategy_presets_router, strategy_specs_router, watchlist_router, research_audit_router,
        research_diagnostics_router, topic_actions_router, run_links_router, topic_runs_router,
        explanation_router, research_topics_router,
    ):
        app.include_router(router)

    app.router.routes.append(Route("/mcp", endpoint=authenticated_mcp))

    @app.get("/api/research-edition/health")
    async def health() -> dict[str, str | bool]:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "edition": "research",
            "sandbox_enabled": settings.research_sandbox_enabled,
            "tushare_configured": bool(settings.tushare_token),
        }

    @app.get("/{resource:path}", include_in_schema=False)
    async def frontend(resource: str) -> FileResponse:
        if resource.split("/", 1)[0] in {"api", "mcp", "ws"}:
            raise HTTPException(status_code=404)
        path = (web / resource).resolve()
        if not path.is_relative_to(web):
            raise HTTPException(status_code=404)
        if path.is_file():
            return FileResponse(path)
        if Path(resource).suffix:
            raise HTTPException(status_code=404)
        return FileResponse(web / "index.html")

    return app
