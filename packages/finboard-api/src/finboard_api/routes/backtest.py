"""回测端点。"""

from __future__ import annotations

from datetime import date as parse_date
from typing import cast

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import (
    BacktestFillOut,
    BacktestHistoryDetailOut,
    BacktestHistoryItemOut,
    BacktestMetricsOut,
    BacktestResultOut,
    BacktestRunRequest,
    EquityPointOut,
    FactorSnapshotOut,
    StrategyInfoOut,
)
from finboard_api.strategy_validation import strategy_info, validate_strategy_params_for_api
from finboard_app.selection_schema import FactorSelectionParams

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/strategies", response_model=list[StrategyInfoOut])
async def list_strategies() -> list[StrategyInfoOut]:
    """列出可用策略及其参数 schema。"""
    from finboard_app.strategies import list_strategy_definitions

    return [strategy_info(definition) for definition in list_strategy_definitions()]


@router.post("/run", response_model=BacktestResultOut)
async def run_backtest(
    req: BacktestRunRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BacktestResultOut:
    """运行回测,返回完整绩效报告。

    注意:回测在请求线程中同步执行(BacktestEngine 是 async,不阻塞事件循环)。
    数据量大时可能需要数秒到数十秒。
    """
    from finboard_app.strategies import create_strategy
    from finboard_backtest import (
        BacktestConfig,
        BacktestEngine,
        PointInTimeFactorSelector,
    )
    from finboard_data import AkShareProvider, YFinanceProvider
    from finboard_persistence import (
        FactorSnapshotRepository,
        ResearchDatasetRepository,
    )

    params = validate_strategy_params_for_api(
        req.strategy,
        req.params,
        require_backtest=True,
    )
    strategy = create_strategy(req.strategy, "backtest", **params)

    import os

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider()
    else:
        provider = YFinanceProvider()
    config = BacktestConfig(
        symbols=req.symbols,
        start=parse_date.fromisoformat(req.start),
        end=parse_date.fromisoformat(req.end),
        initial_capital=req.capital,
        adjust=req.adjust,
        strategy_params=params,
        commission_rate=req.commission_rate,
        commission_min=req.commission_min,
        stamp_tax_rate=req.stamp_tax_rate,
        slippage_bps=req.slippage_bps,
        selection=req.selection.to_domain(),
    )
    factor_selector = (
        PointInTimeFactorSelector(
            reader=ResearchDatasetRepository(session),
            writer=FactorSnapshotRepository(session),
        )
        if req.selection.enabled
        else None
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=provider,
        config=config,
        factor_selector=factor_selector,
    )
    try:
        result = await engine.run()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("backtest.run_failed")
        raise HTTPException(
            status_code=502,
            detail=f"回测执行失败(通常是数据源连接错误): {exc}",
        ) from exc

    equity_curve = [
        EquityPointOut(
            date=str(d),
            equity=float(e),
        )
        for d, e in result.equity_curve
    ]

    # 补充 benchmark
    if result.benchmark_curve:
        bench_map = {str(d): float(b) for d, b in result.benchmark_curve}
        for point in equity_curve:
            point.benchmark = bench_map.get(point.date)

    fills = [
        BacktestFillOut(
            date=str(f.filled_at.date()) if f.filled_at else "",
            symbol=f.symbol.code,
            side=f.side,
            quantity=f.quantity,
            price=f.price,
            commission=f.commission,
        )
        for f in result.fills
    ]

    metrics = BacktestMetricsOut(
        total_return=result.total_return,
        annualized_return=result.annualized_return,
        sharpe_ratio=result.sharpe_ratio,
        max_drawdown=result.max_drawdown,
        win_rate=result.win_rate,
        trade_count=result.trade_count,
        turnover=result.turnover,
        commission_paid=result.commission_paid,
        stamp_tax_paid=result.stamp_tax_paid,
        benchmark_return=result.benchmark_return,
        excess_return=result.excess_return,
        initial_capital=result.initial_capital,
        final_equity=result.final_equity,
    )
    selection_snapshots = [
        FactorSnapshotOut(
            id=snapshot.snapshot_id,
            decision_at=snapshot.decision_at,
            business_date=str(snapshot.business_date),
            effective_date=str(snapshot.effective_date),
            selected_symbols=list(snapshot.selected_symbols),
            status=snapshot.status.value,
            skip_reason=snapshot.skip_reason,
            dataset_versions=snapshot.dataset_versions,
            factor_version=snapshot.factor_version,
            checksum=snapshot.checksum,
        )
        for snapshot in result.selection_snapshots
    ]

    # 落库 —— 保存参数 + 完整结果,供历史切换查看
    import json

    from finboard_persistence import BacktestRunModel, BacktestRunRepository

    run_row = BacktestRunModel(
        strategy=req.strategy,
        symbols=req.symbols,
        start=req.start,
        end=req.end,
        capital=req.capital,
        adjust=req.adjust,
        params=params,
        selection=req.selection.model_dump(mode="json"),
        metrics=json.loads(metrics.model_dump_json()),
        equity_curve=[p.model_dump(mode="json") for p in equity_curve],
        fills=[f.model_dump(mode="json") for f in fills],
        summary=result.summary(),
        dataset_versions=result.dataset_versions,
        factor_version=result.factor_version,
        selection_snapshots=[snapshot.model_dump(mode="json") for snapshot in selection_snapshots],
        matching_model=result.matching_model,
        asset_rules=result.asset_rules,
        fee_assumptions=result.fee_assumptions,
        benchmark_config=result.benchmark_config,
    )
    repo = BacktestRunRepository(session)
    await repo.save(run_row)
    await session.commit()
    run_id = run_row.id

    return BacktestResultOut(
        metrics=metrics,
        equity_curve=equity_curve,
        fills=fills,
        summary=result.summary(),
        run_id=run_id,
        selection_snapshots=selection_snapshots,
        dataset_versions=result.dataset_versions,
        factor_version=result.factor_version,
        matching_model=result.matching_model,
        asset_rules=result.asset_rules,
        fee_assumptions=result.fee_assumptions,
        benchmark_config=result.benchmark_config,
    )


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
