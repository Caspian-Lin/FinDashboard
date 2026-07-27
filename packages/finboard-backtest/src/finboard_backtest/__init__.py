"""finboard-backtest: 回测引擎 — 行情回放 + 研究级纸面撮合 + 绩效分析。

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

__all__ = [
    "ASSET_RULES_VERSION",
    "MATCHING_MODEL_VERSION",
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
    "FeeOverrides",
    "FillTiming",
    "InstrumentResolver",
    "MatchingModel",
    "PointInTimeFactorSelector",
    "SimulatedClock",
    "default_rule_table",
]
