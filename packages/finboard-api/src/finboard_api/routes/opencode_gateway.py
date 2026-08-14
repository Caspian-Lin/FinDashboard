"""OpenCode Web 研究工作台网关路由(issue #118 / 重构 #121 / #157)。

FinBoard 网关是 OpenCode Web 的**控制面**:
- ``GET /status``:隔离实例运行状态 + 内嵌 FinBoard MCP server 运行状态(#157);
- ``GET /health``:代理健康探测;
- ``POST /access``:签发访问信息(#157 移除 basic auth 后只有明文 ``web_url``,
  单用户模型,无凭证字段),前端 iframe 跨源嵌入时使用。#121 重构后不绑定
  conversation_id —— OpenCode 自身管理会话。

未启用(``opencode_web_enabled=false``)时所有端点返回 503,前端据此隐藏入口。
红线:不透传 OpenCode 流量、不连接实盘、不暴露未授权端口(127.0.0.1 绑定)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from finboard_api.deps import (
    get_opencode_access_issuer,
    get_opencode_process_manager,
)
from finboard_app.config import Settings
from finboard_opencode import (
    AccessIssuer,
    OpenCodeProcessManager,
    ProcessStatus,
)

router = APIRouter(prefix="/api/opencode", tags=["opencode-gateway"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AccessOut(BaseModel):
    """#157 移除 basic auth 后不再有 username/password 字段(明文 URL 直连)。"""

    web_url: str
    agent_name: str


class McpStatusOut(BaseModel):
    """内嵌 FinBoard MCP server 状态(#157:前端据此显示 MCP 是否已连接)。"""

    #: 是否配置了内嵌启动(opencode_embed_mcp)。
    embedded_configured: bool
    #: 内嵌 server 是否已在运行(lifespan 已启动 uvicorn 后台任务)。
    embedded_running: bool
    #: 实际绑定地址(127.0.0.1 或容器模式自动放宽的 0.0.0.0)。
    host: str | None
    port: int | None
    #: 容器内 opencode 使用的 MCP URL(opencode_mcp_remote_url 渲染进容器配置)。
    remote_url: str
    #: 是否配置了 Bearer token(HTTP 传输强制鉴权)。
    auth: bool


class StatusOut(BaseModel):
    running: bool
    managed: bool
    container_id: str | None
    base_url: str
    healthy: bool | None
    version: str | None
    started_at: datetime | None
    mcp: McpStatusOut | None = None


class HealthOut(BaseModel):
    healthy: bool
    version: str | None = None
    detail: dict[str, Any] | None = None


def _require_manager(
    manager: OpenCodeProcessManager | None,
) -> OpenCodeProcessManager:
    if manager is None:
        raise HTTPException(status_code=503, detail="opencode web gateway disabled")
    return manager


def _mcp_status(request: Request, settings: Settings) -> McpStatusOut:
    """从 app.state / settings 构造内嵌 MCP server 状态快照。"""
    server = getattr(request.app.state, "opencode_mcp_server", None)
    bound_host = getattr(request.app.state, "opencode_mcp_host", None)
    return McpStatusOut(
        embedded_configured=settings.opencode_embed_mcp,
        embedded_running=server is not None,
        host=bound_host,
        port=settings.mcp_port,
        remote_url=settings.opencode_mcp_remote_url,
        auth=bool(settings.mcp_auth_token),
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/status", response_model=StatusOut)
async def opencode_status(
    request: Request,
    manager: OpenCodeProcessManager | None = Depends(get_opencode_process_manager),
) -> StatusOut:
    """隔离实例运行状态 + 内嵌 FinBoard MCP 状态(#157,无敏感字段)。"""
    mgr = _require_manager(manager)
    status: ProcessStatus = await mgr.status()
    settings: Settings = request.app.state.settings
    return StatusOut(
        running=status.running,
        managed=status.managed,
        container_id=status.container_id,
        base_url=status.base_url,
        healthy=status.healthy,
        version=status.version,
        started_at=status.started_at,
        mcp=_mcp_status(request, settings),
    )


@router.get("/health", response_model=HealthOut)
async def opencode_health(
    manager: OpenCodeProcessManager | None = Depends(get_opencode_process_manager),
) -> HealthOut:
    """代理健康探测(不暴露 OpenCode 原始端点给浏览器)。"""
    mgr = _require_manager(manager)
    detail = await mgr.health()
    version: str | None = None
    if isinstance(detail, dict):
        raw_version = detail.get("version")
        if isinstance(raw_version, str):
            version = raw_version
    return HealthOut(
        healthy=detail is not None,
        version=version,
        detail=detail,
    )


@router.post("/access", response_model=AccessOut)
async def opencode_access(
    issuer: AccessIssuer | None = Depends(get_opencode_access_issuer),
) -> AccessOut:
    """签发 OpenCode Web 访问信息(明文 URL,#157 无凭证;#121 后无 conversation 绑定)。

    OpenCode 自身管理会话/历史/恢复;FinBoard 只负责隔离实例的进程托管与
    访问信息签发。前端 iframe 与「新窗口打开」共用同一 ``web_url``。
    """
    if issuer is None:
        raise HTTPException(status_code=503, detail="opencode web gateway disabled")
    info = issuer.issue_default()
    return AccessOut(
        web_url=info.web_url,
        agent_name=info.agent_name,
    )
