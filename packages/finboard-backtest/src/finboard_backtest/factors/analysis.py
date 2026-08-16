"""因子分析报告:单因子边际贡献、相关性、组合暴露。

从回测期间的因子评分时间序列和标的收益计算各因子的边际贡献,
帮助判断哪些因子真正驱动了组合表现。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FactorAnalysisReport:
    """因子分析报告。"""

    single_factor_ic: dict[str, float]
    single_factor_ic_ir: dict[str, float]
    factor_correlation: dict[str, dict[str, float]]
    portfolio_factor_exposure: dict[str, float]
    avg_turnover: float
    avg_n_holdings: float
    n_periods: int
    capital_tier_feasibility: dict[str, bool]

    @property
    def best_factor(self) -> str | None:
        if not self.single_factor_ic:
            return None
        return max(self.single_factor_ic, key=lambda k: abs(self.single_factor_ic[k]))

    @property
    def worst_factor(self) -> str | None:
        if not self.single_factor_ic:
            return None
        return min(self.single_factor_ic, key=lambda k: abs(self.single_factor_ic[k]))


def compute_factor_analysis(
    factor_scores_history: list[dict[str, dict[str, float]]],
    forward_returns: list[dict[str, float]],
    selected_symbols_history: list[tuple[str, ...]],
    *,
    capital_tiers: dict[str, float] | None = None,
    min_price: float = 5.0,
) -> FactorAnalysisReport:
    """计算因子分析报告。

    Args:
        factor_scores_history: 每期各因子的标准化得分 {period: {factor: {symbol: score}}}
        forward_returns: 每期标的的前瞻收益 {period: {symbol: return}}
        selected_symbols_history: 每期选中的标的
        capital_tiers: 资金档位 {name: capital}
        min_price: 最小单价(用于资金可行性判断)

    Returns:
        FactorAnalysisReport
    """
    factor_names = set()
    for period_scores in factor_scores_history:
        factor_names |= set(period_scores.keys())
    factor_names_sorted = sorted(factor_names)

    ic_results: dict[str, list[float]] = {f: [] for f in factor_names_sorted}
    for i, period_scores in enumerate(factor_scores_history):
        if i >= len(forward_returns):
            break
        fwd = forward_returns[i]
        for factor_name, scores in period_scores.items():
            ic = _spearman_ic(scores, fwd)
            if ic is not None:
                ic_results[factor_name].append(ic)

    single_factor_ic: dict[str, float] = {}
    single_factor_ic_ir: dict[str, float] = {}
    for f, ics in ic_results.items():
        if ics:
            single_factor_ic[f] = float(np.mean(ics))
            ic_std = float(np.std(ics))
            single_factor_ic_ir[f] = float(np.mean(ics) / ic_std) if ic_std > 1e-12 else 0.0
        else:
            single_factor_ic[f] = 0.0
            single_factor_ic_ir[f] = 0.0

    factor_correlation = _compute_factor_correlation(factor_scores_history)

    portfolio_exposure = _compute_portfolio_exposure(
        factor_scores_history, selected_symbols_history
    )

    turnovers = _compute_turnover(selected_symbols_history)
    avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0
    avg_holdings = float(np.mean([len(s) for s in selected_symbols_history])) if selected_symbols_history else 0.0

    tier_feasibility: dict[str, bool] = {}
    if capital_tiers:
        for tier_name, capital in capital_tiers.items():
            tier_feasibility[tier_name] = _check_capital_feasibility(
                selected_symbols_history, capital, min_price, avg_holdings
            )

    return FactorAnalysisReport(
        single_factor_ic=single_factor_ic,
        single_factor_ic_ir=single_factor_ic_ir,
        factor_correlation=factor_correlation,
        portfolio_factor_exposure=portfolio_exposure,
        avg_turnover=avg_turnover,
        avg_n_holdings=avg_holdings,
        n_periods=len(factor_scores_history),
        capital_tier_feasibility=tier_feasibility,
    )


def _spearman_ic(
    scores: dict[str, float],
    returns: dict[str, float],
) -> float | None:
    """计算 Rank IC (Spearman rank correlation)。"""
    common = sorted(set(scores) & set(returns))
    if len(common) < 5:
        return None
    s_vals = np.array([scores[s] for s in common])
    r_vals = np.array([returns[s] for s in common])
    mask = np.isfinite(s_vals) & np.isfinite(r_vals)
    if mask.sum() < 5:
        return None
    s_valid = s_vals[mask]
    r_valid = r_vals[mask]
    s_ranks = _rank(s_valid)
    r_ranks = _rank(r_valid)
    s_centered = s_ranks - s_ranks.mean()
    r_centered = r_ranks - r_ranks.mean()
    denom = np.sqrt((s_centered**2).sum() * (r_centered**2).sum())
    if denom < 1e-12:
        return 0.0
    return float((s_centered * r_centered).sum() / denom)


def _compute_factor_correlation(
    scores_history: list[dict[str, dict[str, float]]],
) -> dict[str, dict[str, float]]:
    """计算因子间平均相关性。"""
    if not scores_history:
        return {}
    factor_names = set()
    for period in scores_history:
        factor_names |= set(period.keys())
    factor_names_sorted = sorted(factor_names)

    accumulated: dict[str, dict[str, list[float]]] = {
        f1: {f2: [] for f2 in factor_names_sorted} for f1 in factor_names_sorted
    }
    for period in scores_history:
        all_syms: set[str] = set()
        for f in factor_names_sorted:
            all_syms |= set(period.get(f, {}).keys())
        for f1 in factor_names_sorted:
            for f2 in factor_names_sorted:
                if f1 >= f2:
                    continue
                s1 = period.get(f1, {})
                s2 = period.get(f2, {})
                common = sorted(all_syms & set(s1) & set(s2))
                if len(common) < 5:
                    continue
                v1 = np.array([s1.get(s, np.nan) for s in common])
                v2 = np.array([s2.get(s, np.nan) for s in common])
                mask = np.isfinite(v1) & np.isfinite(v2)
                if mask.sum() < 5:
                    continue
                corr = float(np.corrcoef(v1[mask], v2[mask])[0, 1])
                if np.isfinite(corr):
                    accumulated[f1][f2].append(corr)
                    accumulated[f2][f1].append(corr)

    result: dict[str, dict[str, float]] = {
        f: {} for f in factor_names_sorted
    }
    for f1 in factor_names_sorted:
        result[f1][f1] = 1.0
        for f2 in factor_names_sorted:
            if f1 >= f2:
                continue
            vals = accumulated[f1][f2]
            if vals:
                result[f1][f2] = float(np.mean(vals))
                result[f2][f1] = float(np.mean(vals))
            else:
                result[f1][f2] = 0.0
                result[f2][f1] = 0.0
    return result


def _compute_portfolio_exposure(
    scores_history: list[dict[str, dict[str, float]]],
    selected_history: list[tuple[str, ...]],
) -> dict[str, float]:
    """计算组合在各因子上的平均暴露。"""
    if not scores_history:
        return {}
    factor_names = set()
    for period in scores_history:
        factor_names |= set(period.keys())

    exposures: dict[str, list[float]] = {f: [] for f in sorted(factor_names)}
    for i, period_scores in enumerate(scores_history):
        if i >= len(selected_history):
            break
        selected = selected_history[i]
        if not selected:
            continue
        for f in factor_names:
            scores = period_scores.get(f, {})
            vals = [scores[s] for s in selected if s in scores and np.isfinite(scores[s])]
            if vals:
                exposures[f].append(float(np.mean(vals)))

    return {
        f: float(np.mean(vals)) if vals else 0.0
        for f, vals in exposures.items()
    }


def _compute_turnover(selected_history: list[tuple[str, ...]]) -> list[float]:
    """计算每期换手率。"""
    if len(selected_history) < 2:
        return []
    turnovers: list[float] = []
    for i in range(1, len(selected_history)):
        prev = set(selected_history[i - 1])
        curr = set(selected_history[i])
        if not curr and not prev:
            continue
        union = len(curr | prev)
        if union == 0:
            continue
        turnover = len(curr - prev) / union
        turnovers.append(turnover)
    return turnovers


def _check_capital_feasibility(
    selected_history: list[tuple[str, ...]],
    capital: float,
    min_price: float,
    avg_holdings: float,
) -> bool:
    """检查给定资金档位是否可行(粗略:足够买 min_holdings 手)。"""
    if avg_holdings < 1:
        return True
    per_position = capital / max(avg_holdings, 1)
    min_lot_cost = min_price * 100
    return per_position >= min_lot_cost * 2


def _rank(arr: np.ndarray) -> np.ndarray:
    """纯 numpy rank。"""
    sorter = np.argsort(arr, kind="mergesort")
    inv = np.empty(sorter.size, dtype=np.intp)
    inv[sorter] = np.arange(sorter.size)
    return inv.astype(np.float64) + 1.0
