"""issue #471 研究运行决策段挂死:三层活性防线的单元可测部分。

覆盖(不依赖 PostgreSQL 的纯逻辑):

* P1.1 引擎工厂:postgres URL 默认注入 #450 keepalive 全套、调用方同名键
  覆盖、非 postgres URL 不注入、``finboard_app.config`` 别名委托;
* P1.2 ``SessionPerOperationResearchRunStore`` 操作级双层界:挂死操作抛
  ``ResearchRunStoreOperationTimeoutError``(继承 ``ResearchRunInterruptedError``
  → interrupted 重试语义)、statement_timeout 的 postgres 方言门控
  (sqlite 跳过)、``iter_artifacts`` 流只设服务端界不设客户端界。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import finboard_persistence.engine as engine_module
from finboard_backtest.background_jobs.executors.research_run import (
    DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS,
    ResearchRunStoreOperationTimeoutError,
    SessionPerOperationResearchRunStore,
    _postgres_statement_timeout_ms,
)
from finboard_backtest.research_run import ResearchRunInterruptedError
from finboard_persistence.engine import create_async_engine, postgres_connect_args

# ---------------------------------------------------------------------------
# P1.1 引擎工厂:postgres keepalive 默认注入(#450/#471)
# ---------------------------------------------------------------------------


class _EngineFactorySpy:
    """记录 SQLAlchemy create_async_engine 入参(不真建引擎)。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return object()


_PG_URL = "postgresql+psycopg://findashboard:pw@127.0.0.1:5432/findashboard"
_KEEPALIVE_KEYS = ("keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count")


class TestEngineFactoryKeepalive:
    def test_postgres_url_gets_default_keepalive(self, monkeypatch) -> None:
        """postgres URL 不带 connect_args → 工厂默认注入 keepalive 全套。"""

        spy = _EngineFactorySpy()
        monkeypatch.setattr(engine_module, "_sa_create_async_engine", spy)
        create_async_engine(_PG_URL)
        assert len(spy.calls) == 1
        connect_args = spy.calls[0]["connect_args"]
        assert connect_args["connect_timeout"] == 10
        for key in _KEEPALIVE_KEYS:
            assert connect_args[key] > 0, f"缺少 keepalive 键: {key}"
        assert spy.calls[0]["pool_pre_ping"] is True

    def test_caller_same_key_overrides_default(self, monkeypatch) -> None:
        """调用方同名键覆盖(dev 预检收紧 connect_timeout),其余默认保留。"""

        spy = _EngineFactorySpy()
        monkeypatch.setattr(engine_module, "_sa_create_async_engine", spy)
        create_async_engine(_PG_URL, connect_args={"connect_timeout": 3})
        connect_args = spy.calls[0]["connect_args"]
        assert connect_args["connect_timeout"] == 3
        for key in _KEEPALIVE_KEYS:
            assert connect_args[key] > 0, "同名键覆盖不应清掉其余 keepalive 默认值"

    def test_non_postgres_url_not_injected(self, monkeypatch) -> None:
        """非 postgres URL(sqlite 测试后端)不注入任何 libpq 参数。"""

        spy = _EngineFactorySpy()
        monkeypatch.setattr(engine_module, "_sa_create_async_engine", spy)
        create_async_engine("sqlite+aiosqlite://")
        assert spy.calls[0]["connect_args"] == {}
        create_async_engine("sqlite:///./local.db", connect_args={"timeout": 5})
        assert spy.calls[1]["connect_args"] == {"timeout": 5}

    def test_config_alias_delegates_to_persistence_impl(self) -> None:
        """finboard_app.config.postgres_connect_args 是 persistence 权威实现的别名。"""

        from finboard_app.config import postgres_connect_args as alias

        assert alias(_PG_URL) == postgres_connect_args(_PG_URL)
        assert alias("sqlite:///x.db") == {}

    async def test_real_engine_builds_with_merged_args(self) -> None:
        """合并后的 connect_args 可被 SQLAlchemy 引擎构造接受(不连接)。"""

        engine = create_async_engine(_PG_URL, connect_args={"connect_timeout": 3})
        await engine.dispose()


# ---------------------------------------------------------------------------
# P1.2 SessionPerOperationResearchRunStore 操作级双层界
# ---------------------------------------------------------------------------


class _FakeSession:
    """记录 execute/commit 的假会话;sync_session.bind.dialect.name 可注入。"""

    def __init__(self, *, dialect_name: str | None = None) -> None:
        self.executed: list[str] = []
        self.commits = 0
        bind = (
            SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
            if dialect_name is not None
            else None
        )
        self.sync_session = SimpleNamespace(bind=bind)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> None:
        self.executed.append(str(stmt))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, sessions: list[_FakeSession], *, dialect_name: str | None = None) -> None:
        self._sessions = sessions
        self._dialect_name = dialect_name
        self.counter = 0

    def __call__(self) -> _FakeSession:
        self.counter += 1
        session = _FakeSession(dialect_name=self._dialect_name)
        self._sessions.append(session)
        return session


