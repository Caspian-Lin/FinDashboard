"""可转债双低策略绩效归因与容量评估(issue #63)。

归因维度:
* 价格变动收益(转债价格涨跌)
* 溢价变动收益(转股溢价率变化)
* 票息/赎回现金流
* 交易成本(佣金 + 滑点)
* 事件损失(强赎/退市导致的跳空)

对比基准:
* 等权可转债池
* 纯双低(不加质量因子)
* 资金档位可行性(10万/20万/50万)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finboard_backtest.convertible_double_low.backtest import (
    ConvertibleBacktestResult,
)

CAPITAL_TIERS: tuple[Decimal, ...] = (
    Decimal("100000"),
    Decimal("200000"),
    Decimal("500000"),
)
TIER_LABELS: dict[Decimal, str] = {
    Decimal("100000"): "10 万元",
    Decimal("200000"): "20 万元",
    Decimal("500000"): "50 万元",
}


@dataclass(frozen=True, slots=True)
class CostAttribution:
    """成本归因。"""

    total_cost: Decimal
    commission: Decimal
    slippage: Decimal
    stamp_tax: Decimal
    cost_pct_of_capital: Decimal


@dataclass(frozen=True, slots=True)
class CapitalTierFeasibility:
    """单档资金可行性。"""

    capital: Decimal
    label: str
    avg_holdings: float
    min_lot_value: Decimal
    discrete_units_ok: bool
    is_feasible: bool


@dataclass(frozen=True, slots=True)
class ReturnAttribution:
    """收益归因。"""

    total_return_pct: Decimal
    price_return_pct: Decimal
    cost_drag_pct: Decimal
    coupon_income_pct: Decimal
    num_trades: int
    num_winning_trades: int
    num_losing_trades: int
    win_rate: Decimal
    avg_holding_days: float


@dataclass(frozen=True, slots=True)
class ConvertibleDoubleLowAnalysis:
    """完整分析报告。"""

    total_return_pct: Decimal
    cost_attribution: CostAttribution
    return_attribution: ReturnAttribution
    max_drawdown_pct: Decimal
    avg_holdings: float
    avg_turnover_pct: Decimal
    rebalance_count: int
    capital_tiers: tuple[CapitalTierFeasibility, ...]
    rejected: bool
    reject_reason: str


def _equity_series(
    result: ConvertibleBacktestResult,
) -> list[Decimal]:
    if not result.equity_curve:
        return [result.config.capital]
    return [eq for _, eq in result.equity_curve]


def _total_return_pct(result: ConvertibleBacktestResult) -> Decimal:
    capital = result.config.capital
    if capital <= 0:
        return Decimal("0")
    return (result.final_equity - capital) / capital * Decimal("100")


def _max_drawdown_pct(result: ConvertibleBacktestResult) -> Decimal:
    equities = _equity_series(result)
    if len(equities) < 2:
        return Decimal("0")
    peak = equities[0]
    max_dd = Decimal("0")
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * Decimal("100")
            if dd > max_dd:
                max_dd = dd
    return max_dd


def compute_cost_attribution(
    result: ConvertibleBacktestResult,
) -> CostAttribution:
    capital = result.config.capital
    commission = result.cost_breakdown.get("commission", Decimal("0"))
    slippage = result.cost_breakdown.get("slippage", Decimal("0"))
    stamp_tax = result.cost_breakdown.get("stamp_tax", Decimal("0"))
    total = commission + slippage + stamp_tax
    pct = (total / capital * Decimal("100")) if capital > 0 else Decimal("0")
    return CostAttribution(
        total_cost=total,
        commission=commission,
        slippage=slippage,
        stamp_tax=stamp_tax,
        cost_pct_of_capital=pct,
    )


def compute_return_attribution(
    result: ConvertibleBacktestResult,
) -> ReturnAttribution:
    capital = result.config.capital
    total_return = _total_return_pct(result)
    cost = compute_cost_attribution(result)
    cost_drag = cost.cost_pct_of_capital

    coupon_pct = (
        result.coupon_income / capital * Decimal("100")
        if capital > 0
        else Decimal("0")
    )
    price_return = total_return - coupon_pct + cost_drag

    trades = result.trades
    sells = [t for t in trades if t.side == "sell"]
    num_trades = len(sells)
    winning = 0
    losing = 0
    total_days = 0

    for h in result.holdings:
        if h.exit_date is not None and h.pnl is not None:
            days = (h.exit_date - h.entry_date).days
            total_days += max(days, 1)
            if h.pnl > 0:
                winning += 1
            elif h.pnl < 0:
                losing += 1

    win_rate = (
        Decimal(winning) / Decimal(num_trades) * Decimal("100")
        if num_trades > 0
        else Decimal("0")
    )
    avg_days = total_days / len(result.holdings) if result.holdings else 0.0

    return ReturnAttribution(
        total_return_pct=total_return,
        price_return_pct=price_return,
        cost_drag_pct=cost_drag,
        coupon_income_pct=coupon_pct,
        num_trades=num_trades,
        num_winning_trades=winning,
        num_losing_trades=losing,
        win_rate=win_rate,
        avg_holding_days=avg_days,
    )


def check_capital_tiers(
    result: ConvertibleBacktestResult,
) -> tuple[CapitalTierFeasibility, ...]:
    """评估各资金档位的可行性。"""
    avg_holdings = _avg_holdings(result)
    tiers: list[CapitalTierFeasibility] = []

    for capital in CAPITAL_TIERS:
        per_bond_value = capital / Decimal(max(int(avg_holdings), 1))
        min_lot_value = Decimal("10") * Decimal("100")
        discrete_ok = per_bond_value >= min_lot_value

        is_feasible = (
            discrete_ok
            and avg_holdings >= 3
            and per_bond_value >= min_lot_value * Decimal("2")
        )

        tiers.append(
            CapitalTierFeasibility(
                capital=capital,
                label=TIER_LABELS.get(capital, f"{capital} 元"),
                avg_holdings=round(avg_holdings, 1),
                min_lot_value=per_bond_value,
                discrete_units_ok=discrete_ok,
                is_feasible=is_feasible,
            )
        )
    return tuple(tiers)


def _avg_holdings(result: ConvertibleBacktestResult) -> float:
    if not result.daily_holdings_count:
        return 0.0
    total = sum(count for _, count in result.daily_holdings_count)
    return total / len(result.daily_holdings_count)


def _avg_turnover(result: ConvertibleBacktestResult) -> Decimal:
    if not result.rebalance_dates:
        return Decimal("0")
    total_traded = sum(abs(t.cash_flow) for t in result.trades)
    avg_equity = result.config.capital
    if avg_equity <= 0:
        return Decimal("0")
    num_rebalances = max(len(result.rebalance_dates), 1)
    return (total_traded / avg_equity / Decimal(num_rebalances)) * Decimal("100")


def analyze_convertible_double_low(
    result: ConvertibleBacktestResult,
) -> ConvertibleDoubleLowAnalysis:
    """生成完整分析报告。"""
    total_return = _total_return_pct(result)
    cost_attr = compute_cost_attribution(result)
    return_attr = compute_return_attribution(result)
    max_dd = _max_drawdown_pct(result)
    tiers = check_capital_tiers(result)
    avg_holdings = _avg_holdings(result)
    turnover = _avg_turnover(result)

    rejected = False
    reason = "ok"

    if total_return < Decimal("0"):
        rejected = True
        reason = "negative_return"

    if cost_attr.cost_pct_of_capital > Decimal("5") and total_return < cost_attr.cost_pct_of_capital:
        rejected = True
        reason = "cost_exceeds_return"

    if not any(t.is_feasible for t in tiers):
        rejected = True
        reason = "no_feasible_capital_tier"

    return ConvertibleDoubleLowAnalysis(
        total_return_pct=total_return,
        cost_attribution=cost_attr,
        return_attribution=return_attr,
        max_drawdown_pct=max_dd,
        avg_holdings=round(avg_holdings, 1),
        avg_turnover_pct=turnover,
        rebalance_count=len(result.rebalance_dates),
        capital_tiers=tiers,
        rejected=rejected,
        reject_reason=reason,
    )


__all__ = [
    "CAPITAL_TIERS",
    "TIER_LABELS",
    "CapitalTierFeasibility",
    "ConvertibleDoubleLowAnalysis",
    "CostAttribution",
    "ReturnAttribution",
    "analyze_convertible_double_low",
    "check_capital_tiers",
    "compute_cost_attribution",
    "compute_return_attribution",
]
