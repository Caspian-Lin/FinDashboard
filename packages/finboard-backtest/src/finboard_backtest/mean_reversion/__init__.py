"""高流动性 ETF 短周期均值回归策略研究框架。

issue #62 的核心目标:long-only、严格成本约束的短周期均值回归研究基线。
所有信号族和参数在实验前预注册,下一 Bar 执行,趋势状态过滤,
硬性风控约束,毛/净收益分开报告,成本 x2 后失效即 rejected。
"""

from finboard_backtest.mean_reversion.analysis import (
    CapitalTierFeasibility,
    CostAttribution,
    CostSensitivityResult,
    CrisisPerformance,
    MeanReversionAnalysis,
    analyze_mean_reversion,
    check_capital_tiers,
    compute_cost_attribution,
    compute_cost_sensitivity,
    compute_crisis_performance,
    compute_trend_correlation,
)
from finboard_backtest.mean_reversion.backtest import (
    MeanReversionResult,
    MeanReversionSimulator,
    Trade,
    run_backtest,
)
from finboard_backtest.mean_reversion.config import (
    MEAN_REVERSION_VERSION,
    MeanReversionConfig,
    ParameterGrid,
    RegimeMethod,
    SignalFamily,
)
from finboard_backtest.mean_reversion.constraints import (
    ConstraintCheckResult,
    check_entry,
    check_max_holding,
    check_participation,
)
from finboard_backtest.mean_reversion.regime import (
    RegimeResult,
    RegimeState,
    classify_regime,
    is_entry_allowed,
)
from finboard_backtest.mean_reversion.signals import (
    SignalResult,
    compute_bollinger_position,
    compute_reversal,
    compute_rsi,
    compute_zscore,
    generate_signals,
)

__all__ = [
    "MEAN_REVERSION_VERSION",
    "CapitalTierFeasibility",
    "ConstraintCheckResult",
    "CostAttribution",
    "CostSensitivityResult",
    "CrisisPerformance",
    "MeanReversionAnalysis",
    "MeanReversionConfig",
    "MeanReversionResult",
    "MeanReversionSimulator",
    "ParameterGrid",
    "RegimeMethod",
    "RegimeResult",
    "RegimeState",
    "SignalFamily",
    "SignalResult",
    "Trade",
    "analyze_mean_reversion",
    "check_capital_tiers",
    "check_entry",
    "check_max_holding",
    "check_participation",
    "classify_regime",
    "compute_bollinger_position",
    "compute_cost_attribution",
    "compute_cost_sensitivity",
    "compute_crisis_performance",
    "compute_reversal",
    "compute_rsi",
    "compute_trend_correlation",
    "compute_zscore",
    "generate_signals",
    "is_entry_allowed",
    "run_backtest",
]
