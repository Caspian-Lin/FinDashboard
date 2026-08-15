"""FastAPI 应用工厂 + 生命周期管理。

设计要点:
* TradingKernel 与 FastAPI **同进程** —— EventBus 是内存 pub-sub,不跨进程。
* 单 kernel 长驻 —— lifespan 启动时创建 session + kernel,handler 共享。
* WebSocket 在 lifespan 中注册 EventBus → broadcast 桥接。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import structlog
import uvicorn
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
    instruments_router,
    jobs_router,
    kill_switch_router,
    mcp_audit_router,
    opencode_gateway_router,
    orders_router,
    portfolio_router,
    positions_router,
    reconcile_router,
    research_memories_router,
    research_router,
    research_runs_router,
    simulation_router,
    strategy_presets_router,
    strategy_specs_router,
    watchlist_router,
)
from finboard_api.simulation_ws import SimulationConnectionManager
from finboard_api.ws import ConnectionManager, setup_event_bridge, teardown_event_bridge
from finboard_app.bootstrap import build_kernel_components
from finboard_app.config import Settings
from finboard_app.logging import setup_logging
from finboard_opencode import (
    AccessIssuer,
    OpenCodeProcessConfig,
    OpenCodeProcessError,
    OpenCodeProcessManager,
    OpenCodeRuntimeClient,
)
from finboard_shared.exceptions import FinboardError
from finboard_shared.types import KillSwitchLevel
from finboard_simulation import SimulationRepository, SimulationService

logger = structlog.get_logger(__name__)


async def _start_embedded_mcp_server(
    app: FastAPI, settings: Settings, *, host: str | None = None
) -> None:
    """在当前事件循环后台启动 finboard-mcp HTTP server(uvicorn 后台任务)。

    容器内 opencode 通过 ``host.docker.internal:{mcp_port}`` 访问该 server。
    复用 ``build_mcp_server()``(研究域独立 lifespan + 独立 AsyncEngine,与
    TradingKernel 无 session 冲突)+ Bearer 鉴权(``mcp_auth_token``)。

    ``host`` 显式覆盖 ``settings.mcp_host``(#157):web 容器模式下容器经
    host.docker.internal 访问宿主机,服务绑 127.0.0.1 时不可达,调用方会在
    默认值时自动改绑 0.0.0.0(Bearer token 保护)。

    失败降级:无 ``mcp_auth_token`` → 跳过(HTTP 传输强制鉴权,空 token 拒绝启动);
    端口已被占用(上次进程残留)→ 后台任务捕获 ``SystemExit``/异常,记 warning,
    不炸 API(容器内 opencode 连不上 MCP 会降级为内置工具)。
    """
    bind_host = host if host is not None else settings.mcp_host
    if not settings.mcp_auth_token:
        logger.warning("api.opencode_mcp_skipped", reason="no_mcp_auth_token")
        return
    from finboard_mcp.auth import wrap_with_bearer_auth
    from finboard_mcp.server import build_mcp_server

    mcp = build_mcp_server()
    starlette_app = mcp.streamable_http_app(host=bind_host)
    wrapped = wrap_with_bearer_auth(starlette_app, token=settings.mcp_auth_token)
    config = uvicorn.Config(
        wrapped,
        host=bind_host,
        port=settings.mcp_port,
        log_level="warning",
        loop="none",  # 复用当前 SelectorEventLoop(避免 Windows Proactor 冲突)
    )
    server = uvicorn.Server(config)
    # 阻止 uvicorn 安装信号处理器(它会 sys.exit,在后台任务里会炸 API)。
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

    async def _serve_with_guard() -> None:
        """``serve()`` 的安全包装:捕获 bind 失败的 SystemExit/OSError。

        uvicorn ``Server.startup()`` 在端口 bind 失败时调 ``sys.exit(STARTUP_FAILURE)``
        (= ``SystemExit(3)``)。在后台任务里未捕获会变成「Task exception was never
        retrieved」+ 可能影响事件循环。这里捕获后记 warning,API 继续运行。
        """
        try:
            await server.serve()
        except (SystemExit, OSError) as exc:
            logger.warning(
                "api.opencode_mcp_bind_failed",
                port=settings.mcp_port,
                error=str(exc),
            )

    app.state.opencode_mcp_server = server
    app.state.opencode_mcp_host = bind_host
    app.state.opencode_mcp_task = asyncio.create_task(_serve_with_guard())
    logger.info(
        "api.opencode_mcp_started",
        host=bind_host,
        port=settings.mcp_port,
    )


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
        app.state.session = session  # kernel 专用长生命周期 session
        app.state.session_maker = components.session_maker  # HTTP 请求每请求独立 session
        app.state.account_id = components.account_id
        app.state.ws_manager = manager
        app.state.simulation_ws_manager = SimulationConnectionManager()

        # OpenCode 研究运行时(issue #109 / #118 / Docker 隔离;#157 移除 basic auth)。
        # 两种部署形态:
        #   (A) web 容器模式(opencode_web_enabled):FinBoard 托管一个 Docker 化的
        #       ``opencode web`` 容器(4097),它**同时**暴露 iframe UI 和 /api/* API。
        #       runtime client 复用该容器(连 4097,无 auth),不再需要独立的
        #       ``opencode serve``(4096)。一个容器服务 iframe 嵌入 + 会话关联 API。
        #   (B) 外部 serve 模式(仅 opencode_enabled,向后兼容):连接外部已启动的
        #       ``opencode serve``(默认 4096,无 auth)。
        # 单用户模型下 OpenCode Web 不做 basic auth(用户决策 #157),127.0.0.1
        # 宿主机侧绑定是唯一网络边界。
        app.state.opencode_runtime = None
        app.state.opencode_process_manager = None
        app.state.opencode_access_issuer = None

        if settings.opencode_web_enabled:
            web_config = OpenCodeProcessConfig(
                image=settings.opencode_image,
                container_name=settings.opencode_container_name,
                port=settings.opencode_web_port,
                hostname=settings.opencode_web_hostname,
                cors_origins=settings.opencode_web_cors_origin_list(),
                # workdir = 仓库根(含 .opencode / .agents),bind mount 进容器。
                # 默认相对 CWD(make dev 在仓库根运行),解析成绝对路径给 docker -v。
                workdir=await asyncio.to_thread(
                    lambda: str(Path(settings.opencode_workdir).resolve())
                ),
                log_path=settings.opencode_log_path,
                env_overrides=settings.opencode_env_override_map(),
                mcp_auth_token=settings.mcp_auth_token,
                # #157:容器内 MCP 地址由该配置渲染进运行时 opencode.json。
                mcp_remote_url=settings.opencode_mcp_remote_url,
            )
            process_manager = OpenCodeProcessManager(
                web_config, manage_process=settings.opencode_manage_process
            )
            web_ready = True
            if settings.opencode_manage_process:
                try:
                    await process_manager.start()
                    await process_manager.wait_ready()
                except OpenCodeProcessError as exc:
                    logger.error(
                        "api.opencode_web_start_failed", error=str(exc)
                    )
                    await process_manager.stop()
                    web_ready = False
            else:
                logger.info(
                    "api.opencode_web_unmanaged", base_url=web_config.base_url
                )
            if web_ready:
                app.state.opencode_process_manager = process_manager
                app.state.opencode_access_issuer = AccessIssuer(
                    process_manager, agent_name=settings.opencode_default_agent
                )
                # runtime client 复用 web 容器:base_url=4097,无 auth(#157)。
                app.state.opencode_runtime = OpenCodeRuntimeClient(
                    base_url=process_manager.base_url,
                    api_prefix=settings.opencode_api_prefix,
                    timeout=settings.opencode_request_timeout_seconds,
                )
                logger.info(
                    "api.opencode_web_ready",
                    base_url=web_config.base_url,
                    managed=settings.opencode_manage_process,
                )
                logger.info(
                    "api.opencode_runtime_started",
                    base_url=process_manager.base_url,
                    agent=settings.opencode_default_agent,
                    mode="web_container",
                    auth=False,
                )
            else:
                logger.warning(
                    "api.opencode_runtime_skipped", reason="web_container_not_ready"
                )
            # 内嵌启动 finboard-mcp HTTP server:容器内 opencode 通过
            # ``host.docker.internal:{mcp_port}`` 访问它。复用 ``build_mcp_server()``
            # + Bearer 鉴权(``mcp_auth_token``),以 uvicorn 后台任务跑在当前事件循环。
            # 这样用户无需单独跑 ``python -m finboard_mcp``;设 ``opencode_embed_mcp``
            # =False 可回退到独立进程模式。
            # #157:容器经 host.docker.internal 跨网络访问宿主机,MCP 服务绑 127.0.0.1
            # 时不可达 —— ``mcp_host`` 为默认值时自动改绑 0.0.0.0(Bearer token 保护);
            # 用户显式配置了其它地址则尊重配置。
            if settings.opencode_embed_mcp:
                mcp_bind_host = settings.mcp_host
                if settings.opencode_web_enabled and mcp_bind_host == "127.0.0.1":
                    mcp_bind_host = "0.0.0.0"
                    logger.info(
                        "api.opencode_mcp_host_widened",
                        reason="container_needs_host_docker_internal",
                        host=mcp_bind_host,
                        port=settings.mcp_port,
                    )
                await _start_embedded_mcp_server(app, settings, host=mcp_bind_host)
        elif settings.opencode_enabled:
            # 形态 (B):外部 opencode serve(4096,无 auth),向后兼容。
            app.state.opencode_runtime = OpenCodeRuntimeClient(
                base_url=settings.opencode_base_url,
                api_prefix=settings.opencode_api_prefix,
                timeout=settings.opencode_request_timeout_seconds,
            )
            logger.info(
                "api.opencode_runtime_started",
                base_url=settings.opencode_base_url,
                agent=settings.opencode_default_agent,
                mode="external_serve",
                auth=False,
            )

        await kernel.start()
        logger.info("api.kernel_started", ready=kernel.ready)
        async with components.session_maker() as simulation_session:
            recovered = await SimulationService(
                SimulationRepository(simulation_session)
            ).recover_active_sessions()
            await simulation_session.commit()
        logger.info(
            "api.simulation_recovered",
            recovered_sessions=len(recovered),
        )

        try:
            yield
        finally:
            teardown_event_bridge(kernel.event_bus, handlers)
            await kernel.stop()
            await session.commit()
            await components.engine.dispose()
            opencode_runtime = getattr(app.state, "opencode_runtime", None)
            if opencode_runtime is not None:
                await opencode_runtime.aclose()
            oc_process_manager = getattr(app.state, "opencode_process_manager", None)
            if oc_process_manager is not None:
                await oc_process_manager.stop()
            # 回收内嵌 finboard-mcp HTTP server(uvicorn 后台任务)。
            mcp_server = getattr(app.state, "opencode_mcp_server", None)
            if mcp_server is not None:
                mcp_server.should_exit = True
                # bind 失败(端口被占)的 server 未完成 startup,无 servers 属性,
                # shutdown() 会 AttributeError —— 只对真正启动过的 server 收尾。
                if getattr(mcp_server, "started", False):
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(mcp_server.shutdown(), timeout=5.0)
                mcp_task = getattr(app.state, "opencode_mcp_task", None)
                if isinstance(mcp_task, asyncio.Task) and not mcp_task.done():
                    mcp_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await mcp_task
                logger.info("api.opencode_mcp_stopped")
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
    app.include_router(watchlist_router)
    app.include_router(strategy_presets_router)
    app.include_router(strategy_specs_router)
    app.include_router(research_router)
    app.include_router(research_memories_router)
    app.include_router(research_runs_router)
    app.include_router(jobs_router)
    app.include_router(mcp_audit_router)
    app.include_router(opencode_gateway_router)
    app.include_router(instruments_router)
    app.include_router(portfolio_router)
    app.include_router(simulation_router)

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

    @app.websocket("/ws/simulation/{session_id}")
    async def ws_simulation(websocket: WebSocket, session_id: str) -> None:
        if not session_id.startswith("SIM-S-"):
            await websocket.close(code=1008, reason="simulation session id required")
            return
        simulation_manager: SimulationConnectionManager = (
            app.state.simulation_ws_manager
        )
        await simulation_manager.connect(session_id, websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await simulation_manager.disconnect(session_id, websocket)

    return app
