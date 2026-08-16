"""ETF 绝对趋势 x 相对动量轮动策略研究框架(issue #61)。

long-only ETF tactical allocation 基线:

1. **候选池**(:mod:`universe`) — A股主流 ETF(宽基/行业/红利/黄金/跨境/国债/货币),
   时点化(PIT)过滤上市状态。
2. **信号**(:mod:`signals`) — 绝对趋势(SMA/lookback return) + 相对动量(3/6/12月)。
3. **分配**(:mod:`allocation`) — 等权 / 逆波动率 + flight-to-safety。
4. **调仓**(:mod:`rebalance`) — 月度/双周 + 再平衡带。
5. **分析**(:mod:`analysis`) — 资产暴露/风险贡献/换手/成本/回撤/资金可行性。

**国债 ETF 是风险资产而非保本现金等价物。**
"""

from finboard_backtest.etf_rotation.allocation import (
    EtfAllocation,
    allocate,
    allocate_equal_weight,
    allocate_inverse_volatility,
)
from finboard_backtest.etf_rotation.analysis import (
    CAPITAL_TIERS,
    TIER_LABELS,
    EtfRotationAnalysis,
    analyze_etf_rotation,
    check_capital_tier_feasibility,
    compute_asset_class_exposure,
    compute_max_drawdown_duration,
    compute_risk_contribution,
    compute_turnover,
)
from finboard_backtest.etf_rotation.config import (
    ETF_ROTATION_VERSION,
    TRADING_DAYS_PER_MONTH,
    AbsoluteTrendMethod,
    AllocationMethod,
    EtfRotationConfig,
    RebalanceFrequency,
)
from finboard_backtest.etf_rotation.rebalance import (
    RebalanceDecision,
    should_rebalance,
)
from finboard_backtest.etf_rotation.signals import (
    EtfSignal,
    MomentumResult,
    TrendResult,
    compute_absolute_trend,
    compute_relative_momentum,
    generate_signals,
)
from finboard_backtest.etf_rotation.universe import (
    DEFAULT_ETF_UNIVERSE,
    SECTOR_TO_ASSET_CLASS,
    EtfSector,
    EtfUniverse,
    EtfUniverseMember,
    UniverseFilterConfig,
)

__all__ = [
    "CAPITAL_TIERS",
    "DEFAULT_ETF_UNIVERSE",
    "ETF_ROTATION_VERSION",
    "SECTOR_TO_ASSET_CLASS",
    "TIER_LABELS",
    "TRADING_DAYS_PER_MONTH",
    "AbsoluteTrendMethod",
    "AllocationMethod",
    "EtfAllocation",
    "EtfRotationAnalysis",
    "EtfRotationConfig",
    "EtfSector",
    "EtfSignal",
    "EtfUniverse",
    "EtfUniverseMember",
    "MomentumResult",
    "RebalanceDecision",
    "RebalanceFrequency",
    "TrendResult",
    "UniverseFilterConfig",
    "allocate",
    "allocate_equal_weight",
    "allocate_inverse_volatility",
    "analyze_etf_rotation",
    "check_capital_tier_feasibility",
    "compute_absolute_trend",
    "compute_asset_class_exposure",
    "compute_max_drawdown_duration",
    "compute_relative_momentum",
    "compute_risk_contribution",
    "compute_turnover",
    "generate_signals",
    "should_rebalance",
]
