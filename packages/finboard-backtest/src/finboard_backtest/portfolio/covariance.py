"""协方差矩阵估计 —— Ledoit-Wolf 收缩 + 缺数 / 新标的处理。

issue #59 的核心安全要求:**只用回看窗口内已知的数据**,不因缺数静默
放大其他资产权重。本模块提供:

* ``CovarianceEstimate``:协方差矩阵估计结果(含收缩强度 / 有效样本量)。
* ``estimate_covariance``:从收益率矩阵计算 Ledoit-Wolf 收缩协方差。
* ``pairwise_aligned_returns``:对齐多标的收益率序列,缺数标的不参与
  协方差计算但权重被压缩(不静默放大)。

Ledoit-Wolf 收缩目标为常数相关系数矩阵,强度由样本量驱动;当窗口内
有效样本不足时收缩到单位矩阵,避免极端权重。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

MIN_OBS_FOR_FULL_COVARIANCE = 30
"""最少观测数:少于该值时收缩强度 → 1.0(完全使用目标矩阵)。"""

DEFAULT_LOOKBACK_DAYS = 252
"""默认回看窗口:约 1 年交易日。"""

PSD_MIN_EIGENVALUE = 1e-12
"""PD 修复的特征值下限:``_ensure_positive_definite`` 把低于该值的特征值
clip 到该值。

