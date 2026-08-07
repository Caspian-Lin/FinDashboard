"""在冻结发布上测量特征快照进程池耗时。"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from finboard_backtest.factor_lab import build_price_feature_snapshot
from finboard_data.releases import FrozenReleaseProvider


async def _run(release_id: str, workers: int) -> None:
    provider = FrozenReleaseProvider(
        release_root=Path("data_releases"),
        release_id=release_id,
        max_concurrency=workers,
    )
    release = provider.release
    decision_at = datetime(
        2026,
        8,
        1,
        16,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    started = time.perf_counter()
    heartbeat_gaps: list[float] = []
    heartbeat_last = time.perf_counter()
    heartbeat_running = True

    async def heartbeat() -> None:
        nonlocal heartbeat_last
        while heartbeat_running:
            await asyncio.sleep(0.25)
            now = time.perf_counter()
            gap = now - heartbeat_last
            heartbeat_gaps.append(gap)
            if gap > 1.0:
                print(
                    f"event_loop_gap={gap:.3f}s elapsed={now - started:.1f}s",
                    flush=True,
                )
            heartbeat_last = now

    heartbeat_task = asyncio.create_task(heartbeat())

    def on_progress(_code: str, completed: int, total: int) -> None:
        now = time.perf_counter()
        if completed == total or completed % 500 == 0:
            print(
                f"progress={completed}/{total} "
                f"elapsed={now - started:.1f}s",
                flush=True,
            )
    try:
        snapshot = await build_price_feature_snapshot(
            provider=provider,
            decision_at=decision_at,
            code_version="benchmark",
            max_concurrency=workers,
            process_workers=workers,
            on_progress=on_progress,
        )
    finally:
        heartbeat_running = False
        await heartbeat_task

    elapsed = time.perf_counter() - started
    max_gap = max(heartbeat_gaps, default=0.0)
    print(
        f"release={release.release_id} instruments={len(release.instruments)} "
        f"rows={release.row_count} workers={workers} "
        f"elapsed={elapsed:.3f}s observations={len(snapshot.observations)} "
        f"max_event_loop_gap={max_gap:.3f}s",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", default="a-share-bars-20260804-v1")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(_run(args.release_id, args.workers))


if __name__ == "__main__":
    main()
