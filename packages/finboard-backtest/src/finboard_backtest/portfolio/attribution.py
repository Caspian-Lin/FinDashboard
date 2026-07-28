"""绩效归因分解 — 按资产 / sleeve 分解收益 / 风险 / 换手 / 回撤。

issue #59 要求报告包含:
* 资产 / sleeve 风险贡献(边际风险贡献 RC_i)
* 收益贡献(w_i * r_i)
* 换手贡献(|Δw_i| / 2)
* 最大回撤贡献
* 现金利用率(非现金资产 / 总资产)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.contracts import MAX_WEIGHT_EPSILON
from finboard_backtest.portfolio.covariance import CovarianceEstimate
from finboard_backtest.portfolio.risk_budget import ANNUALIZATION_FACTOR


@dataclass(frozen=True, slots=True)
class AssetContribution:
    """单标的的归因贡献。

    所有贡献字段均为组合级别的绝对值(非百分比):各标的贡献之和 =
    组合总指标。
    """

    code: str
    weight: float
    return_contribution: float
    risk_contribution: float
    turnover_contribution: float
    drawdown_contribution: float


@dataclass(frozen=True, slots=True)
class SleeveContribution:
    """资产类别(sleeve)的聚合归因贡献。"""

    sleeve_name: str
    weight: float
    return_contribution: float
    risk_contribution: float
    turnover_contribution: float
    drawdown_contribution: float
    n_assets: int


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """组合绩效归因报告。

    属性:
        by_asset: 各标的的贡献。
        by_sleeve: 各 sleeve 的聚合贡献。
        total_return: 组合总收益。
        total_risk: 组合年化波动率(数值)。
        total_turnover: 组合换手率(|Δw|/2 之和)。
        max_drawdown: 组合最大回撤(负数)。
        cash_utilization: 现金利用率 = 投资市值 / 总资产。
        leverage_ratio: 实际杠杆 = gross_weight / net_weight。
    """

    by_asset: list[AssetContribution]
    by_sleeve: list[SleeveContribution]
    total_return: float
    total_risk: float
    total_turnover: float
    max_drawdown: float
    cash_utilization: float
    leverage_ratio: float

    @property
    def n_assets(self) -> int:
        return len(self.by_asset)

    @property
    def n_sleeves(self) -> int:
        return len(self.by_sleeve)

    def asset_contribution(self, code: str) -> AssetContribution | None:
        for ac in self.by_asset:
            if ac.code == code:
                return ac
        return None

    def sleeve_contribution(self, name: str) -> SleeveContribution | None:
        for sc in self.by_sleeve:
            if sc.sleeve_name == name:
                return sc
        return None


def compute_attribution(
    weights_history: list[dict[str, float]],
    returns_by_ticker: dict[str, npt.NDArray[np.float64]],
    covariance: CovarianceEstimate,
    *,
    sleeve_map: dict[str, str] | None = None,
    trading_days: int = 252,
) -> AttributionReport:
    """计算组合归因分解。

    参数:
        weights_history: 每期权重快照 ``[{code: w}]``。
        returns_by_ticker: 各标的的日收益率序列。
        covariance: 协方差估计(用于风险贡献)。
        sleeve_map: ``{code: sleeve_name}`` 映射(如 "600519.SH" → "equity")。
            为 None 时所有标的归入 "default" sleeve。
        trading_days: 年化天数。

    返回:
        ``AttributionReport``。
    """
    if not weights_history:
        return _empty_report()
    if not returns_by_ticker:
        return _empty_report()

    latest_weights = weights_history[-1]
    active_codes = [c for c, w in latest_weights.items() if w > MAX_WEIGHT_EPSILON]
    if not active_codes:
        return _empty_report()

    avg_weights = _average_weights(weights_history, active_codes)

    ret_map = {c: r for c, r in returns_by_ticker.items() if c in active_codes}

    total_ret = 0.0
    return_contributions: dict[str, float] = {}
    for code in active_codes:
        w = avg_weights.get(code, 0.0)
        rets = ret_map.get(code)
        r = float(np.prod(1.0 + rets) - 1.0) if rets is not None and len(rets) > 0 else 0.0
        contribution = w * r
        return_contributions[code] = contribution
        total_ret += contribution

    tickers = covariance.tickers
    idx_map = {t: i for i, t in enumerate(tickers)}
    w_vec = np.zeros(len(tickers))
    for code in active_codes:
        w = avg_weights.get(code, 0.0)
        if code in idx_map:
            w_vec[idx_map[code]] = w

    portfolio_var = float(w_vec @ covariance.matrix @ w_vec)
    portfolio_vol = np.sqrt(max(portfolio_var, 0.0)) * ANNUALIZATION_FACTOR

    risk_contributions: dict[str, float] = {}
    if portfolio_var > MAX_WEIGHT_EPSILON:
        mrc = covariance.matrix @ w_vec
        rc_vec = w_vec * mrc
        total_rc = rc_vec.sum()
        if total_rc > MAX_WEIGHT_EPSILON:
            for code in active_codes:
                idx = idx_map.get(code)
                if idx is not None:
                    risk_contributions[code] = float(rc_vec[idx] / total_rc)
                else:
                    risk_contributions[code] = 0.0
        else:
            for code in active_codes:
                risk_contributions[code] = 0.0
    else:
        for code in active_codes:
            risk_contributions[code] = 0.0

    turnover_contributions = _turnover_contributions(weights_history, active_codes)

    drawdown_contributions = _drawdown_contributions(
        active_codes, avg_weights, ret_map
    )

    by_asset: list[AssetContribution] = []
    for code in active_codes:
        by_asset.append(AssetContribution(
            code=code,
            weight=avg_weights.get(code, 0.0),
            return_contribution=return_contributions.get(code, 0.0),
            risk_contribution=risk_contributions.get(code, 0.0),
            turnover_contribution=turnover_contributions.get(code, 0.0),
            drawdown_contribution=drawdown_contributions.get(code, 0.0),
        ))

    by_sleeve = _aggregate_sleeves(
        by_asset, sleeve_map or dict.fromkeys(active_codes, "default")
    )

    gross = sum(avg_weights.values())
    cash_util = gross / 1.0 if gross > 0 else 0.0
    leverage = gross / (gross + (1.0 - gross)) if gross > MAX_WEIGHT_EPSILON else 0.0

    max_dd = _max_drawdown_from_returns(
        active_codes, avg_weights, ret_map
    )

    return AttributionReport(
        by_asset=by_asset,
        by_sleeve=by_sleeve,
        total_return=total_ret,
        total_risk=portfolio_vol,
        total_turnover=sum(turnover_contributions.values()),
        max_drawdown=max_dd,
        cash_utilization=min(cash_util, 1.0),
        leverage_ratio=leverage,
    )


def _empty_report() -> AttributionReport:
    return AttributionReport(
        by_asset=[],
        by_sleeve=[],
        total_return=0.0,
        total_risk=0.0,
        total_turnover=0.0,
        max_drawdown=0.0,
        cash_utilization=0.0,
        leverage_ratio=0.0,
    )


def _average_weights(
    weights_history: list[dict[str, float]],
    codes: list[str],
) -> dict[str, float]:
    """计算平均权重。"""
    n = len(weights_history)
    if n == 0:
        return {}
    totals: dict[str, float] = dict.fromkeys(codes, 0.0)
    for snapshot in weights_history:
        for code in codes:
            totals[code] += snapshot.get(code, 0.0)
    return {c: t / n for c, t in totals.items()}


def _turnover_contributions(
    weights_history: list[dict[str, float]],
    codes: list[str],
) -> dict[str, float]:
    """计算各标的换手贡献 = Sum(|delta_w_i|) / (2 * (T-1))。"""
    if len(weights_history) < 2:
        return dict.fromkeys(codes, 0.0)
    totals: dict[str, float] = dict.fromkeys(codes, 0.0)
    for i in range(1, len(weights_history)):
        prev = weights_history[i - 1]
        curr = weights_history[i]
        for code in codes:
            totals[code] += abs(curr.get(code, 0.0) - prev.get(code, 0.0))
    n_periods = len(weights_history) - 1
    return {c: t / (2 * n_periods) for c, t in totals.items()}


def _drawdown_contributions(
    codes: list[str],
    weights: dict[str, float],
    returns_by_ticker: dict[str, npt.NDArray[np.float64]],
) -> dict[str, float]:
    """估算各标的回撤贡献(近似:权重 * 标的自身最大回撤)。

    这是一个近似分解:组合回撤不完全等于各标的回撤之和(因为有相关性),
    但用于贡献排序足够。
    """
    result: dict[str, float] = {}
    for code in codes:
        w = weights.get(code, 0.0)
        rets = returns_by_ticker.get(code)
        if rets is None or len(rets) == 0:
            result[code] = 0.0
            continue
        cum = np.cumprod(1.0 + rets)
        running_max = np.maximum.accumulate(cum)
        dd = (cum - running_max) / running_max
        max_dd = float(dd.min())
        result[code] = w * max_dd
    return result


def _max_drawdown_from_returns(
    codes: list[str],
    weights: dict[str, float],
    returns_by_ticker: dict[str, npt.NDArray[np.float64]],
) -> float:
    """计算组合级别的最大回撤。"""
    if not codes:
        return 0.0
    max_len = max(
        (len(returns_by_ticker[c]) for c in codes if c in returns_by_ticker),
        default=0,
    )
    if max_len == 0:
        return 0.0

    portfolio_returns = np.zeros(max_len)
    for code in codes:
        w = weights.get(code, 0.0)
        rets = returns_by_ticker.get(code)
        if rets is None or w == 0:
            continue
        offset = max_len - len(rets)
        portfolio_returns[offset:] += w * rets

    cum = np.cumprod(1.0 + portfolio_returns)
    running_max = np.maximum.accumulate(cum)
    dd = (cum - running_max) / running_max
    return float(dd.min())


def _aggregate_sleeves(
    by_asset: list[AssetContribution],
    sleeve_map: dict[str, str],
) -> list[SleeveContribution]:
    """按 sleeve 聚合资产贡献。"""
    sleeve_data: dict[str, dict[str, float]] = {}
    sleeve_counts: dict[str, int] = {}

    for ac in by_asset:
        sleeve = sleeve_map.get(ac.code, "default")
        if sleeve not in sleeve_data:
            sleeve_data[sleeve] = {
                "weight": 0.0,
                "return": 0.0,
                "risk": 0.0,
                "turnover": 0.0,
                "drawdown": 0.0,
            }
            sleeve_counts[sleeve] = 0
        sleeve_data[sleeve]["weight"] += ac.weight
        sleeve_data[sleeve]["return"] += ac.return_contribution
        sleeve_data[sleeve]["risk"] += ac.risk_contribution
        sleeve_data[sleeve]["turnover"] += ac.turnover_contribution
        sleeve_data[sleeve]["drawdown"] += ac.drawdown_contribution
        sleeve_counts[sleeve] += 1

    return [
        SleeveContribution(
            sleeve_name=name,
            weight=data["weight"],
            return_contribution=data["return"],
            risk_contribution=data["risk"],
            turnover_contribution=data["turnover"],
            drawdown_contribution=data["drawdown"],
            n_assets=sleeve_counts[name],
        )
        for name, data in sorted(sleeve_data.items())
    ]


__all__ = [
    "AssetContribution",
    "AttributionReport",
    "SleeveContribution",
    "compute_attribution",
]
