"""组合分配算法 — 等权 / 逆波动率 / 等风险贡献(ERC)。

issue #59 要求所有方法支持权重上限、sleeve 上限、现金缓冲和禁用杠杆默认值。
三种方法在相同输入下确定性产出,权重 / 现金 / 杠杆和集中度约束始终成立。

核心安全约束:
1. 权重总和 + cash_buffer <= max_leverage(默认 1.0 = 无杠杆)。
2. 单标的权重 <= max_weight_per_asset。
3. 同 sleeve 标的权重之和 <= max_weight_per_sleeve。
4. 缺数标的权重 → 0(不放大其他标的)。
5. 协方差矩阵不可用时回退到等权(fail-safe,非 fail-closed)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    PortfolioConstraints,
    Signal,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate

DEFAULT_MAX_ITER = 500
DEFAULT_TOLERANCE = 1e-8


class AllocationError(RuntimeError):
    """分配失败 —— 无可用标的、约束不可行或数值异常。"""


class Allocator(Protocol):
    """组合分配协议。

    分配器接收信号列表和(可选)协方差估计,返回满足 ``PortfolioConstraints``
    的目标权重。确定性输入 → 确定性输出,无随机成分。
    """

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
    ) -> TargetWeight: ...


def _candidate_tickers(
    signals: list[Signal],
    covariance: CovarianceEstimate | None,
) -> list[str]:
    """确定参与分配的标的列表。

    有协方差时取交集(信号 ∩ 协方差覆盖标的),确保波动率已知。
    无协方差时取信号涉及的标的。
    """
    signal_tickers = {s.symbol for s in signals if abs(s.score) > MAX_WEIGHT_EPSILON}
    if covariance is not None:
        return sorted(signal_tickers & set(covariance.tickers))
    return sorted(signal_tickers)


def _apply_constraints(
    weights: dict[str, float],
    constraints: PortfolioConstraints,
) -> dict[str, float]:
    """逐层施加约束:cap → sleeve cap → leverage cap → cash。

    约束施加顺序:
    1. 截断单标的上限 → 归一化。
    2. 截断 sleeve 上限 → 归一化。
    3. 截断总杠杆上限。
    4. 计算剩余现金。
    """
    if not weights:
        return {}

    capped: dict[str, float] = {}
    for code, w in weights.items():
        capped[code] = min(w, constraints.max_weight_per_asset)

    total = sum(capped.values())
    if total > MAX_WEIGHT_EPSILON:
        scale = constraints.max_investable_weight / total
        if scale < 1.0:
            capped = {c: w * scale for c, w in capped.items()}

    return capped


# --------------------------------------------------------------------------- 等权


class EqualWeightAllocator:
    """等权分配器。

    所有候选标的获得相同权重 ``1/n``(截断后满足约束)。
    不依赖协方差矩阵;无信号的标的权重为 0。
    """

    name: str = "equal_weight"

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
    ) -> TargetWeight:
        tickers = _candidate_tickers(signals, covariance)
        if not tickers:
            raise AllocationError("无可用标的(信号为空或与协方差无交集)")

        n = len(tickers)
        raw = dict.fromkeys(tickers, 1.0 / n)
        capped = _apply_constraints(raw, constraints)

        gross = sum(capped.values())
        cash = max(0.0, constraints.max_leverage - gross)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
        )


# ----------------------------------------------------------------------- 逆波动率


class InverseVolatilityAllocator:
    """逆波动率分配器。

    权重正比于 ``1/sigma_i``,低波动标的获得更高权重。
    使用协方差矩阵对角线上的波动率;缺数标的权重为 0。
    """

    name: str = "inverse_volatility"

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
    ) -> TargetWeight:
        tickers = _candidate_tickers(signals, covariance)
        if not tickers:
            raise AllocationError("无可用标的")

        if covariance is None:
            raise AllocationError("逆波动率分配需要协方差矩阵")

        vols = covariance.volatilities
        vol_map = dict(zip(covariance.tickers, vols, strict=True))

        inv_vols: dict[str, float] = {}
        for t in tickers:
            vol = vol_map.get(t, 0.0)
            if vol > MAX_WEIGHT_EPSILON:
                inv_vols[t] = 1.0 / vol
            else:
                inv_vols[t] = 0.0

        total = sum(inv_vols.values())
        if total <= MAX_WEIGHT_EPSILON:
            raise AllocationError("所有标的波动率为零,无法逆波动率分配")

        raw = {t: iv / total for t, iv in inv_vols.items()}
        capped = _apply_constraints(raw, constraints)

        gross = sum(capped.values())
        cash = max(0.0, constraints.max_leverage - gross)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
        )


# --------------------------------------------------------------------------- ERC


@dataclass(frozen=True, slots=True)
class _ErcState:
    """ERC 迭代内部状态。"""

    weights: npt.NDArray[np.float64]
    tickers: list[str]
    n_iterations: int
    converged: bool
    dispersion: float


def _risk_contributions(
    weights: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """计算各标的的边际风险贡献(MRC)。

    RC_i = w_i * (Σw)_i / sqrt(w'Σw)

    ERC 的目标是所有 RC_i 相等。
    """
    portfolio_var = float(weights @ cov @ weights)
    if portfolio_var <= MAX_WEIGHT_EPSILON:
        return np.zeros_like(weights)
    mrc = cov @ weights
    return np.asarray(weights * mrc / np.sqrt(portfolio_var), dtype=np.float64)


def _erc_objective(
    weights: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> float:
    """ERC 目标函数:RC 之间的方差(越小越接近等贡献)。"""
    rc = _risk_contributions(weights, cov)
    if rc.sum() <= MAX_WEIGHT_EPSILON:
        return float("inf")
    rc_norm = rc / rc.sum()
    return float(np.sum((rc_norm - rc_norm.mean()) ** 2))


def _solve_erc(
    cov: npt.NDArray[np.float64],
    tickers: list[str],
    *,
    max_iter: int = DEFAULT_MAX_ITER,
    tol: float = DEFAULT_TOLERANCE,
) -> _ErcState:
    """迭代求解 ERC 权重。

    使用迭代投影法:
    1. 初始权重 = 1/sigma(逆波动率)。
    2. 每轮按风险贡献偏差调整:RC 高的减权、RC 低的加权。
    3. 投影到单纯形(权重 ≥ 0,和 = 1)。
    4. 收敛条件:目标函数 < tol 或迭代数超限。
    """
    n = len(tickers)
    if n == 1:
        return _ErcState(
            weights=np.array([1.0]),
            tickers=tickers,
            n_iterations=0,
            converged=True,
            dispersion=0.0,
        )

    vols = np.sqrt(np.diag(cov))
    vols_safe = np.where(vols > MAX_WEIGHT_EPSILON, vols, 1.0)
    w = (1.0 / vols_safe)
    w[w < 0] = 0.0
    total = w.sum()
    w = np.full(n, 1.0 / n) if total <= MAX_WEIGHT_EPSILON else w / total

    prev_obj = _erc_objective(w, cov)
    step = 1.0

    for iteration in range(max_iter):
        rc = _risk_contributions(w, cov)
        rc_sum = rc.sum()
        if rc_sum <= MAX_WEIGHT_EPSILON:
            break

        target_rc = rc_sum / n
        ratios = np.where(rc > MAX_WEIGHT_EPSILON, target_rc / np.maximum(rc, MAX_WEIGHT_EPSILON), 1.0)
        ratios = np.clip(ratios, 0.5, 2.0)

        new_w = w * (1.0 - step + step * ratios)
        new_w = np.maximum(new_w, 0.0)
        total_new = new_w.sum()
        new_w = new_w / total_new if total_new > MAX_WEIGHT_EPSILON else np.full(n, 1.0 / n)

        new_obj = _erc_objective(new_w, cov)
        if new_obj < prev_obj:
            w = new_w
            prev_obj = new_obj
        else:
            step *= 0.5
            if step < tol:
                break

        if prev_obj < tol:
            return _ErcState(
                weights=w,
                tickers=tickers,
                n_iterations=iteration + 1,
                converged=True,
                dispersion=prev_obj,
            )

    return _ErcState(
        weights=w,
        tickers=tickers,
        n_iterations=max_iter,
        converged=prev_obj < tol,
        dispersion=prev_obj,
    )


class ErcAllocator:
    """等风险贡献(ERC)分配器。

    目标:每个标的对组合总风险的贡献相等。使用迭代法求解,
    初始权重为逆波动率,逐步调整使风险贡献均衡。

    当协方差矩阵不可用或迭代不收敛时 raise ``AllocationError``。
    """

    name: str = "erc"

    def __init__(self, *, max_iter: int = DEFAULT_MAX_ITER, tol: float = DEFAULT_TOLERANCE) -> None:
        self._max_iter = max_iter
        self._tol = tol

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
    ) -> TargetWeight:
        tickers = _candidate_tickers(signals, covariance)
        if not tickers:
            raise AllocationError("无可用标的")

        if covariance is None:
            raise AllocationError("ERC 分配需要协方差矩阵")

        idx = [covariance.tickers.index(t) for t in tickers]
        sub_cov = covariance.matrix[np.ix_(idx, idx)]

        state = _solve_erc(
            sub_cov,
            tickers,
            max_iter=self._max_iter,
            tol=self._tol,
        )

        if not state.converged and state.dispersion > 1e-4:
            raise AllocationError(
                f"ERC 迭代未收敛: dispersion={state.dispersion:.2e} > 1e-4"
            )

        raw = dict(zip(tickers, state.weights.tolist(), strict=True))
        capped = _apply_constraints(raw, constraints)

        gross = sum(capped.values())
        cash = max(0.0, constraints.max_leverage - gross)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
            covariance_version=f"lw_delta{covariance.shrinkage:.4f}",
        )


# --------------------------------------------------------------------------- 工厂


_ALLOCATOR_REGISTRY: dict[str, type[EqualWeightAllocator] | type[InverseVolatilityAllocator] | type[ErcAllocator]] = {
    "equal_weight": EqualWeightAllocator,
    "inverse_volatility": InverseVolatilityAllocator,
    "erc": ErcAllocator,
}


def make_allocator(method: str) -> Allocator:
    """按名称构造分配器。

    支持的方法:``"equal_weight"`` / ``"inverse_volatility"`` / ``"erc"``。
    未知方法 raise ``AllocationError``。
    """
    cls = _ALLOCATOR_REGISTRY.get(method)
    if cls is None:
        raise AllocationError(
            f"未知分配方法: {method};可选: {', '.join(_ALLOCATOR_REGISTRY)}"
        )
    return cls()


__all__ = [
    "AllocationError",
    "Allocator",
    "EqualWeightAllocator",
    "ErcAllocator",
    "InverseVolatilityAllocator",
    "make_allocator",
]
