"""issue #450 追续:research_run store 逐操作短会话包装。

此前 executor 把单一 AsyncSession 包成 store 复用整个 run(可达数小时):
任一次连接中断(WSL2 转发 PG 长跑实测 SSL 10053)把事务打进 invalid 态,
后续全部 store 操作 PendingRollbackError——原始错误被掩盖、run 行卡
running、attempt 重试撞状态机秒败。包装后每个操作开新会话并在关闭前
commit,断连只损失当次操作;``checkpoint`` 变 no-op。
"""

from __future__ import annotations

from typing import Any

import pytest

from finboard_backtest.background_jobs.executors.research_run import (
    SessionPerOperationResearchRunStore,
)
from finboard_backtest.research_run import ResearchRunStore


class _FakeSession:
    """记录 commit/close 的假会话(async 上下文管理器)。"""

    def __init__(self, name: str, log: list[tuple[str, str]]) -> None:
        self.name = name
        self._log = log

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._log.append((self.name, "close"))

    async def commit(self) -> None:
        self._log.append((self.name, "commit"))

    async def rollback(self) -> None:
        self._log.append((self.name, "rollback"))


class _FakeSessionFactory:
    """每次调用发一个新假会话。"""

    def __init__(self, log: list[tuple[str, str]]) -> None:
        self.log = log
        self.counter = 0

    def __call__(self) -> _FakeSession:
        self.counter += 1
        return _FakeSession(f"s{self.counter}", self.log)


class _RecordingStore(ResearchRunStore):
    """最小桩:get/list_artifacts 返回空,其余不触(测试只走只读路径)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_or_get(self, manifest: Any) -> Any:
        self.calls.append("create_or_get")
        raise NotImplementedError

    async def get(self, run_id: str):
        self.calls.append("get")
        return None

    async def list_by_status(self, statuses: Any) -> list[Any]:
        self.calls.append("list_by_status")
        return []

    async def transition(self, run_id: str, **kwargs: Any) -> Any:
        self.calls.append("transition")
        raise NotImplementedError

    async def save_result(self, run_id: str, **kwargs: Any) -> Any:
        self.calls.append("save_result")
        raise NotImplementedError

    async def append_artifact(self, artifact: Any) -> bool:
        self.calls.append("append_artifact")
        return False

    async def append_artifacts(self, artifacts: Any) -> list[bool]:
        self.calls.append("append_artifacts")
        return [False] * len(artifacts)

    async def list_artifacts(self, run_id: str):
        self.calls.append("list_artifacts")
        return []

    async def iter_artifacts(self, run_id: str):
        self.calls.append("iter_artifacts")
        return
        yield

    async def list_artifact_digests(self, run_id: str):
        self.calls.append("list_artifact_digests")
        return []

    async def checkpoint(self) -> None:
        self.calls.append("checkpoint")


def _make_wrapper(
    log: list[tuple[str, str]],
) -> tuple[SessionPerOperationResearchRunStore, list[_RecordingStore]]:
    factory = _FakeSessionFactory(log)
    stores: list[_RecordingStore] = []

    def store_factory(session: _FakeSession) -> ResearchRunStore:
        store = _RecordingStore()
        stores.append(store)
        return store

    wrapper = SessionPerOperationResearchRunStore(factory, store_factory)  # type: ignore[arg-type]
    return wrapper, stores


@pytest.mark.asyncio
async def test_each_operation_uses_fresh_session_and_commits() -> None:
    """每个操作开新会话、关闭前 commit;会话绝不复用(断连隔离的关键)。"""
    log: list[tuple[str, str]] = []
    wrapper, stores = _make_wrapper(log)

    assert await wrapper.get("RR-missing") is None
    assert await wrapper.list_artifacts("RR-missing") == []

    # 两个操作 = 两个不同会话、两个不同内层 store,各自 commit + close
    assert len(stores) == 2
    names = [entry[0] for entry in log if entry[1] == "commit"]
    assert len(set(names)) == 2, f"会话被复用: {names}"
    for name in names:
        assert (name, "close") in log


@pytest.mark.asyncio
async def test_checkpoint_is_noop_without_session() -> None:
    """checkpoint 在包装下是 no-op(逐操作已即时持久化),不开会话。"""
    log: list[tuple[str, str]] = []
    wrapper, _stores = _make_wrapper(log)
    await wrapper.checkpoint()
    assert log == []


@pytest.mark.asyncio
async def test_operations_delegate_to_inner_store() -> None:
    """包装把操作原样委托给内层 store(参数/返回值透传)。"""
    log: list[tuple[str, str]] = []
    wrapper, stores = _make_wrapper(log)

    assert await wrapper.get("RR-a") is None
    assert stores[-1].calls == ["get"]
    assert await wrapper.list_artifacts("RR-a") == []
    assert stores[-1].calls == ["list_artifacts"]
