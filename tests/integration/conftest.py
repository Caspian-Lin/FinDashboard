"""集成测试共享夹具。

* ``TEST_DB_URL``  —— 测试数据库 URL(issue #167:独立测试库隔离)。
  解析优先级:``FINBOARD_TEST_DB_URL`` → ``FINBOARD_DB_URL``(显式覆盖,
  仍指向共享/开发库的场景)→ 默认 ``findashboard_test``。
  首次连接自动 ``CREATE DATABASE``(经 ``postgres`` 维护库,需有建库权限;
  CI 的 postgres 服务用户是 superuser,可直接建)。
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
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_persistence import Base, session_factory
from finboard_shared.identifiers import AccountId
from finboard_shared.types import OrderStatus

DEFAULT_TEST_DB_NAME = "findashboard_test"

TEST_DB_URL = os.getenv(
    "FINBOARD_TEST_DB_URL",
    os.getenv(
        "FINBOARD_DB_URL",
        f"postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/{DEFAULT_TEST_DB_NAME}",
    ),
)

#: 兼容别名:旧文件 ``from tests.integration.conftest import DB_URL`` 仍可用。
DB_URL = TEST_DB_URL

#: 显式经 FINBOARD_DB_URL 指向共享/开发库时,清表须保护用户数据;
#: 专用测试库(默认或 FINBOARD_TEST_DB_URL)则全清。
_IS_SHARED_DB_OVERRIDE = os.getenv("FINBOARD_TEST_DB_URL") is None and os.getenv(
    "FINBOARD_DB_URL"
) is not None

_PRESERVE_TABLES = (
    {
        "instruments",
        # 研究数据发布是用户生成的不可变资产。集成测试只清理自己使用的
        # integration-* 记录,不能因为测试连接了开发库就抹掉真实发布清单。
        "research_dataset_releases",
        "dataset_manifests",
    }
    if _IS_SHARED_DB_OVERRIDE
    else set()
)


async def ensure_test_db(url: str) -> None:
    """目标库不存在时自动建库(经 postgres 维护库)。

    幂等:库已存在直接返回。建库失败(无权限/维护库不可达)抛错并给出
    手动建库指引。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect():
            return  # 库已存在
    except Exception:
        pass
    finally:
        await engine.dispose()

    admin_url = make_url(url).set(database="postgres")
    admin_engine = create_async_engine(
        # str(URL) 会脱敏密码(显示 ***),必须用 render_as_string 保留真实凭据。
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            dbname = make_url(url).database
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": dbname},
            )
            if exists.scalar() is None:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    except Exception as exc:  # pragma: no cover - 依赖环境权限
        raise RuntimeError(
            f"测试库 {make_url(url).database} 不存在且自动建库失败"
            f"(需有 postgres 维护库建库权限)。手动创建:"
            f"createdb -U findashboard {make_url(url).database}"
        ) from exc
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def _db_url() -> str:
    await ensure_test_db(TEST_DB_URL)
    return TEST_DB_URL


async def clean_tables(conn: Any) -> None:
    """清空全部业务表(默认测试库全清;显式共享库覆盖时保护用户数据表)。

    供所有集成测试的 fixture 调用,避免每个文件各自维护清表逻辑。
    """
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in _PRESERVE_TABLES:
            await conn.execute(table.delete())


class ApiTestApp:
    """测试辅助:封装 httpx client + 可访问的 broker 引用。"""

    def __init__(self, client: Any, broker: Any) -> None:
        self.client = client
        self.broker = broker


@pytest_asyncio.fixture(scope="module")
async def _api_engine() -> AsyncIterator[Any]:
    """完整 FastAPI app(含 lifespan)测试用的 module 级 engine。

    从 test_api_e2e 提升到 conftest(issue #167),供所有需要驱动真实
    app 的集成测试共享(如 test_session_hygiene 的会话卫生断言)。
    """
    from finboard_persistence import Base, create_async_engine

    await ensure_test_db(TEST_DB_URL)
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await clean_tables(conn)
    await engine.dispose()


@pytest_asyncio.fixture
async def api(_api_engine: Any) -> AsyncIterator[ApiTestApp]:
    """MockBroker + 真实 PG 的完整 API 链路(httpx.AsyncClient + ASGITransport)。"""
    from httpx import ASGITransport, AsyncClient

    from finboard_api.app import create_app
    from finboard_app.config import Settings
    from finboard_persistence import session_factory
    from finboard_shared.types import BrokerKind

    engine = _api_engine
    smaker = session_factory(engine)
    async with smaker() as clean:
        await clean_tables(clean)
        await clean.commit()

    settings = Settings(
        broker=BrokerKind.MOCK,
        account_id="test-account-api",
        db_url=TEST_DB_URL,
        risk_allow_market_order=True,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield ApiTestApp(client=c, broker=app.state.components.broker)


@pytest_asyncio.fixture(scope="module")
async def _engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    await ensure_test_db(_db_url)
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
