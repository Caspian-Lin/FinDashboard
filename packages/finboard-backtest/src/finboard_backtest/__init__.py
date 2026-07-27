"""finboard-backtest: 回测引擎 — 行情回放 + 研究级纸面撮合 + 绩效分析 + 样本外验证。

策略代码在回测和实盘中完全一致,无需感知运行环境。
"""

from finboard_backtest.asset_rules import (
    ASSET_RULES_VERSION,
    AssetRule,
    AssetRuleResolutionError,
    AssetRuleTable,
    default_rule_table,
)
from finboard_backtest.bar_universe import (
    BarUniverseConfig,
    BarUniverseMode,
    BarUniverseSelector,
)
from finboard_backtest.broker import BacktestBroker, InstrumentResolver
from finboard_backtest.clock import SimulatedClock
from finboard_backtest.config import (
    MATCHING_MODEL_VERSION,
    BacktestConfig,
    BenchmarkConfig,
    FeeOverrides,
    FillTiming,
    MatchingModel,
)
from finboard_backtest.context import BacktestContext
from finboard_backtest.engine import BacktestEngine
from finboard_backtest.result import BacktestResult
from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_backtest.validation import (
    AcceptanceThresholds,
    ExperimentStatus,
    ExperimentVerdict,
    ResearchExperiment,
    RobustnessPlan,
    StatisticalReport,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    ValidationRunner,
    VersionStamp,
    WindowMetrics,
    WindowRole,
    deflated_sharpe_ratio,
    generate_walk_forward_windows,
    grid_candidates,
    new_experiment,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    stationary_bootstrap,
)

__all__ = [
    "ASSET_RULES_VERSION",
    "MATCHING_MODEL_VERSION",
    "AcceptanceThresholds",
    "AssetRule",
    "AssetRuleResolutionError",
    "AssetRuleTable",
    "BacktestBroker",
    "BacktestConfig",
    "BacktestContext",
    "BacktestEngine",
    "BacktestResult",
    "BarUniverseConfig",
    "BarUniverseMode",
    "BarUniverseSelector",
    "BenchmarkConfig",
    "ExperimentStatus",
    "ExperimentVerdict",
    "FeeOverrides",
    "FillTiming",
    "InstrumentResolver",
    "MatchingModel",
    "PointInTimeFactorSelector",
    "ResearchExperiment",
    "RobustnessPlan",
    "SimulatedClock",
    "StatisticalReport",
    "TrialRecord",
    "TrialStatus",
    "ValidationMode",
    "ValidationPlan",
    "ValidationRunner",
    "VersionStamp",
    "WindowMetrics",
    "WindowRole",
    "default_rule_table",
    "deflated_sharpe_ratio",
    "generate_walk_forward_windows",
    "grid_candidates",
    "new_experiment",
    "probabilistic_sharpe_ratio",
    "probability_of_backtest_overfitting",
    "stationary_bootstrap",
]
