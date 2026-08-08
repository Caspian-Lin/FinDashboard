"""OpenCode Web 研究工作台网关路由(issue #118)。

FinBoard 网关是 OpenCode Web 的**控制面**:
- ``GET /status``:隔离实例运行状态(脱敏,不含密码);
- ``GET /health``:代理健康探测;
- ``POST /access``:为已授权会话签发访问凭证(OpenCode Web URL + basic auth),
  前端 iframe 跨源嵌入时使用。

未启用(``opencode_web_enabled=false``)时所有端点返回 503,前端据此隐藏入口。
红线:不透传 OpenCode 流量、不连接实盘、不暴露未授权端口。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from finboard_api.deps import (
    get_conversation_service,
    get_opencode_access_issuer,
    get_opencode_process_manager,
)
from finboard_opencode import (
    AccessCredentialIssuer,
    AccessNotAuthorizedError,
    ConversationNotFoundError,
    ConversationService,
    OpenCodeProcessManager,
    ProcessStatus,
)

router = APIRouter(prefix="/api/opencode", tags=["opencode-gateway"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AccessRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=32)


class AccessOut(BaseModel):
    conversation_id: str
    opencode_session_id: str
    web_url: str
    username: str
    password: str
    agent_name: str


class StatusOut(BaseModel):
    running: bool
    managed: bool
    pid: int | None
    base_url: str
    healthy: bool | None
    version: str | None
    started_at: datetime | None


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


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/status", response_model=StatusOut)
async def opencode_status(
    manager: OpenCodeProcessManager | None = Depends(get_opencode_process_manager),
) -> StatusOut:
    """隔离实例运行状态(脱敏:不含密码)。"""
    mgr = _require_manager(manager)
    status: ProcessStatus = await mgr.status()
    return StatusOut(
        running=status.running,
        managed=status.managed,
        pid=status.pid,
        base_url=status.base_url,
        healthy=status.healthy,
        version=status.version,
        started_at=status.started_at,
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
    body: AccessRequest,
    issuer: AccessCredentialIssuer | None = Depends(get_opencode_access_issuer),
    service: ConversationService = Depends(get_conversation_service),
) -> AccessOut:
    """为已授权会话签发 OpenCode Web 访问凭证。

    流程:查 conversation → 授权校验(存在 + ACTIVE) → 签发凭证。
    单用户场景下"归属"退化为存在性 + 状态;多用户需叠加 owner 校验。
    """
    if issuer is None:
        raise HTTPException(status_code=503, detail="opencode web gateway disabled")
    try:
        record = await service.get_conversation(body.conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        info = issuer.issue(record, conversation_id=body.conversation_id)
    except AccessNotAuthorizedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return AccessOut(
        conversation_id=info.conversation_id,
        opencode_session_id=info.opencode_session_id,
        web_url=info.web_url,
        username=info.username,
        password=info.password,
        agent_name=info.agent_name,
    )
