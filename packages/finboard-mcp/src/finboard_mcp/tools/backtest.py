"""``finboard.backtest.*`` 工具 —— 回测引擎查询与同步运行(issue #127)。

- 只读(3):``backtest_strategy_list``(可用策略 + 参数 schema)/
  ``backtest_history_list``(历史回测列表)/ ``backtest_history_get``(历史详情)
- 写(2):``backtest_run``(同步运行回测,返回 metrics/equity/fills,
  对应 ``POST /api/backtest/run``)/ ``backtest_history_delete``

复用现有 ``BacktestEngine`` / ``BacktestRunRepository`` /
``list_strategy_definitions`` / ``get_strategy_definition``,不重复实现。
不触及交易安全红线(回测是纸面撮合,不发真实订单)。
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from finboard_persistence.models import BacktestRunModel

__all__ = ["register"]


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _validate_backtest_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """校验回测策略参数,要求 ``supports_backtest=True``。

    复用 ``get_strategy_definition`` / ``params_model``,绕过 FastPI 耦合的
    ``validate_strategy_params_for_api``(后者抛 ``HTTPException``)。
    """
    from finboard_app.strategies import get_strategy_definition

    try:
        definition = get_strategy_definition(kind)
    except ValueError as exc:
        raise McpToolError("invalid_argument", f"未知策略: {kind}") from exc
    if not definition.supports_backtest:
        raise McpToolError(
            "invalid_argument",
            f"策略 {kind} 不支持回测(supports_backtest=False)",
        )
    try:
        validated = definition.params_model.model_validate(params or {})
    except ValidationError as exc:
        raise McpToolError(
            "invalid_argument", f"策略参数校验失败: {exc}"
        ) from exc
    return validated.model_dump(mode="json")


def _strategy_info(definition: Any) -> dict[str, Any]:
    """把 ``StrategyDefinition`` 序列化为参数 schema(复用 API 逻辑)。"""
    from finboard_api.strategy_validation import strategy_info

    return cast(dict[str, Any], to_jsonable(strategy_info(definition).model_dump()))


def _history_item(row: BacktestRunModel) -> dict[str, Any]:
    """历史列表项(摘要,不含完整 equity/fills)。"""
    return {
        "id": row.id,
        "strategy": row.strategy,
        "symbols": list(row.symbols) if row.symbols else [],
        "start": row.start,
        "end": row.end,
        "capital": to_jsonable(row.capital),
        "adjust": row.adjust,
        "metrics": dict(row.metrics) if row.metrics else {},
        "factor_version": row.factor_version,
        "created_at": to_jsonable(row.created_at),
    }


def _history_detail(row: BacktestRunModel) -> dict[str, Any]:
    """历史详情(完整 equity_curve / fills / summary)。"""
    detail = _history_item(row)
    detail.update(
        {
            "params": dict(row.params) if row.params else {},
            "selection": dict(row.selection) if row.selection else {},
            "equity_curve": list(row.equity_curve) if row.equity_curve else [],
            "fills": list(row.fills) if row.fills else [],
            "summary": row.summary,
            "selection_snapshots": (
                list(row.selection_snapshots) if row.selection_snapshots else []
            ),
            "dataset_versions": (
                dict(row.dataset_versions) if row.dataset_versions else {}
            ),
            "matching_model": (
                dict(row.matching_model) if row.matching_model else {}
            ),
            "asset_rules": dict(row.asset_rules) if row.asset_rules else None,
            "fee_assumptions": (
                dict(row.fee_assumptions) if row.fee_assumptions else {}
            ),
            "benchmark_config": (
                dict(row.benchmark_config) if row.benchmark_config else {}
            ),
        }
    )
    return cast(dict[str, Any], to_jsonable(detail))


# --------------------------------------------------------------------------- #
# finboard.backtest.strategy_list(只读,无 DB)
# --------------------------------------------------------------------------- #
async def backtest_strategy_list(app: McpAppContext) -> ToolEnvelope:
    """列出支持回测的内置策略及其参数 schema。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_app.strategies import list_strategy_definitions

        return [
            _strategy_info(d)
            for d in list_strategy_definitions()
            if d.supports_backtest
        ]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.strategy_list",
        arguments={},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.run(写,同步执行)
