"""新增研究级指标函数的单元测试。

覆盖:
* ``sortino_ratio`` —— 仅对下行波动惩罚;
* ``calmar_ratio`` —— 年化收益 / 最大回撤;
* ``monthly_returns`` / ``monthly_win_rate`` —— 月度聚合;
* ``drawdown_durations`` / ``max_drawdown_duration`` —— 持续期;
* ``value_at_risk`` / ``conditional_value_at_risk`` —— 尾部风险;
* ``beta`` / ``alpha`` / ``information_ratio`` —— 相对基准指标;
* ``rolling_sharpe`` —— 滚动窗口;
* ``daily_returns`` —— 基础工具。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.metrics import (
    alpha,
    beta,
    calmar_ratio,
    conditional_value_at_risk,
    daily_returns,
    drawdown_durations,
    information_ratio,
    max_drawdown_duration,
    monthly_returns,
    monthly_win_rate,
    rolling_sharpe,
    sharpe_ratio,
    sortino_ratio,
    value_at_risk,
)


def _curve(returns: list[float], start: date = date(2024, 1, 1)) -> list[tuple[date, Decimal]]:
    """根据收益率序列生成权益曲线。"""
    equity = 1.0
    curve: list[tuple[date, Decimal]] = [(start, Decimal("1"))]
    for i, r in enumerate(returns, start=1):
        equity *= 1 + r
        curve.append((start + timedelta(days=i), Decimal(str(round(equity, 6)))))
    return curve


class TestDailyReturns:
    def test_basic(self) -> None:
        returns = [0.01, -0.02, 0.005]
        curve = _curve(returns)
        out = daily_returns(curve)
        assert len(out) == 3
        assert out[0] == pytest.approx(0.01, abs=1e-6)
        assert out[1] == pytest.approx(-0.02, abs=1e-6)

    def test_empty(self) -> None:
        assert daily_returns([]) == []
        assert daily_returns([(date(2024, 1, 1), Decimal("1"))]) == []


class TestSortino:
    def test_higher_than_sharpe_for_positive_skew(self) -> None:
        # 上行多于下行 → Sortino > Sharpe
        returns = [0.01, 0.02, 0.005, -0.005, 0.015, 0.01, 0.02]
        curve = _curve(returns)
        s = sharpe_ratio(curve, risk_free_annual=0.0)
        so = sortino_ratio(curve, risk_free_annual=0.0)
        assert so > s

    def test_zero_returns(self) -> None:
        curve = _curve([0.0] * 5)
        # 零收益相对 3% rf → Sortino < 0(策略跑输无风险利率)
        assert sortino_ratio(curve) < 0
        # rf=0 时 → 无下行偏差 → 0
        assert sortino_ratio(curve, risk_free_annual=0.0) == 0.0


class TestCalmar:
    def test_no_drawdown_inf(self) -> None:
        # 单调上涨 → 无回撤 → inf
        returns = [0.01, 0.02, 0.015, 0.01]
        curve = _curve(returns)
        c = calmar_ratio(curve)
        assert c == float("inf")

    def test_with_drawdown(self) -> None:
        # 先涨后跌 → 有回撤
        returns = [0.1, -0.05, -0.05, 0.02]
        curve = _curve(returns)
        c = calmar_ratio(curve)
        assert isinstance(c, float)


class TestMonthly:
    def test_monthly_returns(self) -> None:
        # 跨 3 个月
        returns = [0.01] * 65  # ~ 3 个月
        curve = _curve(returns, start=date(2024, 1, 1))
        months = monthly_returns(curve)
        assert len(months) >= 2  # 至少 2 个月
        # 都应该是正收益
        for _, _, r in months:
            assert r > 0

    def test_monthly_win_rate(self) -> None:
        # 全正收益
        returns = [0.005] * 65
        curve = _curve(returns, start=date(2024, 1, 1))
        wr = monthly_win_rate(curve)
        assert wr == pytest.approx(1.0)


class TestDrawdownDuration:
    def test_no_drawdown(self) -> None:
        returns = [0.01, 0.02, 0.01]
        curve = _curve(returns)
        assert max_drawdown_duration(curve) == 0
        assert drawdown_durations(curve) == []

    def test_with_drawdown(self) -> None:
        # 涨到峰值 → 跌 3 天 → 反弹
        returns = [0.1, -0.01, -0.01, -0.01, 0.1]
        curve = _curve(returns)
        durations = drawdown_durations(curve)
        assert len(durations) >= 1
        assert max(durations) >= 3


class TestValueAtRisk:
    def test_returns_negative_for_loss(self) -> None:
        # 一半收益、一半损失
        returns = [0.01, -0.02, 0.005, -0.015, 0.01, -0.025]
        curve = _curve(returns)
        var = value_at_risk(curve, confidence=0.95)
        assert var < 0  # 95% 置信下单日最大损失

    def test_cvar_worse_than_var(self) -> None:
        returns = [0.01, -0.02, 0.005, -0.015, 0.01, -0.025, -0.03]
        curve = _curve(returns)
        var = value_at_risk(curve, confidence=0.95)
        cvar = conditional_value_at_risk(curve, confidence=0.95)
        # CVaR 应当 ≤ VaR(更深的尾部)
        assert cvar <= var + 1e-9


class TestBetaAlpha:
    def test_beta_one_for_identical(self) -> None:
        returns = [0.01, -0.02, 0.005, 0.015]
        curve = _curve(returns)
        b = beta(curve, curve)
        assert b == pytest.approx(1.0, abs=1e-6)

    def test_beta_zero_for_uncorrelated(self) -> None:
        rng_returns = [0.01, -0.01, 0.02, -0.02]
        opposite = [-0.01, 0.01, -0.02, 0.02]
        curve1 = _curve(rng_returns)
        curve2 = _curve(opposite)
        b = beta(curve1, curve2)
        # 完全反向 → beta 应为负
        assert b < 0

    def test_alpha_zero_for_identical(self) -> None:
        returns = [0.01, -0.02, 0.005]
        curve = _curve(returns)
        a = alpha(curve, curve, risk_free_annual=0.0)
        assert a == pytest.approx(0.0, abs=1e-3)


class TestInformationRatio:
    def test_zero_for_identical(self) -> None:
        returns = [0.01, -0.02, 0.005, 0.015, 0.01, -0.005]
        curve = _curve(returns)
        ir = information_ratio(curve, curve)
        assert ir == pytest.approx(0.0, abs=1e-6)

    def test_positive_for_outperforming(self) -> None:
        # 策略稳定跑赢基准
        strat_returns = [0.01, 0.015, 0.012, 0.014]
        bench_returns = [0.005, 0.005, 0.005, 0.005]
        strat = _curve(strat_returns)
        bench = _curve(bench_returns)
        ir = information_ratio(strat, bench)
        assert ir > 0


class TestRollingSharpe:
    def test_window_too_short(self) -> None:
        returns = [0.01, 0.02]
        curve = _curve(returns)
        out = rolling_sharpe(curve, window=63)
        assert out == []

    def test_returns_one_point_per_window(self) -> None:
        returns = [0.005] * 100
        curve = _curve(returns)
        out = rolling_sharpe(curve, window=20)
        assert len(out) >= 1
        # 每个点对应一个 date
        for d, s in out:
            assert isinstance(d, date)
            assert isinstance(s, float)
