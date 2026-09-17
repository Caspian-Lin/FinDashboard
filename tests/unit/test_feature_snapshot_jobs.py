"""特征快照后台任务状态机测试。"""

from __future__ import annotations

import asyncio

import pytest

from finboard_api.feature_snapshot_jobs import (
    FeatureSnapshotJobConflictError,
    FeatureSnapshotJobManager,
)


@pytest.mark.asyncio
async def test_job_reports_progress_eta_and_terminal_snapshot() -> None:
    manager = FeatureSnapshotJobManager()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def runner(job):
        started.set()
        job.update_progress(2)
        await release.wait()
        finished.set()
        return "FS-TEST"

    job = manager.start(total_symbols=4, runner=runner)
    assert job.as_dict()["status"] == "queued"
    await started.wait()

    running = job.as_dict()
    assert running["status"] == "running"
    assert running["completed_symbols"] == 2
    assert running["estimated_remaining_seconds"] is not None

    with pytest.raises(FeatureSnapshotJobConflictError):
        manager.start(total_symbols=1, runner=runner)

    release.set()
    await finished.wait()
    await asyncio.sleep(0)

    assert job.status == "succeeded"
    assert job.snapshot_id == "FS-TEST"
    assert job.completed_symbols == 4
    assert job.as_dict()["estimated_remaining_seconds"] is None
    await manager.close()


@pytest.mark.asyncio
async def test_job_converts_runner_failure_to_failed_state() -> None:
    manager = FeatureSnapshotJobManager()
    finished = asyncio.Event()

    async def runner(_job):
        try:
            raise RuntimeError("冻结发布文件损坏")
        finally:
            finished.set()

    job = manager.start(total_symbols=2, runner=runner)
    await finished.wait()
    await asyncio.sleep(0)

    assert job.status == "failed"
    assert job.error == "冻结发布文件损坏"
    assert job.finished_at is not None
    await manager.close()
