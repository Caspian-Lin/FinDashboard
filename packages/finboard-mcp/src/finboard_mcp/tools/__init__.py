"""FinBoard MCP 工具集。"""

from __future__ import annotations

from finboard_mcp.tools.backtest import register as register_backtest_tools
from finboard_mcp.tools.data import register as register_data_tools
from finboard_mcp.tools.data_write import register as register_data_write_tools
from finboard_mcp.tools.factors import register as register_factor_tools
from finboard_mcp.tools.jobs import register as register_jobs_tools
from finboard_mcp.tools.memories import register as register_memory_tools
from finboard_mcp.tools.portfolio import register as register_portfolio_tools
from finboard_mcp.tools.reports import register as register_report_tools
from finboard_mcp.tools.runs import register as register_run_tools
from finboard_mcp.tools.simulation import register as register_simulation_tools
from finboard_mcp.tools.strategies import register as register_strategy_tools
from finboard_mcp.tools.validation_experiments import (
    register as register_validation_experiment_tools,
)
from finboard_mcp.tools.watchlists import register as register_watchlist_tools

__all__ = [
    "register_backtest_tools",
    "register_data_tools",
    "register_data_write_tools",
    "register_factor_tools",
    "register_jobs_tools",
    "register_memory_tools",
    "register_portfolio_tools",
    "register_report_tools",
    "register_run_tools",
    "register_simulation_tools",
    "register_strategy_tools",
    "register_validation_experiment_tools",
    "register_watchlist_tools",
]
