"""回测绩效指标计算。

所有指标基于每日权益曲线(equity curve)和成交记录(Fill list)计算。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from finboard_shared.models import Bar, Fill


def total_return(equity_curve: Sequence[tuple[date, Decimal]]) -> float:
    """总收益率。"""
    if len(equity_curve) < 2:
        return 0.0
    start = float(equity_curve[0][1])
    end = float(equity_curve[-1][1])
    if start <= 0:
        return 0.0
    return (end - start) / start


def annualized_return(equity_curve: Sequence[tuple[date, Decimal]]) -> float:
    """年化收益率。"""
    if len(equity_curve) < 2:
        return 0.0
    start_val = float(equity_curve[0][1])
    end_val = float(equity_curve[-1][1])
    days = (equity_curve[-1][0] - equity_curve[0][0]).days
    if days <= 0 or start_val <= 0:
        return 0.0
    years = days / 365.25
    return float((end_val / start_val) ** (1.0 / years) - 1.0)


def sharpe_ratio(
    equity_curve: Sequence[tuple[date, Decimal]],
    risk_free_annual: float = 0.03,
) -> float:
    """夏普比率(日频 → 年化)。

    无风险利率默认 3%/年。
    """
    if len(equity_curve) < 3:
        return 0.0

    daily_returns: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1][1])
        curr = float(equity_curve[i][1])
        if prev > 0:
            daily_returns.append((curr - prev) / prev)

    if not daily_returns:
        return 0.0

    mean_r = sum(daily_returns) / len(daily_returns)
    var_r = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
    std_r = math.sqrt(var_r)
    if std_r == 0:
        return 0.0

    rf_daily = risk_free_annual / 252
    excess = mean_r - rf_daily
    return excess / std_r * math.sqrt(252)


def max_drawdown(equity_curve: Sequence[tuple[date, Decimal]]) -> float:
    """最大回撤(返回负数,如 -0.15 表示 15% 回撤)。"""
    if len(equity_curve) < 2:
        return 0.0

    peak = float(equity_curve[0][1])
    max_dd = 0.0
    for _, val in equity_curve:
        v = float(val)
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def win_rate(fills: Sequence[Fill]) -> float:
    """胜率 —— 基于完整的买卖配对。

    简化 FIFO 配对:先买入的仓位,先与后续卖出配对。
    """
    buy_queue: list[tuple[Decimal, Decimal]] = []  # (qty, price)
    wins = 0
    total_pairs = 0

    for fill in fills:
        if fill.side.value == "buy":
            buy_queue.append((fill.quantity, fill.price))
        else:
            remaining = fill.quantity
            sell_price = fill.price
            while remaining > 0 and buy_queue:
                buy_qty, buy_price = buy_queue[0]
                matched = min(remaining, buy_qty)
                if sell_price > buy_price:
                    wins += 1
                total_pairs += 1
                remaining -= matched
                buy_qty -= matched
                if buy_qty <= 0:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_qty, buy_price)

    if total_pairs == 0:
        return 0.0
    return wins / total_pairs


def total_commission(fills: Sequence[Fill]) -> Decimal:
    return sum((f.commission for f in fills), Decimal("0"))


def total_tax(fills: Sequence[Fill]) -> Decimal:
    return sum((f.tax for f in fills), Decimal("0"))


def turnover_ratio(
    fills: Sequence[Fill],
    equity_curve: Sequence[tuple[date, Decimal]],
) -> float:
    """换手率 = 总成交额 / 平均权益。"""
    if not equity_curve:
        return 0.0
    total_turnover = sum(float(f.quantity * f.price) for f in fills)
    avg_equity = sum(float(v) for _, v in equity_curve) / len(equity_curve)
    if avg_equity <= 0:
        return 0.0
    days = len(equity_curve)
    return (total_turnover / avg_equity) * (252 / days) if days > 0 else 0.0


def buy_and_hold_return(
    bars: Sequence[tuple[date, Decimal]],
    initial_capital: Decimal,
) -> list[tuple[date, Decimal]]:
    """买入持有基准权益曲线。

    :param bars: [(date, close_price), ...]
    :param initial_capital: 初始资金
    :returns: 每日权益曲线
    """
    if not bars:
        return []
    first_price = bars[0][1]
    shares = (initial_capital / first_price).quantize(Decimal("1"))
    return [(d, shares * p) for d, p in bars]


def equal_weight_universe_return(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    initial_capital: Decimal,
) -> list[tuple[date, Decimal]]:
    """等权多标的买入持有基准。

    每个标的分配 ``initial_capital / N``;每条 Bar 上对齐所有标的的日期,
    在每个共同日期上求和得到基准权益。空候选池返回空曲线。

    issue #56:多标的回测的默认基准不再只取第一个标的。
    """
    symbols = [code for code, bars in bars_by_symbol.items() if bars]
    if not symbols:
        return []

    per_symbol_capital = initial_capital / Decimal(len(symbols))

    # 把每个 symbol 的(date, close)按日期索引
    price_by_date: dict[date, dict[str, Decimal]] = defaultdict(dict)
    shares_by_symbol: dict[str, Decimal] = {}
    for code in symbols:
        bars = bars_by_symbol[code]
        if not bars:
            continue
        first_close = bars[0].close
        if first_close <= 0:
            continue
        shares = (per_symbol_capital / first_close).quantize(Decimal("1"))
        shares_by_symbol[code] = shares
        for bar in bars:
            price_by_date[bar.timestamp.date()][code] = bar.close

    if not shares_by_symbol:
        return []

    common_dates = sorted(
        {
            d
            for d, codes in price_by_date.items()
            if set(codes) >= set(shares_by_symbol)
        }
    )
    curve: list[tuple[date, Decimal]] = []
    for d in common_dates:
        total = Decimal("0")
        for code, shares in shares_by_symbol.items():
            price = price_by_date[d].get(code)
            if price is None:
                # 缺数据时沿用上一根 bar 的市值(简化)
                if curve:
                    total += curve[-1][1] / Decimal(len(shares_by_symbol))
                continue
            total += shares * price
        curve.append((d, total))
    return curve


def trading_days_between(start: date, end: date) -> int:
    """估算交易日数(排除周末,粗略)。"""
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# 研究级指标(issue #57) —— 用于样本外验证与多重试验修正
# ---------------------------------------------------------------------------

_ANNUALIZATION = 252  # 年化因子(交易日)


def daily_returns(
    equity_curve: Sequence[tuple[date, Decimal]],
) -> list[float]:
    """从权益曲线计算日收益率序列(长度 = len-1)。"""
    out: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1][1])
        curr = float(equity_curve[i][1])
        if prev > 0:
            out.append((curr - prev) / prev)
        else:
            out.append(0.0)
    return out


def sortino_ratio(
    equity_curve: Sequence[tuple[date, Decimal]],
    risk_free_annual: float = 0.03,
    target_daily_return: float = 0.0,
) -> float:
    """Sortino 比率 —— 仅对下行波动率惩罚。

    与 Sharpe 的差异:Sortino 不惩罚上行波动,因此对"上涨快、回撤小"的策略
    给出更高评分。研究流水线应同时报告 Sharpe 与 Sortino。
    """
    if len(equity_curve) < 3:
        return 0.0
    returns = daily_returns(equity_curve)
    rf_daily = risk_free_annual / _ANNUALIZATION
    excess = [r - rf_daily for r in returns]
    downside = [(r - target_daily_return) ** 2 for r in excess if r < target_daily_return]
    if not downside:
        return 0.0
    downside_dev = math.sqrt(sum(downside) / len(downside))
    if downside_dev == 0:
        return 0.0
    mean_excess = sum(excess) / len(excess)
    return mean_excess / downside_dev * math.sqrt(_ANNUALIZATION)


def calmar_ratio(equity_curve: Sequence[tuple[date, Decimal]]) -> float:
    """Calmar 比率 = 年化收益率 / |最大回撤|。

    高 Calmar 表示策略在控制下行风险的前提下仍能产生收益。无回撤时返回 ``inf``;
    研究门应在阈值中使用 ``min(...)`` 处理 ``inf``。
    """
    if len(equity_curve) < 3:
        return 0.0
    ann = annualized_return(equity_curve)
    mdd = abs(max_drawdown(equity_curve))
    if mdd == 0:
        return float("inf") if ann > 0 else 0.0
    return ann / mdd


def monthly_returns(equity_curve: Sequence[tuple[date, Decimal]]) -> list[tuple[int, int, float]]:
    """按月聚合的收益率序列,返回 ``[(year, month, return), ...]``。

    用于检验月度胜率、长期稳定性和季节性。空曲线或单点曲线返回空列表。
    """
    if len(equity_curve) < 2:
        return []

    by_month: dict[tuple[int, int], Decimal] = {}
    for d, v in equity_curve:
        key = (d.year, d.month)
        if key not in by_month:
            by_month[key] = v
        # 月内最后一个值覆盖(用 close-to-close 而不是 open-to-close)
        by_month[key] = v

    sorted_months = sorted(by_month.items())
    out: list[tuple[int, int, float]] = []
    for i in range(1, len(sorted_months)):
        (y, m), last = sorted_months[i]
        _, prev = sorted_months[i - 1]
        prev_f = float(prev)
        last_f = float(last)
        if prev_f > 0:
            out.append((y, m, (last_f - prev_f) / prev_f))
        else:
            out.append((y, m, 0.0))
    return out


def monthly_win_rate(equity_curve: Sequence[tuple[date, Decimal]]) -> float:
    """月度胜率 = 正收益月份占比。"""
    months = monthly_returns(equity_curve)
    if not months:
        return 0.0
    positive = sum(1 for _, _, r in months if r > 0)
    return positive / len(months)


def drawdown_durations(equity_curve: Sequence[tuple[date, Decimal]]) -> list[int]:
    """每次回撤的持续交易日数(从 peak 到 recover 到 new peak)。

    持续期长的策略即使总回撤不大也可能在心理上难以承受。研究门应同时
    检查最大回撤幅度与最长持续期。
    """
    if len(equity_curve) < 2:
        return []
    peak = float(equity_curve[0][1])
    durations: list[int] = []
    cur_duration = 0
    in_drawdown = False
    for _, val in equity_curve[1:]:
        v = float(val)
        if v >= peak:
            if in_drawdown:
                durations.append(cur_duration)
                cur_duration = 0
                in_drawdown = False
            peak = v
        else:
            in_drawdown = True
            cur_duration += 1
    if in_drawdown:
        durations.append(cur_duration)
    return durations


def max_drawdown_duration(equity_curve: Sequence[tuple[date, Decimal]]) -> int:
    """最大回撤持续期(交易日数)。"""
    durations = drawdown_durations(equity_curve)
    return max(durations) if durations else 0


def value_at_risk(
    equity_curve: Sequence[tuple[date, Decimal]],
    confidence: float = 0.95,
) -> float:
    """历史 VaR(Value at Risk)。

    返回收益分布的 ``confidence`` 分位数(返回负数,如 -0.02 表示在 95% 置信度下
    单日最大损失约 2%)。历史法不依赖分布假设,但对尾部不敏感。
    """
    if len(equity_curve) < 3:
        return 0.0
    returns = sorted(daily_returns(equity_curve))
    if not returns:
        return 0.0
    # 经验分位数(线性插值)
    idx = (1 - confidence) * (len(returns) - 1)
    lower = int(idx)
    frac = idx - lower
    if lower + 1 < len(returns):
        return returns[lower] * (1 - frac) + returns[lower + 1] * frac
    return returns[lower]


def conditional_value_at_risk(
    equity_curve: Sequence[tuple[date, Decimal]],
    confidence: float = 0.95,
) -> float:
    """条件 VaR(CVaR / Expected Shortfall)。

    返回 ``confidence`` 分位数以下所有收益的平均值(返回负数)。比 VaR 更
    好地捕捉尾部风险。
    """
    if len(equity_curve) < 3:
        return 0.0
    returns = sorted(daily_returns(equity_curve))
    if not returns:
        return 0.0
    cutoff = int((1 - confidence) * len(returns))
    if cutoff < 1:
        cutoff = 1
    tail = returns[:cutoff]
    return sum(tail) / len(tail)


def beta(
    equity_curve: Sequence[tuple[date, Decimal]],
    benchmark_curve: Sequence[tuple[date, Decimal]],
) -> float:
    """策略相对基准的 Beta。

    基准曲线通过 ``date`` 对齐。Beta=1 表示策略与基准同向同幅度;Beta<1
    表示策略波动小于基准;Beta<0 表示反向。
    """
    strat_returns, bench_returns = _aligned_returns(equity_curve, benchmark_curve)
    if len(strat_returns) < 3:
        return 0.0
    var_b = _variance(bench_returns)
    if var_b == 0:
        return 0.0
    cov = _covariance(strat_returns, bench_returns)
    return cov / var_b


def alpha(
    equity_curve: Sequence[tuple[date, Decimal]],
    benchmark_curve: Sequence[tuple[date, Decimal]],
    risk_free_annual: float = 0.03,
) -> float:
    """Jensen's Alpha —— CAPM 超额收益(年化)。

    Beta 与 Alpha 一起衡量策略是否在承担同等系统性风险的前提下跑赢基准。
    """
    strat_returns, bench_returns = _aligned_returns(equity_curve, benchmark_curve)
    if len(strat_returns) < 3:
        return 0.0
    var_b = _variance(bench_returns)
    if var_b == 0:
        return 0.0
    cov = _covariance(strat_returns, bench_returns)
    b = cov / var_b
    rf_daily = risk_free_annual / _ANNUALIZATION
    mean_strat = sum(strat_returns) / len(strat_returns)
    mean_bench = sum(bench_returns) / len(bench_returns)
    excess_strat = mean_strat - rf_daily
    excess_bench = mean_bench - rf_daily
    daily_alpha = excess_strat - b * excess_bench
    return daily_alpha * _ANNUALIZATION


def information_ratio(
    equity_curve: Sequence[tuple[date, Decimal]],
    benchmark_curve: Sequence[tuple[date, Decimal]],
) -> float:
    """信息比率 = 平均超额收益 / 跟踪误差(年化)。

    衡量策略相对基准的稳定超额收益能力。``tracking_error`` 越小、alpha 越稳定,
    IR 越高。
    """
    strat_returns, bench_returns = _aligned_returns(equity_curve, benchmark_curve)
    if len(strat_returns) < 3:
        return 0.0
    excess = [s - b for s, b in zip(strat_returns, bench_returns, strict=True)]
    mean_excess = sum(excess) / len(excess)
    tracking_error = math.sqrt(_variance(excess))
    if tracking_error == 0:
        return 0.0
    return mean_excess / tracking_error * math.sqrt(_ANNUALIZATION)


def rolling_sharpe(
    equity_curve: Sequence[tuple[date, Decimal]],
    window: int = 63,
    risk_free_annual: float = 0.03,
) -> list[tuple[date, float]]:
    """滚动窗口 Sharpe —— 用于检测策略稳定性。

    返回 ``[(date, sharpe), ...]``。window 单位为交易日(默认 63 ≈ 季度)。
    若窗口内波动为 0 返回 0,持续多个 0 提示策略可能极少交易或异常。
    """
    if window < 3 or len(equity_curve) < window + 1:
        return []
    returns = daily_returns(equity_curve)
    rf_daily = risk_free_annual / _ANNUALIZATION
    out: list[tuple[date, float]] = []
    # returns[i] 对应 equity_curve[i+1] 的日期
    for i in range(window, len(returns) + 1):
        window_returns = returns[i - window : i]
        if not window_returns:
            continue
        mean_r = sum(window_returns) / len(window_returns)
        var_r = sum((r - mean_r) ** 2 for r in window_returns) / len(window_returns)
        std_r = math.sqrt(var_r)
        sharpe = (
            0.0
            if std_r == 0
            else (mean_r - rf_daily) / std_r * math.sqrt(_ANNUALIZATION)
        )
        out.append((equity_curve[i][0], sharpe))
    return out


def downside_deviation_annual(
    equity_curve: Sequence[tuple[date, Decimal]],
    target_daily_return: float = 0.0,
) -> float:
    """年化下行偏差(用于 Sortino/Robustness 报告)。"""
    if len(equity_curve) < 3:
        return 0.0
    returns = daily_returns(equity_curve)
    downside = [(r - target_daily_return) ** 2 for r in returns if r < target_daily_return]
    if not downside:
        return 0.0
    return math.sqrt(sum(downside) / len(downside)) * math.sqrt(_ANNUALIZATION)


# ---------------------------------------------------------------------------
# 内部统计工具
# ---------------------------------------------------------------------------


def _variance(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    return sum((x - mean) ** 2 for x in xs) / len(xs)


def _covariance(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / len(xs)


def _aligned_returns(
    equity_curve: Sequence[tuple[date, Decimal]],
    benchmark_curve: Sequence[tuple[date, Decimal]],
) -> tuple[list[float], list[float]]:
    """按 ``date`` 对齐两条权益曲线,返回对应的日收益率序列。"""
    if len(equity_curve) < 2 or len(benchmark_curve) < 2:
        return [], []
    strat_index = dict(equity_curve)
    bench_index = dict(benchmark_curve)
    common = sorted(set(strat_index) & set(bench_index))
    if len(common) < 2:
        return [], []
    strat_out: list[float] = []
    bench_out: list[float] = []
    for i in range(1, len(common)):
        sp = float(strat_index[common[i - 1]])
        sc = float(strat_index[common[i]])
        bp = float(bench_index[common[i - 1]])
        bc = float(bench_index[common[i]])
        if sp > 0 and bp > 0:
            strat_out.append((sc - sp) / sp)
            bench_out.append((bc - bp) / bp)
    return strat_out, bench_out
