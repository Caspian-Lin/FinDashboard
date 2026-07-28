"""离散手数求解 — 目标权重 → 可成交手数。

issue #59 的核心可行性要求:10万 / 20万 / 50万元三档资金,在 A 股手数
(100股/手)、ETF(100份/10份)、可转债(10张)、期货乘数 / 保证金、
最小佣金和剩余现金共同约束下求解出无负现金、无碎片的实际持仓。

算法:
1. 每个标的的 ``target_value = weight * capital``。
2. ``target_lots = floor(target_value / unit_value)``,其中
   ``unit_value = price * lot_size``(期货为 ``price * multiplier``)。
3. 逐标的买入后计算剩余现金;若负现金,从偏离最大的标的减 1 手,
   直到现金 >= 0。
4. 输出 ``RebalancePlan`` —— 每笔 trade 的 delta_shares、target_shares。
"""

from __future__ import annotations

from dataclasses import dataclass

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
        for code in self.target.weights:
            if code not in self.prices:
                raise SizingError(f"缺少 {code} 的价格")
            if self.prices[code] <= 0:
                raise SizingError(f"{code} 价格必须为正: {self.prices[code]}")
            if code not in self.lot_info:
                raise SizingError(f"缺少 {code} 的手数信息")


def _unit_value(code: str, price: float, info: AssetLotInfo) -> float:
    """单手(或 1 手期货)的合约价值。"""
    return price * info.lot_size * info.multiplier


def _solve_discrete_lots(
    target_values: dict[str, float],
    lot_info: dict[str, AssetLotInfo],
    prices: dict[str, float],
    available_cash: float,
    *,
    commission_rate: float,
    commission_min: float,
    stamp_tax_rate: float,
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

        if unit_val <= 0:
            lots[code] = 0
            continue

        raw_units = int(target_val // unit_val)
        n_units = max(0, raw_units)

        est_commission = max(n_units * unit_val * commission_rate, commission_min)
        est_cost = n_units * unit_val + est_commission

        lots[code] = n_units
        remaining_cash -= est_cost

        items.append((code, unit_val, target_val, n_units))

    items.sort(key=lambda x: x[2] - x[3] * x[1], reverse=True)

    while remaining_cash < 0 and items:
        reduced = False
        for i, (code, unit_val, _, n_units) in enumerate(items):
            if n_units <= 0:
                continue

            old_cost = n_units * unit_val + max(
                n_units * unit_val * commission_rate, commission_min
            )
            new_units = n_units - 1
            new_cost = new_units * unit_val + max(
                new_units * unit_val * commission_rate, commission_min
            ) if new_units > 0 else 0.0

            remaining_cash += old_cost - new_cost
            lots[code] = new_units
            items[i] = (code, unit_val, items[i][2], new_units)
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

    cash_budget = capital - sum(p.market_value for p in current.values())

    lots = _solve_discrete_lots(
        target_values,
        lot_info,
        prices,
        cash_budget,
        commission_rate=sizing_input.commission_rate,
        commission_min=sizing_input.commission_min,
        stamp_tax_rate=sizing_input.stamp_tax_rate,
    )

    trades: list[RebalanceTrade] = []
    total_commission = 0.0
    total_tax = 0.0
    total_turnover = 0.0
    total_position_value = 0.0

    all_codes = set(target.weights.keys()) | set(current.keys())

    for code in sorted(all_codes):
        info = lot_info.get(code)
        if info is None:
            info = AssetLotInfo(code=code, lot_size=1, multiplier=1)

        price = prices.get(code, 0.0)
        target_shares = lots.get(code, 0) * info.lot_size
        current_shares = current.get(code, PositionSnapshot(code, 0, 0.0)).shares
        delta_shares = target_shares - current_shares

        target_value = target_shares * price * info.multiplier
        current_value = current_shares * price * info.multiplier
        delta_value = delta_shares * price * info.multiplier

        trades.append(RebalanceTrade(
            symbol=code,
            delta_shares=delta_shares,
            target_shares=target_shares,
            current_shares=current_shares,
            target_value=target_value,
            current_value=current_value,
            delta_value=delta_value,
        ))

        trade_value = abs(delta_value)
        total_turnover += trade_value
        total_position_value += target_value

        if trade_value > 0:
            total_commission += max(
                trade_value * sizing_input.commission_rate,
                sizing_input.commission_min,
            )
            if delta_shares < 0:
                total_tax += trade_value * sizing_input.stamp_tax_rate

    cash_before = capital - sum(p.market_value for p in current.values())
    cash_after = cash_before - total_position_value - total_commission - total_tax

    return RebalancePlan(
        trades=trades,
        total_capital=capital,
        cash_before=cash_before,
        cash_after=cash_after,
        est_commission=total_commission,
        est_tax=total_tax,
        total_turnover=total_turnover,
        as_of=target.as_of,
        strategy_id=target.strategy_id,
    )


def get_capital_tier(name: str) -> CapitalTier:
    """按名称获取资金档位。"""
    tier = CAPITAL_TIERS.get(name)
    if tier is None:
        raise SizingError(
            f"未知资金档位: {name};可选: {', '.join(CAPITAL_TIERS)}"
        )
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