class _HangingGetStore:
    """get 永不返回(模拟黑洞连接 / 卡死的原生调用)。"""

    async def get(self, run_id: str) -> None:
        await asyncio.sleep(999)
        return None


class _PlainGetStore:
    async def get(self, run_id: str) -> None:
        return None


class _SlowStreamStore:
    """逐条慢产出的 artifact 流(每条间隔 > 操作超时,但流本身合法)。"""

    def __init__(self, items: int = 2, gap_seconds: float = 0.15) -> None:
        self._items = items
        self._gap = gap_seconds

    async def iter_artifacts(self, run_id: str) -> AsyncIterator[str]:
        for index in range(self._items):
            await asyncio.sleep(self._gap)
            yield f"artifact-{index}"


class TestStoreOperationTimeout:
    async def test_hanging_operation_raises_named_timeout_in_bounded_time(self) -> None:
        """挂死操作在客户端兜底界(0.2s x 1.25)抛具名超时,而不是挂到外层。"""

        sessions: list[_FakeSession] = []
        wrapper = SessionPerOperationResearchRunStore(
            _FakeSessionFactory(sessions),  # type: ignore[arg-type]
            lambda session: _HangingGetStore(),  # type: ignore[arg-type, return-value]
            operation_timeout_seconds=0.2,
        )
        started = time.monotonic()
        with pytest.raises(ResearchRunStoreOperationTimeoutError) as excinfo:
            await wrapper.get("RR-471")
        assert time.monotonic() - started < 5.0, "客户端兜底界未生效"
        assert isinstance(excinfo.value, ResearchRunInterruptedError), (
            "超时必须走 interrupted 重试语义(继承 ResearchRunInterruptedError)"
        )
        message = str(excinfo.value)
        assert "get" in message, "错误摘要应携带具名操作(定位哪个操作挂死)"
        assert "471" in message

    async def test_statement_timeout_set_on_postgres_dialect(self) -> None:
        """postgres 会话:每操作先 SET statement_timeout(服务端界先行)。"""

        sessions: list[_FakeSession] = []
        wrapper = SessionPerOperationResearchRunStore(
            _FakeSessionFactory(sessions, dialect_name="postgresql"),  # type: ignore[arg-type]
            lambda session: _PlainGetStore(),  # type: ignore[arg-type, return-value]
            operation_timeout_seconds=120.0,
        )
        assert await wrapper.get("RR-471") is None
        assert len(sessions) == 1
        assert any("statement_timeout" in stmt for stmt in sessions[0].executed), (
            "postgres 会话缺少 SET statement_timeout"
        )

    async def test_statement_timeout_skipped_on_sqlite_dialect(self) -> None:
        """sqlite 测试后端:方言门控跳过 SET,操作正常完成(不会执行非法 SQL)。"""

        sessions: list[_FakeSession] = []
        wrapper = SessionPerOperationResearchRunStore(
            _FakeSessionFactory(sessions, dialect_name="sqlite"),  # type: ignore[arg-type]
            lambda session: _PlainGetStore(),  # type: ignore[arg-type, return-value]
            operation_timeout_seconds=120.0,
        )
        assert await wrapper.get("RR-471") is None
        assert sessions[0].executed == [], "sqlite 会话不应执行 SET statement_timeout"

    async def test_iter_artifacts_stream_has_no_client_bound(self) -> None:
        """iter_artifacts 流只设服务端界:逐条间隔超过操作超时的合法长流不被杀。"""

        sessions: list[_FakeSession] = []
        wrapper = SessionPerOperationResearchRunStore(
            _FakeSessionFactory(sessions, dialect_name="postgresql"),  # type: ignore[arg-type]
            lambda session: _SlowStreamStore(items=2, gap_seconds=0.15),  # type: ignore[arg-type, return-value]
            operation_timeout_seconds=0.1,  # 单操作界 0.1s;流内每条间隔 0.15s
        )
        collected: list[str] = []
        async for artifact in wrapper.iter_artifacts("RR-471"):
            collected.append(str(artifact))
        assert collected == ["artifact-0", "artifact-1"], "合法长流被客户端界误杀"
        assert any("statement_timeout" in stmt for stmt in sessions[0].executed)

    async def test_postgres_statement_timeout_ms_dialect_gate(self) -> None:
        """纯函数门控:postgresql → 毫秒值;sqlite / 无 bind → None。"""

        pg = _FakeSession(dialect_name="postgresql")
        lite = _FakeSession(dialect_name="sqlite")
        no_bind = _FakeSession()
        assert _postgres_statement_timeout_ms(pg, 120.0) == 120000  # type: ignore[arg-type]
        assert _postgres_statement_timeout_ms(lite, 120.0) is None  # type: ignore[arg-type]
        assert _postgres_statement_timeout_ms(no_bind, 120.0) is None  # type: ignore[arg-type]

    def test_default_operation_timeout_is_120s(self) -> None:
        """默认操作界 120s(健康路径毫秒到秒级,两个数量级余量)。"""

        assert DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS == 120.0
