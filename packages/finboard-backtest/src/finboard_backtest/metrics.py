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
