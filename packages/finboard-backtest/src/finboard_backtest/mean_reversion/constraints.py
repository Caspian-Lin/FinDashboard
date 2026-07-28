"""均值回归策略的硬性风控约束。

所有约束都是不可绕过的硬上限:

* **持仓数**:同时持有的标的数 <= ``max_positions``。
* **单标的权重**:单个标的权重 <= ``max_weight_per_position``。
* **最长持有期**:持仓超过 ``max_holding_days`` → 强制平仓。
* **冷却期**:平仓后 ``cooldown_days`` 内不允许重新入场。
* **日换手率**:单日换手率(买入+卖出 / 组合净值) <= ``max_daily_turnover``。
* **参与率**:单标的成交量 / 当日成交量 <= ``max_participation``。
* **禁止摊平**:`no_averaging_down=True` 时不允许对浮亏头寸加仓。
"""

from __future__ import annotations

from dataclasses import dataclass

from finboard_backtest.mean_reversion.config import MeanReversionConfig


@dataclass(slots=True)
class _OpenPosition:
    """运行时持仓状态(可变)。"""

    symbol: str
    entry_bar: int
    entry_price: float
    shares: float
    weight: float
    bars_held: int = 0


@dataclass(slots=True)
class _CooldownState:
    """冷却期跟踪。"""

    symbol: str
    remaining: int


@dataclass(slots=True)
class ConstraintCheckResult:
    """约束检查结果。"""

    allowed: bool
    reason: str
    """如果不允许,说明原因。"""

    @property
    def blocked(self) -> bool:
        return not self.allowed


def check_entry(
    symbol: str,
    current_positions: list[_OpenPosition],
    cooldowns: list[_CooldownState],
    config: MeanReversionConfig,
    *,
    pending_buy_value: float = 0.0,
    portfolio_value: float = 0.0,
    daily_turnover_so_far: float = 0.0,
) -> ConstraintCheckResult:
    """检查是否允许对 ``symbol`` 发起新入场。

    依次检查:冷却期 → 持仓数上限 → 已有持仓(禁止重复/摊平)→ 日换手率。

    :param pending_buy_value: 本 Bar 已经挂出的买单金额(用于换手率累计)。
    :param portfolio_value: 当前组合净值。
    :param daily_turnover_so_far: 本日已发生的换手率。
    """
    for cd in cooldowns:
        if cd.symbol == symbol and cd.remaining > 0:
            return ConstraintCheckResult(
                allowed=False,
                reason=f"{symbol} 处于冷却期(剩余 {cd.remaining} 天)",
            )

    if len(current_positions) >= config.max_positions:
        return ConstraintCheckResult(
            allowed=False,
            reason=f"已达最大持仓数 {config.max_positions}",
        )

    for pos in current_positions:
        if pos.symbol == symbol:
            if config.no_averaging_down:
                return ConstraintCheckResult(
                    allowed=False,
                    reason=f"{symbol} 已有持仓,禁止摊平加仓",
                )
            return ConstraintCheckResult(
                allowed=False,
                reason=f"{symbol} 已有持仓,不重复入场",
            )

    if portfolio_value > 0:
        order_value = pending_buy_value
        new_turnover = daily_turnover_so_far + (
            order_value / portfolio_value if portfolio_value > 0 else 0.0
        )
        if new_turnover > config.max_daily_turnover:
            return ConstraintCheckResult(
                allowed=False,
                reason=f"换手率 {new_turnover:.2%} 超过上限 {config.max_daily_turnover:.2%}",
            )

    return ConstraintCheckResult(allowed=True, reason="")


def check_participation(
    order_shares: float,
    price: float,
    daily_volume: float,
    config: MeanReversionConfig,
) -> ConstraintCheckResult:
    """检查成交量参与率约束。

    :param order_shares: 拟下单数量。
    :param price: 拟成交价。
    :param daily_volume: 当日成交量(股)。
    """
    if daily_volume <= 0:
        return ConstraintCheckResult(
            allowed=False,
            reason="当日成交量为零,无法参与",
        )
    participation = order_shares / daily_volume
    if participation > config.max_participation:
        return ConstraintCheckResult(
            allowed=False,
            reason=(
                f"参与率 {participation:.2%} 超过上限 {config.max_participation:.2%}"
            ),
        )
    return ConstraintCheckResult(allowed=True, reason="")


def check_max_holding(
    position: _OpenPosition,
    current_bar: int,
    config: MeanReversionConfig,
) -> bool:
    """检查持仓是否超过最长持有期。

    :return: True 表示应强制平仓。
    """
    return position.bars_held >= config.max_holding_days
