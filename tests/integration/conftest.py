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


_PRESERVE_TABLES = {
    "instruments",
    # 研究数据发布是用户生成的不可变资产。集成测试只清理自己使用的
    # integration-* 记录,不能因为测试连接了开发库就抹掉真实发布清单。
    "research_dataset_releases",
    "dataset_manifests",
}


async def clean_tables(conn: Any) -> None:
    """清空全部业务表(跳过 instruments —— 由 akshare 同步,属持久化用户数据)。

    供所有集成测试的 fixture 调用,避免每个文件各自维护清表逻辑。
    """
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in _PRESERVE_TABLES:
            await conn.execute(table.delete())


@pytest_asyncio.fixture(scope="module")
async def _engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    # lock_timeout:等行/表锁超过 10s 直接报错而不是无限挂起。本地开发库可能
    # 有残留的 idle-in-transaction 会话(或与 finboard dev 并存),没有它一个
    # 僵尸事务就能把整个测试运行卡死在 clean_tables 的 DELETE 上。
    # 注:finboard_persistence.create_async_engine 是固定参数的包装,不透传
    # connect_args,这里直接用 SQLAlchemy 原生构造。
    from sqlalchemy.ext.asyncio import create_async_engine as _sa_create_async_engine

    engine = _sa_create_async_engine(
        _db_url,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=10000"},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await clean_tables(conn)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    smaker = session_factory(_engine)
    async with smaker() as session:
        await clean_tables(session)
        await session.commit()
        yield session
        await session.rollback()


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
