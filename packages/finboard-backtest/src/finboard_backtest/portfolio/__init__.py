"""组合构建层 — 目标权重 / 风险预算 / 离散手数 / 绩效归因。

issue #59:策略输出标准化 Signal / TargetWeight,组合层统一生成调仓订单意图,
而非让每个信号各自争抢现金。

公共 API:

* 契约: ``Signal``, ``TargetWeight``, ``PortfolioConstraints``, ``RebalancePlan``
* 分配: ``EqualWeightAllocator``, ``InverseVolatilityAllocator``, ``ErcAllocator``,
  ``MaxIrAllocator`` (issue #266 最大 IR 切点组合)
* 协方差: ``estimate_covariance`` (Ledoit-Wolf 收缩)
* 风险预算: ``scale_to_target_volatility``, ``needs_rebalance``
* 中性化: ``project_risk_factor_neutralization`` (issue #266 风险因子暴露上限)
* 离散求解: ``solve_sizing`` (10万/20万/50万元可行性)
* 归因: ``compute_attribution`` (资产 / sleeve 分解)
"""

from finboard_backtest.portfolio.allocators import (
    AllocationContext,
    AllocationError,
    Allocator,
    ConstraintAdjustment,
    ConstraintApplication,
    EqualWeightAllocator,
    ErcAllocator,
    InverseVolatilityAllocator,
    apply_portfolio_constraints,
    make_allocator,
)
from finboard_backtest.portfolio.attribution import (
    AssetContribution,
    AttributionReport,
    SleeveContribution,
    compute_attribution,
)
from finboard_backtest.portfolio.builder import (
    PortfolioBuildInput,
    PortfolioBuildResult,
    PortfolioRiskReport,
    SignalConflictPolicy,
    SignalResolution,
    build_portfolio,
    constraint_impact_summary,
    to_research_constraint_outcomes,
    to_research_rebalance_instructions,
    to_research_targets,
)
from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    MAX_WEIGHT_EPSILON,
    PORTFOLIO_CONTRACT_VERSION,
    AssetLotInfo,
    CapitalTier,
    CovarianceFailureMode,
    PortfolioConstraints,
    RebalancePlan,
    RebalanceTrade,
    RiskFactorLimit,
    Signal,
    Sleeve,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import (
    CovarianceError,
    CovarianceEstimate,
    estimate_covariance,
)
from finboard_backtest.portfolio.exits import (
    RISK_EXIT_EXECUTOR_VERSION,
    ExitDecision,
    ExitPositionSnapshot,
    RiskExitResult,
    execute_risk_exit_policy,
)
from finboard_backtest.portfolio.feasibility import (
    CapitalFeasibilityInput,
    CapitalTierFeasibility,
    FeasiblePosition,
    asset_lot_info_from_metadata,
    evaluate_capital_tiers,
)
from finboard_backtest.portfolio.max_ir import (
    MaxIrAllocator,
    MaxIrSolution,
    project_onto_capped_simplex,
    solve_max_ir,
)
from finboard_backtest.portfolio.neutralization import (
    NEUTRALIZATION_CONSTRAINT,
    NEUTRALIZATION_INACTIVE_WARNING,
    NEUTRALIZATION_SKIPPED_CONSTRAINT,
    FactorNeutralizationAudit,
    FactorNeutralizationResult,
    neutralization_audit_rows,
    project_risk_factor_neutralization,
)
from finboard_backtest.portfolio.risk_budget import (
    ConcentrationCheck,
    RiskBudgetError,
    RiskContributionProjection,
    enforce_risk_contribution_cap,
    needs_rebalance,
    portfolio_volatility,
    risk_concentration_check,
    scale_to_target_volatility,
)
from finboard_backtest.portfolio.sizing import (
    PositionSnapshot,
    SizingError,
    SizingInput,
    get_capital_tier,
    solve_sizing,
)

__all__ = [
    "CAPITAL_TIERS",
    "MAX_WEIGHT_EPSILON",
    "NEUTRALIZATION_CONSTRAINT",
    "NEUTRALIZATION_INACTIVE_WARNING",
    "NEUTRALIZATION_SKIPPED_CONSTRAINT",
    "PORTFOLIO_CONTRACT_VERSION",
    "RISK_EXIT_EXECUTOR_VERSION",
    "AllocationContext",
    "AllocationError",
    "Allocator",
    "AssetContribution",
    "AssetLotInfo",
    "AttributionReport",
    "CapitalFeasibilityInput",
    "CapitalTier",
    "CapitalTierFeasibility",
    "ConcentrationCheck",
    "ConstraintAdjustment",
    "ConstraintApplication",
    "CovarianceError",
    "CovarianceEstimate",
    "CovarianceFailureMode",
    "EqualWeightAllocator",
    "ErcAllocator",
    "ExitDecision",
    "ExitPositionSnapshot",
    "FactorNeutralizationAudit",
    "FactorNeutralizationResult",
    "FeasiblePosition",
    "InverseVolatilityAllocator",
    "MaxIrAllocator",
    "MaxIrSolution",
    "PortfolioBuildInput",
    "PortfolioBuildResult",
    "PortfolioConstraints",
    "PortfolioRiskReport",
    "PositionSnapshot",
    "RebalancePlan",
    "RebalanceTrade",
    "RiskBudgetError",
    "RiskContributionProjection",
    "RiskExitResult",
    "RiskFactorLimit",
    "Signal",
    "SignalConflictPolicy",
    "SignalResolution",
    "SizingError",
    "SizingInput",
    "Sleeve",
    "SleeveContribution",
    "TargetWeight",
    "apply_portfolio_constraints",
    "asset_lot_info_from_metadata",
    "build_portfolio",
    "compute_attribution",
    "constraint_impact_summary",
    "enforce_risk_contribution_cap",
    "estimate_covariance",
    "evaluate_capital_tiers",
    "execute_risk_exit_policy",
    "get_capital_tier",
    "make_allocator",
    "needs_rebalance",
    "neutralization_audit_rows",
    "portfolio_volatility",
    "project_onto_capped_simplex",
    "project_risk_factor_neutralization",
    "risk_concentration_check",
    "scale_to_target_volatility",
    "solve_max_ir",
    "solve_sizing",
    "to_research_constraint_outcomes",
    "to_research_rebalance_instructions",
    "to_research_targets",
]
