"""回测端点。"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobOut
from finboard_api.schemas import (
    BacktestFillOut,
    BacktestHistoryDetailOut,
    BacktestHistoryItemOut,
    BacktestRunRequest,
    EquityPointOut,
    FactorSnapshotOut,
    StrategyInfoOut,
)
from finboard_api.strategy_validation import strategy_info
from finboard_app.selection_schema import FactorSelectionParams
from finboard_persistence import BackgroundJobPersistenceConflictError

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/strategies", response_model=list[StrategyInfoOut])
async def list_strategies() -> list[StrategyInfoOut]:
    """列出可用策略及其参数 schema。"""
    from finboard_app.strategies import list_strategy_definitions

    return [strategy_info(definition) for definition in list_strategy_definitions()]


@router.post("/run", response_model=JobOut, status_code=202)
async def run_backtest(
    req: BacktestRunRequest,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记回测任务,立即返回 202 + job_id(issue #144)。

    实际执行(策略构造 + BacktestEngine.run + 完整结果落库)由 worker 消费
    kind=backtest_run 任务。回测完成后 JobOut.result_ref = str(run_id);
    前端轮询 /api/jobs/{job_id} 拿到 run_id 后查 /api/backtest/history/{run_id}。
    """
    import hashlib
    import os

    from finboard_api.job_helpers import enqueue_job

    settings = getattr(request.app.state, "settings", None)
    provider_name = (
        settings.data_provider
        if settings is not None
        else os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    )
    request_dict = req.model_dump(mode="json")
    payload: dict[str, Any] = {
        "request": request_dict,
        "provider_name": provider_name,
    }
    params_digest = hashlib.sha256(
        json.dumps(request_dict, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    idempotency_key = f"backtest:{req.strategy}:{params_digest}"
    try:
        job = await enqueue_job(
            session,
            response,
            kind="backtest_run",
            queue="data",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="api:backtest_run",
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job



# --------------------------------------------------------------------------- History
@router.get("/history", response_model=list[BacktestHistoryItemOut])
async def list_history(
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
) -> list[BacktestHistoryItemOut]:
    """列出最近回测记录(摘要)。"""
    from finboard_persistence import BacktestRunRepository

    repo = BacktestRunRepository(session)
    rows = await repo.list_recent(limit=limit)
    await session.commit()
    return [
        BacktestHistoryItemOut(
            id=r.id,
            strategy=r.strategy,
            symbols=r.symbols,
            start=r.start,
            end=r.end,
            capital=r.capital,
            adjust=r.adjust,
            metrics=r.metrics,
            factor_version=r.factor_version,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/history/{run_id}", response_model=BacktestHistoryDetailOut)
async def get_history(
    run_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> BacktestHistoryDetailOut:
    """获取单次回测的完整详情。"""
    from finboard_persistence import BacktestRunRepository

    repo = BacktestRunRepository(session)
    r = await repo.get(run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    await session.commit()
    return BacktestHistoryDetailOut(
        id=r.id,
        strategy=r.strategy,
        symbols=r.symbols,
        start=r.start,
        end=r.end,
        capital=r.capital,
        adjust=r.adjust,
        params=r.params,
        selection=FactorSelectionParams.model_validate(r.selection),
        metrics=r.metrics,
        equity_curve=[EquityPointOut(**p) for p in r.equity_curve],
        fills=[BacktestFillOut(**f) for f in r.fills],
        summary=r.summary,
        selection_snapshots=[
            FactorSnapshotOut.model_validate(snapshot) for snapshot in r.selection_snapshots
        ],
        dataset_versions=cast(dict[str, list[str]], r.dataset_versions),
        factor_version=r.factor_version,
        matching_model=r.matching_model if r.matching_model is not None else {},
        asset_rules=r.asset_rules,
        fee_assumptions=r.fee_assumptions if r.fee_assumptions is not None else {},
        benchmark_config=r.benchmark_config if r.benchmark_config is not None else {},
        created_at=r.created_at,
    )


@router.delete("/history/{run_id}", status_code=204)
async def delete_history(
    run_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """删除单次回测记录。"""
    from finboard_persistence import BacktestRunRepository

    repo = BacktestRunRepository(session)
    ok = await repo.delete(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    await session.commit()
