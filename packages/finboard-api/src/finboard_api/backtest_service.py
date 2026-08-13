"""回测编排:运行策略 + 持久化结果(issue #144)。

把 ``routes/backtest.run_backtest`` 的核心编排(engine 构造、运行、指标映射、
``BacktestRunModel`` 落库)抽成可复用函数,供 REST 路由(现已改为入队)与
``BacktestRunExecutor`` 统一队列消费方共用,避免双份重复实现。

边界:只读 ``factor_feature_snapshots`` / ``research_dataset_releases``,只写
``backtest_runs`` 表;不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

import json
from datetime import date as parse_date
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.schemas import (
    BacktestFillOut,
    BacktestMetricsOut,
    EquityPointOut,
    FactorSnapshotOut,
)
from finboard_api.strategy_validation import validate_strategy_params_for_api

if TYPE_CHECKING:
    from finboard_api.schemas import BacktestRunRequest


class BacktestRunError(RuntimeError):
    """回测编排失败(策略参数非法 / 引擎执行失败)。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


async def run_backtest_and_persist(
    session: AsyncSession,
    request: BacktestRunRequest,
    *,
    provider_name: str,
) -> int:
    """运行回测并把完整结果写入 ``backtest_runs``,返回自增 ``run_id``。

    * ``request``:已通过 Pydantic 校验的 :class:`BacktestRunRequest`;
    * ``provider_name``:行情源(akshare / tushare / yfinance),由调用方解析;
    * 抛 :class:`BacktestRunError` 表示不可重试的业务失败。
    """
    from finboard_app.strategies import create_strategy
    from finboard_backtest import (
        BacktestConfig,
        BacktestEngine,
        PointInTimeFactorSelector,
    )
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider
    from finboard_persistence import (
        BacktestRunModel,
        BacktestRunRepository,
        FactorSnapshotRepository,
        ResearchDatasetRepository,
    )

    params = validate_strategy_params_for_api(
        request.strategy,
        request.params,
        require_backtest=True,
    )
    strategy = create_strategy(request.strategy, "backtest", **params)

    if provider_name == "akshare":
        provider: AkShareProvider | TushareBarProvider | YFinanceProvider = (
            AkShareProvider()
        )
    elif provider_name == "tushare":
        provider = TushareBarProvider()
    else:
        provider = YFinanceProvider()

    config = BacktestConfig(
        symbols=request.symbols,
        start=parse_date.fromisoformat(request.start),
        end=parse_date.fromisoformat(request.end),
        initial_capital=request.capital,
        adjust=request.adjust,
        strategy_params=params,
        commission_rate=request.commission_rate,
        commission_min=request.commission_min,
        stamp_tax_rate=request.stamp_tax_rate,
        slippage_bps=request.slippage_bps,
        selection=request.selection.to_domain(),
    )
    factor_selector = (
        PointInTimeFactorSelector(
            reader=ResearchDatasetRepository(session),
            writer=FactorSnapshotRepository(session),
        )
        if request.selection.enabled
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
        raise BacktestRunError(
            code="engine_failed",
            summary=f"回测执行失败(通常是数据源连接错误): {exc}",
        ) from exc

    equity_curve = [
        EquityPointOut(date=str(d), equity=float(e)) for d, e in result.equity_curve
    ]
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

    run_row = BacktestRunModel(
        strategy=request.strategy,
        symbols=request.symbols,
        start=request.start,
        end=request.end,
        capital=request.capital,
        adjust=request.adjust,
        params=params,
        selection=request.selection.model_dump(mode="json"),
        metrics=json.loads(metrics.model_dump_json()),
        equity_curve=[p.model_dump(mode="json") for p in equity_curve],
        fills=[f.model_dump(mode="json") for f in fills],
        summary=result.summary(),
        dataset_versions=result.dataset_versions,
        factor_version=result.factor_version,
        selection_snapshots=[
            snapshot.model_dump(mode="json") for snapshot in selection_snapshots
        ],
        matching_model=result.matching_model,
        asset_rules=result.asset_rules,
        fee_assumptions=result.fee_assumptions,
        benchmark_config=result.benchmark_config,
    )
    repo = BacktestRunRepository(session)
    await repo.save(run_row)
    await session.commit()
    return int(run_row.id)


__all__ = ["BacktestRunError", "run_backtest_and_persist"]
