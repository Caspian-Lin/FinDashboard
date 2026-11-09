"""研究操作审计读取 API(issue #518)。

三个只读端点 + 一个默认关闭的客户端声明写入端点:

* ``GET /api/research/audit`` —— 有界筛选 + ``(occurred_at, id)`` 倒序游标;
* ``GET /api/research/audit/{audit_id}`` —— 单条(只返回脱敏摘要与 digest);
* ``GET /api/research/audit/coverage`` —— 历史覆盖声明(迁移前无记录如实标示);
* ``POST /api/research/audit/client-events`` —— 外部 Harness 上报,
  ``trust=client_declared``,**默认关闭**(``FINBOARD_RESEARCH_AUDIT_CLIENT_EVENTS``)。

不返回原始参数(从未入库);不触碰实盘 ``audit_logs``。
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_app.research_audit import MAX_VALUE_CHARS, sanitize_params
from finboard_app.research_operation import ResearchOperationContext
from finboard_mcp.identity import McpClientIdentity, hash_client_token
from finboard_persistence import McpClientRepository
from finboard_persistence.research_audit_repo import (
    MAX_AUDIT_PAGE,
    ResearchAuditRepository,
)

logger = structlog.get_logger("finboard.api.research_audit")

router = APIRouter(prefix="/api/research/audit", tags=["research-audit"])

#: 历史覆盖缺口(迁移/接入前根本没有行,读取方必须显示「此前无记录」)。
_COVERAGE_GAPS: tuple[str, ...] = (
    "research_writes_before_518",
    "mcp_tool_calls_before_518",
    "client_declared_events_not_backfilled",
)

Outcome = Literal["succeeded", "failed", "rejected", "denied"]


class ResearchAuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    audit_id: str
    occurred_at: datetime
    actor: str
    actor_type: str
    source: str
    client_id: str | None
    topic_id: str | None
    entry_id: str | None
    operation: str
    entity_type: str | None
    entity_id: str | None
    outcome: str
    result_code: str | None
    correlation: dict[str, Any]
    params_summary: dict[str, Any]
    params_digest: str
    error_summary: str | None
    idempotency_key: str | None
    trust: str


class ResearchAuditPageOut(BaseModel):
    items: list[ResearchAuditEventOut]
    limit: int
    has_more: bool
    next_cursor: str | None


class ResearchAuditCoverageOut(BaseModel):
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    total: int
    server_observed_from: datetime | None = None
    client_declared_from: datetime | None = None
    gaps: list[str]
    retention_days: int
    auto_purge: bool = False
    note: str = (
        "迁移/接入之前的写操作没有审计行;读取结果为空表示「此前无审计记录」,"
        "不代表没有发生操作。"
    )

    model_config = ConfigDict(populate_by_name=True)


class ResearchClientEventIn(BaseModel):
    """客户端声明事件的白名单字段(未知字段直接 422)。"""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["tool_call", "external_call", "run"] = "tool_call"
    client_event_id: str = Field(min_length=1, max_length=128)
    tool_name: str | None = Field(default=None, max_length=128)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    outcome: Literal["ok", "error", "denied", "rejected"] = "ok"
    correlation: dict[str, str] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=2048)


class ResearchClientEventOut(BaseModel):
    audit_id: str
    created: bool
    trust: Literal["client_declared"] = "client_declared"
    deduplicated: bool


def _encode_cursor(occurred_at: datetime, row_id: int) -> str:
    payload = json.dumps({"t": occurred_at.isoformat(), "id": row_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(value: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        decoded = json.loads(raw)
        occurred_at = datetime.fromisoformat(str(decoded["t"]))
        row_id = int(decoded["id"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="cursor 非法") from exc
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return occurred_at, row_id


@router.get("/coverage", response_model=ResearchAuditCoverageOut)
async def audit_coverage(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchAuditCoverageOut:
    """历史覆盖声明:最早/最晚记录、按信任级别的最早记录与已知缺口。"""

    coverage = await ResearchAuditRepository(session).coverage()
    settings = getattr(request.app.state, "settings", None)
    retention_days = int(getattr(settings, "research_audit_retention_days", 365))
    return ResearchAuditCoverageOut(
        **{"from": coverage["from"]},
        to=coverage["to"],
        total=coverage["total"],
        server_observed_from=coverage["server_observed_from"],
        client_declared_from=coverage["client_declared_from"],
        gaps=list(_COVERAGE_GAPS),
        retention_days=retention_days,
    )


@router.get("", response_model=ResearchAuditPageOut)
async def list_audit(
    actor: str | None = Query(default=None, max_length=128),
    client_id: str | None = Query(default=None, max_length=32),
    source: Literal["web", "mcp", "worker", "system"] | None = None,
    topic_id: str | None = Query(default=None, max_length=32),
    entry_id: str | None = Query(default=None, max_length=32),
    entity_id: str | None = Query(default=None, max_length=64),
    operation: str | None = Query(default=None, max_length=64),
    outcome: Outcome | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=MAX_AUDIT_PAGE),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchAuditPageOut:
    """有界筛选 + ``(occurred_at, id)`` 倒序游标(只返回脱敏摘要与 digest)。"""

    repo = ResearchAuditRepository(session)
    rows = await repo.list_events(
        actor=actor,
        client_id=client_id,
        source=source,
        topic_id=topic_id,
        entry_id=entry_id,
        entity_id=entity_id,
        operation=operation,
        outcome=outcome,
        since=from_,
        until=to,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit,
    )
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = (
        _encode_cursor(page[-1].occurred_at, page[-1].id) if has_more and page else None
    )
    return ResearchAuditPageOut(
        items=[ResearchAuditEventOut.model_validate(row) for row in page],
        limit=limit,
        has_more=has_more,
        next_cursor=next_cursor,
    )


def _settings_flag(request: Request, name: str, default: bool = False) -> bool:
    settings = getattr(request.app.state, "settings", None)
    return bool(getattr(settings, name, default))


async def _mcp_credential(
    authorization: str | None,
    session: AsyncSession,
) -> McpClientIdentity:
    """解析 MCP 客户端 Bearer 凭据(与 #517 同源:只存 sha256 摘要)。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 MCP Bearer 凭据")
    token = authorization[7:].strip()
    digest = hash_client_token(token)
    row = await McpClientRepository(session).get_by_token_hash(digest)
    if row is None:
        # 常量时间哑比较,避免暴露「token 是否存在」。
        secrets.compare_digest(digest, "0" * 64)
        raise HTTPException(status_code=401, detail="凭据无效")
    now = datetime.now(UTC)
    if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
        raise HTTPException(status_code=401, detail="凭据已吊销或过期")
    if not secrets.compare_digest(row.token_hash, digest):
        raise HTTPException(status_code=401, detail="凭据无效")
    return McpClientIdentity(
        client_id=row.client_id,
        name=row.name,
        scopes=frozenset(row.scopes),
    )


@router.post("/client-events", response_model=ResearchClientEventOut, status_code=201)
async def post_client_event(
    body: ResearchClientEventIn,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchClientEventOut:
    """接收外部 Harness 上报的调用事件(默认关闭;``trust=client_declared``)。"""

    if not _settings_flag(request, "research_audit_client_events"):
        raise HTTPException(status_code=404, detail="客户端声明事件未启用")
    identity = await _mcp_credential(authorization, session)
    if "research.write" not in identity.scopes:
        raise HTTPException(status_code=403, detail="客户端缺少 research.write 作用域")

    outcome_map: dict[str, Outcome] = {
        "ok": "succeeded",
        "error": "failed",
        "denied": "denied",
        "rejected": "rejected",
    }
    outcome = outcome_map[body.outcome]
    context = ResearchOperationContext.for_mcp(client_id=identity.client_id)
    correlation: dict[str, str] = {
        str(key)[:64]: str(value)[:256] for key, value in body.correlation.items()
    }
    if body.tool_name:
        correlation.setdefault("tool_name", body.tool_name)
    params_summary = sanitize_params(
        {
            "event_type": body.event_type,
            "tool_name": body.tool_name,
            "started_at": body.started_at.isoformat() if body.started_at else None,
            "ended_at": body.ended_at.isoformat() if body.ended_at else None,
            "summary": body.summary[:MAX_VALUE_CHARS] if body.summary else None,
        }
    )
    from finboard_app.research_audit import research_audit_recorder

    row, created = await research_audit_recorder().record_client_declared(
        session,
        context=context,
        client_event_id=body.client_event_id,
        operation=f"client.{body.event_type}"[:64],
        outcome=outcome,
        params_summary=params_summary,
        correlation=correlation,
        result_code=body.outcome,
    )
    await session.commit()
    assert row is not None
    logger.info(
        "research.audit.client_event",
        client_id=identity.client_id,
        audit_id=row.audit_id,
        created=created,
    )
    return ResearchClientEventOut(
        audit_id=row.audit_id,
        created=created,
        deduplicated=not created,
    )


@router.get("/{audit_id}", response_model=ResearchAuditEventOut)
async def get_audit(
    audit_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchAuditEventOut:
    """按 ``RA-`` 读取单条审计(不存在 404)。"""

    row = await ResearchAuditRepository(session).get(audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审计事件不存在")
    return ResearchAuditEventOut.model_validate(row)


__all__ = ["router"]
