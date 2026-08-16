"""ETF 组合分配 —— 等权 / 逆波动率 + 避险切换。

分配流程:
1. 从信号中取出 selected=True 的 ETF。
2. 按配置方法(等权 / 逆波动率)计算初始权重。
3. 强制单 ETF 上限和资产大类上限。
4. 如果没有风险 ETF 通过趋势 → 分配到避险国债 ETF(必须独立通过趋势)
   或现金(flight-to-safety)。

**国债 ETF 是风险资产而非保本现金等价物。**
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from finboard_backtest.etf_rotation.config import (
    AllocationMethod,
    EtfRotationConfig,
)
from finboard_backtest.etf_rotation.signals import (
    EtfSignal,
    compute_absolute_trend,
)
from finboard_backtest.etf_rotation.universe import (
    EtfUniverse,
)


@dataclass(frozen=True, slots=True)
class EtfAllocation:
    """ETF 轮动策略的目标权重。"""

    weights: dict[str, float]
    """symbol → 目标权重(不含现金)。"""
    cash_weight: float
    safe_haven_weight: float
    """避险资产(国债 ETF)的权重;0 表示未启用避险。"""
    flight_to_safety: bool
    """本轮是否触发了避险切换。"""
    asset_class_exposure: dict[str, float] = field(default_factory=dict)
    """各资产大类的总权重 {asset_class: weight}。"""
    n_risk_holdings: int = 0
    """持有的风险资产(非避险)ETF 数量。"""

    @property
    def gross_weight(self) -> float:
        return sum(self.weights.values())

    @property
    def n_holdings(self) -> int:
        return len(self.weights)


def _std_dev(values: Sequence[float]) -> float:
    """计算标准差(总体)。"""
    n = len(values)
    if n < 2:
        return float("nan")
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


def _daily_returns(closes: Sequence[float]) -> list[float]:
    """从收盘价序列计算日收益率。"""
    if len(closes) < 2:
        return []
    returns: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            returns.append(closes[i] / prev - 1.0)
    return returns


def _volatility(closes: Sequence[float], window: int) -> float:
    """最近 window 期的日波动率(标准差)。"""
    returns = _daily_returns(closes[-(window + 1):])
    return _std_dev(returns)


def _enforce_asset_class_caps(
    weights: dict[str, float],
    universe: EtfUniverse,
    config: EtfRotationConfig,
) -> dict[str, float]:
    """迭代强制资产大类上限。

    超限的大类按比例缩减,剩余空间分配给未超限的大类(按比例放大)。
    """
    if not weights:
        return weights

    result = dict(weights)
    for _ in range(20):
        class_totals: dict[str, float] = {}
        for sym, w in result.items():
            member = universe.get(sym)
            if member is not None:
                ac = member.asset_class.value
                class_totals[ac] = class_totals.get(ac, 0.0) + w

        any_violated = False
        for ac, total in class_totals.items():
            if total > config.max_weight_per_asset_class + 1e-9:
                any_violated = True
                scale_factor = config.max_weight_per_asset_class / total
                for sym in result:
                    member = universe.get(sym)
                    if member is not None and member.asset_class.value == ac:
                        result[sym] *= scale_factor

        if not any_violated:
            break

    return result


def _apply_etf_cap(
    weights: dict[str, float],
    cap: float,
) -> dict[str, float]:
    """强制单 ETF 权重上限,超出部分按比例分配给未达上限的。"""
    if not weights:
        return weights

    result = dict(weights)
    for _ in range(20):
        over: dict[str, float] = {}
        room: dict[str, float] = {}
        for sym, w in result.items():
            if w > cap + 1e-9:
                over[sym] = w - cap
                result[sym] = cap
            elif w < cap - 1e-9:
                room[sym] = cap - w

        total_over = sum(over.values())
        total_room = sum(room.values())
        if total_over <= 1e-9 or total_room <= 1e-9:
            break

        for sym, r in room.items():
            share = total_over * (r / total_room)
            result[sym] += share
            if result[sym] > cap + 1e-9:
                excess = result[sym] - cap
                result[sym] = cap
                total_over += excess

    return result


def allocate_equal_weight(
    selected_codes: Sequence[str],
    universe: EtfUniverse,
    config: EtfRotationConfig,
) -> dict[str, float]:
    """等权分配,然后强制上限。

    可投资额度 = 1 - cash_buffer。
    """
    if not selected_codes:
        return {}
    max_inv = 1.0 - config.cash_buffer
    raw_w = max_inv / len(selected_codes)
    weights = dict.fromkeys(selected_codes, raw_w)
    weights = _apply_etf_cap(weights, config.max_weight_per_etf)
    weights = _enforce_asset_class_caps(weights, universe, config)
    return weights


def allocate_inverse_volatility(
    selected_codes: Sequence[str],
    closes: Mapping[str, Sequence[float]],
    universe: EtfUniverse,
    config: EtfRotationConfig,
) -> dict[str, float]:
    """逆波动率分配:vol 越低权重越高。"""
    if not selected_codes:
        return {}
    max_inv = 1.0 - config.cash_buffer

    inv_vols: dict[str, float] = {}
    for code in selected_codes:
        prices = closes.get(code)
        if prices is None or len(prices) < config.vol_lookback + 1:
            inv_vols[code] = 0.0
        else:
            vol = _volatility(prices, config.vol_lookback)
            if vol != vol or vol <= 0:  # NaN or zero
                inv_vols[code] = 0.0
            else:
                inv_vols[code] = 1.0 / vol

    total_iv = sum(inv_vols.values())
    if total_iv <= 0:
        raw_w = max_inv / len(selected_codes)
        weights = dict.fromkeys(selected_codes, raw_w)
    else:
        weights = {code: max_inv * (iv / total_iv) for code, iv in inv_vols.items()}

    weights = _apply_etf_cap(weights, config.max_weight_per_etf)
    weights = _enforce_asset_class_caps(weights, universe, config)
    return weights


def allocate(
    signals: list[EtfSignal],
    closes: Mapping[str, Sequence[float]],
    universe: EtfUniverse,
    config: EtfRotationConfig,
) -> EtfAllocation:
    """完整分配管线:信号 → 权重 → 避险切换。

    如果没有风险 ETF 被选中(selected=True 为空):
    * 检查避险国债 ETF 是否通过独立趋势检验。
    * 通过 → 全仓避险 ETF(受 max_weight_per_etf 和 cash_buffer 约束)。
    * 未通过 → 全仓现金(flight_to_safety=True)。

    如果有风险 ETF 被选中但避险 ETF 也通过了趋势,避险 ETF 仍可能被
    纳入风险资产组合(它出现在 closes 中且被 generate_signals 选中)。
    """
    selected_codes = [s.symbol for s in signals if s.selected]

    if not selected_codes:
        return _flight_to_safety(closes, universe, config)

    if config.allocation_method is AllocationMethod.INVERSE_VOLATILITY:
        weights = allocate_inverse_volatility(selected_codes, closes, universe, config)
    else:
        weights = allocate_equal_weight(selected_codes, universe, config)

    class_exposure = _compute_asset_class_exposure(weights, universe)

    return EtfAllocation(
        weights=weights,
        cash_weight=config.cash_buffer,
        safe_haven_weight=0.0,
        flight_to_safety=False,
        asset_class_exposure=class_exposure,
        n_risk_holdings=len(weights),
    )


def _flight_to_safety(
    closes: Mapping[str, Sequence[float]],
    universe: EtfUniverse,
    config: EtfRotationConfig,
) -> EtfAllocation:
    """无风险 ETF 通过趋势时,分配到避险国债 ETF 或现金。"""
    safe_code = config.safe_haven_symbol
    member = universe.get(safe_code)

    if member is None:
        return EtfAllocation(
            weights={},
            cash_weight=1.0,
            safe_haven_weight=0.0,
            flight_to_safety=True,
            asset_class_exposure={"cash": 1.0},
            n_risk_holdings=0,
        )

    if config.safe_haven_trend_enabled:
        if safe_code not in closes or len(closes[safe_code]) < 2:
            return EtfAllocation(
                weights={},
                cash_weight=1.0,
                safe_haven_weight=0.0,
                flight_to_safety=True,
                asset_class_exposure={"cash": 1.0},
                n_risk_holdings=0,
            )

        trend = compute_absolute_trend({safe_code: closes[safe_code]}, config)
        safe_result = trend.get(safe_code)
        if safe_result is None or not safe_result.passes:
            return EtfAllocation(
                weights={},
                cash_weight=1.0,
                safe_haven_weight=0.0,
                flight_to_safety=True,
                asset_class_exposure={"cash": 1.0},
                n_risk_holdings=0,
            )

    max_inv = 1.0 - config.cash_buffer
    safe_weight = min(max_inv, config.max_weight_per_etf)
    remaining_cash = 1.0 - safe_weight

    return EtfAllocation(
        weights={safe_code: safe_weight},
        cash_weight=remaining_cash,
        safe_haven_weight=safe_weight,
        flight_to_safety=True,
        asset_class_exposure={member.asset_class.value: safe_weight, "cash": remaining_cash},
        n_risk_holdings=0,
    )


def _compute_asset_class_exposure(
    weights: dict[str, float],
    universe: EtfUniverse,
) -> dict[str, float]:
    """计算各资产大类的总权重。"""
    exposure: dict[str, float] = {}
    for sym, w in weights.items():
        member = universe.get(sym)
        ac = member.asset_class.value if member is not None else "unknown"
        exposure[ac] = exposure.get(ac, 0.0) + w
    return exposure