# --------------------------------------------------------------------------- #
async def backtest_run(
    app: McpAppContext,
    *,
    strategy: str,
    symbols: list[str],
    start: str,
    end: str,
    capital: Decimal = Decimal("100000"),
    adjust: str = "qfq",
    params: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
    commission_rate: Decimal = Decimal("0.0003"),
    commission_min: Decimal = Decimal("1"),
    stamp_tax_rate: Decimal = Decimal("0.0005"),
    slippage_bps: Decimal = Decimal("0"),
) -> ToolEnvelope:
    """同步运行回测,返回 metrics/equity/fills/snapshots 并落库。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        import json

        from finboard_app.selection_schema import FactorSelectionParams
        from finboard_app.strategies import create_strategy
        from finboard_backtest import (
            BacktestConfig,
            BacktestEngine,
            PointInTimeFactorSelector,
        )
        from finboard_data import (
            AkShareProvider,
            TushareBarProvider,
            YFinanceProvider,
        )
        from finboard_persistence import (
            BacktestRunModel,
            BacktestRunRepository,
            FactorSnapshotRepository,
            ResearchDatasetRepository,
        )

        # 校验参数 + 构建策略
        validated_params = _validate_backtest_params(strategy, params or {})
        strat = create_strategy(strategy, "backtest", **validated_params)

        # 选择数据源
        provider_name = getattr(app.settings, "data_provider", "akshare")
        if provider_name == "akshare":
            data_provider: (
                AkShareProvider | TushareBarProvider | YFinanceProvider
            ) = AkShareProvider()
        elif provider_name == "tushare":
            data_provider = TushareBarProvider(
                token=app.settings.tushare_token,
                requests_per_minute=app.settings.tushare_requests_per_minute,
                daily_request_limit=app.settings.tushare_daily_request_limit,
                usage_file=app.settings.tushare_usage_file,
            )
        else:
            data_provider = YFinanceProvider()

        # 解析 selection
        try:
            selection_model = FactorSelectionParams.model_validate(selection or {})
        except ValidationError as exc:
            raise McpToolError(
                "invalid_argument", f"selection 参数校验失败: {exc}"
            ) from exc

        config = BacktestConfig(
            symbols=list(symbols),
            start=date_type.fromisoformat(start),
            end=date_type.fromisoformat(end),
            initial_capital=capital,
            adjust=adjust,
            strategy_params=validated_params,
            commission_rate=commission_rate,
            commission_min=commission_min,
            stamp_tax_rate=stamp_tax_rate,
            slippage_bps=slippage_bps,
            selection=selection_model.to_domain(),
        )

        async with app.session_maker() as session:
            factor_selector = (
                PointInTimeFactorSelector(
                    reader=ResearchDatasetRepository(session),
                    writer=FactorSnapshotRepository(session),
                )
                if selection_model.enabled
                else None
            )
            engine = BacktestEngine(
                strategy=strat,
                data_provider=data_provider,
                config=config,
                factor_selector=factor_selector,
            )
            try:
                result = await engine.run()
            except Exception as exc:
                raise McpToolError(
                    "unavailable",
                    f"回测执行失败(通常是数据源连接错误): {exc}",
                ) from exc

            # 构建结果字典(与 API 响应字段一一对应)
            equity_curve: list[dict[str, Any]] = [
                {"date": str(d), "equity": float(e)}
                for d, e in result.equity_curve
            ]
            bench_map: dict[str, float] = {}
            if result.benchmark_curve:
                bench_map = {str(d): float(b) for d, b in result.benchmark_curve}
                for point in equity_curve:
                    point["benchmark"] = bench_map.get(point["date"])
            fills = [
                {
                    "date": str(f.filled_at.date()) if f.filled_at else "",
                    "symbol": f.symbol.code,
                    "side": f.side,
                    "quantity": to_jsonable(f.quantity),
                    "price": to_jsonable(f.price),
                    "commission": to_jsonable(f.commission),
                }
                for f in result.fills
            ]
            metrics = {
                "total_return": result.total_return,
                "annualized_return": result.annualized_return,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "win_rate": result.win_rate,
                "trade_count": result.trade_count,
                "turnover": result.turnover,
                "commission_paid": to_jsonable(result.commission_paid),
                "stamp_tax_paid": to_jsonable(result.stamp_tax_paid),
                "benchmark_return": result.benchmark_return,
                "excess_return": result.excess_return,
                "initial_capital": to_jsonable(result.initial_capital),
                "final_equity": to_jsonable(result.final_equity),
            }
            selection_snapshots = [
                {
                    "decision_at": to_jsonable(s.decision_at),
                    "business_date": str(s.business_date),
                    "effective_date": str(s.effective_date),
                    "selected_symbols": list(s.selected_symbols),
                    "status": s.status.value,
                    "skip_reason": s.skip_reason,
                    "dataset_versions": dict(s.dataset_versions),
                    "factor_version": s.factor_version,
                    "checksum": s.checksum,
                }
                for s in result.selection_snapshots
            ]

            # 落库
            run_row = BacktestRunModel(
                strategy=strategy,
                symbols=list(symbols),
                start=start,
                end=end,
                capital=capital,
                adjust=adjust,
                params=validated_params,
                selection=selection_model.model_dump(mode="json"),
                metrics=json.loads(json.dumps(metrics, default=str)),
                equity_curve=equity_curve,
                fills=fills,
                summary=result.summary(),
                dataset_versions=result.dataset_versions,
                factor_version=result.factor_version,
                selection_snapshots=selection_snapshots,
                matching_model=result.matching_model,
                asset_rules=result.asset_rules,
                fee_assumptions=result.fee_assumptions,
                benchmark_config=result.benchmark_config,
            )
            repo = BacktestRunRepository(session)
            await repo.save(run_row)
            await session.commit()
            run_id = run_row.id

        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "run_id": run_id,
                    "metrics": metrics,
                    "equity_curve": equity_curve,
                    "fills": fills,
                    "summary": result.summary(),
                    "selection_snapshots": selection_snapshots,
                    "dataset_versions": result.dataset_versions,
                    "factor_version": result.factor_version,
                    "matching_model": result.matching_model,
                    "asset_rules": result.asset_rules,
                    "fee_assumptions": result.fee_assumptions,
                    "benchmark_config": result.benchmark_config,
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.run",
        arguments={
            "strategy": strategy,
            "symbols": symbols,
            "start": start,
            "end": end,
            "capital": str(capital),
            "adjust": adjust,
            "params": params,
            "selection": selection,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_list(只读)
# --------------------------------------------------------------------------- #
async def backtest_history_list(
    app: McpAppContext,
    *,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import BacktestRunRepository

        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            rows = await repo.list_recent(limit=safe_limit)
            return [_history_item(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_list",
        arguments={"limit": limit},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_get(只读)
# --------------------------------------------------------------------------- #
async def backtest_history_get(
    app: McpAppContext,
    run_id: int,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_persistence import BacktestRunRepository

        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"回测记录不存在: {run_id}")
            return _history_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_get",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_delete(写)
# --------------------------------------------------------------------------- #
async def backtest_history_delete(
    app: McpAppContext,
    run_id: int,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_persistence import BacktestRunRepository

        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            deleted = await repo.delete(run_id)
            if not deleted:
                raise McpToolError("not_found", f"回测记录不存在: {run_id}")
            await session.commit()
            return {"run_id": run_id, "deleted": True}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_delete",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """把回测工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_backtest_strategy_list",
        description=(
            "列出所有支持回测的内置策略及其参数 schema(supports_backtest=true)。"
            "返回策略 kind、名称、描述及参数字段清单(类型/默认值/范围)。"
        ),
    )
    async def _strategy_list(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_strategy_list(app_context(ctx))

    @mcp.tool(
        name="finboard_backtest_run",
        description=(
            "同步运行回测(纸面撮合,不发真实订单),返回完整 metrics/"
            "equity_curve/fills/selection_snapshots 并落库。"
            "参数:strategy(如 ma_cross)、symbols、start/end(ISO 日期)、"
            "capital、adjust(qfq/hfq/none)、params(策略参数)、selection"
            "(因子选股配置)。复用 BacktestEngine + 数据源(akshare/tushare/yfinance)。"
        ),
    )
    async def _run(
        strategy: str,
        symbols: list[str],
        start: str,
        end: str,
        capital: str = "100000",
        adjust: str = "qfq",
        params: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        commission_rate: str = "0.0003",
        commission_min: str = "1",
        stamp_tax_rate: str = "0.0005",
        slippage_bps: str = "0",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_run(
            app_context(ctx),
            strategy=strategy,
            symbols=symbols,
            start=start,
            end=end,
            capital=Decimal(capital),
            adjust=adjust,
            params=params,
            selection=selection,
            commission_rate=Decimal(commission_rate),
            commission_min=Decimal(commission_min),
            stamp_tax_rate=Decimal(stamp_tax_rate),
            slippage_bps=Decimal(slippage_bps),
        )

    @mcp.tool(
        name="finboard_backtest_history_list",
        description="列出最近的回测历史记录(摘要,不含完整 equity/fills)。",
    )
    async def _history_list(
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_list(app_context(ctx), limit=limit)

    @mcp.tool(
        name="finboard_backtest_history_get",
        description="查询单条回测历史详情(含完整 equity_curve/fills/summary)。",
    )
    async def _history_get(
        run_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_get(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_backtest_history_delete",
        description="删除一条回测历史记录(写操作,mcp_readonly_only=true 时拒绝)。",
    )
    async def _history_delete(
        run_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_delete(app_context(ctx), run_id)
