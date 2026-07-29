"""信号到目标仓位的统一离线组合构建入口(issue #81)。

本模块只生成可审计目标权重,不连接 Broker、不创建实盘订单、也不修改持仓。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import numpy as np

from finboard_backtest.portfolio.allocators import (
    AllocationContext,
    AllocationError,
    ConstraintAdjustment,
    apply_portfolio_constraints,
    make_allocator,
)
from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    CovarianceFailureMode,
    PortfolioConstraints,
    RebalancePlan,
    Signal,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate
from finboard_backtest.portfolio.risk_budget import (
    RiskBudgetError,
    enforce_risk_contribution_cap,
    portfolio_volatility,
    scale_to_target_volatility,
)
from finboard_backtest.research_run.contracts import (
    ConstraintOutcome,
    RebalanceInstruction,
    ResearchFillAction,
    ResearchPositionSide,
    TargetPosition,
)


class SignalConflictPolicy(StrEnum):
    """同标的多信号冲突的聚合方式。"""

    NET = "net"
    NEUTRALIZE = "neutralize"


@dataclass(frozen=True, slots=True)
class SignalResolution:
    """同标的信号的可解释聚合结果。"""

    symbol: str
    positive_strength: float
    negative_strength: float
    resolved_score: float
    included: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PortfolioBuildInput:
    """统一组合构建所需的冻结输入。"""

    signals: tuple[Signal, ...]
    method: str
    constraints: PortfolioConstraints
    covariance: CovarianceEstimate | None = None
    sleeve_map: dict[str, str] = field(default_factory=dict)
    disabled_symbols: frozenset[str] = frozenset()
    current_weights: dict[str, float] = field(default_factory=dict)
    target_gross_exposure: float | None = None
    betas: dict[str, float] = field(default_factory=dict)
    max_drawdown: float = 0.0
    conflict_policy: SignalConflictPolicy = SignalConflictPolicy.NET

    def __post_init__(self) -> None:
        if not self.signals:
            raise AllocationError("signals 不能为空")
        if len({signal.strategy_id for signal in self.signals}) != 1 or len(
            {signal.timestamp for signal in self.signals}
        ) != 1:
            raise AllocationError("同一次组合构建的信号必须具有相同策略和决策日期")
        if self.target_gross_exposure is not None and self.target_gross_exposure < 0:
            raise AllocationError("target_gross_exposure 不能为负")
        if not 0 <= self.max_drawdown <= 1:
            raise AllocationError("max_drawdown 必须落在 [0, 1]")


@dataclass(frozen=True, slots=True)
class PortfolioRiskReport:
    """约束后目标仓位的风险摘要。"""

    projected_volatility: float | None
    beta: float | None
    asset_risk_contribution: dict[str, float]
    sleeve_risk_contribution: dict[str, float]
    max_asset_risk_contribution: float | None
    concentration: float
    max_drawdown: float
    constraint_impact: float


@dataclass(frozen=True, slots=True)
class PortfolioBuildResult:
    """约束前后目标、约束差异与风险输出。"""

    resolutions: tuple[SignalResolution, ...]
    target_before_constraints: TargetWeight
    target_after_constraints: TargetWeight
    adjustments: tuple[ConstraintAdjustment, ...]
    risk: PortfolioRiskReport
    covariance_fallback_used: bool


def _covariance_problem(
    covariance: CovarianceEstimate,
    symbols: set[str],
) -> str | None:
    missing = sorted(symbols - set(covariance.tickers))
    if missing:
        return f"协方差缺少信号标的: {missing}"
    if len(covariance.tickers) != len(set(covariance.tickers)):
        return "协方差标的列表存在重复"
    matrix = covariance.matrix
    if not np.isfinite(matrix).all():
        return "协方差矩阵包含非有限值"
    if not np.allclose(matrix, matrix.T, atol=1e-12):
        return "协方差矩阵不对称"
    try:
        min_eigenvalue = float(np.linalg.eigvalsh(matrix).min())
    except np.linalg.LinAlgError:
        return "协方差矩阵无法进行特征值分解"
    eigenvalue_tolerance = (
        np.finfo(np.float64).eps
        * max(1.0, float(np.abs(matrix).max()))
        * len(covariance.tickers)
    )
    if min_eigenvalue <= eigenvalue_tolerance:
        return f"协方差矩阵奇异或非正定: min_eigenvalue={min_eigenvalue:.3e}"
    return None


def _resolve_signals(
    build_input: PortfolioBuildInput,
) -> tuple[tuple[Signal, ...], tuple[SignalResolution, ...]]:
    grouped: dict[str, list[Signal]] = {}
    for signal in build_input.signals:
        grouped.setdefault(signal.symbol, []).append(signal)

    first = build_input.signals[0]
    resolved: list[Signal] = []
    audit: list[SignalResolution] = []
    for symbol in sorted(grouped):
        items = grouped[symbol]
        positive = sum(signal.score * signal.confidence for signal in items if signal.score > 0)
        negative = sum(
            abs(signal.score) * signal.confidence for signal in items if signal.score < 0
        )
        score = positive - negative
        reason = "按 score*confidence 净额合并"
        if (
            build_input.conflict_policy is SignalConflictPolicy.NEUTRALIZE
            and positive > MAX_WEIGHT_EPSILON
            and negative > MAX_WEIGHT_EPSILON
        ):
            score = 0.0
            reason = "正负信号冲突,按 neutralize 规则置为中性"
        if symbol in build_input.disabled_symbols:
            score = 0.0
            reason = "标的已禁用,不纳入组合决策"
        if build_input.constraints.long_only and score < 0:
            score = 0.0
            reason = "负信号在 long-only 组合中映射为空仓"
        included = abs(score) > MAX_WEIGHT_EPSILON
        if not included and reason == "按 score*confidence 净额合并":
            reason = "信号为中性,不纳入组合决策"
        audit.append(
            SignalResolution(
                symbol=symbol,
                positive_strength=positive,
                negative_strength=negative,
                resolved_score=score,
                included=included,
                reason=reason,
            )
        )
        if included:
            resolved.append(
                Signal(
                    symbol=symbol,
                    score=score,
                    confidence=1.0,
                    timestamp=first.timestamp,
                    strategy_id=first.strategy_id,
                    factor_snapshot_id=items[-1].factor_snapshot_id,
                )
            )
    return tuple(resolved), tuple(audit)


def _target_from_weights(
    weights: dict[str, float],
    source: TargetWeight,
    constraints: PortfolioConstraints,
) -> TargetWeight:
    gross = sum(abs(weight) for weight in weights.values())
    cash = constraints.min_cash_buffer
    if constraints.long_only and constraints.max_leverage <= 1 + MAX_WEIGHT_EPSILON:
        cash = max(cash, 1.0 - gross)
    return TargetWeight(
        weights={symbol: float(weight) for symbol, weight in weights.items()},
        as_of=source.as_of,
        strategy_id=source.strategy_id,
        cash_buffer=cash,
        max_leverage=constraints.max_leverage,
        long_only=constraints.long_only,
        factor_snapshot_id=source.factor_snapshot_id,
        covariance_version=source.covariance_version,
    )


def _risk_report(
    target: TargetWeight,
    covariance: CovarianceEstimate | None,
    build_input: PortfolioBuildInput,
    adjustments: tuple[ConstraintAdjustment, ...],
) -> PortfolioRiskReport:
    projected_volatility: float | None = None
    asset_rc: dict[str, float] = {}
    sleeve_rc: dict[str, float] = {}
    max_rc: float | None = None

    if covariance is not None and target.weights:
        projected_volatility = portfolio_volatility(target.weights, covariance)
        index = {ticker: i for i, ticker in enumerate(covariance.tickers)}
        vector = np.zeros(len(covariance.tickers), dtype=np.float64)
        for symbol, weight in target.weights.items():
            if symbol in index:
                vector[index[symbol]] = weight
        marginal = covariance.matrix @ vector
        contributions = vector * marginal
        denominator = float(contributions.sum())
        if abs(denominator) > MAX_WEIGHT_EPSILON:
            for symbol, idx in index.items():
                if symbol not in target.weights:
                    continue
                contribution = float(contributions[idx] / denominator)
                asset_rc[symbol] = contribution
                sleeve = build_input.sleeve_map.get(symbol, "default")
                sleeve_rc[sleeve] = sleeve_rc.get(sleeve, 0.0) + contribution
            if asset_rc:
                max_rc = max(asset_rc.values())

    beta = None
    if build_input.betas:
        beta = sum(
            weight * build_input.betas.get(symbol, 0.0) for symbol, weight in target.weights.items()
        )
    concentration = max(
        (abs(weight) for weight in target.weights.values()),
        default=0.0,
    )
    impact = sum(abs(item.before_value - item.after_value) for item in adjustments)
    return PortfolioRiskReport(
        projected_volatility=projected_volatility,
        beta=beta,
        asset_risk_contribution=asset_rc,
        sleeve_risk_contribution=sleeve_rc,
        max_asset_risk_contribution=max_rc,
        concentration=float(concentration),
        max_drawdown=build_input.max_drawdown,
        constraint_impact=float(impact),
    )


def build_portfolio(build_input: PortfolioBuildInput) -> PortfolioBuildResult:
    """执行信号聚合→分配→约束→波动率→再平衡带→风险报告。"""
    resolved, resolutions = _resolve_signals(build_input)
    if not resolved:
        first = build_input.signals[0]
        empty = TargetWeight(
            weights={},
            as_of=first.timestamp,
            strategy_id=first.strategy_id,
            cash_buffer=1.0,
            max_leverage=build_input.constraints.max_leverage,
            long_only=build_input.constraints.long_only,
        )
        return PortfolioBuildResult(
            resolutions=resolutions,
            target_before_constraints=empty,
            target_after_constraints=empty,
            adjustments=(),
            risk=_risk_report(empty, build_input.covariance, build_input, ()),
            covariance_fallback_used=False,
        )

    method = build_input.method
    covariance = build_input.covariance
    fallback = False
    covariance_needed = method in {"inverse_volatility", "erc"}
    if covariance is not None:
        covariance_problem = _covariance_problem(
            covariance, {signal.symbol for signal in resolved}
        )
        if covariance_problem is not None:
            if (
                build_input.constraints.covariance_failure_mode
                is CovarianceFailureMode.FAIL_CLOSED
            ):
                raise AllocationError(f"{covariance_problem},按 fail_closed 拒绝")
            if covariance_needed:
                method = "equal_weight"
            covariance = None
            fallback = True
    if covariance_needed and covariance is None:
        if build_input.constraints.covariance_failure_mode is CovarianceFailureMode.FAIL_CLOSED:
            raise AllocationError(f"{method} 缺少协方差矩阵,按 fail_closed 拒绝")
        method = "equal_weight"
        fallback = True

    relaxed = PortfolioConstraints(
        max_weight_per_asset=1.0,
        max_weight_per_sleeve=1.0,
        min_cash_buffer=0.0,
        max_leverage=max(1.0, build_input.constraints.max_leverage),
        long_only=build_input.constraints.long_only,
        max_risk_contribution=1.0,
    )
    context = AllocationContext(
        sleeve_map=build_input.sleeve_map,
        disabled_symbols=build_input.disabled_symbols,
    )
    allocator = make_allocator(method)
    try:
        raw = allocator.allocate(
            list(resolved),
            covariance,
            relaxed,
            context=context,
        )
    except (AllocationError, ValueError, np.linalg.LinAlgError) as exc:
        if (
            build_input.constraints.covariance_failure_mode is CovarianceFailureMode.FAIL_CLOSED
            or method == "equal_weight"
        ):
            raise AllocationError(f"组合分配失败,按 fail_closed 拒绝: {exc}") from exc
        raw = make_allocator("equal_weight").allocate(
            list(resolved), None, relaxed, context=context
        )
        fallback = True

    desired_gross = (
        build_input.target_gross_exposure
        if build_input.target_gross_exposure is not None
        else build_input.constraints.max_investable_weight
    )
    if raw.gross_exposure > MAX_WEIGHT_EPSILON:
        raw_weights = {
            symbol: float(weight * desired_gross / raw.gross_exposure)
            for symbol, weight in raw.weights.items()
        }
    else:
        raw_weights = {}
    before = TargetWeight(
        weights=raw_weights,
        as_of=raw.as_of,
        strategy_id=raw.strategy_id,
        cash_buffer=max(0.0, 1.0 - desired_gross),
        max_leverage=max(1.0, desired_gross),
        long_only=build_input.constraints.long_only,
        factor_snapshot_id=raw.factor_snapshot_id,
        covariance_version=raw.covariance_version,
    )

    application = apply_portfolio_constraints(
        before.weights,
        build_input.constraints,
        context=context,
    )
    after = _target_from_weights(application.weights, before, build_input.constraints)
    adjustments = list(application.adjustments)

    if (
        build_input.constraints.target_volatility is not None
        or build_input.constraints.max_volatility is not None
    ):
        if covariance is None:
            if build_input.constraints.covariance_failure_mode is CovarianceFailureMode.FAIL_CLOSED:
                raise AllocationError("波动率约束缺少协方差矩阵,按 fail_closed 拒绝")
            fallback = True
            adjustments.append(
                ConstraintAdjustment(
                    constraint="volatility",
                    symbol=None,
                    before_value=0.0,
                    after_value=0.0,
                    limit=build_input.constraints.max_volatility,
                    passed=False,
                    reason="缺少协方差,fail-safe 保持已降杠杆目标且记录降级",
                )
            )
        else:
            vol_before = portfolio_volatility(after.weights, covariance)
            scaled = scale_to_target_volatility(after, covariance, build_input.constraints)
            vol_after = portfolio_volatility(scaled.weights, covariance)
            limit = (
                build_input.constraints.max_volatility or build_input.constraints.target_volatility
            )
            adjustments.append(
                ConstraintAdjustment(
                    constraint="volatility",
                    symbol=None,
                    before_value=vol_before,
                    after_value=vol_after,
                    limit=limit,
                    passed=limit is None or vol_before <= limit + MAX_WEIGHT_EPSILON,
                    reason="目标/最大年化波动率缩放",
                )
            )
            after = scaled

    if build_input.current_weights:
        banded = dict(after.weights)
        for symbol in sorted(set(banded) | set(build_input.current_weights)):
            desired = banded.get(symbol, 0.0)
            current = build_input.current_weights.get(symbol, 0.0)
            difference = abs(desired - current)
            hold = (
                difference <= build_input.constraints.rebalance_threshold
                or difference < build_input.constraints.min_weight_to_trade
            )
            if hold and abs(current) <= build_input.constraints.max_weight_per_asset:
                if abs(current) > MAX_WEIGHT_EPSILON:
                    banded[symbol] = current
                else:
                    banded.pop(symbol, None)
            adjustments.append(
                ConstraintAdjustment(
                    constraint="rebalance_band",
                    symbol=symbol,
                    before_value=current,
                    after_value=banded.get(symbol, 0.0),
                    limit=build_input.constraints.rebalance_threshold,
                    passed=hold,
                    reason="偏差在再平衡带/最小交易权重内则维持实际已成交持仓",
                )
            )
        final_application = apply_portfolio_constraints(
            banded, build_input.constraints, context=context
        )
        adjustments.extend(final_application.adjustments)
        after = _target_from_weights(final_application.weights, before, build_input.constraints)

    if build_input.constraints.max_risk_contribution < 1.0 - MAX_WEIGHT_EPSILON:
        if covariance is None:
            raise AllocationError(
                "风险贡献硬约束缺少可用协方差矩阵,无法验证且按 fail_closed 拒绝"
            )
        try:
            projection = enforce_risk_contribution_cap(
                after.weights,
                covariance,
                threshold=build_input.constraints.max_risk_contribution,
            )
        except RiskBudgetError as exc:
            raise AllocationError(
                f"风险贡献硬约束不可满足,按 fail_closed 拒绝: {exc}"
            ) from exc
        after = _target_from_weights(
            projection.weights,
            after,
            build_input.constraints,
        )
        adjustments.append(
            ConstraintAdjustment(
                constraint="max_risk_contribution",
                symbol=projection.after_argmax or None,
                before_value=projection.before_max_contribution,
                after_value=projection.after_max_contribution,
                limit=build_input.constraints.max_risk_contribution,
                passed=projection.converged
                and projection.after_max_contribution
                <= build_input.constraints.max_risk_contribution
                + MAX_WEIGHT_EPSILON,
                reason=(
                    "单资产风险贡献硬上限采用只减仓投影;"
                    f"iterations={projection.iterations},"
                    f"before_argmax={projection.before_argmax or 'none'},"
                    f"after_argmax={projection.after_argmax or 'none'}"
                ),
            )
        )
    elif after.weights:
        adjustments.append(
            ConstraintAdjustment(
                constraint="max_risk_contribution",
                symbol=None,
                before_value=0.0,
                after_value=0.0,
                limit=build_input.constraints.max_risk_contribution,
                passed=True,
                reason="风险贡献上限为 1.0,对单资产贡献不构成约束",
            )
        )

    cash_passed = after.cash_buffer + MAX_WEIGHT_EPSILON >= (
        build_input.constraints.min_cash_buffer
    )
    adjustments.append(
        ConstraintAdjustment(
            constraint="min_cash_buffer",
            symbol=None,
            before_value=before.cash_buffer,
            after_value=after.cash_buffer,
            limit=build_input.constraints.min_cash_buffer,
            passed=cash_passed,
            reason="约束后保留最小现金缓冲",
        )
    )
    adjustment_tuple = tuple(adjustments)
    return PortfolioBuildResult(
        resolutions=resolutions,
        target_before_constraints=before,
        target_after_constraints=after,
        adjustments=adjustment_tuple,
        risk=_risk_report(after, covariance, build_input, adjustment_tuple),
        covariance_fallback_used=fallback,
    )


def to_research_constraint_outcomes(
    result: PortfolioBuildResult,
) -> tuple[ConstraintOutcome, ...]:
    """把约束审计转换为 #80 ``DecisionBundle`` 可直接消费的契约。"""
    outcomes: list[ConstraintOutcome] = []
    max_constraints = {
        "investable_universe",
        "max_weight_per_asset",
        "max_weight_per_sleeve",
        "gross_leverage",
        "volatility",
        "max_risk_contribution",
    }
    for item in result.adjustments:
        hard = item.constraint != "rebalance_band"
        passed = item.passed
        changed = abs(item.before_value - item.after_value) > MAX_WEIGHT_EPSILON
        if (
            hard
            and not passed
            and changed
            and item.limit is not None
            and item.constraint in max_constraints
        ):
            passed = abs(item.after_value) <= item.limit + MAX_WEIGHT_EPSILON
        outcomes.append(
            ConstraintOutcome(
                constraint=item.constraint,
                passed=passed,
                before_value=item.before_value,
                after_value=item.after_value,
                limit=item.limit,
                reason=item.reason,
                hard=hard,
            )
        )
    return tuple(outcomes)


