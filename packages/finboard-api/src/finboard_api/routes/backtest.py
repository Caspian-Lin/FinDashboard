"""回测端点。"""

from __future__ import annotations

from datetime import date as parse_date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_session
from finboard_api.schemas import (
    BacktestFillOut,
    BacktestHistoryDetailOut,
    BacktestHistoryItemOut,
    BacktestMetricsOut,
    BacktestResultOut,
    BacktestRunRequest,
    EquityPointOut,
    StrategyInfoOut,
    StrategyParamInfo,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/strategies", response_model=list[StrategyInfoOut])
async def list_strategies() -> list[StrategyInfoOut]:
    """列出可用策略及其参数 schema。"""
    from finboard_app.strategies import _REGISTRY
    from finboard_app.strategies.ma_cross import MaCrossStrategy

    strategies: list[StrategyInfoOut] = []
    for kind, cls in _REGISTRY.items():
        params: list[StrategyParamInfo] = []
        if kind == "ma_cross":
            params = [
                StrategyParamInfo(
                    name="short_window",
                    type="int",
                    default=5,
                    description="短期均线周期",
                ),
                StrategyParamInfo(
                    name="long_window",
                    type="int",
                    default=20,
                    description="长期均线周期",
                ),
            ]
        strategies.append(
            StrategyInfoOut(
                kind=kind,
                name=cls.__name__,
                params=params,
            )
        )
    _ = MaCrossStrategy  # 避免未使用 import 警告
    return strategies


@router.post("/run", response_model=BacktestResultOut)
async def run_backtest(
    req: BacktestRunRequest,
    session: AsyncSession = Depends(get_session),
) -> BacktestResultOut:
    """运行回测,返回完整绩效报告。

    注意:回测在请求线程中同步执行(BacktestEngine 是 async,不阻塞事件循环)。
    数据量大时可能需要数秒到数十秒。
    """
    from finboard_app.strategies import create_strategy
    from finboard_backtest import BacktestConfig, BacktestEngine
    from finboard_data import AkShareProvider, YFinanceProvider

    strategy = create_strategy(req.strategy, "backtest", **req.params)

    import os
    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
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
        strategy_params=req.params,
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=provider,
        config=config,
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
        params=req.params,
        metrics=json.loads(metrics.model_dump_json()),
        equity_curve=[p.model_dump() for p in equity_curve],
        fills=[f.model_dump() for f in fills],
        summary=result.summary(),
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
    )


# --------------------------------------------------------------------------- History
@router.get("/history", response_model=list[BacktestHistoryItemOut])
async def list_history(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
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
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/history/{run_id}", response_model=BacktestHistoryDetailOut)
async def get_history(
    run_id: int,
    session: AsyncSession = Depends(get_session),
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
        metrics=r.metrics,
        equity_curve=[EquityPointOut(**p) for p in r.equity_curve],
        fills=[BacktestFillOut(**f) for f in r.fills],
        summary=r.summary,
        created_at=r.created_at,
    )


@router.delete("/history/{run_id}", status_code=204)
async def delete_history(
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除单次回测记录。"""
    from finboard_persistence import BacktestRunRepository

    repo = BacktestRunRepository(session)
    ok = await repo.delete(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    await session.commit()
