"""回测端点。"""

from __future__ import annotations

from datetime import date as parse_date

from fastapi import APIRouter

from finboard_api.schemas import (
    BacktestFillOut,
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
async def run_backtest(req: BacktestRunRequest) -> BacktestResultOut:
    """运行回测,返回完整绩效报告。

    注意:回测在请求线程中同步执行(BacktestEngine 是 async,不阻塞事件循环)。
    数据量大时可能需要数秒到数十秒。
    """
    from finboard_app.strategies import create_strategy
    from finboard_backtest import BacktestConfig, BacktestEngine
    from finboard_data import AkShareProvider

    strategy = create_strategy(req.strategy, "backtest", **req.params)
    provider = AkShareProvider()
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
    result = await engine.run()

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

    return BacktestResultOut(
        metrics=metrics,
        equity_curve=equity_curve,
        fills=fills,
        summary=result.summary(),
    )
