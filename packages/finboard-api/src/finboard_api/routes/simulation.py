"""与实盘路由硬隔离的持久化模拟交易 API(issue #83)。

没有“直接创建订单”端点。订单只能由结构化目标仓位决策经模拟 runner 生成。
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.simulation_schemas import (
    SimulationAccountCreateIn,
    SimulationAccountOut,
    SimulationActorIn,
    SimulationAuditOut,
    SimulationBarIn,
    SimulationDecisionIn,
    SimulationDecisionOut,
    SimulationDecisionResultOut,
    SimulationEvaluateIn,
    SimulationFillOut,
    SimulationLedgerOut,
    SimulationOrderOut,
    SimulationPositionOut,
    SimulationProcessOut,
    SimulationResetIn,
    SimulationResetOut,
    SimulationSessionCreateIn,
    SimulationSessionOut,
)
from finboard_api.simulation_ws import SimulationConnectionManager
from finboard_persistence.models import (
    SimulationAccountModel,
    SimulationSessionModel,
)
from finboard_simulation import (
    SimulationConflictError,
    SimulationError,
    SimulationIsolationError,
    SimulationNotFoundError,
    SimulationRepository,
    SimulationRiskError,
    SimulationService,
    SimulationSessionStatus,
    SimulationTransitionError,
)

router = APIRouter(prefix="/api/simulation", tags=["simulation"])


@router.post("/accounts", response_model=SimulationAccountOut, status_code=201)
async def create_simulation_account(
    body: SimulationAccountCreateIn,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationAccountOut:
    result = await _write(
        session,
        SimulationService(SimulationRepository(session)).create_account(
            name=body.name,
            initial_cash=body.initial_cash,
            actor=body.actor,
            currency=body.currency,
        ),
    )
    return SimulationAccountOut.model_validate(result)


@router.get("/accounts", response_model=list[SimulationAccountOut])
async def list_simulation_accounts(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationAccountOut]:
    rows = await SimulationRepository(session).list_accounts(limit=limit)
    return [SimulationAccountOut.model_validate(item) for item in rows]


@router.get("/accounts/{account_id}", response_model=SimulationAccountOut)
async def get_simulation_account(
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationAccountOut:
    row = await _require_account(SimulationRepository(session), account_id)
    return SimulationAccountOut.model_validate(row)


@router.post("/sessions", response_model=SimulationSessionOut, status_code=201)
async def create_simulation_session(
    body: SimulationSessionCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    result = await _write(
        session,
        SimulationService(SimulationRepository(session)).create_session(
            account_id=body.simulation_account_id,
            strategy_id=body.strategy_id,
            strategy_version=body.strategy_version,
            validation_run_id=body.validation_run_id,
            data_release_id=body.data_release_id,
            source_mode=body.source_mode,
            config=body.to_config(),
            actor=body.actor,
        ),
    )
    await _notify(
        request,
        result.simulation_session_id,
        {"type": "session_created", "status": result.status},
    )
    return SimulationSessionOut.model_validate(result)


@router.get("/sessions", response_model=list[SimulationSessionOut])
async def list_simulation_sessions(
    account_id: str | None = None,
    status: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationSessionOut]:
    valid = {item.value for item in SimulationSessionStatus}
    if status is not None and not set(status).issubset(valid):
        raise HTTPException(status_code=422, detail="未知模拟会话状态")
    rows = await SimulationRepository(session).list_sessions(
        account_id=account_id, statuses=status, limit=limit
    )
    return [SimulationSessionOut.model_validate(item) for item in rows]


@router.get("/sessions/{session_id}", response_model=SimulationSessionOut)
async def get_simulation_session(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    row = await _require_session(SimulationRepository(session), session_id)
    return SimulationSessionOut.model_validate(row)


@router.post("/sessions/{session_id}/start", response_model=SimulationSessionOut)
async def start_simulation_session(
    session_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    return await _transition(
        session_id,
        body,
        request,
        session,
        SimulationSessionStatus.RUNNING,
    )


@router.post("/sessions/{session_id}/pause", response_model=SimulationSessionOut)
async def pause_simulation_session(
    session_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    return await _transition(
        session_id,
        body,
        request,
        session,
        SimulationSessionStatus.PAUSED,
    )


@router.post("/sessions/{session_id}/stop", response_model=SimulationSessionOut)
async def stop_simulation_session(
    session_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    return await _transition(
        session_id,
        body,
        request,
        session,
        SimulationSessionStatus.STOPPED,
    )


@router.post("/sessions/{session_id}/archive", response_model=SimulationSessionOut)
async def archive_simulation_session(
    session_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationSessionOut:
    return await _transition(
        session_id,
        body,
        request,
        session,
        SimulationSessionStatus.ARCHIVED,
    )


@router.post(
    "/sessions/{session_id}/reset",
    response_model=SimulationResetOut,
    status_code=201,
)
async def reset_simulation_session(
    session_id: str,
    body: SimulationResetIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationResetOut:
    account, new_session = await _write(
        session,
        SimulationService(SimulationRepository(session)).reset_session(
            session_id,
            actor=body.actor,
            initial_cash=body.initial_cash,
        ),
    )
    await _notify(
        request,
        session_id,
        {
            "type": "session_reset",
            "new_session_id": new_session.simulation_session_id,
        },
    )
    return SimulationResetOut(
        account=SimulationAccountOut.model_validate(account),
        session=SimulationSessionOut.model_validate(new_session),
    )


@router.post(
    "/sessions/{session_id}/decisions",
    response_model=SimulationDecisionResultOut,
    status_code=201,
)
async def submit_simulation_decision(
    session_id: str,
    body: SimulationDecisionIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationDecisionResultOut:
    decision, orders, duplicate = await _write(
        session,
        SimulationService(SimulationRepository(session)).submit_decision(
            session_id, body.to_domain()
        ),
    )
    await _notify(
        request,
        session_id,
        {
            "type": "strategy_decision",
            "decision_id": decision.decision_id,
            "order_ids": [item.simulation_order_id for item in orders],
            "duplicate": duplicate,
        },
    )
    return SimulationDecisionResultOut(
        decision=SimulationDecisionOut.model_validate(decision),
        orders=[SimulationOrderOut.model_validate(item) for item in orders],
        duplicate=duplicate,
    )


@router.get(
    "/sessions/{session_id}/decisions",
    response_model=list[SimulationDecisionOut],
)
async def list_simulation_decisions(
    session_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationDecisionOut]:
    repo = SimulationRepository(session)
    await _require_session(repo, session_id)
    rows = await repo.list_decisions(session_id, limit=limit)
    return [SimulationDecisionOut.model_validate(item) for item in rows]


@router.post(
    "/sessions/{session_id}/market-events",
    response_model=SimulationProcessOut,
)
async def process_simulation_market_event(
    session_id: str,
    body: SimulationBarIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationProcessOut:
    result = await _write(
        session,
        SimulationService(SimulationRepository(session)).process_bar(session_id, body.to_domain()),
    )
    await _notify(
        request,
        session_id,
        {
            "type": "market_event_processed",
            "source_event_id": result.source_event_id,
            "fill_ids": list(result.fill_ids),
            "rejected_order_ids": list(result.rejected_order_ids),
            "equity": str(result.equity),
            "duplicate": result.duplicate,
        },
    )
    return SimulationProcessOut.model_validate(result, from_attributes=True)


@router.post(
    "/sessions/{session_id}/orders/{order_id}/cancel",
    response_model=SimulationOrderOut,
)
async def cancel_simulation_order(
    session_id: str,
    order_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SimulationOrderOut:
    order = await _write(
        session,
        SimulationService(SimulationRepository(session)).cancel_order(
            session_id, order_id, actor=body.actor
        ),
    )
    await _notify(
        request,
        session_id,
        {"type": "order_cancelled", "order_id": order_id},
    )
    return SimulationOrderOut.model_validate(order)


@router.get(
    "/sessions/{session_id}/orders",
    response_model=list[SimulationOrderOut],
)
async def list_simulation_orders(
    session_id: str,
    status: list[str] | None = Query(default=None),
    symbol: str | None = None,
    limit: int = Query(default=1000, ge=1, le=10000),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationOrderOut]:
    repo = SimulationRepository(session)
    await _require_session(repo, session_id)
    rows = await repo.list_orders(session_id, statuses=status, symbol=symbol, limit=limit)
    return [SimulationOrderOut.model_validate(item) for item in rows]


@router.get(
    "/sessions/{session_id}/fills",
    response_model=list[SimulationFillOut],
)
async def list_simulation_fills(
    session_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationFillOut]:
    repo = SimulationRepository(session)
    await _require_session(repo, session_id)
    rows = await repo.list_fills(session_id, limit=limit)
    return [SimulationFillOut.model_validate(item) for item in rows]


@router.get(
    "/sessions/{session_id}/positions",
    response_model=list[SimulationPositionOut],
)
async def list_simulation_positions(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationPositionOut]:
    repo = SimulationRepository(session)
    simulation_session = await _require_session(repo, session_id)
    rows = await repo.list_positions(simulation_session.simulation_account_id)
    return [SimulationPositionOut.model_validate(item) for item in rows]


@router.get(
    "/sessions/{session_id}/ledger",
    response_model=list[SimulationLedgerOut],
)
async def list_simulation_ledger(
    session_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationLedgerOut]:
    repo = SimulationRepository(session)
    await _require_session(repo, session_id)
    rows = await repo.list_ledger(session_id, limit=limit)
    return [SimulationLedgerOut.model_validate(item) for item in rows]


@router.get(
    "/sessions/{session_id}/audit",
    response_model=list[SimulationAuditOut],
)
async def list_simulation_audit(
    session_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    session: AsyncSession = Depends(get_db_session),
) -> list[SimulationAuditOut]:
    repo = SimulationRepository(session)
    await _require_session(repo, session_id)
    rows = await repo.list_audit(session_id, limit=limit)
    return [SimulationAuditOut.model_validate(item) for item in rows]


@router.get("/sessions/{session_id}/report")
async def get_simulation_report(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await _read(SimulationService(SimulationRepository(session)).build_report(session_id))


@router.post("/sessions/{session_id}/evaluate")
async def evaluate_simulation_session(
    session_id: str,
    body: SimulationEvaluateIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await _write(
        session,
        SimulationService(SimulationRepository(session)).evaluate_session(
            session_id,
            actor=body.actor,
            minimum_trading_days=body.minimum_trading_days,
        ),
    )
    await _notify(
        request,
        session_id,
        {
            "type": "promotion_evaluated",
            "promotion_status": result["promotion_status"],
            "automatic_live_promotion": False,
        },
    )
    return result


async def _transition(
    session_id: str,
    body: SimulationActorIn,
    request: Request,
    session: AsyncSession,
    target: SimulationSessionStatus,
) -> SimulationSessionOut:
    row = await _write(
        session,
        SimulationService(SimulationRepository(session)).transition_session(
            session_id, target=target, actor=body.actor
        ),
    )
    await _notify(
        request,
        session_id,
        {"type": "session_transition", "status": row.status},
    )
    return SimulationSessionOut.model_validate(row)


async def _require_session(repo: SimulationRepository, session_id: str) -> SimulationSessionModel:
    if not session_id.startswith("SIM-S-"):
        raise HTTPException(status_code=422, detail="模拟接口只接受 SIM-S- ID")
    row = await repo.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟会话不存在")
    return row


async def _require_account(repo: SimulationRepository, account_id: str) -> SimulationAccountModel:
    if not account_id.startswith("SIM-A-"):
        raise HTTPException(status_code=422, detail="模拟接口只接受 SIM-A- ID")
    row = await repo.get_account(account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟账户不存在")
    return row


async def _write[T](session: AsyncSession, operation: Awaitable[T]) -> T:
    try:
        result = await operation
        await session.commit()
        return result
    except Exception as exc:
        await session.rollback()
        _raise_http(exc)


async def _read[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except Exception as exc:
        _raise_http(exc)


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, SimulationNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            SimulationConflictError,
            SimulationTransitionError,
            SimulationIsolationError,
            IntegrityError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (SimulationRiskError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, SimulationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


async def _notify(request: Request, session_id: str, payload: dict[str, object]) -> None:
    manager = getattr(request.app.state, "simulation_ws_manager", None)
    if isinstance(manager, SimulationConnectionManager):
        await manager.publish(session_id, payload)


__all__ = ["router"]
