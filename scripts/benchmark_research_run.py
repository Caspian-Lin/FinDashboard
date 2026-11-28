"""对隔离 PostgreSQL 中的冻结研究运行测量耗时、资源占用和逐产物校验和。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import threading
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
from inspect import signature
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url

from finboard_backtest.background_jobs.executors.research_run import (
    SessionPerOperationResearchRunStore,
    default_store_factory,
)
from finboard_backtest.research_run import (
    DecisionBundle,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunStatus,
    ResearchStrategyAdapter,
    manifest_from_json,
)
from finboard_backtest.research_run.runner import DECISION_STAGE_COUNT
from finboard_backtest.research_run.signal_engine import (
    SIGNAL_ENGINE_STRATEGY_KINDS,
    build_signal_engine_adapter_factory,
)
from finboard_persistence import create_async_engine, session_factory


@contextmanager
def _process_resources() -> Iterator[dict[str, object]]:
    psutil = import_module("psutil")

    process = psutil.Process()
    logical_cpus = len(process.cpu_affinity()) if hasattr(process, "cpu_affinity") else (
        os.cpu_count() or 1
    )
    result: dict[str, object] = {
        "logical_cpus": logical_cpus,
        "host_memory_bytes": psutil.virtual_memory().total,
        "rss_sample_seconds": 0.25,
    }
    cpu_by_pid: dict[int, float] = {}
    peak_rss = 0
    ended = threading.Event()

    def sample() -> None:
        nonlocal peak_rss
        while True:
            total_rss = 0
            for member in [process, *process.children(recursive=True)]:
                try:
                    with member.oneshot():
                        total_rss += member.memory_info().rss
                        cpu = member.cpu_times()
                        cpu_by_pid[member.pid] = cpu.user + cpu.system
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            peak_rss = max(peak_rss, total_rss)
            if ended.wait(0.25):
                return

    cpu_start = process.cpu_times()
    started = time.perf_counter()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        yield result
    finally:
        ended.set()
        sampler.join()
        cpu_end = process.cpu_times()
        total_cpu = (
            cpu_end.user + cpu_end.system - cpu_start.user - cpu_start.system
            + sum(seconds for pid, seconds in cpu_by_pid.items() if pid != process.pid)
        )
        elapsed = time.perf_counter() - started
        result["peak_process_tree_rss_bytes"] = peak_rss
        result["process_tree_cpu_seconds_sampled"] = round(total_cpu, 3)
        result["mean_logical_cpu_utilization_percent_sampled"] = round(
            100 * total_cpu / elapsed / logical_cpus, 3
        )


class _InterruptAfter:
    def __init__(self, adapter: ResearchStrategyAdapter, count: int) -> None:
        self.adapter = adapter
        self.count = count

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    def can_defer_resume_features(self) -> bool:
        supports = getattr(self.adapter, "can_defer_resume_features", None)
        return bool(callable(supports) and supports())

    async def decisions(self, manifest: ResearchRunManifest) -> AsyncIterator[DecisionBundle]:
        source = self.adapter.decisions(manifest)
        produced = 0
        try:
            async for decision in source:
                yield decision
                produced += 1
                if produced == self.count:
                    raise ResearchRunInterruptedError("benchmark checkpoint")
        finally:
            if isinstance(source, AsyncGenerator):
                await source.aclose()


async def _benchmark(args: argparse.Namespace) -> dict[str, object]:
    url = make_url(os.environ["FINBOARD_BENCH_DB_URL"])
    if not url.database or not url.database.startswith("findashboard_bench_"):
        raise ValueError("隔离库名必须以 findashboard_bench_ 开头")
    raw = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest = manifest_from_json(raw)
    if manifest.strategy_kind not in SIGNAL_ENGINE_STRATEGY_KINDS:
        raise ValueError("请使用内置研究规格的 manifest")
    run_id = f"RR-{sha256(args.run_key.encode()).hexdigest()[:24]}"
    manifest = replace(
        manifest, run_id=run_id, idempotency_key=f"bench:{args.run_key}",
        replay_of_run_id=None, replay_source_status=None,
    )

    engine = create_async_engine(url.render_as_string(hide_password=False))
    session_maker = session_factory(engine)
    store = SessionPerOperationResearchRunStore(session_maker, default_store_factory)
    try:
        source = await store.get(run_id)
        if args.resume and (
            source is None or source.status is not ResearchRunStatus.INTERRUPTED
            or source.manifest.checksum != manifest.checksum
        ):
            raise ValueError("续跑需要同一冻结 manifest 的中断检查点")
        if not args.resume and source is not None:
            raise ValueError("此运行键已存在")

        baseline = len(await store.list_artifact_digests(run_id))
        if baseline % DECISION_STAGE_COUNT:
            raise ValueError("基准检查点必须由完整决策组组成")
        prefix = baseline // DECISION_STAGE_COUNT
        if args.interrupt_after is not None and args.interrupt_after <= prefix:
            raise ValueError("中断决策序号必须晚于当前前缀")

        options: dict[str, Any] = {}
        if "prefetch_next_batch" in signature(build_signal_engine_adapter_factory).parameters:
            options["prefetch_next_batch"] = args.prefetch
        elif not args.prefetch:
            raise ValueError("当前基线不支持关闭预取")
        factory = build_signal_engine_adapter_factory(
            session_maker,
            release_root=args.release_root,
            settings_factory=lambda: SimpleNamespace(
                factor_series_artifact_root=args.factor_series_root,
                research_price_feature_process_workers=args.process_workers,
            ),
            **options,
        )
        adapter = factory(manifest)
        if args.interrupt_after is not None:
            adapter = _InterruptAfter(adapter, args.interrupt_after)

        committed_at: list[float] = []
        started = time.perf_counter()

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            if phase is not None and phase.startswith("research_run:ledger#"):
                committed_at.append(time.perf_counter() - started)
                if len(committed_at) % 20 == 0:
                    print(f"已提交 {len(committed_at)} 期", file=sys.stderr, flush=True)

        with _process_resources() as resources:
            record = await ResearchRunCoordinator(store).execute(
                manifest, adapter, progress=progress
            )
        elapsed = time.perf_counter() - started
        digests = await store.list_artifact_digests(run_id)
        intervals: dict[int, list[float]] = defaultdict(list)
        for i, (before, after) in enumerate(pairwise(committed_at), start=1):
            intervals[i % 4].append(after - before)
        async with session_maker() as session:
            stored_bytes = (
                await session.execute(
                    text("SELECT COALESCE(sum(pg_column_size(payload)), 0) "
                         "FROM research_run_artifacts WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    return {
        "host": platform.platform(),
        "python": platform.python_version(),
        "cache_state": args.cache_state,
        "prefetch": args.prefetch,
        "feature_process_workers": args.process_workers,
        "inputs": manifest.input_checksum,
        "run_id": run_id,
        "status": record.status.value,
        "error_code": record.error_code,
        "error_summary": record.error_summary,
        "prefix_decisions": prefix,
        "decision_count": len(committed_at),
        "first_new_decision_seconds": round(committed_at[prefix], 3)
        if len(committed_at) > prefix else None,
        "ledger_interval_mean_seconds_by_slot": {
            str(slot): round(sum(values) / len(values), 6) for slot, values in intervals.items()
        },
        "elapsed_seconds": round(elapsed, 3),
        "resources": resources,
        "stored_payload_bytes": int(stored_bytes),
        "timing": record.timing,
        "result_checksum": record.result_checksum,
        "artifact_digests": [
            (item.stage.value, (item.decision_id or "").rsplit(":", 1)[-1], item.checksum)
            for item in digests
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--factor-series-root", type=Path, default=Path("data_cache/factor_series"))
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--cache-state", required=True, choices=("cold", "warm", "uncontrolled"))
    parser.add_argument("--prefetch", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--process-workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--interrupt-after", type=int)
    parser.add_argument("--expected-status", choices=("completed", "rejected", "interrupted"))
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.process_workers < 0:
        parser.error("--process-workers must be nonnegative")
    result = asyncio.run(
        _benchmark(args),
        loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
    )
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{result['status']} | {result['elapsed_seconds']}s | {args.output}", flush=True)
    expected = args.expected_status or (
        "completed" if args.interrupt_after is None else "interrupted"
    )
    if result["status"] != expected:
        raise SystemExit(1)
    if args.compare is not None:
        reference = json.loads(args.compare.read_text(encoding="utf-8"))
        for key in ("inputs", "status", "error_code", "result_checksum", "artifact_digests"):
            if json.loads(json.dumps(result[key])) != reference[key]:
                raise SystemExit(f"对照运行的 {key} 不一致")


if __name__ == "__main__":
    main()
