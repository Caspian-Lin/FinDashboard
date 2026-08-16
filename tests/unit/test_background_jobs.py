"""统一后台任务队列 —— 纯逻辑单元测试(issue #117 / #142)。

只覆盖不依赖 PostgreSQL 的部分:状态枚举、ID 生成格式、执行器注册表、
echo 执行器的进度回调与产物引用。依赖 PG 的状态机 / SKIP LOCKED / lease 回收
由 ``tests/integration/test_background_job_persistence.py`` 覆盖。
"""

from __future__ import annotations

import pytest

from finboard_backtest.background_jobs.contracts import JobRecord, JobResult
from finboard_backtest.background_jobs.executors import EchoExecutor
from finboard_backtest.background_jobs.registry import (
    JobExecutorRegistry,
    UnknownJobKindError,
)
from finboard_shared.background_jobs import (
    TERMINAL_STATUSES,
    BackgroundJobStatus,
    generate_background_job_id,
)


class TestBackgroundJobStatus:
    def test_status_values_are_strings(self) -> None:
        assert BackgroundJobStatus.QUEUED.value == "queued"
        assert BackgroundJobStatus.RETRY_WAITING.value == "retry_waiting"
        assert BackgroundJobStatus.CANCEL_REQUESTED.value == "cancel_requested"
        assert BackgroundJobStatus.INTERRUPTED.value == "interrupted"

    def test_str_coerces_to_value(self) -> None:
        assert str(BackgroundJobStatus.RUNNING) == "running"

    def test_terminal_statuses_cover_all_final_states(self) -> None:
        assert frozenset(
            {
                BackgroundJobStatus.SUCCEEDED.value,
                BackgroundJobStatus.FAILED.value,
                BackgroundJobStatus.CANCELLED.value,
                BackgroundJobStatus.INTERRUPTED.value,
            }
        ) == TERMINAL_STATUSES
        # 中间态不应被误判为终态
        assert BackgroundJobStatus.RUNNING.value not in TERMINAL_STATUSES
        assert BackgroundJobStatus.QUEUED.value not in TERMINAL_STATUSES


class TestGenerateBackgroundJobId:
    def test_prefix_and_length(self) -> None:
        jid = generate_background_job_id()
        assert jid.startswith("BJ-")
        suffix = jid[len("BJ-") :]
        assert len(suffix) == 16
        # 全大写 hex
        assert suffix == suffix.upper()

    def test_uniqueness(self) -> None:
        ids = {generate_background_job_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_does_not_collide_with_order_prefix(self) -> None:
        # 与实盘 client_order_id 的 F- 前缀区分开,便于审计识别。
        assert not generate_background_job_id().startswith("F-")


class TestJobExecutorRegistry:
    def test_register_and_get(self) -> None:
        registry = JobExecutorRegistry()
        executor = EchoExecutor()
        registry.register("echo", executor)
        assert registry.has("echo")
        assert registry.get("echo") is executor
        assert "echo" in registry.kinds

    def test_get_unknown_kind_raises(self) -> None:
        registry = JobExecutorRegistry()
        with pytest.raises(UnknownJobKindError):
            registry.get("nope")

    def test_register_empty_kind_raises(self) -> None:
        registry = JobExecutorRegistry()
        with pytest.raises(ValueError, match="kind"):
            registry.register("", EchoExecutor())


class TestEchoExecutor:
    @staticmethod
    def _record(payload: dict[str, object] | None = None) -> JobRecord:
        return JobRecord(
            job_id="BJ-TEST",
            kind="echo",
            queue="default",
            payload=payload or {},
            attempt=1,
            max_attempts=3,
            requested_by="tester",
        )

    async def test_succeeded_with_result_ref(self) -> None:
        executor = EchoExecutor()
        calls: list[tuple[int, int | None, str | None]] = []

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            calls.append((done, total, phase))

        result = await executor.execute(self._record({"steps": 3}), progress)
        assert result.status == "succeeded"
        assert result.result_ref is not None
        assert result.result_ref.startswith("echo:")
        assert result.progress_total == 3
        # 进度回调从 0 单调走到 3
        assert [c[0] for c in calls] == [0, 1, 2, 3]

    async def test_result_ref_stable_for_same_payload(self) -> None:
        executor = EchoExecutor()

        async def noop(done: int, total: int | None, phase: str | None) -> None:
            del done, total, phase

        r1 = await executor.execute(self._record({"a": 1, "b": 2}), noop)
        r2 = await executor.execute(self._record({"b": 2, "a": 1}), noop)
        assert r1.result_ref == r2.result_ref  # dict 顺序不影响摘要

    async def test_result_ref_differs_for_different_payload(self) -> None:
        executor = EchoExecutor()

        async def noop(done: int, total: int | None, phase: str | None) -> None:
            del done, total, phase

        r1 = await executor.execute(self._record({"a": 1}), noop)
        r2 = await executor.execute(self._record({"a": 2}), noop)
        assert r1.result_ref != r2.result_ref


class TestJobResultDataclass:
    def test_defaults(self) -> None:
        result = JobResult(status="succeeded")
        assert result.result_ref is None
        assert result.error_code is None
        assert result.progress_total is None
