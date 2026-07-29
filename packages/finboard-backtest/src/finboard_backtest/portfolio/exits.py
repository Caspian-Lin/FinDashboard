"""版本化退出策略执行器(issue #81)。

执行器只把风控规则转换成目标权重调整和审计原因。持仓输入必须来自研究撮合后的
实际成交快照;本模块从不修改持仓,也不创建 Broker 请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from finboard_backtest.portfolio.contracts import MAX_WEIGHT_EPSILON, TargetWeight
from finboard_backtest.strategy_spec.contracts import (
    RiskExitPolicy,
    RiskExitRule,
    RiskExitType,
)

RISK_EXIT_EXECUTOR_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class ExitPositionSnapshot:
    """由实际成交推导的只读持仓与风险测量。"""

    symbol: str
    filled_quantity: float
    average_price: float
    current_price: float
    opened_on: date
    high_water_price: float | None = None
    realized_volatility: float | None = None
    atr: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol 不能为空")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity 不能为负")
        if self.average_price <= 0 or self.current_price <= 0:
            raise ValueError("持仓价格必须为正")
        if self.high_water_price is not None and self.high_water_price <= 0:
            raise ValueError("high_water_price 必须为正")
        if self.realized_volatility is not None and self.realized_volatility < 0:
            raise ValueError("realized_volatility 不能为负")
        if self.atr is not None and self.atr < 0:
            raise ValueError("atr 不能为负")


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """一条可审计退出或组合降风险决定。"""

    rule_type: RiskExitType
    symbol: str | None
    triggered: bool
    metric: float | None
    threshold: float | None
    before_weight: float
    after_weight: float
    reason: str


@dataclass(frozen=True, slots=True)
class RiskExitResult:
    """退出执行结果;只包含新目标与状态建议。"""

    target: TargetWeight
    decisions: tuple[ExitDecision, ...]
    cooldown_until: dict[str, date]
    portfolio_paused: bool
    executor_version: str = RISK_EXIT_EXECUTOR_VERSION


def _asset_metric(
    rule: RiskExitRule,
    position: ExitPositionSnapshot,
    as_of: date,
) -> tuple[float | None, bool]:
    if rule.rule_type is RiskExitType.PRICE_STOP_LOSS:
        metric = (position.current_price - position.average_price) / position.average_price
        return metric, rule.threshold is not None and metric <= -rule.threshold
    if rule.rule_type is RiskExitType.TAKE_PROFIT:
        metric = (position.current_price - position.average_price) / position.average_price
        return metric, rule.threshold is not None and metric >= rule.threshold
    if rule.rule_type is RiskExitType.VOLATILITY_STOP:
        volatility_metric: float | None = (
            position.atr / position.current_price
            if position.atr is not None
            else position.realized_volatility
        )
        return volatility_metric, (
            volatility_metric is not None
            and rule.threshold is not None
            and volatility_metric >= rule.threshold
        )
    if rule.rule_type is RiskExitType.MAX_HOLDING_DAYS:
        metric = float((as_of - position.opened_on).days)
        return metric, rule.days is not None and metric >= rule.days
    return None, False


def execute_risk_exit_policy(
    *,
    policy: RiskExitPolicy,
    target: TargetWeight,
    positions: tuple[ExitPositionSnapshot, ...],
    as_of: date,
    portfolio_drawdown: float = 0.0,
    cooldown_until: dict[str, date] | None = None,
) -> RiskExitResult:
    """把启用规则应用到目标仓位,不触碰实际持仓。

    ``filled_quantity`` 为零的拒单/未成交目标不会被当成持仓。部分成交只按实际
    剩余数量参与退出判断,后续卖出目标仍由 sizing 与撮合层决定。
    """
    if not 0 <= portfolio_drawdown <= 1:
        raise ValueError("portfolio_drawdown 必须落在 [0, 1]")
    weights = dict(target.weights)
    decisions: list[ExitDecision] = []
    cooldowns = dict(cooldown_until or {})
    active_rules = {rule.rule_type: rule for rule in policy.rules if rule.enabled}
    cooldown_rule = active_rules.get(RiskExitType.COOLDOWN)
    asset_rule_types = (
        RiskExitType.PRICE_STOP_LOSS,
        RiskExitType.VOLATILITY_STOP,
        RiskExitType.TAKE_PROFIT,
        RiskExitType.MAX_HOLDING_DAYS,
    )

    for position in sorted(positions, key=lambda item: item.symbol):
        before = weights.get(position.symbol, 0.0)
        if position.filled_quantity <= MAX_WEIGHT_EPSILON:
            decisions.append(
                ExitDecision(
                    rule_type=RiskExitType.MAX_HOLDING_DAYS,
                    symbol=position.symbol,
                    triggered=False,
                    metric=0.0,
                    threshold=None,
                    before_weight=before,
                    after_weight=before,
                    reason="实际成交数量为零,不把未成交目标当成持仓",
                )
            )
            continue
        triggered_rule: RiskExitRule | None = None
        triggered_metric: float | None = None
        for rule_type in asset_rule_types:
            rule = active_rules.get(rule_type)
            if rule is None:
                continue
            metric, triggered = _asset_metric(rule, position, as_of)
            decisions.append(
                ExitDecision(
                    rule_type=rule.rule_type,
                    symbol=position.symbol,
                    triggered=triggered,
                    metric=metric,
                    threshold=(
                        float(rule.days)
                        if rule.rule_type is RiskExitType.MAX_HOLDING_DAYS and rule.days is not None
                        else rule.threshold
                    ),
                    before_weight=before,
                    after_weight=0.0 if triggered else before,
                    reason=rule.rationale,
                )
            )
            if triggered and triggered_rule is None:
                triggered_rule = rule
                triggered_metric = metric
        if triggered_rule is not None:
            weights.pop(position.symbol, None)
            if cooldown_rule is not None and cooldown_rule.days is not None:
                cooldowns[position.symbol] = as_of + timedelta(days=cooldown_rule.days)
                decisions.append(
                    ExitDecision(
                        rule_type=RiskExitType.COOLDOWN,
                        symbol=position.symbol,
                        triggered=True,
                        metric=float(cooldown_rule.days),
                        threshold=float(cooldown_rule.days),
                        before_weight=before,
                        after_weight=0.0,
                        reason=(
                            f"{triggered_rule.rule_type.value} 触发后进入冷却期;"
                            f"metric={triggered_metric}"
                        ),
                    )
                )

    for symbol, until in sorted(cooldowns.items()):
        before = weights.get(symbol, 0.0)
        if until >= as_of and abs(before) > MAX_WEIGHT_EPSILON:
            weights.pop(symbol, None)
            decisions.append(
                ExitDecision(
                    rule_type=RiskExitType.COOLDOWN,
                    symbol=symbol,
                    triggered=True,
                    metric=float((until - as_of).days),
                    threshold=0.0,
                    before_weight=before,
                    after_weight=0.0,
                    reason=f"冷却期截至 {until.isoformat()},禁止重新纳入目标仓位",
                )
            )

    paused = False
    drawdown_rule = active_rules.get(RiskExitType.PORTFOLIO_DRAWDOWN_DERISK)
    if (
        drawdown_rule is not None
        and drawdown_rule.threshold is not None
        and portfolio_drawdown >= drawdown_rule.threshold
    ):
        before_gross = sum(abs(weight) for weight in weights.values())
        target_gross = drawdown_rule.target_gross_exposure or 0.0
        if before_gross > MAX_WEIGHT_EPSILON:
            scale = min(1.0, target_gross / before_gross)
            weights = {
                symbol: weight * scale
                for symbol, weight in weights.items()
                if abs(weight * scale) > MAX_WEIGHT_EPSILON
            }
        after_gross = sum(abs(weight) for weight in weights.values())
        paused = target_gross <= MAX_WEIGHT_EPSILON
        decisions.append(
            ExitDecision(
                rule_type=RiskExitType.PORTFOLIO_DRAWDOWN_DERISK,
                symbol=None,
                triggered=True,
                metric=portfolio_drawdown,
                threshold=drawdown_rule.threshold,
                before_weight=before_gross,
                after_weight=after_gross,
                reason=drawdown_rule.rationale,
            )
        )

    gross = sum(abs(weight) for weight in weights.values())
    cash = target.cash_buffer
    if target.long_only and target.max_leverage <= 1 + MAX_WEIGHT_EPSILON:
        cash = max(cash, 1.0 - gross)
    adjusted_target = TargetWeight(
        weights=weights,
        as_of=target.as_of,
        strategy_id=target.strategy_id,
        cash_buffer=cash,
        max_leverage=target.max_leverage,
        long_only=target.long_only,
        contract_version=target.contract_version,
        factor_snapshot_id=target.factor_snapshot_id,
        covariance_version=target.covariance_version,
    )
    return RiskExitResult(
        target=adjusted_target,
        decisions=tuple(decisions),
        cooldown_until=cooldowns,
        portfolio_paused=paused,
    )


__all__ = [
    "RISK_EXIT_EXECUTOR_VERSION",
    "ExitDecision",
    "ExitPositionSnapshot",
    "RiskExitResult",
    "execute_risk_exit_policy",
]
