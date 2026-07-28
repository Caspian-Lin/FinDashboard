"""API 路由聚合。"""

from finboard_api.routes.account import router as account_router
from finboard_api.routes.audit import router as audit_router
from finboard_api.routes.backtest import router as backtest_router
from finboard_api.routes.data import router as data_router
from finboard_api.routes.fills import router as fills_router
from finboard_api.routes.health import router as health_router
from finboard_api.routes.instruments import router as instruments_router
from finboard_api.routes.kill_switch import router as kill_switch_router
from finboard_api.routes.orders import router as orders_router
from finboard_api.routes.positions import router as positions_router
from finboard_api.routes.reconcile import router as reconcile_router
from finboard_api.routes.research import router as research_router
from finboard_api.routes.strategy_presets import router as strategy_presets_router
from finboard_api.routes.watchlist import router as watchlist_router

__all__ = [
    "account_router",
    "audit_router",
    "backtest_router",
    "data_router",
    "fills_router",
    "health_router",
    "instruments_router",
    "kill_switch_router",
    "orders_router",
    "positions_router",
    "reconcile_router",
    "research_router",
    "strategy_presets_router",
    "watchlist_router",
]
