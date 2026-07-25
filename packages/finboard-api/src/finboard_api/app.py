"""FastAPI 应用工厂 + 生命周期管理。

设计要点:
* TradingKernel 与 FastAPI **同进程** —— EventBus 是内存 pub-sub,不跨进程。
* 单 kernel 长驻 —— lifespan 启动时创建 session + kernel,handler 共享。
* WebSocket 在 lifespan 中注册 EventBus → broadcast 桥接。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from finboard_api.errors import finboard_error_handler
from finboard_api.routes import (
    account_router,
    audit_router,
    backtest_router,
    data_router,
    fills_router,
    health_router,
    kill_switch_router,
    orders_router,
    positions_router,
    reconcile_router,
)
from finboard_api.ws import ConnectionManager, setup_event_bridge, teardown_event_bridge
from finboard_app.bootstrap import build_kernel_components
from finboard_app.config import Settings
from finboard_app.logging import setup_logging
from finboard_shared.exceptions import FinboardError
from finboard_shared.types import KillSwitchLevel

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    setup_logging(settings)

    components = build_kernel_components(settings)
    async with components.session_maker() as session:
        kernel = components.new_kernel(session)

        if settings.kill_switch_initial is not KillSwitchLevel.OFF:
            await kernel.activate_kill_switch(
                settings.kill_switch_initial, reason="initial state from config"
            )

        # WebSocket 事件桥接
        manager = ConnectionManager()
        handlers = setup_event_bridge(kernel.event_bus, manager)

        app.state.components = components
        app.state.kernel = kernel
        app.state.session = session
        app.state.account_id = components.account_id
        app.state.ws_manager = manager

        await kernel.start()
        logger.info("api.kernel_started", ready=kernel.ready)

        try:
            yield
        finally:
            teardown_event_bridge(kernel.event_bus, handlers)
            await kernel.stop()
            await session.commit()
            await components.engine.dispose()
            logger.info("api.kernel_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用实例。"""
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="FinDashboard Trading Console",
        description="人工交易控制台 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings

    # CORS — 开发期允许前端 dev server 跨域
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 异常处理
    app.add_exception_handler(FinboardError, finboard_error_handler)  # type: ignore[arg-type]

    # 路由
    app.include_router(health_router)
    app.include_router(account_router)
    app.include_router(positions_router)
    app.include_router(orders_router)
    app.include_router(fills_router)
    app.include_router(kill_switch_router)
    app.include_router(reconcile_router)
    app.include_router(audit_router)
    app.include_router(data_router)
    app.include_router(backtest_router)

    # WebSocket
    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        manager: ConnectionManager = app.state.ws_manager
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    return app
