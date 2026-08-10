"""``finboard.portfolio.*`` 工具 —— 组合计算(issue #128)。

4 个纯计算工具(无 DB 写入,无副作用):

- ``portfolio_allocate`` —— 目标权重分配(equal_weight / inverse_volatility /
  erc,含约束 + 风险报告)。对应 ``POST /api/portfolio/allocate``,调
  ``build_portfolio``。
- ``portfolio_sizing`` —— 离散手数 sizing(目标权重 → 考虑最小手数 / 资金约束
  的可执行手数 + 费用 / 保证金)。对应 ``POST /api/portfolio/sizing``,调
  ``solve_sizing``。
- ``portfolio_feasibility`` —— 固定资金档位(10万/20万/50万)可行性评估。
  对应 ``POST /api/portfolio/feasibility``,调 ``evaluate_capital_tiers``。
- ``portfolio_attribution`` —— 绩效归因(Brinson 式分解,需协方差)。
  对应 ``POST /api/portfolio/attribution``,调 ``compute_attribution``。

复用现有 ``finboard_backtest.portfolio`` 模块,不重复实现。输入参数与 REST 端点
一致。按 ROADMAP(#128 / #122)归类为「研究写」(自主执行),经
``_require_write_enabled`` 门控(与 ``strategy_validate`` 同级);纯计算无 DB 写
入,回滚 = 不调用即可。
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

__all__ = ["register"]


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。

    portfolio 工具按 ROADMAP 归类为「研究写,自主执行」(与 ``strategy_validate``
    同级 —— 纯计算但不属于只读查询类),故同样经此门控。
    """
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),portfolio 计算不可用",
        )


def _parse_date(value: str, *, field: str = "as_of") -> date_type:
    """把 ISO 字符串解析为 ``date``;失败 → ``invalid_argument``。"""
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise McpToolError(
            "invalid_argument", f"{field} 必须是 ISO 日期(YYYY-MM-DD): {value}"
        ) from exc


def _build_signals(
    signals_in: list[dict[str, Any]],
    *,
    as_of: date_type,
    strategy_id: str,
) -> list[Any]:
    """从 dict 列表构造 ``Signal`` 域对象。"""
    from finboard_backtest.portfolio import Signal

    out: list[Any] = []
    for i, s in enumerate(signals_in):
        try:
            out.append(
                Signal(
                    symbol=s["symbol"],
                    score=float(s["score"]),
                    confidence=float(s.get("confidence", 1.0)),
                    timestamp=as_of,
                    strategy_id=strategy_id,
                )
            )
        except KeyError as exc:
            raise McpToolError(
                "invalid_argument", f"signals[{i}] 缺少必填字段: {exc}"
            ) from exc
        except ValueError as exc:
            raise McpToolError(
                "invalid_argument", f"signals[{i}] 校验失败: {exc}"
            ) from exc
    return out


def _build_lot_map(lot_info_in: list[dict[str, Any]]) -> dict[str, Any]:
    """从 dict 列表构造 ``{code: AssetLotInfo}``;不包装 ValueError ——
    与 API 路由一致(路由未 try/except,依赖上游约束;MCP 把裸 ValueError 透传给
    ``_map_exception`` → ``invalid_argument``)。"""
    from finboard_backtest.portfolio import AssetLotInfo

    lot_map: dict[str, Any] = {}
    for i, li in enumerate(lot_info_in):
        if "code" not in li:
            raise McpToolError(
                "invalid_argument", f"lot_info[{i}] 缺少必填字段 code"
            )
        lot_map[li["code"]] = AssetLotInfo(
            code=li["code"],
            lot_size=int(li.get("lot_size", 100)),
            multiplier=float(li.get("multiplier", 1.0)),
            margin_rate=li.get("margin_rate"),
            commission_rate=li.get("commission_rate"),
            commission_min=li.get("commission_min"),
            stamp_tax_rate=li.get("stamp_tax_rate"),
            slippage_bps=float(li.get("slippage_bps", 0.0)),
            max_participation=li.get("max_participation"),
            available_volume=li.get("available_volume"),
            tradable=bool(li.get("tradable", True)),
            unavailable_reason=li.get("unavailable_reason"),
        )
    return lot_map


