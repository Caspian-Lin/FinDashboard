"""集成测试共享夹具。

* ``_engine``     —— module 级 engine,建表 / 测试结束清表;
* ``db_session``  —— 每个测试一个 session,结束 rollback 做隔离;
* ``account_id``  —— integration 专用账户(覆盖根 conftest 的 unit 版);
* ``wait_for_status`` —— 返回 async callable,轮询等待订单到达目标状态
  (OrderManager 的 broker-event 消费是异步的,测试不能假设 cancel/fill
  调用返回时 DB 已更新)。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_persistence import Base, create_async_engine, session_factory
from finboard_shared.identifiers import AccountId
from finboard_shared.types import OrderStatus

DB_URL = os.getenv(
    "FINBOARD_DB_URL",
    "postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/findashboard",
)


@pytest.fixture(scope="module")
def _db_url() -> str:
    return DB_URL


@pytest_asyncio.fixture(scope="module")
async def _engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        # 测试结束后清表,保留 schema 便于调试
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    smaker = session_factory(_engine)
    async with smaker() as session:
        yield session


@pytest.fixture
def account_id() -> AccountId:
    return AccountId("test-account-integration")


@pytest.fixture
def wait_for_status() -> Callable[..., Coroutine[Any, Any, Any]]:
    """返回一个 async callable:``await wait_for_status(repo, cid, target)``。"""

    async def _impl(
        repo: Any,
        client_order_id: str,
        target: OrderStatus,
        *,
        wait_seconds: float = 2.0,
        interval: float = 0.02,
    ) -> Any:
        async with asyncio.timeout(wait_seconds):
            while True:
                last = await repo.get(client_order_id)
                if last is not None and last.status is target:
                    return last
                await asyncio.sleep(interval)

    return _impl
