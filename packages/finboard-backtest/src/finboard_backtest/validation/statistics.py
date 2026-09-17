"""统计推断工具(issue #57)。

提供四类多重试验 / 非正态修正:

1. **Block bootstrap CI** —— 对 Sharpe / 最大回撤生成置信区间,
   保留时序相关性(politis-romano stationary bootstrap 简化版)。
2. **Deflated Sharpe Ratio (DSR)** —— Bailey & López de Prado (2014) 的
   多重试验修正,已知试验次数 N、样本长度 T、偏度 κ、超额夏普最大值。
3. **Probabilistic Sharpe Ratio (PSR)** —— 给定基准 Sharpe,推断真实
   Sharpe 大于基准的概率。
4. **PBO (Probability of Backtest Overfitting)** —— Bailey et al. (2017)
   的 CSCV(Combinatorially Symmetric Cross-Validation)实现。

注意:统计门只降低虚假发现概率,**不承诺未来收益**。所有阈值必须在实验前
声明,不得事后调整。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from finboard_backtest.metrics import sharpe_from_daily_returns


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def _std(xs: Sequence[float]) -> float:
    return math.sqrt(_variance(xs))


def _skewness(xs: Sequence[float]) -> float:
    if len(xs) < 3:
        return 0.0
    m = _mean(xs)
    s = _std(xs)
    if s == 0:
        return 0.0
    return sum(((x - m) / s) ** 3 for x in xs) / len(xs)


def _kurtosis(xs: Sequence[float]) -> float:
    """超额峰度(excess kurtosis)。"""
    if len(xs) < 4:
        return 0.0
    m = _mean(xs)
    s = _std(xs)
    if s == 0:
        return 0.0
    return sum(((x - m) / s) ** 4 for x in xs) / len(xs) - 3.0


def sharpe_from_returns(
    daily_returns: Sequence[float],
    risk_free_annual: float = 0.03,
    annualization: int = 252,
) -> float:
    """从日收益率序列直接计算年化 Sharpe。

    口径(issue #262):默认 rf=3%/年、总体标准差(ddof=0)、√年化 —— 与
    事件驱动引擎主口径 ``metrics.sharpe_ratio`` 一致;PBO 等排名场景显式传
    ``risk_free_annual=0.0``(对同一批 trial 的 rank 无影响,保持既有约定)。
    委托 ``metrics.sharpe_from_daily_returns`` 统一实现。
    """
    if len(daily_returns) < 3:
        return 0.0
    return sharpe_from_daily_returns(daily_returns, risk_free_annual, ddof=0, annualization=annualization)


def max_drawdown_from_equity(equity: Sequence[float]) -> float:
    """从权益曲线(纯数值)计算最大回撤(负数)。"""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Stationary bootstrap (Politis & Romano 1994,简化版)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """单次 bootstrap 置信区间输出。"""

    point_estimate: float
    ci_low: float
    ci_high: float
    n_resamples: int
    confidence: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "point_estimate": self.point_estimate,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "n_resamples": self.n_resamples,
            "confidence": self.confidence,
        }


def stationary_bootstrap(
    returns: Sequence[float],
    statistic: str = "sharpe",
    *,
    n_resamples: int = 1000,
    confidence: float = 0.95,
    block_prob: float = 0.05,
    seed: int = 0,
    risk_free_annual: float = 0.03,
) -> BootstrapResult:
    """Stationary block bootstrap 置信区间。

    参数
    ----
    returns
        原始日收益率序列。
    statistic
        ``"sharpe"`` 或 ``"max_drawdown"``。
    n_resamples
        重采样次数(默认 1000)。
    confidence
        置信水平(0.95 = 95% CI)。
    block_prob
        块结束概率(平均块长 = 1 / block_prob;0.05 → 平均块长 20)。
    seed
        随机种子(同种子结果可复现)。

    返回
    ----
    ``BootstrapResult`` —— 包含点估计与置信区间上下界。
    """
    if len(returns) < 5:
        point = _point_statistic(returns, statistic, risk_free_annual)
        return BootstrapResult(
            point_estimate=point,
            ci_low=point,
            ci_high=point,
            n_resamples=0,
            confidence=confidence,
        )
    if not 0 < block_prob <= 1:
        raise ValueError("block_prob must be in (0, 1]")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")

    rng = random.Random(seed)
    n = len(returns)
    point = _point_statistic(returns, statistic, risk_free_annual)
    estimates: list[float] = []

    for _ in range(n_resamples):
        sample = _resample_block(returns, n, block_prob, rng)
        estimates.append(_point_statistic(sample, statistic, risk_free_annual))

    estimates.sort()
    alpha = (1 - confidence) / 2
    low_idx = max(0, int(alpha * n_resamples))
    high_idx = min(n_resamples - 1, int((1 - alpha) * n_resamples))
    return BootstrapResult(
        point_estimate=point,
        ci_low=estimates[low_idx],
        ci_high=estimates[high_idx],
        n_resamples=n_resamples,
        confidence=confidence,
    )


def _point_statistic(
    returns: Sequence[float], statistic: str, risk_free_annual: float
) -> float:
    if statistic == "sharpe":
        return sharpe_from_returns(returns, risk_free_annual=risk_free_annual)
    if statistic == "max_drawdown":
        # 把 returns 转成等价权益曲线
        equity = [1.0]
        for r in returns:
            equity.append(equity[-1] * (1.0 + r))
        return max_drawdown_from_equity(equity)
    raise ValueError(f"unknown statistic: {statistic}")


def _resample_block(
    returns: Sequence[float], n: int, block_prob: float, rng: random.Random
) -> list[float]:
    """Stationary bootstrap 重采样(平均块长 = 1 / block_prob)。"""
    out: list[float] = []
    i = rng.randint(0, n - 1)
    while len(out) < n:
        out.append(returns[i])
        i = rng.randint(0, n - 1) if rng.random() < block_prob else (i + 1) % n
    return out


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & López de Prado 2014)
# ---------------------------------------------------------------------------


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    n_obs: int,
    *,
    skewness: float | None = None,
    kurtosis: float | None = None,
    returns: Sequence[float] | None = None,
) -> float:
    """Deflated Sharpe Ratio。

    修正"从 N 次试验中挑出最优 Sharpe"产生的多重比较偏差。

    参数
    ----
    observed_sharpe
        观测到的最高 Sharpe(从 N 次试验中选出)。
    n_trials
        实际进行的试验总数(包括失败 / 拒绝的)。
    n_obs
        每次试验的观测数(样本长度,通常是交易日数)。
    skewness / kurtosis
        收益分布的偏度 / 超额峰度。若不传则用 ``returns`` 估计。
    returns
        最优 trial 的日收益率序列(用于估计 skew / kurt)。

    返回
    ----
    DSR ∈ [0, 1]:DSR 越接近 1,Sharpe 在多重试验后仍然显著。
    """
    if n_trials < 1:
        n_trials = 1
    if n_obs < 1:
        return 0.0

    if skewness is None or kurtosis is None:
        if returns is None or len(returns) < 4:
            skewness = 0.0 if skewness is None else skewness
            kurtosis = 0.0 if kurtosis is None else kurtosis
        else:
            skewness = _skewness(returns) if skewness is None else skewness
            kurtosis = _kurtosis(returns) if kurtosis is None else kurtosis

    # 估计 Expected Maximum of N iid Standard Normal: E[max_N]
    emn = _expected_max_normal(n_trials)

    # Sharpe 的方差估计(考虑偏度 gamma 与峰度 kappa 修正)
    # Var[SR] ~= (1 + 0.5 SR^2 - gamma*SR + (kappa-1)/4 SR^2) / (T-1)
    sr = observed_sharpe
    var_sr = (1 + 0.5 * sr**2 - skewness * sr + (kurtosis / 4) * sr**2) / max(1, n_obs - 1)
    std_sr = math.sqrt(var_sr) if var_sr > 0 else 1.0

    # Deflated Sharpe:把观测 SR 与"零假设下的期望最大 SR"对比
    # SR_0 = emn * std_sr
    sr_null = emn * std_sr

    # 计算 P(SR > SR_null | observed)
    # 用非中心 t 分布的近似(SR 在零假设下近似正态)
    if std_sr == 0:
        return 0.5 if observed_sharpe > sr_null else 0.0
    z = (sr - sr_null) / std_sr
    return _standard_normal_cdf(z)


def _expected_max_normal(n: int) -> float:
    """Approximation of E[max of N iid Standard Normal].

    For large N, ~ sqrt(2 ln N). See Bailey & Lopez de Prado 2014.
    """
    if n <= 1:
        return 0.0
    return math.sqrt(2 * math.log(n))


def _standard_normal_cdf(z: float) -> float:
    """标准正态 CDF(用 erf)。"""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Probabilistic Sharpe Ratio (López de Prado 2012)
# ---------------------------------------------------------------------------


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    n_obs: int,
    *,
    skewness: float | None = None,
    kurtosis: float | None = None,
    returns: Sequence[float] | None = None,
) -> float:
    """Probabilistic Sharpe Ratio(PSR)。

    返回"真实 Sharpe 大于 benchmark_sharpe"的估计概率。
    PSR < 0.5 提示观测 SR 不可靠地超过基准。
    """
    if n_obs < 2:
        return 0.0

    if skewness is None or kurtosis is None:
        if returns is None or len(returns) < 4:
            skewness = 0.0 if skewness is None else skewness
            kurtosis = 0.0 if kurtosis is None else kurtosis
        else:
            skewness = _skewness(returns) if skewness is None else skewness
            kurtosis = _kurtosis(returns) if kurtosis is None else kurtosis

    sr = observed_sharpe
    # PSR variance: Var[SR] ~ (1 - gamma*SR + (kappa-1)/4 SR^2) / (T-1)
    var_sr = (1 - skewness * sr + (kurtosis / 4) * sr**2) / max(1, n_obs - 1)
    std_sr = math.sqrt(var_sr) if var_sr > 0 else 1.0

    if std_sr == 0:
        return 0.5 if sr > benchmark_sharpe else 0.0
    z = (sr - benchmark_sharpe) / std_sr
    return _standard_normal_cdf(z)


# ---------------------------------------------------------------------------
# Probability of Backtest Overfitting (PBO) via CSCV
# Bailey, Borwein, López de Prado, Zhu (2017)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PBOReport:
    """PBO 完整报告。"""

    pbo: float  # PBO ∈ [0, 1]
    logit_pbo: float  # logit(PBO),用于做灵敏度分析
    n_trials: int
    n_partitions: int
    methodology: str = "CSCV"

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "pbo": self.pbo,
            "logit_pbo": self.logit_pbo,
            "n_trials": self.n_trials,
            "n_partitions": self.n_partitions,
            "methodology": self.methodology,
        }


def probability_of_backtest_overfitting(
    returns_matrix: Sequence[Sequence[float]],
    *,
    n_partitions: int = 16,
    seed: int = 0,
) -> PBOReport:
    """PBO via Combinatorially Symmetric Cross-Validation (CSCV)。

    参数
    ----
    returns_matrix
        ``shape = (n_trials, n_obs)`` —— 每个试验的日收益率序列。所有试验
        必须等长(对齐到同一交易日)。
    n_partitions
        把时间序列分成多少份(默认 16),取一半作 IS、一半作 OOS,组合对称。
    seed
        随机种子。

    返回
    ----
    ``PBOReport`` —— ``pbo`` 越接近 1 越过拟合;``pbo < 0.5`` 通常算可接受。
    """
    n_trials = len(returns_matrix)
    if n_trials < 2:
        return PBOReport(pbo=0.0, logit_pbo=float("-inf"), n_trials=n_trials, n_partitions=n_partitions)
    n_obs = len(returns_matrix[0])
    for r in returns_matrix:
        if len(r) != n_obs:
            raise ValueError("all trials must have the same length")
    if n_obs < n_partitions * 2:
        # 样本不足以做 CSCV,返回保守估计
        return PBOReport(pbo=0.5, logit_pbo=0.0, n_trials=n_trials, n_partitions=n_partitions)

    rng = random.Random(seed)
    # 把时间序列分成 n_partitions 个等长块
    block_size = n_obs // n_partitions
    indices = list(range(n_obs))
    rng.shuffle(indices)  # 用 shuffle 保持块内时序但块间随机
    blocks = [indices[i * block_size : (i + 1) * block_size] for i in range(n_partitions)]

    # 取一半作 IS,一半作 OOS —— 对称组合(简化:用蒙特卡洛采样)
    n_combinations = min(100, _n_choose_k(n_partitions, n_partitions // 2))
    n_is_blocks = n_partitions // 2
    n_oos = 0
    n_total = 0

    for _ in range(n_combinations):
        is_blocks_idx = rng.sample(range(n_partitions), n_is_blocks)
        oos_blocks_idx = [i for i in range(n_partitions) if i not in is_blocks_idx]
        is_idx = sorted(idx for b in is_blocks_idx for idx in blocks[b])
        oos_idx = sorted(idx for b in oos_blocks_idx for idx in blocks[b])

        # 计算每个 trial 在 IS / OOS 的 Sharpe
        is_sharpes: list[float] = []
        oos_sharpes: list[float] = []
        for trial_returns in returns_matrix:
            is_returns = [trial_returns[i] for i in is_idx]
            oos_returns = [trial_returns[i] for i in oos_idx]
            is_sharpes.append(sharpe_from_returns(is_returns, risk_free_annual=0.0))
            oos_sharpes.append(sharpe_from_returns(oos_returns, risk_free_annual=0.0))

        # IS 最优 trial
        is_best_idx = is_sharpes.index(max(is_sharpes))
        # OOS rank
        oos_ranked = sorted(range(n_trials), key=lambda i: oos_sharpes[i])
        oos_rank = oos_ranked.index(is_best_idx)  # 0 = 最差
        # PBO 定义:IS 最优在 OOS 的相对 rank ≤ 0.5
        relative_rank = (oos_rank + 1) / n_trials
        if relative_rank <= 0.5:
            n_oos += 1
        n_total += 1

    if n_total == 0:
        return PBOReport(pbo=0.5, logit_pbo=0.0, n_trials=n_trials, n_partitions=n_partitions)
    pbo = n_oos / n_total
    if pbo in (0.0, 1.0):
        logit = float("-inf") if pbo == 0.0 else float("inf")
    else:
        logit = math.log(pbo / (1 - pbo))
    return PBOReport(pbo=pbo, logit_pbo=logit, n_trials=n_trials, n_partitions=n_partitions)


def _n_choose_k(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    if k == 0 or k == n:
        return 1
    k = min(k, n - k)
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


# ---------------------------------------------------------------------------
# 综合报告生成
# ---------------------------------------------------------------------------


def build_statistical_report(
    *,
    best_trial_returns: Sequence[float],
    all_trial_returns_matrix: Sequence[Sequence[float]] | None,
    n_trials: int,
    benchmark_sharpe: float = 0.0,
    risk_free_annual: float = 0.03,
    bootstrap_seed: int = 0,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> tuple[float, float, float, float, float, float, float]:
    """计算一个 trial 的统计修正报告(返回 7 个值)。

    返回
    ----
    ``(dsr, psr, pbo, sharpe_ci_low, sharpe_ci_high, mdd_ci_low, mdd_ci_high)``
    """
    if not best_trial_returns:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    n_obs = len(best_trial_returns)
    observed_sharpe = sharpe_from_returns(best_trial_returns, risk_free_annual)

    dsr = deflated_sharpe_ratio(
        observed_sharpe,
        n_trials=n_trials,
        n_obs=n_obs,
        returns=best_trial_returns,
    )
    psr = probabilistic_sharpe_ratio(
        observed_sharpe,
        benchmark_sharpe=benchmark_sharpe,
        n_obs=n_obs,
        returns=best_trial_returns,
    )

    if all_trial_returns_matrix and len(all_trial_returns_matrix) >= 2:
        pbo_report = probability_of_backtest_overfitting(
            all_trial_returns_matrix, seed=bootstrap_seed
        )
        pbo = pbo_report.pbo
    else:
        pbo = 0.0

    sharpe_ci = stationary_bootstrap(
        best_trial_returns,
        "sharpe",
        n_resamples=n_bootstrap,
        confidence=confidence,
        seed=bootstrap_seed,
        risk_free_annual=risk_free_annual,
    )
    mdd_ci = stationary_bootstrap(
        best_trial_returns,
        "max_drawdown",
        n_resamples=n_bootstrap,
        confidence=confidence,
        seed=bootstrap_seed,
        risk_free_annual=risk_free_annual,
    )

    return (
        dsr,
        psr,
        pbo,
        sharpe_ci.ci_low,
        sharpe_ci.ci_high,
        mdd_ci.ci_low,
        mdd_ci.ci_high,
    )
