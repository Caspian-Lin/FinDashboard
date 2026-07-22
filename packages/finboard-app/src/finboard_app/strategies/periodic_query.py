"""定时查询策略 —— 每次定时回调查询账户/持仓并记录日志。

验证目的: 查询链路长期可用、策略生命周期管理正常。
"""

from __future__ import annotations

import structlog

from finboard_core.strategy import Strategy, StrategyContext, TimerEvent
from finboard_shared.identifiers import StrategyId

logger = structlog.get_logger(__name__)


class PeriodicQueryStrategy(Strategy):
    """每次 ``on_timer`` 查询并记录账户 / 持仓快照。"""

    def __init__(self, *, strategy_id: str) -> None:
        self._id = StrategyId(strategy_id)

    @property
    def strategy_id(self) -> StrategyId:
        return self._id

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info(
            "periodic_query.started", strategy_id=str(self._id)
        )

    async def on_timer(self, event: TimerEvent, ctx: StrategyContext) -> None:
        account = await ctx.get_account()
        positions = await ctx.list_positions()
        logger.info(
            "periodic_query.snapshot",
            strategy_id=str(self._id),
            cash=str(account.cash) if account else None,
            total_asset=str(account.total_asset) if account else None,
            position_count=len(positions),
        )
