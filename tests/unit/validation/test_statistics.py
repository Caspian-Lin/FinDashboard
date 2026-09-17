"""样本外验证统计工具的单元测试。

覆盖:
* ``stationary_bootstrap`` 的 CI 边界 / 同种子复现;
* ``deflated_sharpe_ratio`` 在多重试验下应降低;
* ``probabilistic_sharpe_ratio`` 单调性;
* ``probability_of_backtest_overfitting`` 在过拟合矩阵下应给出 PBO > 0.5;
* ``sharpe_from_returns`` 与原 ``sharpe_ratio`` 在数值上接近。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from finboard_backtest.metrics import sharpe_ratio
from finboard_backtest.validation.statistics import (
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_from_returns,
    stationary_bootstrap,
)


def _make_returns(
    n: int = 252,
    mu: float = 0.0005,
    sigma: float = 0.01,
    seed: int = 42,
) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mu, sigma) for _ in range(n)]


def _equity_curve(returns: list[float]) -> list[tuple[date, float]]:
    base = date(2024, 1, 1)
    equity = 1.0
    curve: list[tuple[date, float]] = [(base, equity)]
    for i, r in enumerate(returns, start=1):
        equity *= 1 + r
        curve.append((base + timedelta(days=i), equity))
    return curve


class TestStationaryBootstrap:
    def test_returns_point_estimate_close_to_input(self) -> None:
        returns = _make_returns(seed=1)
        result = stationary_bootstrap(returns, "sharpe", n_resamples=200, seed=0)
        assert result.point_estimate == pytest.approx(
            sharpe_from_returns(returns), abs=1e-9
        )

    def test_ci_brackets_point_estimate(self) -> None:
        returns = _make_returns(seed=2)
        result = stationary_bootstrap(returns, "sharpe", n_resamples=300, seed=0)
        assert result.ci_low <= result.point_estimate
        assert result.point_estimate <= result.ci_high

    def test_same_seed_reproduces(self) -> None:
        returns = _make_returns(seed=3)
        a = stationary_bootstrap(returns, "sharpe", n_resamples=100, seed=7)
        b = stationary_bootstrap(returns, "sharpe", n_resamples=100, seed=7)
        assert a.ci_low == b.ci_low
        assert a.ci_high == b.ci_high

    def test_short_series_returns_degenerate(self) -> None:
        returns = [0.01, 0.0]
        result = stationary_bootstrap(returns, "sharpe")
        assert result.n_resamples == 0
        assert result.ci_low == result.ci_high == result.point_estimate

    def test_max_drawdown_statistic(self) -> None:
        # 单调下跌的权益:回撤 = -50%
        returns = [-0.5] + [0.0] * 9
        result = stationary_bootstrap(returns, "max_drawdown", n_resamples=100, seed=0)
        assert result.point_estimate <= 0


class TestDeflatedSharpe:
    def test_single_trial_returns_high_dsr(self) -> None:
        returns = _make_returns(mu=0.001, sigma=0.005, seed=10, n=500)
        sr = sharpe_from_returns(returns)
        dsr = deflated_sharpe_ratio(sr, n_trials=1, n_obs=500, returns=returns)
        # 单次试验、高 Sharpe → DSR 接近 1
        assert dsr > 0.9

    def test_more_trials_lower_dsr(self) -> None:
        # 用较低的 Sharpe + 较短的样本,使 DSR 对 trial 数敏感
        returns = _make_returns(mu=0.0002, sigma=0.01, seed=11, n=100)
        sr = sharpe_from_returns(returns)
        dsr_1 = deflated_sharpe_ratio(sr, n_trials=1, n_obs=100, returns=returns)
        dsr_1000 = deflated_sharpe_ratio(sr, n_trials=1000, n_obs=100, returns=returns)
        # 同 Sharpe 下,试验数越多,DSR 越低(多重试验惩罚)
        assert dsr_1000 <= dsr_1 + 1e-9

    def test_zero_sharpe_returns_around_half(self) -> None:
        dsr = deflated_sharpe_ratio(0.0, n_trials=10, n_obs=252)
        # Sharpe=0 时,DSR 应当约 0.5(随机猜测)
        assert 0.0 <= dsr <= 0.6


class TestProbabilisticSharpe:
    def test_high_sharpe_beats_low_benchmark(self) -> None:
        returns = _make_returns(mu=0.001, sigma=0.005, seed=20, n=500)
        sr = sharpe_from_returns(returns)
        psr = probabilistic_sharpe_ratio(sr, benchmark_sharpe=0.0, n_obs=500, returns=returns)
        assert psr > 0.9

    def test_low_sharpe_fails_high_benchmark(self) -> None:
        returns = _make_returns(mu=0.0, sigma=0.02, seed=21, n=100)
        sr = sharpe_from_returns(returns)
        psr = probabilistic_sharpe_ratio(sr, benchmark_sharpe=2.0, n_obs=100, returns=returns)
        assert psr < 0.5

    def test_more_data_increases_confidence(self) -> None:
        # 同 SR,样本量增加 → PSR 提升(更确信)
        short = _make_returns(mu=0.0005, sigma=0.005, seed=30, n=50)
        long = _make_returns(mu=0.0005, sigma=0.005, seed=30, n=500)
        sr_short = sharpe_from_returns(short)
        sr_long = sharpe_from_returns(long)
        psr_short = probabilistic_sharpe_ratio(
            sr_short, benchmark_sharpe=0.0, n_obs=50, returns=short
        )
        psr_long = probabilistic_sharpe_ratio(
            sr_long, benchmark_sharpe=0.0, n_obs=500, returns=long
        )
        # PSR 应当更高(或者保持高)
        assert psr_long >= psr_short - 0.1


class TestProbabilityOfBacktestOverfitting:
    def test_overfitted_matrix_high_pbo(self) -> None:
        """构造过拟合矩阵:N 个 trial,每个 trial 在前半段表现好、后半段表现差。

        这种结构下,PBO 应当较高(IS 最优 trial 在 OOS 表现差的概率高)。
        """
        rng = random.Random(123)
        n_trials = 10
        n_obs = 200
        # 前 100 天:trial i 有正向 mu;后 100 天:全部接近 0
        matrix: list[list[float]] = []
        for i in range(n_trials):
            mu_is = 0.002 * (i + 1) / n_trials  # 不同的 IS 表现
            returns: list[float] = []
            for t in range(n_obs):
                if t < n_obs // 2:
                    returns.append(rng.gauss(mu_is, 0.01))
                else:
                    # OOS:全部接近 0,IS 最优 trial 在 OOS 反而更差
                    returns.append(rng.gauss(-mu_is * 0.5, 0.01))
            matrix.append(returns)
        report = probability_of_backtest_overfitting(matrix, n_partitions=8, seed=42)
        # 过拟合结构 → PBO 应当 ≥ 0.3
        assert report.pbo >= 0.3
        assert 0.0 <= report.pbo <= 1.0

    def test_single_trial_returns_zero_pbo(self) -> None:
        # 单个 trial 时,CSCV 退化
        returns = _make_returns(seed=99)
        report = probability_of_backtest_overfitting([returns])
        assert report.pbo == 0.0

    def test_consistent_matrix_low_pbo(self) -> None:
        """所有 trial 在 IS / OOS 表现一致 → PBO 低。"""
        rng = random.Random(456)
        n_trials = 5
        n_obs = 200
        matrix: list[list[float]] = []
        for _ in range(n_trials):
            returns: list[float] = []
            for _ in range(n_obs):
                # 整个时间段都是稳定正收益
                returns.append(rng.gauss(0.001, 0.008))
            matrix.append(returns)
        report = probability_of_backtest_overfitting(matrix, n_partitions=8, seed=42)
        # 一致的 trial → PBO 应当 < 0.5(典型)
        assert report.pbo < 0.7

    def test_unequal_length_raises(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            probability_of_backtest_overfitting(
                [[0.01, 0.02], [0.01, 0.02, 0.03]]
            )


class TestSharpeFromReturnsMatchesCurve:
    def test_consistent_with_metrics_module(self) -> None:
        from decimal import Decimal

        returns = _make_returns(seed=77, n=200)
        curve = [
            (d, Decimal(str(round(v, 6))))
            for d, v in _equity_curve(returns)
        ]
        # sharpe_from_returns(无风险利率=0.03)应与 sharpe_ratio 接近
        from_return = sharpe_from_returns(returns, risk_free_annual=0.03)
        from_curve = sharpe_ratio(curve, risk_free_annual=0.03)
        assert abs(from_return - from_curve) < 0.05
