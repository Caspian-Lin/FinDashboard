"""TSMOM 回测归因分析 —— 收益拆分 / 资金可行性 / 压力测试。

收益归因维度:
1. **方向收益**(direction return):持仓方向与价格变动的一致性。
2. **展期收益**(roll yield):换月产生的价差损益(backwardation/contango)。
3. **手续费**(commission):每次成交产生的费用。
4. **滑点**(slippage):成交价与理论价的偏差。
5. **保证金资金成本**(margin interest):保证金占用的机会成本。

资金可行性:按整数手计算,10/20/50 万元分别判断可交易性。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.metrics import sharpe_from_equity_values

from .backtest import TsmomResult
from .contracts import ContractSpec
from .roll import ActiveContractSeries


@dataclass(frozen=True, slots=True)
class ReturnAttribution:
    """收益归因。"""

    total_return: float
    direction_return: float
    roll_yield: float
    commission_cost: float
    slippage_cost: float
    margin_interest: float
    direction_pct: float
    roll_pct: float

    def as_dict(self) -> dict[str, float]:
        return {
            "total_return": self.total_return,
            "direction_return": self.direction_return,
            "roll_yield": self.roll_yield,
            "commission_cost": self.commission_cost,
            "slippage_cost": self.slippage_cost,
            "margin_interest": self.margin_interest,
            "direction_pct": self.direction_pct,
            "roll_pct": self.roll_pct,
        }


@dataclass(frozen=True, slots=True)
class CostAttribution:
    """成本拆分。"""

    total_cost: float
    commission: float
    slippage: float
    margin_interest: float
    cost_as_pct_of_nav: float
    cost_per_trade: float
    turnover_notional: float

    def as_dict(self) -> dict[str, float]:
        return {
            "total_cost": self.total_cost,
            "commission": self.commission,
            "slippage": self.slippage,
            "margin_interest": self.margin_interest,
            "cost_as_pct_of_nav": self.cost_as_pct_of_nav,
            "cost_per_trade": self.cost_per_trade,
            "turnover_notional": self.turnover_notional,
        }


@dataclass(frozen=True, slots=True)
class CapitalTierFeasibility:
    """资金档位可行性。"""

    tier: str
    capital: float
    feasible: bool
    min_contract_notional: float
    max_contracts: int
    avg_gross_exposure: float
    avg_margin_usage: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "capital": self.capital,
            "feasible": self.feasible,
            "min_contract_notional": self.min_contract_notional,
            "max_contracts": self.max_contracts,
            "avg_gross_exposure": self.avg_gross_exposure,
            "avg_margin_usage": self.avg_margin_usage,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CrisisPerformance:
    """危机期表现。"""

    max_drawdown: float
    max_drawdown_start_idx: int
    max_drawdown_end_idx: int
    max_drawdown_duration: int
    worst_daily_return: float
    volatility_annualized: float
    trades_during_crisis: int

    def as_dict(self) -> dict[str, object]:
        return {
            "max_drawdown": self.max_drawdown,
            "max_drawdown_start_idx": self.max_drawdown_start_idx,
            "max_drawdown_end_idx": self.max_drawdown_end_idx,
            "max_drawdown_duration": self.max_drawdown_duration,
            "worst_daily_return": self.worst_daily_return,
            "volatility_annualized": self.volatility_annualized,
            "trades_during_crisis": self.trades_during_crisis,
        }


@dataclass(frozen=True, slots=True)
class StressTestResult:
    """压力测试结果。"""

    scenario: str
    base_return: float
    stressed_return: float
    return_degradation: float
    survives: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "base_return": self.base_return,
            "stressed_return": self.stressed_return,
            "return_degradation": self.return_degradation,
            "survives": self.survives,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TsmomAnalysis:
    """TSMOM 综合分析报告。"""

    total_return: float
    annualized_return: float
    sharpe_ratio: float
    max_drawdown: float
    calmar_ratio: float
    sortino_ratio: float
    win_rate: float
    trade_count: int
    turnover_rate: float
    return_attribution: ReturnAttribution
    cost_attribution: CostAttribution
    capital_tiers: tuple[CapitalTierFeasibility, ...]
    crisis_performance: CrisisPerformance
    stress_tests: tuple[StressTestResult, ...]
    avg_leverage: float
    max_leverage: float
    rejected: bool
    rejection_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "calmar_ratio": self.calmar_ratio,
            "sortino_ratio": self.sortino_ratio,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "turnover_rate": self.turnover_rate,
            "return_attribution": self.return_attribution.as_dict(),
            "cost_attribution": self.cost_attribution.as_dict(),
            "capital_tiers": [t.as_dict() for t in self.capital_tiers],
            "crisis_performance": self.crisis_performance.as_dict(),
            "stress_tests": [s.as_dict() for s in self.stress_tests],
            "avg_leverage": self.avg_leverage,
            "max_leverage": self.max_leverage,
            "rejected": self.rejected,
            "rejection_reasons": list(self.rejection_reasons),
        }


def _compute_sharpe(equity_curve: Sequence[float], risk_free: float = 0.03) -> float:
    """口径(issue #262):rf=3%/年、样本标准差(ddof=1)、√252;委托统一实现。"""
    if len(equity_curve) < 2:
        return 0.0
    return sharpe_from_equity_values(equity_curve, risk_free_annual=risk_free, ddof=1)


def _compute_sortino(equity_curve: Sequence[float], risk_free: float = 0.03) -> float:
    if len(equity_curve) < 2:
        return 0.0
    rets = [equity_curve[i] / equity_curve[i - 1] - 1.0 for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    mean_r = sum(rets) / len(rets)
    downside = [min(0.0, r - risk_free / 252.0) for r in rets]
    ds_var = sum(d ** 2 for d in downside) / len(downside) if downside else 0.0
    ds_std = math.sqrt(ds_var) if ds_var > 0 else 0.0
    if ds_std == 0:
        return 0.0
    daily_excess = mean_r - risk_free / 252.0
    return daily_excess * math.sqrt(252.0) / ds_std


def _compute_max_drawdown(equity_curve: Sequence[float]) -> tuple[float, int, int, int]:
    if len(equity_curve) < 2:
        return 0.0, 0, 0, 0
    peak = equity_curve[0]
    peak_idx = 0
    mdd = 0.0
    mdd_start = 0
    mdd_end = 0
    mdd_duration = 0
    current_duration = 0
    for i, eq in enumerate(equity_curve):
        if eq > peak:
            peak = eq
            peak_idx = i
            current_duration = 0
        else:
            current_duration = i - peak_idx
            dd = peak - eq
            dd_pct = dd / peak if peak > 0 else 0.0
            if dd_pct > mdd:
                mdd = dd_pct
                mdd_start = peak_idx
                mdd_end = i
                mdd_duration = current_duration
    return mdd, mdd_start, mdd_end, mdd_duration


def _compute_annualized_return(equity_curve: Sequence[float]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    total_days = len(equity_curve) - 1
    if total_days <= 0:
        return 0.0
    total_ret = equity_curve[-1] / equity_curve[0] - 1.0 if equity_curve[0] > 0 else 0.0
    years = total_days / 252.0
    if years <= 0:
        return 0.0
    return float((1.0 + total_ret) ** (1.0 / years) - 1.0)


def compute_return_attribution(
    result: TsmomResult,
    initial_capital: float,
) -> ReturnAttribution:
    """拆分收益来源。"""
    total_ret = result.total_return
    commission = result.total_commission / initial_capital
    slippage = result.total_slippage / initial_capital

    roll_yield_total = sum(ev.roll_yield for ev in result.roll_events) if result.roll_events else 0.0
    roll_contribution = roll_yield_total * 0.1  # heuristic: ~10% of roll yield translates to P&L

    avg_margin = sum(r.margin_used for r in result.daily_records) / max(len(result.daily_records), 1)
    margin_interest = avg_margin * result.config.margin_interest_rate / 252.0 * len(result.equity_curve) / initial_capital

    direction_ret = total_ret + commission + slippage + margin_interest - roll_contribution

    return ReturnAttribution(
        total_return=total_ret,
        direction_return=direction_ret,
        roll_yield=roll_contribution,
        commission_cost=-commission,
        slippage_cost=-slippage,
        margin_interest=-margin_interest,
        direction_pct=direction_ret / total_ret if abs(total_ret) > 1e-10 else 0.0,
        roll_pct=roll_contribution / total_ret if abs(total_ret) > 1e-10 else 0.0,
    )


def compute_cost_attribution(
    result: TsmomResult,
    initial_capital: float,
) -> CostAttribution:
    """拆分成本来源。"""
    commission = result.total_commission
    slippage = result.total_slippage
    avg_margin = sum(r.margin_used for r in result.daily_records) / max(len(result.daily_records), 1)
    margin_interest = avg_margin * result.config.margin_interest_rate / 252.0 * len(result.equity_curve)
    total_cost = commission + slippage + margin_interest
    turnover_notional = sum(t.notional for t in result.trades)

    return CostAttribution(
        total_cost=total_cost,
        commission=commission,
        slippage=slippage,
        margin_interest=margin_interest,
        cost_as_pct_of_nav=total_cost / initial_capital if initial_capital > 0 else 0.0,
        cost_per_trade=total_cost / max(result.trade_count, 1),
        turnover_notional=turnover_notional,
    )


def check_capital_tiers(
    specs: Mapping[str, ContractSpec],
    result: TsmomResult,
    initial_capital: float,
) -> tuple[CapitalTierFeasibility, ...]:
    """检查 10/20/50 万元资金档位可行性。"""
    tiers_data = [
        ("10万", 100_000.0),
        ("20万", 200_000.0),
        ("50万", 500_000.0),
    ]

    min_notional = min(
        spec.multiplier * 3500.0  # assume ~3500 index points or bond price
        for spec in specs.values()
    )

    avg_gross = sum(r.gross_exposure for r in result.daily_records) / max(len(result.daily_records), 1)
    avg_margin_pct = sum(
        r.margin_used / r.equity for r in result.daily_records if r.equity > 0
    ) / max(len(result.daily_records), 1)

    results: list[CapitalTierFeasibility] = []
    for tier_name, capital in tiers_data:
        max_contracts = int(capital * 0.3 / min_notional) if min_notional > 0 else 0
        feasible = max_contracts >= 1 and capital * avg_margin_pct * 0.5 <= capital

        reason = ""
        if max_contracts < 1:
            reason = f"最小合约名义价值 {min_notional:.0f} 元超过 30% 资金上限"
        elif not feasible:
            reason = "保证金占用过高"
        else:
            reason = f"可交易 {max_contracts} 手, 平均杠杆 {avg_gross / initial_capital:.2f}x"

        results.append(CapitalTierFeasibility(
            tier=tier_name,
            capital=capital,
            feasible=feasible,
            min_contract_notional=min_notional,
            max_contracts=max_contracts,
            avg_gross_exposure=avg_gross * (capital / initial_capital) if initial_capital > 0 else 0.0,
            avg_margin_usage=avg_margin_pct,
            reason=reason,
        ))

    return tuple(results)


def compute_crisis_performance(result: TsmomResult) -> CrisisPerformance:
    """危机期表现(基于全程最大回撤)。"""
    eq = result.equity_curve
    mdd, mdd_start, mdd_end, mdd_dur = _compute_max_drawdown(eq)

    if len(eq) < 2:
        return CrisisPerformance(
            max_drawdown=0.0, max_drawdown_start_idx=0, max_drawdown_end_idx=0,
            max_drawdown_duration=0, worst_daily_return=0.0,
            volatility_annualized=0.0, trades_during_crisis=0,
        )

    daily_rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1] > 0]
    worst = min(daily_rets) if daily_rets else 0.0
    mean_r = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    var = sum((r - mean_r) ** 2 for r in daily_rets) / max(len(daily_rets) - 1, 1)
    vol_annual = math.sqrt(var) * math.sqrt(252.0) if var > 0 else 0.0

    trades_in_crisis = sum(1 for t in result.trades if mdd_start <= t.bar_index <= mdd_end)

    return CrisisPerformance(
        max_drawdown=mdd,
        max_drawdown_start_idx=mdd_start,
        max_drawdown_end_idx=mdd_end,
        max_drawdown_duration=mdd_dur,
        worst_daily_return=worst,
        volatility_annualized=vol_annual,
        trades_during_crisis=trades_in_crisis,
    )


def compute_stress_tests(
    result: TsmomResult,
    *,
    closes_by_symbol: Mapping[str, Sequence[float]],
    opens_by_symbol: Mapping[str, Sequence[float]],
    volumes_by_symbol: Mapping[str, Sequence[float]],
    specs: Mapping[str, ContractSpec],
    active_series: Mapping[str, ActiveContractSeries] | None = None,
) -> tuple[StressTestResult, ...]:
    """成本和保证金压力测试。"""
    from .backtest import run_backtest

    base_ret = result.total_return
    tests: list[StressTestResult] = []

    # Cost x2
    doubled_cfg = result.config.cost_multiplier_config(2.0)
    stressed = run_backtest(
        closes_by_symbol=closes_by_symbol,
        opens_by_symbol=opens_by_symbol,
        volumes_by_symbol=volumes_by_symbol,
        specs=specs,
        config=doubled_cfg,
        active_series=active_series,
    )
    degradation = base_ret - stressed.total_return
    tests.append(StressTestResult(
        scenario="cost_x2",
        base_return=base_ret,
        stressed_return=stressed.total_return,
        return_degradation=degradation,
        survives=stressed.total_return > 0,
        reason="成本翻倍后正收益" if stressed.total_return > 0 else "成本翻倍后亏损",
    ))

    # Margin x2
    margin_cfg = result.config.margin_multiplier_config(2.0)
    stressed2 = run_backtest(
        closes_by_symbol=closes_by_symbol,
        opens_by_symbol=opens_by_symbol,
        volumes_by_symbol=volumes_by_symbol,
        specs=specs,
        config=margin_cfg,
        active_series=active_series,
    )
    degradation2 = base_ret - stressed2.total_return
    tests.append(StressTestResult(
        scenario="margin_x2",
        base_return=base_ret,
        stressed_return=stressed2.total_return,
        return_degradation=degradation2,
        survives=stressed2.total_return > 0,
        reason="保证金翻倍后正收益" if stressed2.total_return > 0 else "保证金翻倍后亏损",
    ))

    return tuple(tests)


def analyze_tsmom(
    result: TsmomResult,
    *,
    closes_by_symbol: Mapping[str, Sequence[float]],
    opens_by_symbol: Mapping[str, Sequence[float]],
    volumes_by_symbol: Mapping[str, Sequence[float]],
    specs: Mapping[str, ContractSpec],
    initial_capital: float = 100_000.0,
    active_series: Mapping[str, ActiveContractSeries] | None = None,
) -> TsmomAnalysis:
    """TSMOM 综合分析入口。"""
    eq = result.equity_curve
    total_ret = result.total_return
    ann_ret = _compute_annualized_return(eq)
    sharpe = _compute_sharpe(eq)
    sortino = _compute_sortino(eq)
    mdd, _, _, _ = _compute_max_drawdown(eq)
    calmar = ann_ret / abs(mdd) if abs(mdd) > 1e-10 else 0.0

    win_count = sum(1 for t in result.trades if t.action.value.startswith(("open",)) and t.fill_price > 0)
    win_rate = win_count / max(result.trade_count, 1)

    turnover_notional = sum(t.notional for t in result.trades)
    turnover_rate = turnover_notional / (initial_capital * max(len(eq), 1))

    ret_attr = compute_return_attribution(result, initial_capital)
    cost_attr = compute_cost_attribution(result, initial_capital)
    tiers = check_capital_tiers(specs, result, initial_capital)
    crisis = compute_crisis_performance(result)

    try:
        stress = compute_stress_tests(
            result,
            closes_by_symbol=closes_by_symbol,
            opens_by_symbol=opens_by_symbol,
            volumes_by_symbol=volumes_by_symbol,
            specs=specs,
            active_series=active_series,
        )
    except Exception:
        stress = ()

    leverages = [r.gross_exposure / r.equity for r in result.daily_records if r.equity > 0]
    avg_lev = sum(leverages) / len(leverages) if leverages else 0.0
    max_lev = max(leverages) if leverages else 0.0

    rejections: list[str] = []
    if total_ret < 0:
        rejections.append("负收益")
    if sharpe < 0:
        rejections.append("Sharpe < 0")
    if not any(t.feasible for t in tiers):
        rejections.append("无可行资金档位")
    for s in stress:
        if not s.survives:
            rejections.append(f"压力测试失败: {s.scenario}")

    return TsmomAnalysis(
        total_return=total_ret,
        annualized_return=ann_ret,
        sharpe_ratio=sharpe,
        max_drawdown=mdd,
        calmar_ratio=calmar,
        sortino_ratio=sortino,
        win_rate=win_rate,
        trade_count=result.trade_count,
        turnover_rate=turnover_rate,
        return_attribution=ret_attr,
        cost_attribution=cost_attr,
        capital_tiers=tiers,
        crisis_performance=crisis,
        stress_tests=stress,
        avg_leverage=avg_lev,
        max_leverage=max_lev,
        rejected=bool(rejections),
        rejection_reasons=tuple(rejections),
    )


__all__ = [
    "CapitalTierFeasibility",
    "CostAttribution",
    "CrisisPerformance",
    "ReturnAttribution",
    "StressTestResult",
    "TsmomAnalysis",
    "analyze_tsmom",
    "check_capital_tiers",
    "compute_cost_attribution",
    "compute_crisis_performance",
    "compute_return_attribution",
    "compute_stress_tests",
]
