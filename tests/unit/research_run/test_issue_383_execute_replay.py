"""issue #383:``ResearchRunExecutor.execute_replay`` —— #305 replay 语义的
诊断重放(不经 worker 领取、不写 background_jobs、进程内同步执行)。

用 Fake store / Fake coordinator(monkeypatch 模块属性)锁定编排契约:
replay 参数形态、幂等键带时间戳(重复采样不被「新 run 已 COMPLETED →
execute 短路」挡住)、异常映射。coordinator 内部状态机由
``test_issue_305_replay_interrupted.py`` 覆盖,此处不重复。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from finboard_backtest.background_jobs.contracts import ExecutorError
from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
)
from finboard_backtest.research_run import ResearchRunConflictError, ResearchRunStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_backtest.background_jobs.executors.research_run import (
        AdapterFactory,
        StoreFactory,
    )

_SRC = "RR-source0000000000000000000000000000"


@dataclass(frozen=True)
class _ReplayableManifest:
    """可 ``dataclasses.replace`` 的最小 manifest 替身(issue #455 起)。

    execute_replay 按新 run 身份预替换 manifest(#455 探针目标修正),
    替身必须携带身份字段集才能走真实 replace 路径;冻结输入在编排契约
    测试中不消费,省略。
    """

    run_id: str
    idempotency_key: str = ""
    requested_by: str = ""
    replay_of_run_id: str | None = None
    replay_source_status: str | None = None


def _source_record() -> SimpleNamespace:
    return SimpleNamespace(
        manifest=_ReplayableManifest(run_id=_SRC),
        status=ResearchRunStatus.COMPLETED,
        result_checksum="a" * 64,
    )


def _completed_record(run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=ResearchRunStatus.COMPLETED,
        manifest=SimpleNamespace(run_id=run_id),
    )


class _FakeStore:
    def __init__(self, record: SimpleNamespace | None) -> None:
        self._record = record
        self.checkpoint_calls = 0

    async def get(self, run_id: str) -> SimpleNamespace | None:
        if self._record is not None and self._record.manifest.run_id == run_id:
            return self._record
        return None

    async def checkpoint(self) -> None:
        self.checkpoint_calls += 1


class _FakeSessionCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSessionMaker:
    def __call__(self) -> _FakeSessionCM:
        return _FakeSessionCM()


class _FakeCoordinator:
    """捕获 replay kwargs 并返回预设 record。"""

    captured: ClassVar[list[dict[str, Any]]] = []
    exc: ClassVar[Exception | None] = None

    def __init__(self, store: object) -> None:
        self.store = store

    async def replay(self, **kwargs: Any) -> SimpleNamespace:
        type(self).captured.append(kwargs)
        exc = type(self).exc
        if exc is not None:
            raise exc
        return _completed_record(kwargs["new_run_id"])


def _executor(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeStore,
    *,
    adapter_factory: Any = None,
    coordinator_cls: Any = _FakeCoordinator,
) -> ResearchRunExecutor:
    """构造被测执行器;Fake 依赖经 cast 适配注入点协议(mypy 安静)。"""

    from typing import cast as _cast

    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.research_run.ResearchRunCoordinator",
        coordinator_cls,
    )
    return ResearchRunExecutor(
        session_maker=_cast("async_sessionmaker[AsyncSession]", _FakeSessionMaker()),
        store_factory=_cast("StoreFactory", lambda session: store),
        adapter_factory=_cast(
            "AdapterFactory", adapter_factory or (lambda manifest: object())
        ),
    )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeCoordinator.captured = []
    _FakeCoordinator.exc = None


class TestExecuteReplay:
    async def test_happy_path_replay_semantics(self, monkeypatch) -> None:
        store = _FakeStore(_source_record())
        executor = _executor(monkeypatch, store)

        result = await executor.execute_replay(_SRC)

        assert result.status == "succeeded"
        assert result.result_ref == _FakeCoordinator.captured[0]["new_run_id"]
        kwargs = _FakeCoordinator.captured[0]
        assert kwargs["source_run_id"] == _SRC
        assert kwargs["requested_by"] == "job-flamegraph"
        # 新 run_id / 幂等键按 REST replay 同口径派生(RR-sha256[:24])。
        expected_id = "RR-" + hashlib.sha256(
            kwargs["idempotency_key"].encode("utf-8")
        ).hexdigest()[:24]
        assert kwargs["new_run_id"] == expected_id
        assert kwargs["idempotency_key"].startswith(f"job-flamegraph:{_SRC}:")
        assert store.checkpoint_calls == 1

    async def test_idempotency_key_varies_per_invocation(self, monkeypatch) -> None:
        """带时间戳的幂等键:重复诊断重放产生新 run,不被 COMPLETED 短路。"""

        store = _FakeStore(_source_record())
        executor = _executor(monkeypatch, store)

        await executor.execute_replay(_SRC)
        await executor.execute_replay(_SRC)

        key_a = _FakeCoordinator.captured[0]["idempotency_key"]
        key_b = _FakeCoordinator.captured[1]["idempotency_key"]
        assert key_a != key_b

    async def test_missing_source_run(self, monkeypatch) -> None:
        executor = _executor(monkeypatch, _FakeStore(None))

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute_replay(_SRC)
        assert exc_info.value.code == "missing_research_run"

    async def test_adapter_factory_failure_wrapped(self, monkeypatch) -> None:
        store = _FakeStore(_source_record())
        executor = _executor(monkeypatch, store, adapter_factory=_boom_factory)

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute_replay(_SRC)
        # 与既有 execute 路径同口径:code 取异常类型名(异常无 .code 属性时)。
        assert exc_info.value.code == "RuntimeError"

    async def test_replay_guard_conflict_wrapped(self, monkeypatch) -> None:
        """源 run 状态不被 replay 接受(CANCELLED 等)→ 具名 ExecutorError。"""

        store = _FakeStore(_source_record())
        _FakeCoordinator.exc = ResearchRunConflictError("状态 cancelled 不可重放")
        executor = _executor(monkeypatch, store)

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute_replay(_SRC)
        assert exc_info.value.code == "replay_guard_rejected"
        assert "cancelled" in (exc_info.value.summary or "")

    async def test_non_completed_terminal_maps_to_failed(self, monkeypatch) -> None:
        """重放后 run 非 COMPLETED(如 FAILED)→ JobResult failed 带原因。"""

        store = _FakeStore(_source_record())

        class _FailCoordinator(_FakeCoordinator):
            async def replay(self, **kwargs: Any) -> SimpleNamespace:
                type(self).captured.append(kwargs)
                return SimpleNamespace(
                    status=ResearchRunStatus.FAILED,
                    manifest=SimpleNamespace(run_id=kwargs["new_run_id"]),
                    error_code="hard_constraint_rejected",
                    error_summary="约束拒绝",
                )

        executor = _executor(monkeypatch, store, coordinator_cls=_FailCoordinator)

        result = await executor.execute_replay(_SRC)
        assert result.status == "failed"
        assert result.error_code == "hard_constraint_rejected"


def _boom_factory(manifest: object) -> object:
    # 适配器工厂与 execute() 同语义:同步抛错才被包装;async 函数只会返回
    # 未 await 的 coroutine,不进 except 分支(与既有 execute 行为一致)。
    raise RuntimeError("boom")