def to_research_targets(target: TargetWeight) -> tuple[TargetPosition, ...]:
    """把目标权重转换为 #80 的 long/short ``TargetPosition``。"""
    return tuple(
        TargetPosition(
            symbol=symbol,
            weight=weight,
            position_side=(
                ResearchPositionSide.LONG if weight >= 0 else ResearchPositionSide.SHORT
            ),
        )
        for symbol, weight in sorted(target.weights.items())
    )


def to_research_rebalance_instructions(
    plan: RebalancePlan,
    *,
    run_id: str,
    decision_index: int,
    lot_sizes: dict[str, int],
) -> tuple[RebalanceInstruction, ...]:
    """把离散计划转换为 #80 研究指令;noop 不会伪装成订单。"""
    if not run_id.startswith("RR-"):
        raise ValueError("run_id 必须使用 RR- 命名空间")
    instructions: list[RebalanceInstruction] = []
    active = [trade for trade in plan.trades if not trade.is_noop]
    missing_lot_sizes = sorted(
        trade.symbol for trade in active if trade.symbol not in lot_sizes
    )
    if missing_lot_sizes:
        raise ValueError(f"研究调仓指令缺少 lot_size: {missing_lot_sizes}")
    for offset, trade in enumerate(active):
        action = (
            ResearchFillAction.OPEN_LONG
            if trade.delta_shares > 0
            else ResearchFillAction.CLOSE_LONG
        )
        reason = "目标权重经组合/风险约束和整数手求解"
        if trade.unfilled_shares:
            reason += f";容量约束未成交 {trade.unfilled_shares}"
        instructions.append(
            RebalanceInstruction(
                instruction_id=f"{run_id}:I:{decision_index:08d}:{offset:04d}",
                symbol=trade.symbol,
                action=action,
                target_quantity=Decimal(trade.target_shares),
                current_quantity=Decimal(trade.current_shares),
                delta_quantity=Decimal(trade.delta_shares),
                lot_size=lot_sizes.get(trade.symbol, 1),
                estimated_value=Decimal(str(abs(trade.delta_value))),
                reason=reason,
            )
        )
    return tuple(instructions)


def constraint_impact_summary(
    result: PortfolioBuildResult,
) -> dict[str, float]:
    """按约束名聚合绝对权重/风险差异,可写入 ``ResearchRunReport``。"""
    summary: dict[str, float] = {}
    for item in result.adjustments:
        summary[item.constraint] = summary.get(item.constraint, 0.0) + abs(
            item.before_value - item.after_value
        )
    return dict(sorted(summary.items()))


__all__ = [
    "PortfolioBuildInput",
    "PortfolioBuildResult",
    "PortfolioRiskReport",
    "SignalConflictPolicy",
    "SignalResolution",
    "build_portfolio",
    "constraint_impact_summary",
    "to_research_constraint_outcomes",
    "to_research_rebalance_instructions",
    "to_research_targets",
]
