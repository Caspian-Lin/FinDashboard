"""离散手数求解 — 目标权重 → 可成交手数。

issue #59 的核心可行性要求:10万 / 20万 / 50万元三档资金,在 A 股手数
(100股/手)、ETF(100份/10份)、可转债(10张)、期货乘数 / 保证金、
最小佣金和剩余现金共同约束下求解出无负现金、无碎片的实际持仓。

算法:
1. 每个标的的 ``target_value = weight * capital``。
2. ``target_lots = floor(target_value / unit_value)``,其中
   ``unit_value = price * lot_size``(期货为 ``price * multiplier``)。
3. 股票按本金、期货按保证金计算资金占用;若负现金,从偏离最大的标的减 1 手,
   直到现金 >= 0。
4. 再应用费用、滑点、参与率、不可交易和最小交易权重,输出 requested/actual/
   unfilled 的 ``RebalancePlan``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    AssetLotInfo,
    CapitalTier,
    PortfolioConstraints,
    RebalancePlan,
    RebalanceTrade,
    TargetWeight,
)

DEFAULT_COMMISSION_RATE = 0.0003
DEFAULT_COMMISSION_MIN = 5.0
DEFAULT_STAMP_TAX_RATE = 0.0005


class SizingError(RuntimeError):
    """离散手数求解失败。"""


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """当前持仓快照(只读)。"""

    code: str
    shares: int
    market_value: float

    def __post_init__(self) -> None:
        if self.shares < 0:
            raise ValueError("shares 不能为负")
        if self.market_value < 0:
            raise ValueError("market_value 不能为负")


@dataclass(frozen=True, slots=True)
class SizingInput:
    """离散手数求解的输入参数。

    属性:
        target: 目标权重。
        capital: 总资金(元)。
        lot_info: 各标的的手数 / 乘数信息。
        prices: 各标的的最新价格。
        current_positions: 当前持仓快照(可为空)。
        commission_rate: 佣金费率(万 N)。
        commission_min: 单笔最小佣金(元)。
        stamp_tax_rate: 印花税(卖出,万 N)。
    """

    target: TargetWeight
    capital: float
    lot_info: dict[str, AssetLotInfo]
    prices: dict[str, float]
    current_positions: dict[str, PositionSnapshot] | None = None
    commission_rate: float = DEFAULT_COMMISSION_RATE
    commission_min: float = DEFAULT_COMMISSION_MIN
    stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise SizingError("capital 必须为正")
        if any(weight < 0 for weight in self.target.weights.values()):
            raise SizingError("通用离散求解仅支持 long-only 目标;空头须使用期货策略适配器")
        required_codes = set(self.target.weights) | set(self.current_positions or {})
        for code in required_codes:
            if code not in self.prices:
                raise SizingError(f"缺少 {code} 的价格")
            if self.prices[code] <= 0:
                raise SizingError(f"{code} 价格必须为正: {self.prices[code]}")
            if code not in self.lot_info:
                raise SizingError(f"缺少 {code} 的手数信息")


def _unit_value(code: str, price: float, info: AssetLotInfo) -> float:
    """单手(或 1 手期货)的合约价值。"""
    return price * info.lot_size * info.multiplier


def _unit_cash_requirement(code: str, price: float, info: AssetLotInfo) -> float:
    """单手本金或保证金占用。"""
    notional = _unit_value(code, price, info)
    return notional * (info.margin_rate if info.margin_rate is not None else 1.0)


def _fee_rate(info: AssetLotInfo, fallback: float) -> float:
    return info.commission_rate if info.commission_rate is not None else fallback


def _fee_minimum(info: AssetLotInfo, fallback: float) -> float:
    return info.commission_min if info.commission_min is not None else fallback


def _solve_discrete_lots(
    target_values: dict[str, float],
    lot_info: dict[str, AssetLotInfo],
    prices: dict[str, float],
    available_cash: float,
    *,
    commission_rate: float,
    commission_min: float,
) -> dict[str, int]:
    """贪心离散手数求解:先 floor,再从偏离最大的减手。

    返回 ``{code: target_shares}``,保证 ``cash >= 0``。
    """
    lots: dict[str, int] = {}
    remaining_cash = available_cash

    items: list[tuple[str, float, float, int]] = []
    for code, target_val in target_values.items():
        info = lot_info[code]
        price = prices[code]
        unit_val = _unit_value(code, price, info)
        cash_per_unit = _unit_cash_requirement(code, price, info)

        if unit_val <= 0 or cash_per_unit <= 0:
            lots[code] = 0
            continue

        raw_units = int(target_val // unit_val)
        n_units = max(0, raw_units)

        rate = _fee_rate(info, commission_rate)
        minimum = _fee_minimum(info, commission_min)
        est_commission = max(n_units * unit_val * rate, minimum) if n_units > 0 else 0.0
        est_slippage = n_units * unit_val * info.slippage_bps / 10_000
        est_cost = n_units * cash_per_unit + est_commission + est_slippage

        lots[code] = n_units
        remaining_cash -= est_cost

        items.append((code, cash_per_unit, target_val, n_units))

    items.sort(key=lambda x: x[2] - x[3] * x[1], reverse=True)

    while remaining_cash < 0 and items:
        reduced = False
        for i, (code, cash_per_unit, _, n_units) in enumerate(items):
            if n_units <= 0:
                continue

            info = lot_info[code]
            notional_per_unit = _unit_value(code, prices[code], info)
            rate = _fee_rate(info, commission_rate)
            minimum = _fee_minimum(info, commission_min)
            old_cost = (
                n_units * cash_per_unit
                + max(n_units * notional_per_unit * rate, minimum)
                + n_units * notional_per_unit * info.slippage_bps / 10_000
            )
            new_units = n_units - 1
            new_cost = (
                new_units * cash_per_unit
                + max(new_units * notional_per_unit * rate, minimum)
                + new_units * notional_per_unit * info.slippage_bps / 10_000
                if new_units > 0
                else 0.0
            )

            remaining_cash += old_cost - new_cost
            lots[code] = new_units
            items[i] = (code, cash_per_unit, items[i][2], new_units)
            reduced = True

            if remaining_cash >= 0:
                break

        if not reduced:
            break

    return lots


def solve_sizing(
    sizing_input: SizingInput,
    *,
    constraints: PortfolioConstraints | None = None,
) -> RebalancePlan:
    """目标权重 → 离散手数 → 再平衡计划。

    步骤:
    1. ``target_value = weight * capital``。
    2. 按手数 floor 求解初始手数。
    3. 负现金时贪心减手。
    4. 与当前持仓比较,生成买卖 trades。
    5. 估算佣金 / 印花税 / 换手。
    """
    target = sizing_input.target
    capital = sizing_input.capital
    lot_info = sizing_input.lot_info
    prices = sizing_input.prices
    current = sizing_input.current_positions or {}

    target_values: dict[str, float] = {}
    for code, weight in target.weights.items():
        if weight > 0:
            target_values[code] = weight * capital

    lots = _solve_discrete_lots(
        target_values,
        lot_info,
        prices,
        capital,
        commission_rate=sizing_input.commission_rate,
        commission_min=sizing_input.commission_min,
    )

    trades: list[RebalanceTrade] = []
    total_commission = 0.0
    total_tax = 0.0
    total_slippage = 0.0
    total_turnover = 0.0
    total_cash_requirement = 0.0
    total_margin_required = 0.0

    all_codes = set(target.weights.keys()) | set(current.keys())

    for code in sorted(all_codes):
        info = lot_info.get(code)
        if info is None:
            info = AssetLotInfo(code=code, lot_size=1, multiplier=1)

        price = prices.get(code, 0.0)
        requested_target_shares = lots.get(code, 0) * info.lot_size
        current_shares = current.get(code, PositionSnapshot(code, 0, 0.0)).shares
        target_shares = requested_target_shares
        reject_reason: str | None = None
        if not info.tradable:
            target_shares = current_shares
            reject_reason = info.unavailable_reason or "标的不可交易"
        elif info.max_participation is not None and info.available_volume is not None:
            max_delta = (
                int(info.available_volume * info.max_participation // info.lot_size) * info.lot_size
            )
            requested_delta = requested_target_shares - current_shares
            if abs(requested_delta) > max_delta:
                target_shares = current_shares + (max_delta if requested_delta > 0 else -max_delta)
                target_shares = max(0, target_shares)
                reject_reason = "成交量参与率限制导致部分可成交"
        delta_shares = target_shares - current_shares

        target_value = target_shares * price * info.multiplier
        current_value = current_shares * price * info.multiplier
        delta_value = delta_shares * price * info.multiplier

        if constraints is not None and abs(delta_value) / capital < constraints.min_weight_to_trade:
            target_shares = current_shares
            delta_shares = 0
            target_value = current_value
            delta_value = 0.0
            reject_reason = "低于最小交易权重"

        margin_required = target_value * info.margin_rate if info.margin_rate is not None else 0.0
        unfilled_shares = abs(requested_target_shares - target_shares)
        rate = _fee_rate(info, sizing_input.commission_rate)
        minimum = _fee_minimum(info, sizing_input.commission_min)
        slippage = abs(delta_value) * info.slippage_bps / 10_000

        trades.append(
            RebalanceTrade(
                symbol=code,
                delta_shares=delta_shares,
                target_shares=target_shares,
                current_shares=current_shares,
                target_value=target_value,
                current_value=current_value,
                delta_value=delta_value,
                requested_target_shares=requested_target_shares,
                unfilled_shares=unfilled_shares,
                reject_reason=reject_reason,
                estimated_slippage=slippage,
                margin_required=margin_required,
            )
        )

        trade_value = abs(delta_value)
        total_turnover += trade_value
        required = margin_required if info.margin_rate is not None else target_value
        total_cash_requirement += required
        total_margin_required += margin_required
        total_slippage += slippage

        if trade_value > 0:
            total_commission += max(
                trade_value * rate,
                minimum,
            )
            if delta_shares < 0:
                tax_rate = (
                    info.stamp_tax_rate
                    if info.stamp_tax_rate is not None
                    else sizing_input.stamp_tax_rate
                )
                total_tax += trade_value * tax_rate

    current_cash_requirement = 0.0
    for code, position in current.items():
        info = lot_info[code]
        notional = position.shares * prices[code] * info.multiplier
        current_cash_requirement += (
            notional * info.margin_rate if info.margin_rate is not None else notional
        )
    cash_before = capital - current_cash_requirement
    cash_after = capital - total_cash_requirement - total_commission - total_tax - total_slippage
    if cash_after < -1e-6:
        raise SizingError(f"费用/保证金后现金不足: cash_after={cash_after:.2f}")

    return RebalancePlan(
        trades=trades,
        total_capital=capital,
        cash_before=cash_before,
        cash_after=cash_after,
        est_commission=total_commission,
        est_tax=total_tax,
        est_slippage=total_slippage,
        margin_required=total_margin_required,
        total_turnover=total_turnover,
        as_of=target.as_of,
        strategy_id=target.strategy_id,
        created_at=datetime.combine(target.as_of, time.min, tzinfo=UTC),
    )


def get_capital_tier(name: str) -> CapitalTier:
    """按名称获取资金档位。"""
    tier = CAPITAL_TIERS.get(name)
    if tier is None:
        raise SizingError(f"未知资金档位: {name};可选: {', '.join(CAPITAL_TIERS)}")
    return tier


__all__ = [
    "DEFAULT_COMMISSION_MIN",
    "DEFAULT_COMMISSION_RATE",
    "DEFAULT_STAMP_TAX_RATE",
    "PositionSnapshot",
    "SizingError",
    "SizingInput",
    "get_capital_tier",
    "solve_sizing",
]
