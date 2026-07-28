"""ETF 绝对趋势 + 相对动量信号生成。

信号管线:
1. **绝对趋势过滤**:SMA(价格 > N 日均线)或 lookback_return(N 月收益为正)。
   未通过趋势的 ETF 不进入相对动量排名。
2. **相对动量评分**:在通过趋势的 ETF 中按 3/6/12 月组合得分排名。
3. **Top-N 选择**:取动量得分最高的 N 只 ETF。

如果**没有**风险资产通过绝对趋势 → 触发 flight-to-safety(避险到国债 ETF/现金)。
国债 ETF 自身也必须通过独立规则(``safe_haven_trend_enabled``)。

所有函数均为纯函数,相同输入永远产出相同输出,不依赖遍历顺序。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.etf_rotation.config import (
    TRADING_DAYS_PER_MONTH,
    AbsoluteTrendMethod,
    EtfRotationConfig,
)


@dataclass(frozen=True, slots=True)
class TrendResult:
    """单只 ETF 的绝对趋势检验结果。"""

    symbol: str
    passes: bool
    detail: float
    """SMA 方法的 close/SMA 比值;LOOKBACK_RETURN 方法的 N 月收益率。"""
    method: str


@dataclass(frozen=True, slots=True)
class MomentumResult:
    """单只 ETF 的相对动量评分结果。"""

    symbol: str
    score: float
    components: dict[str, float]
    """各回溯期的子分数 {f"{months}m": return}。"""


@dataclass(frozen=True, slots=True)
class EtfSignal:
    """ETF 轮动策略输出的标准化信号。"""

    symbol: str
    passes_trend: bool
    momentum_score: float | None
    selected: bool
    """是否进入最终持仓(Top-N 之列)。"""


def _sma(values: Sequence[float], window: int) -> float:
    """简单移动平均(取最后 window 个值)。"""
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def _return_over(values: Sequence[float], lookback_days: int) -> float:
    """计算 lookback_days 日前的价格到当前的收益率。"""
    if len(values) < lookback_days + 1:
        return float("nan")
    start = values[-(lookback_days + 1)]
    end = values[-1]
    if start <= 0:
        return float("nan")
    return (end / start) - 1.0


def compute_absolute_trend(
    closes: Mapping[str, Sequence[float]],
    config: EtfRotationConfig,
) -> dict[str, TrendResult]:
    """对候选池中每只 ETF 执行绝对趋势检验。

    :param closes: ``{symbol: [close_day1, close_day2, ...]}``,按时间排列。
    :param config: 策略配置。
    :return: ``{symbol: TrendResult}``,数据不足的 ETF ``passes=False``。
    """
    results: dict[str, TrendResult] = {}
    for symbol, prices in closes.items():
        if len(prices) < 2:
            results[symbol] = TrendResult(
                symbol=symbol, passes=False, detail=0.0,
                method=config.trend_method.value,
            )
            continue

        if config.trend_method is AbsoluteTrendMethod.SMA:
            sma = _sma(prices, config.sma_window)
            if sma != sma:  # NaN check
                results[symbol] = TrendResult(
                    symbol=symbol, passes=False, detail=0.0, method="sma",
                )
            else:
                current = prices[-1]
                ratio = current / sma if sma > 0 else 0.0
                results[symbol] = TrendResult(
                    symbol=symbol, passes=ratio > 1.0, detail=ratio, method="sma",
                )
        else:  # LOOKBACK_RETURN
            days = config.lookback_months * TRADING_DAYS_PER_MONTH
            ret = _return_over(prices, days)
            if ret != ret:  # NaN
                results[symbol] = TrendResult(
                    symbol=symbol, passes=False, detail=0.0,
                    method="lookback_return",
                )
            else:
                results[symbol] = TrendResult(
                    symbol=symbol, passes=ret > 0.0, detail=ret,
                    method="lookback_return",
                )
    return results


def compute_relative_momentum(
    closes: Mapping[str, Sequence[float]],
    trend_results: dict[str, TrendResult],
    config: EtfRotationConfig,
) -> dict[str, MomentumResult]:
    """在通过绝对趋势的 ETF 中计算相对动量评分。

    评分 = sum(weight_i * return_i),其中 return_i 是第 i 个回溯期的收益率。
    未通过趋势的 ETF 得到 ``score=NaN``,不参与排名。

    **同分处理**:同分的 ETF 按代码字母序排列,保证确定性。
    """
    results: dict[str, MomentumResult] = {}
    for symbol, prices in closes.items():
        trend = trend_results.get(symbol)
        if trend is not None and not trend.passes:
            results[symbol] = MomentumResult(
                symbol=symbol, score=float("nan"), components={},
            )
            continue

        components: dict[str, float] = {}
        weighted_sum = 0.0
        valid = True
        for months, weight in zip(
            config.momentum_lookbacks, config.momentum_weights, strict=True
        ):
            days = months * TRADING_DAYS_PER_MONTH
            ret = _return_over(prices, days)
            if ret != ret:  # NaN — 数据不足
                valid = False
                break
            components[f"{months}m"] = ret
            weighted_sum += weight * ret

        if not valid:
            results[symbol] = MomentumResult(
                symbol=symbol, score=float("nan"), components=components,
            )
        else:
            results[symbol] = MomentumResult(
                symbol=symbol, score=weighted_sum, components=components,
            )
    return results


def generate_signals(
    closes: Mapping[str, Sequence[float]],
    config: EtfRotationConfig,
) -> list[EtfSignal]:
    """完整的信号生成管线:趋势过滤 → 动量评分 → Top-N 选择。

    返回的信号列表按 ``(momentum_score desc, symbol asc)`` 排序,
    保证确定性 —— 相同输入永远产出相同顺序。

    如果没有风险 ETF 通过趋势,返回空列表(调用方应触发 flight-to-safety)。
    """
    trend = compute_absolute_trend(closes, config)
    momentum = compute_relative_momentum(closes, trend, config)

    passing: list[tuple[str, float]] = []
    for symbol, m in momentum.items():
        if m.score == m.score:  # not NaN
            passing.append((symbol, m.score))

    passing.sort(key=lambda x: (-x[1], x[0]))

    top_n = config.top_n
    selected_codes = {sym for sym, _ in passing[:top_n]}

    signals: list[EtfSignal] = []
    for symbol in sorted(closes.keys()):
        t = trend.get(symbol)
        m_res: MomentumResult | None = momentum.get(symbol)
        passes = t.passes if t is not None else False
        score = m_res.score if m_res is not None and m_res.score == m_res.score else None
        signals.append(EtfSignal(
            symbol=symbol,
            passes_trend=passes,
            momentum_score=score,
            selected=symbol in selected_codes,
        ))
    return signals
