"""批量参数网格回测仓储(issue #175)。

``backtest_grid_runs`` 表只存网格**定义**(组合展开结果 + 逐组合 job_id),
聚合对比表由 MCP ``grid_get`` 实时计算,不在本表缓存结果 —— 因此仓储只有
按身份查询 + 写入,无状态机 / 更新路径。

边界:只读写 ``backtest_grid_runs`` 表(独立产物表,不建任何外键),
不触碰实盘 orders / fills / positions / audit_logs。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import BacktestGridRunModel


class BacktestGridRunRepository:
    """``backtest_grid_runs`` 表的读写入口;``__init__`` 只持有 session,不自己 commit。

    遵循现有约定(见 ``background_job_repo.py``):调用方负责 commit / rollback。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_grid_id(self, grid_id: str) -> BacktestGridRunModel | None:
        stmt = select(BacktestGridRunModel).where(BacktestGridRunModel.grid_id == grid_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> BacktestGridRunModel | None:
        stmt = select(BacktestGridRunModel).where(
            BacktestGridRunModel.idempotency_key == idempotency_key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, row: BacktestGridRunModel) -> BacktestGridRunModel:
        self._session.add(row)
        await self._session.flush()
        return row


__all__ = ["BacktestGridRunRepository"]
