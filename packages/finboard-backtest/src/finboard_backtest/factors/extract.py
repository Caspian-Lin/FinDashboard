"""因子提取:从 FactorInputBatch / 原始数据构建因子矩阵。

将 DailySecurityMetrics + FinancialIndicator + 价格历史转换为统一的
``dict[str, dict[str, float]]`` (symbol → factor_name → value),供评分管线使用。
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from finboard_data.factors import FactorInputBatch, FactorInputRecord


def extract_factor_matrix(
    batch: FactorInputBatch,
    *,
    price_history: dict[str, list[float]] | None = None,
    momentum_lookback: int = 20,
    volatility_windows: tuple[int, ...] = (20, 60, 120),
) -> dict[str, dict[str, float]]:
    """从 FactorInputBatch 提取因子矩阵。

    Args:
        batch: 横截面研究数据
        price_history: {symbol: [close_prices]} 用于计算动量和波动率
        momentum_lookback: 动量回看窗口(交易日)
        volatility_windows: 波动率计算窗口列表

    Returns:
        {factor_name: {symbol: float_value}}
    """
    matrix: dict[str, dict[str, float]] = {}

    for record in batch.records:
        sym = record.symbol
        _add_daily_factors(matrix, sym, record)
        _add_financial_factors(matrix, sym, record)

    if price_history:
        _add_price_factors(
            matrix, price_history, momentum_lookback, volatility_windows
        )

    return matrix


def _add_daily_factors(
    matrix: dict[str, dict[str, float]],
    sym: str,
    record: FactorInputRecord,
) -> None:
    if record.daily is None:
        return
    d = record.daily
    _set(matrix, "pb", sym, d.pb)
    _set(matrix, "turnover_rate", sym, d.turnover_rate)
    _set(matrix, "market_cap", sym, d.total_market_cap)

    pe_ttm = d.pe_ttm
    if pe_ttm is not None and pe_ttm > 0:
        _set(matrix, "earnings_yield", sym, Decimal(1) / pe_ttm)
    elif pe_ttm is not None and pe_ttm < 0:
        _set(matrix, "earnings_yield", sym, Decimal(0))

    _set(matrix, "dividend_yield", sym, d.dividend_yield_ttm)


def _add_financial_factors(
    matrix: dict[str, dict[str, float]],
    sym: str,
    record: FactorInputRecord,
) -> None:
    if record.financial is None:
        return
    f = record.financial
    _set(matrix, "roe", sym, f.return_on_equity)
    _set(matrix, "gross_profit_margin", sym, f.gross_profit_margin)
    _set(matrix, "debt_to_assets", sym, f.debt_to_assets)
    _set(matrix, "revenue_yoy", sym, f.revenue_yoy)


def _add_price_factors(
    matrix: dict[str, dict[str, float]],
    price_history: dict[str, list[float]],
    momentum_lookback: int,
    volatility_windows: tuple[int, ...],
) -> None:
    for sym, prices in price_history.items():
        if len(prices) < max(momentum_lookback, max(volatility_windows)) + 1:
            continue
        closes = np.array(prices, dtype=np.float64)
        returns = np.diff(closes) / closes[:-1]
        returns = returns[np.isfinite(returns)]

        if len(closes) > momentum_lookback:
            momentum = float(closes[-1] / closes[-momentum_lookback - 1] - 1)
            _set_raw(matrix, "momentum", sym, momentum)

        for window in volatility_windows:
            if len(returns) >= window:
                vol = float(np.std(returns[-window:], ddof=1))
                _set_raw(matrix, f"volatility_{window}d", sym, vol)

        downside_window = min(60, len(returns))
        if downside_window >= 10:
            recent = returns[-downside_window:]
            downside = recent[recent < 0]
            if len(downside) >= 3:
                dvol = float(np.std(downside, ddof=1))
                _set_raw(matrix, "downside_volatility", sym, dvol)


def _set(
    matrix: dict[str, dict[str, float]],
    factor: str,
    sym: str,
    value: Decimal | None,
) -> None:
    if value is None:
        return
    v = float(value)
    if np.isfinite(v):
        matrix.setdefault(factor, {})[sym] = v


def _set_raw(
    matrix: dict[str, dict[str, float]],
    factor: str,
    sym: str,
    value: float,
) -> None:
    if np.isfinite(value):
        matrix.setdefault(factor, {})[sym] = value