同时也是 PSD 校验容差的对齐锚(issue #465):``builder._covariance_problem``
与 ``max_ir`` 的同公式校验容差取 ``min(eps * max(1, ‖A‖∞) * N,
PSD_MIN_EIGENVALUE / 2)`` —— 此前纯数值容差在大 N(N≈5000)时约 1.1e-12,
反而高于 clip 下限 1e-12,存在「修复过的矩阵仍被校验拒绝」的边界;对齐后
修复过的矩阵(最小特征值 >= 1e-12)恒过校验,真坏矩阵(最小特征值 <= 0
或 NaN / 不对称)照旧被拦。"""


class CovarianceError(RuntimeError):
    """协方差估计失败 —— 标的为空、窗口不足或矩阵非正定。"""


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    """协方差矩阵估计结果。

    属性:
        matrix: NxN 正定协方差矩阵(日收益率尺度)。
        tickers: 与矩阵行列对齐的标的列表。
        shrinkage: Ledoit-Wolf 收缩强度 delta in [0, 1];0 = 纯样本,
            1 = 纯目标矩阵。
        n_observations: 有效观测数(对齐后)。
        method: 估计方法标识("ledoit_wolf")。
    """

    matrix: npt.NDArray[np.float64]
    tickers: list[str]
    shrinkage: float
    n_observations: int
    method: str = "ledoit_wolf"

    def __post_init__(self) -> None:
        n = len(self.tickers)
        if n == 0:
            raise CovarianceError("标的列表为空")
        if self.matrix.shape != (n, n):
            raise CovarianceError(
                f"协方差矩阵形状 {self.matrix.shape} 与标的数 {n} 不匹配"
            )
        if not (0.0 <= self.shrinkage <= 1.0):
            raise CovarianceError("shrinkage 必须落在 [0, 1]")
        if self.n_observations <= 0:
            raise CovarianceError("n_observations 必须为正")

    @property
    def n_assets(self) -> int:
        return len(self.tickers)

    @property
    def volatilities(self) -> npt.NDArray[np.float64]:
        """各标的日波动率(对角线开方)。"""
        return np.sqrt(np.diag(self.matrix))

    def annualized_volatilities(self, trading_days: int = 252) -> npt.NDArray[np.float64]:
        """年化波动率。"""
        result: npt.NDArray[np.float64] = self.volatilities * float(np.sqrt(trading_days))
        return result

    def correlation(self) -> npt.NDArray[np.float64]:
        """相关系数矩阵。"""
        vols = self.volatilities
        mask = vols > 0
        corr = np.eye(self.n_assets)
        outer = np.outer(vols, vols)
        nonzero = outer > 0
        corr[nonzero] = self.matrix[nonzero] / outer[nonzero]
        corr[~mask, :] = 0.0
        corr[:, ~mask] = 0.0
        for i in range(self.n_assets):
            corr[i, i] = 1.0 if mask[i] else 0.0
        return corr


def pairwise_aligned_returns(
    returns_by_ticker: dict[str, npt.NDArray[np.float64]],
    min_overlap: int = MIN_OBS_FOR_FULL_COVARIANCE,
) -> tuple[list[str], npt.NDArray[np.float64]]:
    """对齐多标的收益率序列,返回 (tickers, matrix[T, N])。

    缺数标的(有效观测 < min_overlap)被**排除**,而不是填充零或插值。
    这避免了"因缺数静默放大其他标的权重"的风险。
    """
    if not returns_by_ticker:
        raise CovarianceError("收益率字典为空")

    max_len = max(len(r) for r in returns_by_ticker.values())
    if max_len < min_overlap:
        raise CovarianceError(
            f"最长序列仅 {max_len} 个观测,不足 {min_overlap}"
        )

    valid_tickers: list[str] = []
    for ticker, rets in returns_by_ticker.items():
        if len(rets) >= min_overlap:
            valid_tickers.append(ticker)

    if not valid_tickers:
        raise CovarianceError(
            f"无标的有效观测数 >= {min_overlap};所有标的被排除"
        )

    valid_tickers.sort()
    n = len(valid_tickers)

    aligned = np.full((max_len, n), np.nan)
    for col, ticker in enumerate(valid_tickers):
        rets = np.asarray(returns_by_ticker[ticker], dtype=np.float64)
        offset = max_len - len(rets)
        aligned[offset:, col] = rets

    mask = ~np.isnan(aligned).any(axis=1)
    clean = aligned[mask]

    if clean.shape[0] < min_overlap:
        raise CovarianceError(
            f"对齐后仅 {clean.shape[0]} 个共同观测,不足 {min_overlap}"
        )

    return valid_tickers, clean


def _ledoit_wolf_target_correlation(
    sample: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], float]:
    """Ledoit-Wolf 常数相关系数收缩目标。

    返回 (target_matrix, shrinkage_intensity)。

    参考:Ledoit & Wolf (2004) "Honey, I Shrunk the Sample Covariance Matrix."
    """
    n_samples, n_assets = sample.shape

    sample_cov = np.cov(sample, rowvar=False, ddof=1)
    if n_assets == 1:
        return sample_cov.copy(), 0.0

    std = np.sqrt(np.diag(sample_cov))
    std_safe = np.where(std > 0, std, 1.0)
    corr = sample_cov / np.outer(std_safe, std_safe)
    np.fill_diagonal(corr, 1.0)

    k_bar = corr.mean()
    target_corr = np.full((n_assets, n_assets), k_bar)
    np.fill_diagonal(target_corr, 1.0)
    target = target_corr * np.outer(std_safe, std_safe)

    diag_cov = np.diag(sample_cov)
    outer_std = np.outer(std_safe, std_safe)
    mask = outer_std > 0
    sum_var_diff = 0.0
    if mask.any():
        sum_var_diff = np.sum(
            (sample_cov[mask] - target[mask]) ** 2
        ) / n_samples

    np.outer(diag_cov, diag_cov)
    off_diag_mask = ~np.eye(n_assets, dtype=bool)
    prod = np.outer(std, std)
    off_diag_valid = off_diag_mask & (prod > 0)

    sum_bar = 0.0
    if off_diag_valid.any():
        r_ij = sample_cov[off_diag_valid] / prod[off_diag_valid]
        r_bar_sq = k_bar ** 2
        sum_bar = np.sum((r_ij - r_bar_sq) ** 2) / n_samples

    denom = sum_var_diff + sum_bar
    if denom <= 0:
        return target, 0.0

    shrinkage = float(min(1.0, max(0.0, sum_var_diff / denom)))

    return target, shrinkage


def estimate_covariance(
    returns_by_ticker: dict[str, npt.NDArray[np.float64]],
    *,
    min_observations: int = MIN_OBS_FOR_FULL_COVARIANCE,
) -> CovarianceEstimate:
    """Ledoit-Wolf 收缩协方差估计。

    步骤:
    1. 对齐收益率序列,排除有效观测不足的标的。
    2. 计算样本协方差矩阵 S。
    3. 构造常数相关系数目标矩阵 F。
    4. 计算最优收缩强度 delta = pi / gamma(Ledoit-Wolf 2004)。
    5. 返回 delta*F + (1-delta)*S。

    当窗口内有效样本不足时,delta -> 1.0,避免不稳定矩阵导致极端权重。
    """
    tickers, aligned = pairwise_aligned_returns(
        returns_by_ticker, min_overlap=min_observations
    )

    n_samples, n_assets = aligned.shape

    if n_assets == 1:
        var = float(np.var(aligned[:, 0], ddof=1)) if n_samples > 1 else 0.0
        return CovarianceEstimate(
            matrix=np.array([[var]], dtype=np.float64),
            tickers=tickers,
            shrinkage=1.0,
            n_observations=n_samples,
        )

    target, shrinkage = _ledoit_wolf_target_correlation(aligned)
    sample_cov = np.cov(aligned, rowvar=False, ddof=1)
    shrunk = shrinkage * target + (1.0 - shrinkage) * sample_cov

    if n_samples < min_observations * 2:
        extra = 1.0 - (n_samples - min_observations) / min_observations
        extra = float(max(0.0, min(1.0, extra)))
        shrinkage = min(1.0, shrinkage + extra * (1.0 - shrinkage))
        shrunk = shrinkage * target + (1.0 - shrinkage) * sample_cov

    shrunk = _ensure_positive_definite(shrunk)

    return CovarianceEstimate(
        matrix=shrunk,
        tickers=tickers,
        shrinkage=shrinkage,
        n_observations=n_samples,
    )


def _ensure_positive_definite(
    matrix: npt.NDArray[np.float64],
    *,
    min_eigenvalue: float = PSD_MIN_EIGENVALUE,
) -> npt.NDArray[np.float64]:
    """确保矩阵正定:对小特征值做 clipping。

    协方差矩阵在数值上可能因舍入误差而非正定(有微小负特征值),
    这里将所有 < min_eigenvalue 的特征值提升到 min_eigenvalue。
    """
    sym = (matrix + matrix.T) / 2.0
    eigenvalues, eigenvectors = np.linalg.eigh(sym)

    if eigenvalues.min() >= min_eigenvalue:
        return sym

    clipped = np.maximum(eigenvalues, min_eigenvalue)
    result = eigenvectors @ np.diag(clipped) @ eigenvectors.T
    return np.array((result + result.T) / 2.0, dtype=np.float64)


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "MIN_OBS_FOR_FULL_COVARIANCE",
    "PSD_MIN_EIGENVALUE",
    "CovarianceError",
    "CovarianceEstimate",
    "estimate_covariance",
    "pairwise_aligned_returns",
]
