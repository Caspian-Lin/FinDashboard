"""finboard-backtest: 回测引擎 — 行情回放 + 纸面撮合 + 绩效分析。

策略代码在回测和实盘中完全一致,无需感知运行环境。
"""

from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock
from finboard_backtest.config import BacktestConfig
from finboard_backtest.context import BacktestContext
from finboard_backtest.engine import BacktestEngine
from finboard_backtest.result import BacktestResult
from finboard_backtest.selection import PointInTimeFactorSelector

__all__ = [
    "BacktestBroker",
    "BacktestConfig",
    "BacktestContext",
    "BacktestEngine",
    "BacktestResult",
    "PointInTimeFactorSelector",
    "SimulatedClock",
]
