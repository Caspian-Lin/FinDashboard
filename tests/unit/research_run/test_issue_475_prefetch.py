"""issue #475:决策加载双缓冲预取的顺序、取消和并发上限契约。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any, cast

import pytest

import finboard_backtest.research_run.signal_engine as signal_engine
from finboard_backtest.research_run.contracts import (
    ResearchRunManifest,
)
from finboard_backtest.research_run.failure_context import read_decision_load_context
from finboard_backtest.research_run.frozen_loader import FrozenInputLoader
from finboard_backtest.research_run.signal_engine import (
    DecisionLoadContext,
    iter_decision_load_contexts,
)

from .test_issue_306_load_probe import (
    _build_release,
    _month_end_decisions,
    _noop_snapshot_provider,
)
from .test_issue_308_progress import _multi_period_manifest

pytestmark = pytest.mark.asyncio


class _LoadGate:
    """在指定批次阻塞 load_context,观察预取是否与消费重叠。"""

    def __init__(self, blocked_dates: set[date]) -> None:
        self.blocked_dates = blocked_dates
        self.started: list[date] = []
        self.started_event = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0


def _prefetch_tasks() -> list[asyncio.Task[Any]]:
    """返回当前事件循环中仍在运行的 issue #475 预取任务。"""

    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "research_run.decision_load_prefetch"
        and not task.done()
    ]


def _factory(provider: Any) -> Any:
    def factory(release_id: str) -> Any:
        del release_id
        return provider

    return factory


async def _make_stream(
    tmp_path: Any,
    *,
    process_workers: int = 0,
) -> tuple[Any, ResearchRunManifest, AsyncGenerator[DecisionLoadContext, None]]:
    provider = await _build_release(tmp_path)
    manifest = _multi_period_manifest()
    generator = cast(
        AsyncGenerator[DecisionLoadContext, None],
        iter_decision_load_contexts(
            manifest,
            release_provider_factory=_factory(provider),
            snapshot_provider=_noop_snapshot_provider,
            process_workers=process_workers,
        ),
    )
    return provider, manifest, generator


