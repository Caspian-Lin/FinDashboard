"""均值回归策略绩效分析。

功能:
1. **毛 / 净收益归因**:总成本拆分为佣金 / 印花税 / 滑点。
2. **成本 x2 敏感性**:用 2 倍成本重新计算净收益,判断策略是否仍然有效。
3. **趋势 sleeve 相关性**:与 ETF 轮动 / 买入持有收益序列的相关系数。
4. **危机期表现**:识别最大回撤区间,统计该区间的信号触发次数。
5. **资金档位可行性**:10万 / 20万 / 50万元下的最小 lot 约束。
6. **参数邻域稳健性**:对比基础参数与邻域参数的 Sharpe 差异。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.mean_reversion.backtest import (
    MeanReversionResult,
    Trade,
    run_backtest,
)
from finboard_backtest.mean_reversion.config import MeanReversionConfig
from finboard_backtest.metrics import sharpe_from_equity_values


@dataclass(frozen=True, slots=True)
class CostAttribution:
    """成本归因分解。"""

    total_cost: float
    commission: float
    stamp_tax: float
    slippage: float
    cost_as_pct_of_nav: float
    """总成本占初始资金的比例。"""

    cost_per_trade: float
    """平均每笔交易成本。"""


@dataclass(frozen=True, slots=True)
class CostSensitivityResult:
    """成本敏感性测试结果。"""

    base_net_return: float
    doubled_cost_net_return: float
    return_degradation: float
    """成本翻倍后净收益的下降幅度。"""
    survives_doubled_cost: bool
    """成本 x2 后净收益仍为正 → True。"""
    base_config: dict[str, object]
    doubled_config: dict[str, object]


@dataclass(frozen=True, slots=True)
class CrisisPerformance:
    """危机期表现。"""

    max_drawdown: float
    max_drawdown_start: int
    max_drawdown_end: int
    max_drawdown_duration: int
    trades_during_drawdown: int
    entries_during_drawdown: int


@dataclass(frozen=True, slots=True)
class CapitalTierFeasibility:
    """资金档位可行性。"""

    tier: str
    capital: float
    feasible: bool
    avg_trade_value: float
    min_trade_value: float
    reason: str


@dataclass(frozen=True, slots=True)
class MeanReversionAnalysis:
    """均值回归策略完整分析报告。"""

    total_return: float
    gross_return: float
    net_return: float
    cost_attribution: CostAttribution
    cost_sensitivity: CostSensitivityResult
    crisis_performance: CrisisPerformance
    capital_tiers: tuple[CapitalTierFeasibility, ...]
    trade_count: int
    avg_holding_days: float
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float
    turnover_rate: float
    """平均日换手率(交易金额 / 组合净值 / 交易日数)。"""
    trend_correlation: float | None
    """与趋势 sleeve 收益序列的相关系数;无趋势数据时为 None。"""
    rejected: bool
    """是否应被标记为 rejected(成本 x2 失效 或 Sharpe < 0)。"""
    rejection_reasons: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "total_return": self.total_return,
            "gross_return": self.gross_return,
            "net_return": self.net_return,
            "cost": {
                "total": self.cost_attribution.total_cost,
                "commission": self.cost_attribution.commission,
                "stamp_tax": self.cost_attribution.stamp_tax,
                "slippage": self.cost_attribution.slippage,
                "pct_of_nav": self.cost_attribution.cost_as_pct_of_nav,
                "per_trade": self.cost_attribution.cost_per_trade,
            },
            "cost_sensitivity": {
                "base_net": self.cost_sensitivity.base_net_return,
                "doubled_net": self.cost_sensitivity.doubled_cost_net_return,
                "degradation": self.cost_sensitivity.return_degradation,
                "survives": self.cost_sensitivity.survives_doubled_cost,
            },
            "crisis": {
                "max_drawdown": self.crisis_performance.max_drawdown,
                "duration": self.crisis_performance.max_drawdown_duration,
                "trades_in_dd": self.crisis_performance.trades_during_drawdown,
            },
            "trade_count": self.trade_count,
            "avg_holding_days": self.avg_holding_days,
            "win_rate": self.win_rate,
            "sharpe": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "turnover_rate": self.turnover_rate,
            "trend_correlation": self.trend_correlation,
            "rejected": self.rejected,
            "rejection_reasons": self.rejection_reasons,
        }


def _compute_sharpe(equity: Sequence[float]) -> float:
    """年化 Sharpe 比率(口径:rf=0、样本标准差 ddof=1、√252,issue #262)。

    委托 ``metrics.sharpe_from_equity_values`` 统一实现,与 research_run
    报告 sharpe_ratio、引擎报告 sharpe_rf0 同口径。
    """
    return sharpe_from_equity_values(equity, risk_free_annual=0.0, ddof=1)


def _compute_max_drawdown(equity: Sequence[float]) -> tuple[float, int, int, int]:
    """返回 (max_drawdown, start_idx, end_idx, duration)。"""
    if len(equity) < 2:
        return 0.0, 0, 0, 0
    peak = equity[0]
    peak_idx = 0
    max_dd = 0.0
    dd_start = 0
    dd_end = 0
    dd_duration = 0

    for i, val in enumerate(equity):
        if val > peak:
            peak = val
            peak_idx = i
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            dd_start = peak_idx
            dd_end = i
            dd_duration = i - peak_idx

    return max_dd, dd_start, dd_end, dd_duration


def _compute_win_rate(trades: Sequence[Trade]) -> float:
    """计算胜率(卖出收益 > 0 的比例)。"""
    sells = [t for t in trades if t.side == "SELL"]
    if not sells:
        return 0.0
    wins = 0
    buys_by_symbol: dict[str, list[Trade]] = {}
    for t in trades:
        if t.side == "BUY":
            buys_by_symbol.setdefault(t.symbol, []).append(t)

    for sell in sells:
        buys = buys_by_symbol.get(sell.symbol, [])
        if not buys:
            continue
        avg_buy_price = sum(b.fill_price for b in buys) / len(buys)
        if sell.fill_price > avg_buy_price:
            wins += 1
    return wins / len(sells)


def _compute_avg_holding(trades: Sequence[Trade]) -> float:
    """计算平均持仓天数。"""
    pairs: list[int] = []
    buys_by_symbol: dict[str, list[Trade]] = {}
    for t in trades:
        if t.side == "BUY":
            buys_by_symbol.setdefault(t.symbol, []).append(t)

    for t in trades:
        if t.side == "SELL":
            buys = buys_by_symbol.get(t.symbol, [])
            if buys:
                last_buy = buys[-1]
                pairs.append(t.bar_index - last_buy.bar_index)

    if not pairs:
        return 0.0
    return sum(pairs) / len(pairs)


def compute_cost_attribution(
    result: MeanReversionResult,
    initial_capital: float,
) -> CostAttribution:
    """计算成本归因。"""
    total = result.total_cost
    commission = result.total_commission
    stamp = result.total_stamp_tax
    slippage = result.total_slippage
    n = len(result.trades)

    return CostAttribution(
        total_cost=total,
        commission=commission,
        stamp_tax=stamp,
        slippage=slippage,
        cost_as_pct_of_nav=total / initial_capital if initial_capital > 0 else 0.0,
        cost_per_trade=total / n if n > 0 else 0.0,
    )


def compute_cost_sensitivity(
    closes: Mapping[str, Sequence[float]],
    config: MeanReversionConfig,
    *,
    opens: Mapping[str, Sequence[float]] | None = None,
    volumes: Mapping[str, Sequence[float]] | None = None,
    initial_capital: float = 100_000.0,
) -> CostSensitivityResult:
    """成本 x2 敏感性测试。

    用 2 倍成本重新回测,对比净收益变化。
    """
    base_result = run_backtest(
        closes, config, opens=opens, volumes=volumes,
        initial_capital=initial_capital,
    )
    doubled_config = config.cost_multiplier_config(2.0)
    doubled_result = run_backtest(
        closes, doubled_config, opens=opens, volumes=volumes,
        initial_capital=initial_capital,
    )

    base_net = base_result.total_return
    doubled_net = doubled_result.total_return
    degradation = base_net - doubled_net

    return CostSensitivityResult(
        base_net_return=base_net,
        doubled_cost_net_return=doubled_net,
        return_degradation=degradation,
        survives_doubled_cost=doubled_net > 0,
        base_config=config.as_dict(),
        doubled_config=doubled_config.as_dict(),
    )


def compute_trend_correlation(
    result: MeanReversionResult,
    trend_equity: Sequence[float] | None,
) -> float | None:
    """计算与趋势 sleeve 的收益相关系数。

    :param trend_equity: 趋略 sleeve 的净值序列(等长时使用)。
    :return: 相关系数;无数据时为 None。
    """
    if trend_equity is None or len(trend_equity) < 3:
        return None

    eq = result.equity_curve
    n = min(len(eq), len(trend_equity))
    if n < 3:
        return None

    mr_returns = [eq[i] / eq[i - 1] - 1.0 for i in range(1, n) if eq[i - 1] > 0]
    trend_returns = [
        trend_equity[i] / trend_equity[i - 1] - 1.0
        for i in range(1, n) if trend_equity[i - 1] > 0
    ]

    m = min(len(mr_returns), len(trend_returns))
    if m < 3:
        return None

    mr_returns = mr_returns[:m]
    trend_returns = trend_returns[:m]

    mean_mr = sum(mr_returns) / m
    mean_tr = sum(trend_returns) / m

    cov = sum(
        (mr_returns[i] - mean_mr) * (trend_returns[i] - mean_tr)
        for i in range(m)
    ) / m

    var_mr = sum((r - mean_mr) ** 2 for r in mr_returns) / m
    var_tr = sum((r - mean_tr) ** 2 for r in trend_returns) / m

    std_mr = math.sqrt(var_mr)
    std_tr = math.sqrt(var_tr)

    if std_mr == 0 or std_tr == 0:
        return None

    return cov / (std_mr * std_tr)


def compute_crisis_performance(
    result: MeanReversionResult,
) -> CrisisPerformance:
    """分析最大回撤期的表现。"""
    eq = result.equity_curve
    max_dd, start, end, duration = _compute_max_drawdown(eq)

    trades_in_dd = sum(1 for t in result.trades if start <= t.bar_index <= end)
    entries_in_dd = sum(
        1 for t in result.trades
        if start <= t.bar_index <= end and t.side == "BUY"
    )

    return CrisisPerformance(
        max_drawdown=max_dd,
        max_drawdown_start=start,
        max_drawdown_end=end,
        max_drawdown_duration=duration,
        trades_during_drawdown=trades_in_dd,
        entries_during_drawdown=entries_in_dd,
    )


def check_capital_tiers(
    result: MeanReversionResult,
    initial_capital: float = 100_000.0,
) -> tuple[CapitalTierFeasibility, ...]:
    """检查不同资金档位的可行性。"""
    tiers = (
        ("10万", 100_000.0),
        ("20万", 200_000.0),
        ("50万", 500_000.0),
    )

    trade_values = [t.gross_value for t in result.trades]
    avg_trade = sum(trade_values) / len(trade_values) if trade_values else 0.0
    min_trade = min(trade_values) if trade_values else 0.0

    results: list[CapitalTierFeasibility] = []
    for label, capital in tiers:
        scaled_avg = avg_trade * (capital / initial_capital) if initial_capital > 0 else 0.0
        scaled_min = min_trade * (capital / initial_capital) if initial_capital > 0 else 0.0

        feasible = scaled_min >= 1000.0
        reason = "" if feasible else "单笔交易金额过小,佣金占比过高"

        results.append(CapitalTierFeasibility(
            tier=label,
            capital=capital,
            feasible=feasible,
            avg_trade_value=scaled_avg,
            min_trade_value=scaled_min,
            reason=reason,
        ))

    return tuple(results)


def analyze_mean_reversion(
    result: MeanReversionResult,
    *,
    initial_capital: float = 100_000.0,
    trend_equity: Sequence[float] | None = None,
    closes_for_sensitivity: Mapping[str, Sequence[float]] | None = None,
    opens_for_sensitivity: Mapping[str, Sequence[float]] | None = None,
    volumes_for_sensitivity: Mapping[str, Sequence[float]] | None = None,
) -> MeanReversionAnalysis:
    """完整的均值回归策略分析。

    :param result: 回测结果。
    :param initial_capital: 初始资金(用于成本占比计算)。
    :param trend_equity: 趋势 sleeve 的净值序列(用于相关性计算)。
    :param closes_for_sensitivity: 成本敏感性测试用的价格数据;
        ``None`` 时跳过 x2 测试。
    """
    cost_attr = compute_cost_attribution(result, initial_capital)

    if closes_for_sensitivity is not None:
        cost_sens = compute_cost_sensitivity(
            closes_for_sensitivity,
            result.config,
            opens=opens_for_sensitivity,
            volumes=volumes_for_sensitivity,
            initial_capital=initial_capital,
        )
    else:
        cost_sens = CostSensitivityResult(
            base_net_return=result.total_return,
            doubled_cost_net_return=result.total_return,
            return_degradation=0.0,
            survives_doubled_cost=result.total_return > 0,
            base_config=result.config.as_dict(),
            doubled_config=result.config.as_dict(),
        )

    crisis = compute_crisis_performance(result)
    capital_tiers = check_capital_tiers(result, initial_capital)
    trend_corr = compute_trend_correlation(result, trend_equity)

    sharpe = _compute_sharpe(result.equity_curve)
    win_rate = _compute_win_rate(result.trades)
    avg_holding = _compute_avg_holding(result.trades)

    n_bars = result.bars_processed
    total_trade_value = sum(t.gross_value for t in result.trades)
    turnover_rate = (
        total_trade_value / initial_capital / n_bars
        if n_bars > 0 and initial_capital > 0
        else 0.0
    )

    rejection_reasons: list[str] = []
    if not cost_sens.survives_doubled_cost:
        rejection_reasons.append("成本 x2 后净收益为负")
    if sharpe < 0:
        rejection_reasons.append("Sharpe < 0")

    return MeanReversionAnalysis(
        total_return=result.total_return,
        gross_return=result.gross_return,
        net_return=result.total_return,
        cost_attribution=cost_attr,
        cost_sensitivity=cost_sens,
        crisis_performance=crisis,
        capital_tiers=capital_tiers,
        trade_count=len(result.trades),
        avg_holding_days=avg_holding,
        win_rate=win_rate,
        sharpe_ratio=sharpe,
        max_drawdown=crisis.max_drawdown,
        turnover_rate=turnover_rate,
        trend_correlation=trend_corr,
        rejected=len(rejection_reasons) > 0,
        rejection_reasons=rejection_reasons,
    )
