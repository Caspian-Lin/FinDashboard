"""只读确定性诊断;与 MCP 同源。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_app.research_diagnostics import compare_runs, decision_projection, interval_report
from finboard_app.research_stress import stress_matrix

router = APIRouter(prefix="/api/research/diagnostics", tags=["research-diagnostics"])


async def call(session: AsyncSession, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if operation == "stress":
            return await stress_matrix(session, **arguments)
        if operation == "interval":
            return await interval_report(session, **arguments)
        if operation == "compare":
            return await compare_runs(session, **arguments)
        return await decision_projection(session, **arguments)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(413 if "payload_too_large" in str(exc) else 422, str(exc)) from exc


@router.get("/{run_id}/interval")
async def interval(
    run_id: str,
    start: str,
    end: str,
    yearly: bool = True,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return await call(
        session, "interval", {"run_id": run_id, "start": start, "end": end, "yearly": yearly}
    )


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[str] = Field(min_length=2, max_length=6)
    allowed_differences: list[str] = Field(default_factory=list)


class StressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_run_id: str
    plan_key: str = Field(min_length=1, max_length=128)
    operation: str = "plan"
    cost_multipliers: list[float] = Field(default_factory=list, max_length=24)
    slippage_bps: list[float] = Field(default_factory=list, max_length=24)
    capitals: list[str] = Field(default_factory=list, max_length=24)
    execution_delay_bars: list[int] = Field(default_factory=list, max_length=24)


@router.post("/stress")
async def stress(body: StressInput, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    return await call(session, "stress", body.model_dump())


@router.post("/compare")
async def comparison(
    body: Comparison, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    return await call(session, "compare", body.model_dump())


@router.get("/{run_id}/projection")
async def projection(
    run_id: str,
    stage: str = "features",
    decision_id: str | None = None,
    symbol: str | None = None,
    fields: list[str] | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return await call(
        session,
        "projection",
        {
            "run_id": run_id,
            "stage": stage,
            "decision_id": decision_id,
            "symbol": symbol,
            "fields": fields,
            "limit": limit,
            "offset": offset,
        },
    )