class TestDecisionLoadPrefetch:
    async def test_next_batch_starts_while_current_batch_is_consumed(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当前批只取出首期时,下一批已有纯读任务开始。"""

        decisions = _month_end_decisions()
        gate = _LoadGate(set(decisions[4:]))
        real_load = FrozenInputLoader.load_context

        async def wrapped_load(
            loader: FrozenInputLoader,
            manifest: ResearchRunManifest,
            *,
            decision_at: Any,
            execution_at: Any,
        ) -> Any:
            day = decision_at.date()
            gate.started.append(day)
            if day in gate.blocked_dates:
                gate.started_event.set()
                try:
                    await gate.release.wait()
                except asyncio.CancelledError:
                    gate.cancelled += 1
                    raise
            return await real_load(
                loader,
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", wrapped_load)
        _, _, stream = await _make_stream(tmp_path)

        first = await stream.__anext__()
        assert first.context.business_date == decisions[0]
        await asyncio.wait_for(gate.started_event.wait(), timeout=2)
        # 只消费了第一批的第 1 期,下一批已经进入 load_context;它仍被
        # gate 阻塞,证明这不是「等当前批全部 yield 后才启动」的假并发。
        assert any(day in gate.blocked_dates for day in gate.started)
        assert len(_prefetch_tasks()) == 1

        gate.release.set()
        rest = [item async for item in stream]
        assert [first.context.business_date, *[item.context.business_date for item in rest]] == decisions

    async def test_prefetched_failure_is_raised_after_current_batch_in_order(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预取批内多个异常仍在当前批完整产出后按原期序抛首异常。"""

        decisions = _month_end_decisions()
        real_load = FrozenInputLoader.load_context

        async def failing_load(
            loader: FrozenInputLoader,
            manifest: ResearchRunManifest,
            *,
            decision_at: Any,
            execution_at: Any,
        ) -> Any:
            if decision_at.date() in set(decisions[4:]):
                await asyncio.sleep(0)
                raise RuntimeError(f"预取损坏:{decision_at.date().isoformat()}")
            return await real_load(
                loader,
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", failing_load)
        _, _, stream = await _make_stream(tmp_path)

        prefix = [await stream.__anext__() for _ in range(4)]
        assert [item.context.business_date for item in prefix] == decisions[:4]
        with pytest.raises(RuntimeError, match=f"预取损坏:{decisions[4].isoformat()}") as excinfo:
            await stream.__anext__()
        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at.date() == decisions[4]
        await stream.aclose()

    async def test_aclose_cancels_and_awaits_prefetch_gather(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """消费者关闭时预取 task 及其 gather 子任务均被收口。"""

        decisions = _month_end_decisions()
        gate = _LoadGate(set(decisions[4:]))
        real_load = FrozenInputLoader.load_context

        async def wrapped_load(
            loader: FrozenInputLoader,
            manifest: ResearchRunManifest,
            *,
            decision_at: Any,
            execution_at: Any,
        ) -> Any:
            day = decision_at.date()
            if day in gate.blocked_dates:
                gate.started_event.set()
                try:
                    await gate.release.wait()
                except asyncio.CancelledError:
                    gate.cancelled += 1
                    raise
            return await real_load(
                loader,
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", wrapped_load)
        _, _, stream = await _make_stream(tmp_path)
        await stream.__anext__()
        await asyncio.wait_for(gate.started_event.wait(), timeout=2)
        assert len(_prefetch_tasks()) == 1

        await stream.aclose()
        await asyncio.sleep(0)
        assert gate.cancelled >= 1
        assert _prefetch_tasks() == []

    async def test_skip_prefix_crossing_chunk_prefetches_only_suffix(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨批 skip_prefix 不会回建前缀,后缀仍按批预取。"""

        decisions = _month_end_decisions()
        started: list[date] = []
        real_load = FrozenInputLoader.load_context

        async def traced_load(
            loader: FrozenInputLoader,
            manifest: ResearchRunManifest,
            *,
            decision_at: Any,
            execution_at: Any,
        ) -> Any:
            started.append(decision_at.date())
            return await real_load(
                loader,
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", traced_load)
        provider = await _build_release(tmp_path)
        stream = cast(
            AsyncGenerator[DecisionLoadContext, None],
            iter_decision_load_contexts(
                _multi_period_manifest(),
                release_provider_factory=_factory(provider),
                snapshot_provider=_noop_snapshot_provider,
                skip_prefix=3,
            ),
        )
        contexts = [item async for item in stream]
        assert [item.context.business_date for item in contexts] == decisions[3:]
        assert sorted(started) == sorted(decisions[3:])

    async def test_only_one_prefetch_task_exists_at_a_time(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """下一批未完成时不会再创建第三批预取 task。"""

        decisions = _month_end_decisions()
        monkeypatch.setattr(signal_engine, "_DECISION_LOAD_CHUNK", 2)
        gate = _LoadGate(set(decisions[2:4]))
        real_load = FrozenInputLoader.load_context

        async def wrapped_load(
            loader: FrozenInputLoader,
            manifest: ResearchRunManifest,
            *,
            decision_at: Any,
            execution_at: Any,
        ) -> Any:
            if decision_at.date() in gate.blocked_dates:
                gate.started_event.set()
                await gate.release.wait()
            return await real_load(
                loader,
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", wrapped_load)
        _, _, stream = await _make_stream(tmp_path)
        await stream.__anext__()
        await asyncio.wait_for(gate.started_event.wait(), timeout=2)
        assert len(_prefetch_tasks()) == 1
        # 第二批仍在加载时,不可能已经创建第三批;这同时钉住双批
        # 内存预算的任务侧上限。
        await asyncio.sleep(0)
        assert len(_prefetch_tasks()) == 1
        gate.release.set()
        contexts = [item async for item in stream]
        assert len(contexts) == len(decisions) - 1
