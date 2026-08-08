"""API 路由聚合。"""

from finboard_api.routes.account import router as account_router
from finboard_api.routes.agent import router as agent_router
from finboard_api.routes.ai_research import router as ai_research_router
from finboard_api.routes.audit import router as audit_router
from finboard_api.routes.backtest import router as backtest_router
from finboard_api.routes.data import router as data_router
from finboard_api.routes.fills import router as fills_router
from finboard_api.routes.health import router as health_router
from finboard_api.routes.instruments import router as instruments_router
from finboard_api.routes.kill_switch import router as kill_switch_router
from finboard_api.routes.opencode_gateway import router as opencode_gateway_router
from finboard_api.routes.orders import router as orders_router
from finboard_api.routes.portfolio import router as portfolio_router
from finboard_api.routes.positions import router as positions_router
from finboard_api.routes.reconcile import router as reconcile_router
from finboard_api.routes.research import router as research_router
from finboard_api.routes.research_memories import router as research_memories_router
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_api.routes.simulation import router as simulation_router
from finboard_api.routes.strategy_presets import router as strategy_presets_router
from finboard_api.routes.strategy_specs import router as strategy_specs_router
from finboard_api.routes.watchlist import router as watchlist_router

__all__ = [
    "account_router",
    "agent_router",
    "ai_research_router",
    "audit_router",
    "backtest_router",
    "data_router",
    "fills_router",
    "health_router",
    "instruments_router",
    "kill_switch_router",
    "opencode_gateway_router",
    "orders_router",
    "portfolio_router",
    "positions_router",
    "reconcile_router",
    "research_memories_router",
    "research_router",
    "research_runs_router",
    "simulation_router",
    "strategy_presets_router",
    "strategy_specs_router",
    "watchlist_router",
]
