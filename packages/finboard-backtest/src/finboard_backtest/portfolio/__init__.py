"""组合构建层 — 目标权重 / 风险预算 / 离散手数 / 绩效归因。

issue #59:策略输出标准化 Signal / TargetWeight,组合层统一生成调仓订单意图,
而非让每个信号各自争抢现金。

公共 API:

* 契约: ``Signal``, ``TargetWeight``, ``PortfolioConstraints``, ``RebalancePlan``
* 分配: ``EqualWeightAllocator``, ``InverseVolatilityAllocator``, ``ErcAllocator``
* 协方差: ``estimate_covariance`` (Ledoit-Wolf 收缩)
* 风险预算: ``scale_to_target_volatility``, ``needs_rebalance``
* 离散求解: ``solve_sizing`` (10万/20万/50万元可行性)
* 归因: ``compute_attribution`` (资产 / sleeve 分解)
"""

from finboard_backtest.portfolio.allocators import (
    AllocationError,
    Allocator,
    EqualWeightAllocator,
    ErcAllocator,
    InverseVolatilityAllocator,
    make_allocator,
)
from finboard_backtest.portfolio.attribution import (
    AssetContribution,
    AttributionReport,
    SleeveContribution,
    compute_attribution,
)
from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    MAX_WEIGHT_EPSILON,
    PORTFOLIO_CONTRACT_VERSION,
    AssetLotInfo,
    CapitalTier,
    PortfolioConstraints,
    RebalancePlan,
    RebalanceTrade,
    Signal,
    Sleeve,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import (
    CovarianceError,
    CovarianceEstimate,
    estimate_covariance,
)
from finboard_backtest.portfolio.risk_budget import (
    ConcentrationCheck,
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
    "PORTFOLIO_CONTRACT_VERSION",
    "AllocationError",
    "Allocator",
    "AssetContribution",
    "AssetLotInfo",
    "AttributionReport",
    "CapitalTier",
    "ConcentrationCheck",
    "CovarianceError",
    "CovarianceEstimate",
    "EqualWeightAllocator",
    "ErcAllocator",
    "InverseVolatilityAllocator",
    "PortfolioConstraints",
    "PositionSnapshot",
    "RebalancePlan",
    "RebalanceTrade",
    "Signal",
    "SizingError",
    "SizingInput",
    "Sleeve",
    "SleeveContribution",
    "TargetWeight",
    "compute_attribution",
    "estimate_covariance",
    "get_capital_tier",
    "make_allocator",
    "needs_rebalance",
    "portfolio_volatility",
    "risk_concentration_check",
    "scale_to_target_volatility",
    "solve_sizing",
]
