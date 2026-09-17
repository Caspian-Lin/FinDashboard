"""共享测试夹具。

* asyncio 挂死守卫(issue #167):Windows 上 pytest-timeout 的 thread 方式
  无法打断阻塞在 C 层(select/IOCP)的事件循环,这里在收集阶段给每个
  async 测试包一层 ``asyncio.timeout``(超时值取 ``timeout`` marker,
  回退 pytest-timeout 的 ini 配置),由事件循环自身计时器唤醒取消任务,
  任何 await 挂死都能在超时后可见失败。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

# psycopg 的异步驱动在 Windows 不支持 ProactorEventLoop。测试进程也必须与
# finboard_app.cli 的运行时入口保持一致,否则仅 DB 集成用例会在连接阶段失败。
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# pytest 进程本身不读 .env(pydantic-settings 才读),
# 这里显式加载,让 tests/integration 里的 os.getenv("FINBOARD_DB_URL") 拿到真实值。
# override=False:CI 中通过真环境变量注入时优先于 .env。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


from finboard_broker import MockBroker
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderType, Side


@pytest.fixture
def account_id() -> AccountId:
    return AccountId("test-account")


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(code="510300.SH", market=Market.A_SHARE)


@pytest.fixture
def limit_buy_request(account_id: AccountId, symbol: Symbol) -> OrderRequest:
    return OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.85"),
    )


@pytest_asyncio.fixture
async def mock_broker(account_id: AccountId) -> AsyncIterator[MockBroker]:
    broker = MockBroker()
    await broker.connect(account_id, {})
    yield broker
    await broker.disconnect()


def _async_test_timeout_seconds(item: pytest.Item) -> float | None:
    """读取 async 测试的超时值:``timeout`` marker 优先,回退 ini。"""
    marker = item.get_closest_marker("timeout")
    if marker is not None:
        timeout = (
            marker.args[0]
            if marker.args
            else marker.kwargs.get("timeout")
        )
    else:
        timeout = item.config.getini("timeout")
    if timeout is None:
        return None
    seconds = float(timeout)
    return None if seconds <= 0 else seconds


def _make_timeout_guard(
    orig: Callable[..., Awaitable[object]], seconds: float
) -> Callable[..., Awaitable[object]]:
    """返回包了 ``asyncio.timeout`` 的协程函数(工厂避免循环闭包 late-binding)。

    ``seconds`` 传入的是 pytest-timeout 生效值;pytest-timeout(thread 方式)
    超时后直接 dump 堆栈 + ``os._exit(1)`` 杀进程,守卫须**先**触发才能给出
    单测试优雅失败,这里统一减 1s(下限 1s)。
    """

    @functools.wraps(orig)
    async def _guarded(*args: object, **kwargs: object) -> object:
        async with asyncio.timeout(max(1.0, seconds - 1.0)):
            return await orig(*args, **kwargs)

    return _guarded


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """给 async 测试包 ``asyncio.timeout`` 守卫(与 pytest-timeout 同源配置)。"""
    for item in items:
        if not inspect.iscoroutinefunction(getattr(item, "obj", None)):
            continue
        seconds = _async_test_timeout_seconds(item)
        if seconds is None:
            continue  # timeout(None)/timeout(0) 显式豁免
        item.obj = _make_timeout_guard(item.obj, seconds)  # type: ignore[attr-defined]