def _parse_weights(weights: dict[str, Any], *, field: str = "weights") -> dict[str, float]:
    """把 ``{code: number}`` 字典转为 ``dict[str, float]``。"""
    return {str(k): float(v) for k, v in weights.items()}


# --------------------------------------------------------------------------- #
# 工具 1: portfolio_allocate
# --------------------------------------------------------------------------- #
async def portfolio_allocate(
    app: McpAppContext,
    *,
    signals: list[dict[str, Any]],
    as_of: str,
    method: str = "equal_weight",
    strategy_id: str = "mcp",
    max_weight_per_asset: float = 0.25,
    max_weight_per_sleeve: float = 0.40,
    min_cash_buffer: float = 0.05,
    max_leverage: float = 1.0,
    long_only: bool = True,
    target_volatility: float | None = None,
    max_volatility: float | None = None,
    rebalance_threshold: float = 0.05,
    min_weight_to_trade: float = 0.001,
    returns_by_ticker: dict[str, list[float]] | None = None,
    sleeve_map: dict[str, str] | None = None,
    disabled_symbols: list[str] | None = None,
    current_weights: dict[str, float] | None = None,
    target_gross_exposure: float | None = None,
    betas: dict[str, float] | None = None,
    max_drawdown: float = 0.0,
    max_risk_contribution: float = 1.0,
    conflict_policy: str = "net",
    covariance_failure_mode: str = "fail_closed",
) -> ToolEnvelope:
    """目标权重分配(equal_weight / inverse_volatility / erc)+ 约束 + 风险报告。

    纯计算,无 DB 写入。对应 ``POST /api/portfolio/allocate``,调 ``build_portfolio``。
    返回 weights / weights_before_constraints / cash_buffer / gross_weight /
    net_weight / adjustments / risk。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        import numpy as np

        from finboard_backtest.portfolio import (
            AllocationError,
            CovarianceFailureMode,
            PortfolioBuildInput,
            PortfolioConstraints,
            SignalConflictPolicy,
            build_portfolio,
            estimate_covariance,
        )
        from finboard_backtest.portfolio.covariance import CovarianceEstimate

        if not signals:
            raise McpToolError("invalid_argument", "signals 不能为空")

        as_of_d = _parse_date(as_of)
        sig_objs = _build_signals(signals, as_of=as_of_d, strategy_id=strategy_id)

        try:
            constraints = PortfolioConstraints(
                max_weight_per_asset=max_weight_per_asset,
                max_weight_per_sleeve=max_weight_per_sleeve,
                min_cash_buffer=min_cash_buffer,
                max_leverage=max_leverage,
                target_volatility=target_volatility,
                max_volatility=max_volatility,
                rebalance_threshold=rebalance_threshold,
                min_weight_to_trade=min_weight_to_trade,
                max_risk_contribution=max_risk_contribution,
                long_only=long_only,
                covariance_failure_mode=CovarianceFailureMode(covariance_failure_mode),
            )
        except ValueError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        covariance: CovarianceEstimate | None = None
        if returns_by_ticker:
            try:
                covariance = estimate_covariance(
                    {
                        k: np.array(v, dtype=np.float64)
                        for k, v in returns_by_ticker.items()
                    }
                )
            except Exception as exc:
                if covariance_failure_mode == CovarianceFailureMode.FAIL_CLOSED.value:
                    raise McpToolError(
                        "invalid_argument", f"协方差估计失败: {exc}"
                    ) from exc
                covariance = None

        try:
            result = build_portfolio(
                PortfolioBuildInput(
                    signals=tuple(sig_objs),
                    method=method,
                    constraints=constraints,
                    covariance=covariance,
                    sleeve_map=sleeve_map or {},
                    disabled_symbols=frozenset(disabled_symbols or []),
                    current_weights=current_weights or {},
                    target_gross_exposure=target_gross_exposure,
                    betas=betas or {},
                    max_drawdown=max_drawdown,
                    conflict_policy=SignalConflictPolicy(conflict_policy),
                )
            )
        except (AllocationError, ValueError) as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        target = result.target_after_constraints
        return {
            "weights": [
                {"code": c, "weight": w} for c, w in target.weights.items()
            ],
            "weights_before_constraints": [
                {"code": c, "weight": w}
                for c, w in result.target_before_constraints.weights.items()
            ],
            "cash_buffer": target.cash_buffer,
            "gross_weight": target.gross_weight,
            "net_weight": target.net_exposure,
            "configured_max_leverage": target.max_leverage,
            "n_assets": target.n_assets,
            "contract_version": target.contract_version,
            "covariance_shrinkage": (
                covariance.shrinkage
                if covariance is not None and not result.covariance_fallback_used
                else None
            ),
            "covariance_fallback_used": result.covariance_fallback_used,
            "adjustments": [
                cast(dict[str, Any], to_jsonable(item))
                for item in result.adjustments
            ],
            "risk": cast(dict[str, Any], to_jsonable(result.risk)),
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.portfolio.allocate",
        arguments={
            "signals": signals,
            "as_of": as_of,
            "method": method,
            "strategy_id": strategy_id,
            "max_weight_per_asset": max_weight_per_asset,
            "max_weight_per_sleeve": max_weight_per_sleeve,
            "min_cash_buffer": min_cash_buffer,
            "max_leverage": max_leverage,
            "long_only": long_only,
            "target_volatility": target_volatility,
            "max_volatility": max_volatility,
            "rebalance_threshold": rebalance_threshold,
            "min_weight_to_trade": min_weight_to_trade,
            "sleeve_map": sleeve_map,
            "disabled_symbols": disabled_symbols,
            "current_weights": current_weights,
            "target_gross_exposure": target_gross_exposure,
            "betas": betas,
            "max_drawdown": max_drawdown,
            "max_risk_contribution": max_risk_contribution,
            "conflict_policy": conflict_policy,
            "covariance_failure_mode": covariance_failure_mode,
            "n_returns_tickers": len(returns_by_ticker) if returns_by_ticker else 0,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 工具 2: portfolio_sizing
# --------------------------------------------------------------------------- #
async def portfolio_sizing(
    app: McpAppContext,
    *,
    weights: dict[str, Any],
    as_of: str,
    capital: float,
    lot_info: list[dict[str, Any]],
    prices: dict[str, Any],
    strategy_id: str = "mcp",
    max_leverage: float = 1.0,
    long_only: bool = True,
    commission_rate: float = 0.0003,
    commission_min: float = 5.0,
    stamp_tax_rate: float = 0.0005,
) -> ToolEnvelope:
    """离散手数 sizing(目标权重 → 可执行手数 + 费用 / 保证金)。

    纯计算,无 DB 写入。对应 ``POST /api/portfolio/sizing``,调 ``solve_sizing``。
    返回 trades / total_capital / cash_before/after / est_commission/tax /
    total_turnover / n_active_trades / est_slippage / margin_required。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.portfolio import solve_sizing
        from finboard_backtest.portfolio.contracts import TargetWeight
        from finboard_backtest.portfolio.sizing import SizingError, SizingInput

        as_of_d = _parse_date(as_of)
        weight_map = _parse_weights(weights)

        try:
            target = TargetWeight(
                weights=weight_map,
                as_of=as_of_d,
                strategy_id=strategy_id,
                max_leverage=max_leverage,
                long_only=long_only,
            )
        except ValueError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        lot_map = _build_lot_map(lot_info)
        price_map = _parse_weights(prices, field="prices")

        try:
            plan = solve_sizing(
                SizingInput(
                    target=target,
                    capital=capital,
                    lot_info=lot_map,
                    prices=price_map,
                    commission_rate=commission_rate,
                    commission_min=commission_min,
                    stamp_tax_rate=stamp_tax_rate,
                )
            )
        except SizingError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        return {
            "trades": [
                cast(dict[str, Any], to_jsonable(t)) for t in plan.trades
            ],
            "total_capital": plan.total_capital,
            "cash_before": plan.cash_before,
            "cash_after": plan.cash_after,
            "est_commission": plan.est_commission,
            "est_tax": plan.est_tax,
            "total_turnover": plan.total_turnover,
            "n_active_trades": plan.n_active_trades,
            "est_slippage": plan.est_slippage,
            "margin_required": plan.margin_required,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.portfolio.sizing",
        arguments={
            "n_weights": len(weights),
            "as_of": as_of,
            "capital": capital,
            "n_lot_info": len(lot_info),
            "n_prices": len(prices),
            "strategy_id": strategy_id,
            "max_leverage": max_leverage,
            "long_only": long_only,
            "commission_rate": commission_rate,
            "commission_min": commission_min,
            "stamp_tax_rate": stamp_tax_rate,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 工具 3: portfolio_feasibility
# --------------------------------------------------------------------------- #
async def portfolio_feasibility(
    app: McpAppContext,
    *,
    weights: dict[str, Any],
    as_of: str,
    lot_info: list[dict[str, Any]],
    prices: dict[str, Any],
    strategy_id: str = "mcp",
    max_leverage: float = 1.0,
    long_only: bool = True,
) -> ToolEnvelope:
    """固定资金档位(10万/20万/50万)可行性评估。

    纯计算,无 DB 写入。对应 ``POST /api/portfolio/feasibility``,调
    ``evaluate_capital_tiers``。返回 list[tier_feasibility](每档一项:
    feasible / cash_utilization / tracking_error / unfillable_symbols /
    capacity_pressure / margin_required / estimated_costs / reasons)。
    """

    async def _do() -> list[dict[str, Any]]:
        await _require_write_enabled(app)
        from finboard_backtest.portfolio import (
            CapitalFeasibilityInput,
            evaluate_capital_tiers,
        )
        from finboard_backtest.portfolio.contracts import TargetWeight
        from finboard_backtest.portfolio.sizing import SizingError

        as_of_d = _parse_date(as_of)
        weight_map = _parse_weights(weights)

        try:
            target = TargetWeight(
                weights=weight_map,
                as_of=as_of_d,
                strategy_id=strategy_id,
                max_leverage=max_leverage,
                long_only=long_only,
            )
        except ValueError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        lot_map = _build_lot_map(lot_info)
        price_map = _parse_weights(prices, field="prices")

        try:
            results = evaluate_capital_tiers(
                CapitalFeasibilityInput(
                    target=target,
                    lot_info=lot_map,
                    prices=price_map,
                )
            )
        except SizingError as exc:
            raise McpToolError("invalid_argument", str(exc)) from exc

        return [
            {
                "tier": item.tier,
                "capital": item.capital,
                "feasible": item.feasible,
                "cash_utilization": item.cash_utilization,
                "tracking_error": item.tracking_error,
                "unfillable_symbols": list(item.unfillable_symbols),
                "capacity_pressure": item.capacity_pressure,
                "margin_required": item.margin_required,
                "estimated_costs": item.estimated_costs,
                "reasons": list(item.reasons),
            }
            for item in results
        ]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.portfolio.feasibility",
        arguments={
            "n_weights": len(weights),
            "as_of": as_of,
            "n_lot_info": len(lot_info),
            "n_prices": len(prices),
            "strategy_id": strategy_id,
            "max_leverage": max_leverage,
            "long_only": long_only,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 工具 4: portfolio_attribution
# --------------------------------------------------------------------------- #
async def portfolio_attribution(
    app: McpAppContext,
    *,
    weights_history: list[dict[str, float]],
    returns_by_ticker: dict[str, list[float]],
    sleeve_map: dict[str, str] | None = None,
) -> ToolEnvelope:
    """绩效归因分解(需协方差)。

    纯计算,无 DB 写入。对应 ``POST /api/portfolio/attribution``,调
    ``compute_attribution``。返回 by_asset / by_sleeve / total_return /
    total_risk / total_turnover / max_drawdown / cash_utilization / leverage_ratio。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        import numpy as np

        from finboard_backtest.portfolio import compute_attribution, estimate_covariance

        if not weights_history:
            raise McpToolError("invalid_argument", "weights_history 不能为空")

        returns_arrays = {
            k: np.array(v, dtype=np.float64)
            for k, v in returns_by_ticker.items()
        }

        try:
            covariance = estimate_covariance(returns_arrays)
        except Exception as exc:
            raise McpToolError(
                "invalid_argument", f"协方差估计失败: {exc}"
            ) from exc

        report = compute_attribution(
            weights_history=weights_history,
            returns_by_ticker=returns_arrays,
            covariance=covariance,
            sleeve_map=sleeve_map,
        )

        return {
            "by_asset": [
                cast(dict[str, Any], to_jsonable(ac)) for ac in report.by_asset
            ],
            "by_sleeve": [
                cast(dict[str, Any], to_jsonable(sc)) for sc in report.by_sleeve
            ],
            "total_return": report.total_return,
            "total_risk": report.total_risk,
            "total_turnover": report.total_turnover,
            "max_drawdown": report.max_drawdown,
            "cash_utilization": report.cash_utilization,
            "leverage_ratio": report.leverage_ratio,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.portfolio.attribution",
        arguments={
            "n_weights_history": len(weights_history),
            "n_returns_tickers": len(returns_by_ticker),
            "has_sleeve_map": sleeve_map is not None,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """注册 ``finboard.portfolio.*`` 工具(4 个纯计算工具)。"""

    @mcp.tool(
        name="finboard_portfolio_allocate",
        description=(
            "组合目标权重分配(纯计算,无 DB 写入)。输入信号清单 + 方法"
            "(equal_weight/inverse_volatility/erc)+ 约束,返回目标权重 / 约束前权重"
            " / cash_buffer / gross/net_weight / 约束调整 / 风险报告。"
            "对应 POST /api/portfolio/allocate,调 build_portfolio。"
            "研究写操作(自主执行,经 mcp_readonly_only 门控)。"
        ),
    )
    async def _allocate(
        signals: list[dict[str, Any]],
        as_of: str,
        method: str = "equal_weight",
        strategy_id: str = "mcp",
        max_weight_per_asset: float = 0.25,
        max_weight_per_sleeve: float = 0.40,
        min_cash_buffer: float = 0.05,
        max_leverage: float = 1.0,
        long_only: bool = True,
        target_volatility: float | None = None,
        max_volatility: float | None = None,
        rebalance_threshold: float = 0.05,
        min_weight_to_trade: float = 0.001,
        returns_by_ticker: dict[str, list[float]] | None = None,
        sleeve_map: dict[str, str] | None = None,
        disabled_symbols: list[str] | None = None,
        current_weights: dict[str, float] | None = None,
        target_gross_exposure: float | None = None,
        betas: dict[str, float] | None = None,
        max_drawdown: float = 0.0,
        max_risk_contribution: float = 1.0,
        conflict_policy: str = "net",
        covariance_failure_mode: str = "fail_closed",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await portfolio_allocate(
            app_context(ctx),
            signals=signals,
            as_of=as_of,
            method=method,
            strategy_id=strategy_id,
            max_weight_per_asset=max_weight_per_asset,
            max_weight_per_sleeve=max_weight_per_sleeve,
            min_cash_buffer=min_cash_buffer,
            max_leverage=max_leverage,
            long_only=long_only,
            target_volatility=target_volatility,
            max_volatility=max_volatility,
            rebalance_threshold=rebalance_threshold,
            min_weight_to_trade=min_weight_to_trade,
            returns_by_ticker=returns_by_ticker,
            sleeve_map=sleeve_map,
            disabled_symbols=disabled_symbols,
            current_weights=current_weights,
            target_gross_exposure=target_gross_exposure,
            betas=betas,
            max_drawdown=max_drawdown,
            max_risk_contribution=max_risk_contribution,
            conflict_policy=conflict_policy,
            covariance_failure_mode=covariance_failure_mode,
        )

    @mcp.tool(
        name="finboard_portfolio_sizing",
        description=(
            "离散手数 sizing(纯计算,无 DB 写入)。目标权重 + 资金 + lot_info + 价格 →"
            " 可执行手数 + 费用 / 保证金 / 滑点。对应 POST /api/portfolio/sizing,"
            "调 solve_sizing。研究写操作(自主执行,经 mcp_readonly_only 门控)。"
        ),
    )
    async def _sizing(
        weights: dict[str, Any],
        as_of: str,
        capital: float,
        lot_info: list[dict[str, Any]],
        prices: dict[str, Any],
        strategy_id: str = "mcp",
        max_leverage: float = 1.0,
        long_only: bool = True,
        commission_rate: float = 0.0003,
        commission_min: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await portfolio_sizing(
            app_context(ctx),
            weights=weights,
            as_of=as_of,
            capital=capital,
            lot_info=lot_info,
            prices=prices,
            strategy_id=strategy_id,
            max_leverage=max_leverage,
            long_only=long_only,
            commission_rate=commission_rate,
            commission_min=commission_min,
            stamp_tax_rate=stamp_tax_rate,
        )

    @mcp.tool(
        name="finboard_portfolio_feasibility",
        description=(
            "固定资金档位可行性评估(纯计算,无 DB 写入)。目标权重 + lot_info + 价格 →"
            " 10万/20万/50万 三档的可行 / 资金利用率 / 跟踪误差 / 不可成交标的 / 容量压力"
            " / 保证金 / 估算费用 / 原因。对应 POST /api/portfolio/feasibility,"
            "调 evaluate_capital_tiers。研究写操作(自主执行,经 mcp_readonly_only 门控)。"
        ),
    )
    async def _feasibility(
        weights: dict[str, Any],
        as_of: str,
        lot_info: list[dict[str, Any]],
        prices: dict[str, Any],
        strategy_id: str = "mcp",
        max_leverage: float = 1.0,
        long_only: bool = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await portfolio_feasibility(
            app_context(ctx),
            weights=weights,
            as_of=as_of,
            lot_info=lot_info,
            prices=prices,
            strategy_id=strategy_id,
            max_leverage=max_leverage,
            long_only=long_only,
        )

    @mcp.tool(
        name="finboard_portfolio_attribution",
        description=(
            "绩效归因分解(纯计算,无 DB 写入,需协方差)。权重历史 + 收益序列 →"
            " by_asset / by_sleeve 贡献分解 + total_return/risk/turnover/"
            "max_drawdown/cash_utilization/leverage_ratio。"
            "对应 POST /api/portfolio/attribution,调 compute_attribution。"
            "研究写操作(自主执行,经 mcp_readonly_only 门控)。"
        ),
    )
    async def _attribution(
        weights_history: list[dict[str, float]],
        returns_by_ticker: dict[str, list[float]],
        sleeve_map: dict[str, str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await portfolio_attribution(
            app_context(ctx),
            weights_history=weights_history,
            returns_by_ticker=returns_by_ticker,
            sleeve_map=sleeve_map,
        )
