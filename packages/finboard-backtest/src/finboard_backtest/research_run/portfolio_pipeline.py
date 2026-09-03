"""ResearchRun 的强制组合风控与确定性研究成交编排(issue #91)。

本模块只消费冻结研究输入并生成 ``RR-`` 命名空间产物。它不导入 Broker、
实盘 OrderManager、PositionManager 或风控包,也不会把研究订单转换为实盘请求。
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import cast

import numpy as np
import structlog

from finboard_backtest.metrics import sharpe_from_equity_values, total_return
from finboard_backtest.portfolio.allocators import AllocationError
from finboard_backtest.portfolio.builder import (
    PortfolioBuildInput,
    SignalConflictPolicy,
    build_portfolio,
    to_research_constraint_outcomes,
    to_research_rebalance_instructions,
    to_research_targets,
)
from finboard_backtest.portfolio.contracts import (
    AssetLotInfo,
    CovarianceFailureMode,
    PortfolioConstraints,
    RiskFactorLimit,
    Signal,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate
from finboard_backtest.portfolio.exits import (
    ExitPositionSnapshot,
    execute_risk_exit_policy,
)
from finboard_backtest.portfolio.feasibility import (
    CapitalFeasibilityInput,
    evaluate_capital_tiers,
)
from finboard_backtest.portfolio.sizing import (
    PositionSnapshot,
    SizingError,
    SizingInput,
    solve_sizing,
)
from finboard_backtest.research_run.adapters import (
    SUPPORTED_RESEARCH_STRATEGIES,
    validate_strategy_dataset_capabilities,
)
from finboard_backtest.research_run.contracts import (
    CapitalTierOutcome,
    DecisionBundle,
    EquityPoint,
    FeatureValue,
    LedgerSnapshot,
    NormalizedSignal,
    ResearchConstraintViolationError,
    ResearchFill,
    ResearchFillAction,
    ResearchOrder,
    ResearchOrderStatus,
    ResearchPipelineEvidence,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRiskState,
    ResearchRunManifest,
    ResearchRunReport,
    RiskExitOutcome,
    UniverseCandidate,
    UnsupportedResearchCapabilityError,
    execution_mode_for,
    pipeline_output_checksum,
    stable_checksum,
)
from finboard_backtest.strategy_spec.contracts import (
    AllocationMethod,
)
from finboard_backtest.strategy_spec.contracts import (
    SignalConflictPolicy as SpecSignalConflictPolicy,
)
from finboard_backtest.strategy_spec.registry import get_strategy_capability

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PortfolioDecisionInput:
    """一个决策时点的冻结研究输入,不含目标、订单、成交或持仓结果。"""

    business_date: date
    decision_at: datetime
    execution_at: datetime
    candidates: tuple[UniverseCandidate, ...]
    features: tuple[FeatureValue, ...]
    signals: tuple[NormalizedSignal, ...]
    prices: dict[str, float]
    execution_prices: dict[str, float]
    lot_info: dict[str, AssetLotInfo]
    input_artifact_ids: tuple[str, ...]
    covariance: CovarianceEstimate | None = None
    sleeve_map: dict[str, str] = field(default_factory=dict)
    disabled_symbols: frozenset[str] = frozenset()
    betas: dict[str, float] = field(default_factory=dict)
    realized_volatility: dict[str, float] = field(default_factory=dict)
    atr: dict[str, float] = field(default_factory=dict)
    fill_ratio_by_symbol: dict[str, float] = field(default_factory=dict)
    rejected_symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_at", self.decision_at),
            ("execution_at", self.execution_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} 必须带时区")
        if self.execution_at <= self.decision_at:
            raise ValueError("研究成交时间必须晚于决策时间,禁止同 Bar 成交")
        if self.decision_at.date() < self.business_date:
            raise ValueError("decision_at 不能早于业务日期")
        if not self.candidates or not self.signals:
            raise ValueError("组合流水线输入必须包含候选池和标准化信号")
        candidate_symbols = [item.symbol for item in self.candidates]
        if len(candidate_symbols) != len(set(candidate_symbols)):
            raise ValueError("组合流水线候选标的不允许重复")
        included_symbols = {
            item.symbol for item in self.candidates if item.included
        }
        signal_symbols = {item.symbol for item in self.signals}
        if not signal_symbols <= included_symbols:
            raise ValueError("标准化信号包含未通过候选池筛选的标的")
        if any(item.available_at > self.decision_at for item in self.features):
            raise ValueError("特征 available_at 不能晚于组合决策时间")
        if not self.input_artifact_ids:
            raise ValueError("组合流水线输入必须绑定冻结 artifact")
        if len(self.input_artifact_ids) != len(set(self.input_artifact_ids)):
            raise ValueError("input_artifact_ids 不允许重复")
        required = {item.symbol for item in self.signals}
        for label, values in (
            ("决策价格", self.prices),
            ("成交价格", self.execution_prices),
            ("执行元数据", self.lot_info),
        ):
            missing = sorted(required - set(values))
            if missing:
                raise ValueError(f"{label}缺少信号标的: {missing}")
        if any(value <= 0 or not math.isfinite(value) for value in self.prices.values()):
            raise ValueError("决策价格必须为正且有限")
        if any(
            value <= 0 or not math.isfinite(value)
            for value in self.execution_prices.values()
        ):
            raise ValueError("成交价格必须为正且有限")
        if any(
            not 0 <= value <= 1 or not math.isfinite(value)
            for value in self.fill_ratio_by_symbol.values()
        ):
            raise ValueError("fill ratio 必须落在 [0, 1]")

    @property
    def checksum(self) -> str:
        covariance_payload: object = None
        if self.covariance is not None:
            covariance_payload = {
                "tickers": self.covariance.tickers,
                "matrix": self.covariance.matrix.tolist(),
                "shrinkage": self.covariance.shrinkage,
                "n_observations": self.covariance.n_observations,
                "method": self.covariance.method,
            }
        return stable_checksum(
            {
                "business_date": self.business_date,
                "decision_at": self.decision_at,
                "execution_at": self.execution_at,
                "candidates": self.candidates,
                "features": self.features,
                "signals": self.signals,
                "prices": self.prices,
                "execution_prices": self.execution_prices,
                "lot_info": self.lot_info,
                "input_artifact_ids": self.input_artifact_ids,
                "covariance": covariance_payload,
                "sleeve_map": self.sleeve_map,
                "disabled_symbols": sorted(self.disabled_symbols),
                "betas": self.betas,
                "realized_volatility": self.realized_volatility,
                "atr": self.atr,
                "fill_ratio_by_symbol": self.fill_ratio_by_symbol,
                "rejected_symbols": sorted(self.rejected_symbols),
            }
        )


@dataclass(slots=True)
class _BookPosition:
    quantity: Decimal = Decimal()
    average_price: Decimal = Decimal()
    realized_pnl: Decimal = Decimal()
    opened_on: date | None = None
    high_water_price: Decimal = Decimal()


@dataclass(slots=True)
class _PipelineState:
    cash: Decimal
    positions: dict[str, _BookPosition] = field(default_factory=dict)
    fees_paid: Decimal = Decimal()
    tax_paid: Decimal = Decimal()
    slippage_paid: Decimal = Decimal()
    fill_shortfall: Decimal = Decimal()
    cooldown_until: dict[str, date] = field(default_factory=dict)
    equity_high_water: Decimal = Decimal()
    portfolio_paused: bool = False


class PortfolioPipelineAdapter:
    """把原始研究信号强制编排为完整 ``DecisionBundle``。

    这是正式 ResearchRun 入口。目标、约束、退出、sizing、研究订单、成交、
    持仓和账本均在同一确定性适配器内生成,调用方不能预先提交这些结果。
    """

    def __init__(
        self,
        *,
        strategy_kind: str,
        decision_inputs: Iterable[PortfolioDecisionInput],
        required_capabilities: Iterable[str] = (),
    ) -> None:
        if strategy_kind not in SUPPORTED_RESEARCH_STRATEGIES:
            raise UnsupportedResearchCapabilityError(
                f"未注册策略适配器: {strategy_kind}"
            )
        get_strategy_capability(strategy_kind)
        self.strategy_kind = strategy_kind
        self._inputs = tuple(decision_inputs)
        self._required_capabilities = tuple(
            sorted(set(required_capabilities))
        )

    def validate_manifest(self, manifest: ResearchRunManifest) -> None:
        if manifest.strategy_kind != self.strategy_kind:
            raise UnsupportedResearchCapabilityError(
                f"manifest={manifest.strategy_kind} 与 adapter={self.strategy_kind} 不一致"
            )
        available = {
            capability
            for release in manifest.dataset_releases
            for capability in release.capabilities
        }
        missing = sorted(set(self._required_capabilities) - available)
        if missing:
            raise UnsupportedResearchCapabilityError(
                f"冻结数据缺少策略能力: {missing}"
            )
        validate_strategy_dataset_capabilities(self.strategy_kind, available)
        frozen_ids = {
            item.artifact_id
            for item in (*manifest.dataset_releases, *manifest.factor_snapshots)
        }
        factor_snapshot_ids = {
            item.artifact_id for item in manifest.factor_snapshots
        }
        seen_dates: set[date] = set()
        previous_at: datetime | None = None
        for item in self._inputs:
            unknown = sorted(set(item.input_artifact_ids) - frozen_ids)
            if unknown:
                raise UnsupportedResearchCapabilityError(
                    f"组合输入引用未冻结 artifact: {unknown}"
                )
            feature_sources = {
                artifact_id
                for feature in item.features
                for artifact_id in feature.source_artifact_ids
            }
            unknown_sources = sorted(
                feature_sources - set(item.input_artifact_ids)
            )
            if unknown_sources:
                raise UnsupportedResearchCapabilityError(
                    f"特征引用未绑定本期冻结输入: {unknown_sources}"
                )
            unknown_snapshots = sorted(
                {
                    signal.factor_snapshot_id
                    for signal in item.signals
                    if signal.factor_snapshot_id is not None
                }
                - factor_snapshot_ids
            )
            if unknown_snapshots:
                raise UnsupportedResearchCapabilityError(
                    f"信号引用未冻结因子快照: {unknown_snapshots}"
                )
            if item.business_date in seen_dates:
                raise UnsupportedResearchCapabilityError(
                    f"同一业务日存在重复组合决策: {item.business_date}"
                )
            if previous_at is not None and item.decision_at <= previous_at:
                raise UnsupportedResearchCapabilityError(
                    "组合决策必须按时间严格递增"
                )
            seen_dates.add(item.business_date)
            previous_at = item.decision_at

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        state = _PipelineState(
            cash=manifest.initial_capital,
            equity_high_water=manifest.initial_capital,
        )
        constraints = _constraints_from_manifest(manifest)
        allocation_method = _allocation_method(manifest)
        conflict_policy = (
            SignalConflictPolicy.NEUTRALIZE
            if manifest.strategy_spec.signal_rules.conflict_policy
            is SpecSignalConflictPolicy.NEUTRALIZE
            else SignalConflictPolicy.NET
        )
        target_gross = manifest.strategy_spec.portfolio_policy.target_gross_exposure

        for index, item in enumerate(self._inputs):
            try:
                yield self._build_decision(
                    manifest=manifest,
                    item=item,
                    index=index,
                    state=state,
                    constraints=constraints,
                    allocation_method=allocation_method,
                    conflict_policy=conflict_policy,
                    target_gross=target_gross,
                )
            except (AllocationError, SizingError, ValueError) as exc:
                raise ResearchConstraintViolationError(str(exc)) from exc

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionBundle],
        *,
        equity_curve: tuple[EquityPoint, ...] = (),
        benchmark_curve: tuple[tuple[date, Decimal], ...] = (),
    ) -> ResearchRunReport:
        execution_mode = execution_mode_for(manifest.parameters)
        if decisions:
            final = decisions[-1].ledger
        else:
            final = LedgerSnapshot(
                cash=manifest.initial_capital,
                market_value=Decimal(),
                margin_used=Decimal(),
                realized_pnl=Decimal(),
                unrealized_pnl=Decimal(),
                equity=manifest.initial_capital,
                fees_paid=Decimal(),
                tax_paid=Decimal(),
                slippage_paid=Decimal(),
            )
        benchmark_return = _benchmark_return(manifest, benchmark_curve)
        benchmark_symbol = _benchmark_symbol(manifest)
        impact: dict[str, float] = {}
        for decision in decisions:
            for outcome in decision.constraints:
                before = outcome.before_value or 0.0
                after = outcome.after_value or 0.0
                impact[outcome.constraint] = impact.get(
                    outcome.constraint, 0.0
                ) + abs(before - after)

        if equity_curve:
            strategy_return, annualized, sharpe, drawdown = _curve_metrics(
                equity_curve, manifest.initial_capital
            )
            final_equity = equity_curve[-1].equity
        else:
            strategy_return = float(final.equity / manifest.initial_capital - 1)
            equity_values = [float(manifest.initial_capital), *[
                float(item.ledger.equity) for item in decisions
            ]]
            period_returns = [
                equity_values[index] / equity_values[index - 1] - 1
                for index in range(1, len(equity_values))
                if equity_values[index - 1] > 0
            ]
            # 期收益率口径(决策间频率,非日频):rf=0、ddof=1,与主路径
            # 的 rf0/ddof=1 一致,但年化因子为名义 252(期频不精确日化)。
            sharpe = 0.0
            if len(period_returns) >= 2:
                volatility = float(np.std(period_returns, ddof=1))
                if volatility > 0:
                    sharpe = float(np.mean(period_returns) / volatility * np.sqrt(252))
            peak = equity_values[0]
            drawdown = 0.0
            for equity in equity_values:
                peak = max(peak, equity)
                if peak > 0:
                    drawdown = max(drawdown, (peak - equity) / peak)
            # 单快照路径保持既有语义:不产出年化(年化只对全区间多期回放有意义)。
            annualized = 0.0
            final_equity = final.equity

        return ResearchRunReport(
            strategy_kind=self.strategy_kind,
            strategy_return=strategy_return,
            benchmark_symbol=benchmark_symbol,
            benchmark_return=benchmark_return,
            excess_return=(
                strategy_return - benchmark_return
                if benchmark_return is not None
                else None
            ),
            sharpe_ratio=sharpe,
            max_drawdown=drawdown,
            final_equity=final_equity,
            final_cash=final.cash,
            commission_paid=final.fees_paid,
            tax_paid=final.tax_paid,
            slippage_paid=final.slippage_paid,
            fill_shortfall=final.fill_shortfall,
            constraint_impact=dict(sorted(impact.items())),
            decision_count=len(decisions),
            order_count=sum(len(item.orders) for item in decisions),
            fill_count=sum(len(item.fills) for item in decisions),
            execution_mode=execution_mode,
            annualized_return=annualized,
            equity_curve=equity_curve,
        )

    def _build_decision(
        self,
        *,
        manifest: ResearchRunManifest,
        item: PortfolioDecisionInput,
        index: int,
        state: _PipelineState,
        constraints: PortfolioConstraints,
        allocation_method: str,
        conflict_policy: SignalConflictPolicy,
        target_gross: float,
    ) -> DecisionBundle:
        current_positions, current_weights, equity = _mark_current_book(
            state, item
        )
        state.equity_high_water = max(state.equity_high_water, equity)
        portfolio_drawdown = (
            float((state.equity_high_water - equity) / state.equity_high_water)
            if state.equity_high_water > 0
            else 0.0
        )
        signals = tuple(
            Signal(
                symbol=signal.symbol,
                score=signal.score,
                timestamp=item.business_date,
                strategy_id=manifest.strategy_spec.strategy_id,
                confidence=1.0,
                factor_snapshot_id=signal.factor_snapshot_id,
            )
            for signal in item.signals
        )
        # issue #266:风险因子中性化的暴露观测取自本期冻结特征 —— 按
        # 声明的因子名(feature_id)过滤;缺失的观测不由管线补造,
        # 交给 build_portfolio 的具名降级路径(factor_neutralization_inactive)。
        # _features_by_source 惰性导入:signal_engine 反向导入本模块,
        # 模块级互相导入会构成环。
        from finboard_backtest.research_run.signal_engine import _features_by_source

        declared_factors = {limit.factor for limit in constraints.risk_factor_limits}
        factor_exposures: dict[str, dict[str, float]] = (
            {
                factor: observations
                for factor, observations in _features_by_source(item.features).items()
                if factor in declared_factors
            }
            if declared_factors
            else {}
        )
        built = build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method=allocation_method,
                constraints=constraints,
                covariance=item.covariance,
                sleeve_map=item.sleeve_map,
                disabled_symbols=item.disabled_symbols,
                current_weights=current_weights,
                target_gross_exposure=target_gross,
                betas=item.betas,
                max_drawdown=portfolio_drawdown,
                conflict_policy=conflict_policy,
                factor_exposures=factor_exposures,
            )
        )
        constraint_outcomes = to_research_constraint_outcomes(built)
        failed_hard = [
            outcome.constraint
            for outcome in constraint_outcomes
            if outcome.hard and not outcome.passed
        ]
        if failed_hard:
            raise ResearchConstraintViolationError(
                f"组合硬约束执行后仍未通过: {failed_hard}"
            )

        exit_positions = tuple(
            ExitPositionSnapshot(
                symbol=symbol,
                filled_quantity=float(book.quantity),
                average_price=float(book.average_price),
                current_price=item.prices[symbol],
                opened_on=book.opened_on,
                high_water_price=float(book.high_water_price),
                realized_volatility=item.realized_volatility.get(symbol),
                atr=item.atr.get(symbol),
            )
            for symbol, book in sorted(state.positions.items())
            if book.quantity > 0 and book.opened_on is not None
        )
        exit_result = execute_risk_exit_policy(
            policy=manifest.strategy_spec.risk_exit_policy,
            target=built.target_after_constraints,
            positions=exit_positions,
            as_of=item.business_date,
            portfolio_drawdown=portfolio_drawdown,
            cooldown_until=state.cooldown_until,
        )
        state.cooldown_until = dict(exit_result.cooldown_until)
        state.portfolio_paused = exit_result.portfolio_paused
        target_after_risk = exit_result.target

        feasibility_checksum = stable_checksum(
            {
                "target": target_after_risk,
                "lot_info": item.lot_info,
                "prices": item.prices,
                "constraints": constraints,
                "input_artifact_ids": item.input_artifact_ids,
                "covariance_version": target_after_risk.covariance_version,
            }
        )
        feasibility = evaluate_capital_tiers(
            CapitalFeasibilityInput(
                target=target_after_risk,
                lot_info=item.lot_info,
                prices=item.prices,
                constraints=constraints,
            )
        )
        tier_outcomes = tuple(
            CapitalTierOutcome(
                tier=result.tier,
                capital=Decimal(str(result.capital)),
                feasible=result.feasible,
                cash_utilization=result.cash_utilization,
                tracking_error=result.tracking_error,
                unfillable_symbols=result.unfillable_symbols,
                capacity_pressure=result.capacity_pressure,
                margin_required=Decimal(str(result.margin_required)),
                estimated_costs=Decimal(str(result.estimated_costs)),
                reasons=result.reasons,
                input_checksum=feasibility_checksum,
            )
            for result in feasibility
        )

        plan = solve_sizing(
            SizingInput(
                target=target_after_risk,
                capital=float(equity),
                lot_info=item.lot_info,
                prices=item.prices,
                current_positions=current_positions,
                commission_rate=manifest.strategy_spec.execution_model.commission_rate,
                commission_min=manifest.strategy_spec.execution_model.minimum_commission,
                stamp_tax_rate=manifest.strategy_spec.execution_model.sell_tax_rate,
            ),
            constraints=constraints,
        )
        instructions = to_research_rebalance_instructions(
            plan,
            run_id=_pipeline_namespace(manifest),
            decision_index=index,
            lot_sizes={
                symbol: info.lot_size for symbol, info in item.lot_info.items()
            },
        )
        orders, fills = _execute_research_plan(
            manifest=manifest,
            item=item,
            index=index,
            plan=plan,
            instructions=instructions,
            state=state,
        )
        positions, ledger = _mark_after_execution(state, item)
        risk_state = _risk_state(state, ledger)
        exit_outcomes = tuple(
            RiskExitOutcome(
                rule_type=decision.rule_type.value,
                symbol=decision.symbol,
                triggered=decision.triggered,
                metric=decision.metric,
                threshold=decision.threshold,
                before_weight=decision.before_weight,
                after_weight=decision.after_weight,
                reason=decision.reason,
            )
            for decision in exit_result.decisions
        )
        decision = DecisionBundle(
            business_date=item.business_date,
            decision_at=item.decision_at,
            candidates=item.candidates,
            features=item.features,
            signals=item.signals,
            targets_before_constraints=to_research_targets(
                built.target_before_constraints
            ),
            constraints=constraint_outcomes,
            targets_after_constraints=to_research_targets(
                built.target_after_constraints
            ),
            risk_exits=exit_outcomes,
            targets_after_risk=to_research_targets(target_after_risk),
            risk_state=risk_state,
            capital_feasibility=tier_outcomes,
            rebalance_plan=instructions,
            orders=orders,
            fills=fills,
            positions=positions,
            ledger=ledger,
        )
        evidence = ResearchPipelineEvidence(
            manifest_input_checksum=manifest.input_checksum,
            input_checksum=item.checksum,
            output_checksum=pipeline_output_checksum(decision),
            hard_constraints_passed=True,
        )
        return replace(decision, pipeline_evidence=evidence)


def _constraints_from_manifest(
    manifest: ResearchRunManifest,
) -> PortfolioConstraints:
    policy = manifest.strategy_spec.portfolio_policy
    overrides = _section_overrides(manifest.portfolio_config)
    failure_mode = CovarianceFailureMode(
        str(overrides.get("covariance_failure_mode", "fail_closed"))
    )
    target_volatility = _optional_float(overrides.get("target_volatility"))
    max_volatility = _optional_float(overrides.get("max_volatility"))
    return PortfolioConstraints(
        max_weight_per_asset=_required_float(
            overrides, "max_weight_per_asset", policy.max_target_weight
        ),
        max_weight_per_sleeve=_required_float(
            overrides, "max_weight_per_sleeve", 0.40
        ),
        min_cash_buffer=_required_float(
            overrides, "min_cash_buffer", policy.cash_buffer
        ),
        max_leverage=_required_float(
            overrides,
            "max_leverage",
            max(1.0, policy.target_gross_exposure),
        ),
        target_volatility=target_volatility,
        max_volatility=max_volatility,
        rebalance_threshold=_required_float(
            overrides, "rebalance_threshold", policy.rebalance_threshold
        ),
        min_weight_to_trade=_required_float(
            overrides, "min_weight_to_trade", policy.min_target_weight
        ),
        max_risk_contribution=_required_float(
            overrides, "max_risk_contribution", 0.35
        ),
        long_only=bool(overrides.get("long_only", True)),
        covariance_failure_mode=failure_mode,
        risk_factor_limits=_risk_factor_limits(overrides),
    )


def _allocation_method(manifest: ResearchRunManifest) -> str:
    overrides = _section_overrides(manifest.portfolio_config)
    configured = overrides.get("allocation_method")
    if configured is not None:
        value = str(configured)
        if value == "equal_risk_contribution":
            return "erc"
        if value == "signal_weight":
            return "equal_weight"
        if value == "volatility_scaled":
            return "inverse_volatility"
        return value
    method = manifest.strategy_spec.portfolio_policy.allocation_method
    mapping = {
        AllocationMethod.EQUAL_WEIGHT: "equal_weight",
        AllocationMethod.SIGNAL_WEIGHT: "equal_weight",
        AllocationMethod.INVERSE_VOLATILITY: "inverse_volatility",
        AllocationMethod.EQUAL_RISK_CONTRIBUTION: "erc",
        AllocationMethod.VOLATILITY_SCALED: "inverse_volatility",
        AllocationMethod.MAX_IR: "max_ir",
    }
    return mapping[method]


def _risk_factor_limits(overrides: Mapping[str, object]) -> tuple[RiskFactorLimit, ...]:
    """解析 ``portfolio_config.overrides.risk_factor_limits``(issue #266)。

    期望形态:``[{"factor": "market_beta", "max_active_exposure": 0.05}]``;
    因子名与冻结特征 ``feature_id`` 同名(行业 one-hot 展开为逐行业列名)。
    声明了上限但决策日特征缺失不是入队错误 —— 执行期按具名 warning 降级
    (``factor_neutralization_inactive:<factor>``),可见但不静默失效。
    """
    raw = overrides.get("risk_factor_limits")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            "portfolio_config.overrides.risk_factor_limits 必须是 "
            "[{factor, max_active_exposure}] 列表"
        )
    limits: list[RiskFactorLimit] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"risk_factor_limits[{index}] 必须是含 factor/max_active_exposure 的对象"
            )
        factor = item.get("factor")
        exposure = item.get("max_active_exposure")
        if not isinstance(factor, str) or not factor.strip():
            raise ValueError(f"risk_factor_limits[{index}].factor 必须是非空字符串")
        if isinstance(exposure, bool) or not isinstance(exposure, (int, float)):
            raise ValueError(
                f"risk_factor_limits[{index}].max_active_exposure 必须为正数"
            )
        try:
            limits.append(
                RiskFactorLimit(
                    factor=factor.strip(),
                    max_active_exposure=float(exposure),
                )
            )
        except ValueError as exc:
            raise ValueError(f"risk_factor_limits[{index}]: {exc}") from exc
    return tuple(limits)


def _section_overrides(section: Mapping[str, object]) -> dict[str, object]:
    value = section.get("overrides", {})
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(float | int | str, value))


def _required_float(
    overrides: Mapping[str, object],
    key: str,
    default: float,
) -> float:
    return float(cast(float | int | str, overrides.get(key, default)))


def _benchmark_symbol(manifest: ResearchRunManifest) -> str:
    """报告展示用的基准标的:优先 manifest.benchmark_config.symbol,
    兼容旧 manifest 回退到验证计划字段。"""
    configured = manifest.benchmark_config.get("symbol")
    if isinstance(configured, str) and configured:
        return configured
    return manifest.strategy_spec.validation_plan.benchmark_symbol


def _benchmark_return(
    manifest: ResearchRunManifest,
    benchmark_curve: tuple[tuple[date, Decimal], ...],
) -> float | None:
    """按 benchmark_config.symbol 取冻结行情计算基准收益(issue #184)。

    优先级:冻结基准曲线点对点收益(真实计算)> 显式 ``overrides.return``
    (发布无该标的行情时的手动兜底);两者都缺失时返回 ``None`` 并由调用方
    记录具名 warning —— 禁止静默 0.0。
    """
    if len(benchmark_curve) >= 2:
        return total_return(benchmark_curve)
    overrides = _section_overrides(manifest.benchmark_config)
    manual = overrides.get("return")
    if manual is not None:
        return float(cast(float | int | str, manual))
    logger.warning(
        "research_run.benchmark_missing",
        symbol=_benchmark_symbol(manifest),
        reason="no_bars_or_manual_override",
    )
    return None


def _curve_metrics(
    equity_curve: tuple[EquityPoint, ...],
    initial_capital: Decimal,
) -> tuple[float, float, float, float]:
    """按每日权益曲线计算 总收益 / 年化 / 夏普 / 最大回撤。

    年化用交易日历 252 折算(与夏普同尺度);单点曲线不产出正收益区间,
    指标按 0 兜底(不抛错,曲线长度由合约保证 >= 2)。

    Sharpe 口径(issue #262):rf=0、样本标准差(ddof=1)、√252 年化 ——
    委托 ``metrics.sharpe_from_equity_values`` 统一实现,与引擎报告的
    ``sharpe_rf0``、mean_reversion 分析同口径;该口径以
    ``ResearchRunReport.risk_free_annual=0.0`` 随报告序列化。
    """
    values = [float(item.equity) for item in equity_curve]
    initial = float(initial_capital)
    strategy_return = values[-1] / initial - 1
    period_returns = [
        values[index] / values[index - 1] - 1
        for index in range(1, len(values))
        if values[index - 1] > 0
    ]
    annualized = (
        (1.0 + strategy_return) ** (252.0 / len(period_returns)) - 1.0
        if period_returns
        else 0.0
    )
    sharpe = sharpe_from_equity_values(values)
    peak = values[0]
    drawdown = 0.0
    for equity in values:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return strategy_return, annualized, sharpe, drawdown


def _mark_current_book(
    state: _PipelineState,
    item: PortfolioDecisionInput,
) -> tuple[dict[str, PositionSnapshot], dict[str, float], Decimal]:
    current: dict[str, PositionSnapshot] = {}
    market_value = Decimal()
    per_symbol_value: dict[str, Decimal] = {}
    for symbol, book in state.positions.items():
        if book.quantity <= 0:
            continue
        if symbol not in item.prices or symbol not in item.lot_info:
            raise ResearchConstraintViolationError(
                f"当前持仓 {symbol} 缺少决策价格或执行元数据"
            )
        multiplier = Decimal(str(item.lot_info[symbol].multiplier))
        value = book.quantity * Decimal(str(item.prices[symbol])) * multiplier
        per_symbol_value[symbol] = value
        market_value += value
        current[symbol] = PositionSnapshot(
            code=symbol,
            shares=int(book.quantity),
            market_value=float(value),
        )
        book.high_water_price = max(
            book.high_water_price,
            Decimal(str(item.prices[symbol])),
        )
    equity = state.cash + market_value
    if equity <= 0:
        raise ResearchConstraintViolationError(
            f"研究账户权益非正,禁止继续组合决策: {equity}"
        )
    weights = {
        symbol: float(value / equity)
        for symbol, value in per_symbol_value.items()
    }
    return current, weights, equity


def _current_weights_view(
    state: _PipelineState,
    *,
    prices: Mapping[str, float],
    lot_info: Mapping[str, AssetLotInfo],
) -> dict[str, float]:
    """只读当前权重视图(issue #218 user_code 权重回显)。

    与 :func:`_mark_current_book` 同一数学(持仓市值 / 权益),但不更新
    ``high_water_price``、不构造快照、权益非正时返回空(正式决策路径随后
    由 ``_mark_current_book`` 以 fail-closed 拒绝)。回显发生在沙箱 decide
    之前 —— decide 看到的是上一决策成交后的真实账本状态。
    """
    per_symbol_value: dict[str, Decimal] = {}
    market_value = Decimal()
    for symbol, book in state.positions.items():
        if book.quantity <= 0:
            continue
        if symbol not in prices or symbol not in lot_info:
            continue
        multiplier = Decimal(str(lot_info[symbol].multiplier))
        value = book.quantity * Decimal(str(prices[symbol])) * multiplier
        per_symbol_value[symbol] = value
        market_value += value
    equity = state.cash + market_value
    if equity <= 0:
        return {}
    return {
        symbol: float(value / equity)
        for symbol, value in per_symbol_value.items()
    }


def _execute_research_plan(
    *,
    manifest: ResearchRunManifest,
    item: PortfolioDecisionInput,
    index: int,
    plan: object,
    instructions: tuple[object, ...],
    state: _PipelineState,
) -> tuple[tuple[ResearchOrder, ...], tuple[ResearchFill, ...]]:
    from finboard_backtest.portfolio.contracts import RebalancePlan
    from finboard_backtest.research_run.contracts import RebalanceInstruction

    assert isinstance(plan, RebalancePlan)
    typed_instructions = cast(tuple[RebalanceInstruction, ...], instructions)
    namespace = _pipeline_namespace(manifest)
    orders: list[ResearchOrder] = []
    fills: list[ResearchFill] = []
    for trade in plan.trades:
        if trade.unfilled_shares > 0:
            price = item.execution_prices.get(
                trade.symbol, item.prices[trade.symbol]
            )
            multiplier = item.lot_info[trade.symbol].multiplier
            state.fill_shortfall += Decimal(
                str(trade.unfilled_shares * price * multiplier)
            )

    for offset, instruction in enumerate(typed_instructions):
        info = item.lot_info[instruction.symbol]
        order_quantity = abs(instruction.delta_quantity)
        order_id = f"{namespace}:O:{index:08d}:{offset:04d}"
        reject_reason: str | None = None
        ratio = item.fill_ratio_by_symbol.get(instruction.symbol, 1.0)
        raw_fill_quantity = int(order_quantity * Decimal(str(ratio)))
        fill_quantity = (
            raw_fill_quantity // info.lot_size * info.lot_size
        )
        if instruction.symbol in item.rejected_symbols:
            reject_reason = "冻结研究输入指定拒单"
            fill_quantity = 0
        elif fill_quantity <= 0:
            reject_reason = "成交比例不足一个合法交易单位"
        if fill_quantity <= 0:
            orders.append(
                ResearchOrder(
                    research_order_id=order_id,
                    instruction_id=instruction.instruction_id,
                    symbol=instruction.symbol,
                    action=instruction.action,
                    quantity=order_quantity,
                    status=ResearchOrderStatus.REJECTED,
                    reject_reason=reject_reason,
                )
            )
            state.fill_shortfall += _notional(
                order_quantity,
                item.execution_prices[instruction.symbol],
                info,
            )
            continue

        filled = Decimal(fill_quantity)
        status = (
            ResearchOrderStatus.FILLED
            if filled == order_quantity
            else ResearchOrderStatus.PARTIALLY_FILLED
        )
        orders.append(
            ResearchOrder(
                research_order_id=order_id,
                instruction_id=instruction.instruction_id,
                symbol=instruction.symbol,
                action=instruction.action,
                quantity=order_quantity,
                status=status,
            )
        )
        if filled < order_quantity:
            state.fill_shortfall += _notional(
                order_quantity - filled,
                item.execution_prices[instruction.symbol],
                info,
            )
        fill_price = Decimal(str(item.execution_prices[instruction.symbol]))
        notional = _notional(filled, float(fill_price), info)
        commission_rate = (
            info.commission_rate
            if info.commission_rate is not None
            else manifest.strategy_spec.execution_model.commission_rate
        )
        commission_min = (
            info.commission_min
            if info.commission_min is not None
            else manifest.strategy_spec.execution_model.minimum_commission
        )
        commission = max(
            notional * Decimal(str(commission_rate)),
            Decimal(str(commission_min)),
        )
        slippage_bps = (
            info.slippage_bps
            if info.slippage_bps > 0
            else manifest.strategy_spec.execution_model.slippage_bps
        )
        # Pydantic JSON 重载可能把 ``5`` 还原为 ``5.0``;先归一为 float,
        # 避免 Decimal 指数差异让等值金额产生不同的重放 checksum。
        slippage = (
            notional
            * Decimal(str(float(slippage_bps)))
            / Decimal("10000")
        )
        tax = Decimal()
        if instruction.action is ResearchFillAction.CLOSE_LONG:
            tax_rate = (
                info.stamp_tax_rate
                if info.stamp_tax_rate is not None
                else manifest.strategy_spec.execution_model.sell_tax_rate
            )
            tax = notional * Decimal(str(tax_rate))
        fill = ResearchFill(
            research_fill_id=f"{namespace}:F:{index:08d}:{offset:04d}",
            research_order_id=order_id,
            symbol=instruction.symbol,
            action=instruction.action,
            quantity=filled,
            price=fill_price,
            commission=commission,
            tax=tax,
            slippage=slippage,
            filled_at=item.execution_at,
        )
        _apply_research_fill(state, fill, info, item.business_date)
        fills.append(fill)
    return tuple(orders), tuple(fills)


def _pipeline_namespace(manifest: ResearchRunManifest) -> str:
    """同一冻结输入跨重放保持稳定的研究身份命名空间。"""
    return f"RR-PIPE-{manifest.input_checksum[:20]}"


def _notional(
    quantity: Decimal,
    price: float,
    info: AssetLotInfo,
) -> Decimal:
    return (
        quantity
        * Decimal(str(price))
        * Decimal(str(info.multiplier))
    )


def _apply_research_fill(
    state: _PipelineState,
    fill: ResearchFill,
    info: AssetLotInfo,
    business_date: date,
) -> None:
    book = state.positions.setdefault(fill.symbol, _BookPosition())
    multiplier = Decimal(str(info.multiplier))
    notional = fill.quantity * fill.price * multiplier
    if fill.action is ResearchFillAction.OPEN_LONG:
        total_quantity = book.quantity + fill.quantity
        book.average_price = (
            (
                book.average_price * book.quantity
                + fill.price * fill.quantity
            )
            / total_quantity
        )
        if book.quantity == 0:
            book.opened_on = business_date
            book.high_water_price = fill.price
        book.quantity = total_quantity
        book.high_water_price = max(book.high_water_price, fill.price)
        state.cash -= notional + fill.commission + fill.slippage
    elif fill.action is ResearchFillAction.CLOSE_LONG:
        if fill.quantity > book.quantity:
            raise ResearchConstraintViolationError(
                f"{fill.symbol} 研究成交导致超卖"
            )
        book.realized_pnl += (
            fill.price - book.average_price
        ) * fill.quantity * multiplier
        book.quantity -= fill.quantity
        state.cash += notional - fill.commission - fill.tax - fill.slippage
        if book.quantity == 0:
            book.average_price = Decimal()
            book.opened_on = None
            book.high_water_price = Decimal()
    else:
        raise ResearchConstraintViolationError(
            "通用组合流水线仅支持 long-only 研究成交"
        )
    state.fees_paid += fill.commission
    state.tax_paid += fill.tax
    state.slippage_paid += fill.slippage
    if state.cash < Decimal("-0.01"):
        raise ResearchConstraintViolationError(
            f"研究成交后现金为负: {state.cash}"
        )


def _mark_after_execution(
    state: _PipelineState,
    item: PortfolioDecisionInput,
) -> tuple[tuple[ResearchPosition, ...], LedgerSnapshot]:
    positions: list[ResearchPosition] = []
    market_value = Decimal()
    realized = Decimal()
    unrealized = Decimal()
    for symbol, book in sorted(state.positions.items()):
        price = Decimal(str(item.execution_prices.get(symbol, 0.0)))
        info = item.lot_info.get(symbol)
        if info is None and (book.quantity > 0 or book.realized_pnl != 0):
            raise ResearchConstraintViolationError(
                f"持仓 {symbol} 缺少执行元数据"
            )
        multiplier = Decimal(str(info.multiplier if info is not None else 1.0))
        value = book.quantity * price * multiplier
        symbol_unrealized = (
            (price - book.average_price) * book.quantity * multiplier
            if book.quantity > 0
            else Decimal()
        )
        market_value += value
        realized += book.realized_pnl
        unrealized += symbol_unrealized
        if book.quantity > 0:
            book.high_water_price = max(book.high_water_price, price)
        if book.quantity > 0 or book.realized_pnl != 0:
            positions.append(
                ResearchPosition(
                    symbol=symbol,
                    position_side=ResearchPositionSide.LONG,
                    quantity=book.quantity,
                    average_price=book.average_price,
                    market_price=price,
                    market_value=value,
                    realized_pnl=book.realized_pnl,
                    unrealized_pnl=symbol_unrealized,
                )
            )
    equity = state.cash + market_value
    state.equity_high_water = max(state.equity_high_water, equity)
    ledger = LedgerSnapshot(
        cash=state.cash,
        market_value=market_value,
        margin_used=Decimal(),
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        equity=equity,
        fees_paid=state.fees_paid,
        tax_paid=state.tax_paid,
        slippage_paid=state.slippage_paid,
        fill_shortfall=state.fill_shortfall,
    )
    return tuple(positions), ledger


def _risk_state(
    state: _PipelineState,
    ledger: LedgerSnapshot,
) -> ResearchRiskState:
    drawdown = (
        float(
            (state.equity_high_water - ledger.equity)
            / state.equity_high_water
        )
        if state.equity_high_water > 0
        else 0.0
    )
    return ResearchRiskState(
        cooldown_until=dict(sorted(state.cooldown_until.items())),
        opened_on={
            symbol: book.opened_on
            for symbol, book in sorted(state.positions.items())
            if book.quantity > 0 and book.opened_on is not None
        },
        high_water_prices={
            symbol: float(book.high_water_price)
            for symbol, book in sorted(state.positions.items())
            if book.quantity > 0
        },
        portfolio_equity_high_water=state.equity_high_water,
        portfolio_drawdown=drawdown,
        portfolio_paused=state.portfolio_paused,
    )


__all__ = [
    "PortfolioDecisionInput",
    "PortfolioPipelineAdapter",
]
