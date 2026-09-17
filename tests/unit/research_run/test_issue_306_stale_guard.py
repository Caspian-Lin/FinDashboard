"""issue #306:run 误标防治 —— ``mark_stale_running_as_interrupted`` 属主检查。

锁定四组行为:

* 注入属主探针且探针判定 job 仍被活跃 worker 持有(True)→ run 跳过误标,
  保持 RUNNING(现状回归:无检查时 worker B 启动会误标 worker A 活跃 run);
* 探针判定真孤儿(False:job 终态 / lease 过期 / 行不存在)→ 照旧标
  INTERRUPTED,恢复通道语义不变;
* 探针抛错 → fail-closed 跳过本轮收敛,不误标(瞬时 DB 故障不把活跃 run
  打成 interrupted);
* 不注入探针(默认 None)→ 旧行为:全部 RUNNING 标 interrupted(纯内存
  测试路径兼容)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from finboard_backtest.research_run import (
    InMemoryResearchRunStore,
    JobOwnershipProbe,
    ResearchRunCoordinator,
    ResearchRunRecord,
    ResearchRunStatus,
)

pytestmark = pytest.mark.asyncio


def _probe(
    verdicts: dict[str, bool],
    *,
    calls: list[str] | None = None,
) -> JobOwnershipProbe:
    async def probe(job_id: str) -> bool:
        if calls is not None:
            calls.append(job_id)
        return verdicts[job_id]

    return probe


def _exploding_probe() -> JobOwnershipProbe:
    async def probe(job_id: str) -> bool:
        raise RuntimeError("probe boom")

    return probe


async def _seed_running_run(
    store: InMemoryResearchRunStore,
    manifest_factory,
    *,
    run_id: str,
    job_id: str | None,
) -> None:
    manifest = manifest_factory(run_id=run_id, idempotency_key=f"idem-{run_id}")
    await store.create_or_get(manifest)
    await store.transition(
        manifest.run_id,
        expected=frozenset({ResearchRunStatus.QUEUED}),
        target=ResearchRunStatus.RUNNING,
    )
    record = await store.get(manifest.run_id)
    assert record is not None
    record.job_id = job_id


async def _recovered_ids(
    store: InMemoryResearchRunStore,
    *,
    probe: Callable[[str], Awaitable[bool]] | None,
) -> tuple[list[ResearchRunRecord], set[str]]:
    """跑一次恢复,返回 (被收敛的记录, 保持 RUNNING 的 run_id 集)。"""

    coordinator = ResearchRunCoordinator(store)
    recovered = await coordinator.mark_stale_running_as_interrupted(
        job_ownership=probe
    )
    still_running = set()
    for run_id in ("RR-owned-run-000001", "RR-orphan-run-000002"):
        record = await store.get(run_id)
        if record is None:
            continue  # 本用例未播种该 run
        if record.status is ResearchRunStatus.RUNNING:
            still_running.add(record.manifest.run_id)
    return recovered, still_running


async def test_owned_job_run_is_skipped_not_mislabeled(manifest_factory) -> None:
    """worker A 活跃执行中(job lease/heartbeat 活跃)→ worker B 启动不误标。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-owned-run-000001", job_id="BJ-OWNED"
    )
    recovered, still_running = await _recovered_ids(
        store,
        probe=_probe({"BJ-OWNED": True}),
    )
    assert list(recovered) == []
    assert still_running == {"RR-owned-run-000001"}
    record = await store.get("RR-owned-run-000001")
    assert record is not None
    assert record.status is ResearchRunStatus.RUNNING
    assert record.error_code is None


async def test_orphan_job_run_still_marked_interrupted(manifest_factory) -> None:
    """真孤儿(job 终态 / 不存在 → 探针 False)照旧标 INTERRUPTED。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-orphan-run-000002", job_id="BJ-DEAD"
    )
    recovered, still_running = await _recovered_ids(
        store,
        probe=_probe({"BJ-DEAD": False}),
    )
    assert len(recovered) == 1
    assert recovered.pop().status is ResearchRunStatus.INTERRUPTED
    assert still_running == set()
    record = await store.get("RR-orphan-run-000002")
    assert record is not None
    assert record.status is ResearchRunStatus.INTERRUPTED
    assert record.error_code == "process_restart"
    # issue #305 恢复承诺保持:replay 通道文案可见。
    assert record.error_summary is not None
    assert "replay" in record.error_summary


async def test_probe_only_runs_for_runs_with_job_id(manifest_factory) -> None:
    """无 job_id 的 run(历史行 / 直连执行)不经过探针,直接标 interrupted。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-orphan-run-000002", job_id=None
    )
    calls: list[str] = []
    recovered, still_running = await _recovered_ids(
        store,
        probe=_probe({}, calls=calls),
    )
    assert calls == []  # 从未调用探针
    assert still_running == set()
    assert recovered


async def test_probe_error_is_fail_closed_skip(manifest_factory) -> None:
    """探针抛错 → 跳过该 run,不误标(fail-closed),其余 run 照常收敛。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-owned-run-000001", job_id="BJ-OWNED"
    )
    recovered, still_running = await _recovered_ids(
        store,
        probe=_exploding_probe(),
    )
    assert list(recovered) == []
    assert still_running == {"RR-owned-run-000001"}


async def test_no_probe_keeps_legacy_mark_all_behavior(manifest_factory) -> None:
    """不注入探针(默认 None)保持旧行为:全部 RUNNING 标 interrupted。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-owned-run-000001", job_id="BJ-OWNED"
    )
    coordinator = ResearchRunCoordinator(store)
    recovered = await coordinator.mark_stale_running_as_interrupted()
    assert len(recovered) == 1
    assert recovered[0].status is ResearchRunStatus.INTERRUPTED


async def test_mixed_ownership_batch(manifest_factory) -> None:
    """批量恢复:活跃属主跳过 + 真孤儿收敛,互不影响。"""

    store = InMemoryResearchRunStore()
    await _seed_running_run(
        store, manifest_factory, run_id="RR-owned-run-000001", job_id="BJ-OWNED"
    )
    await _seed_running_run(
        store, manifest_factory, run_id="RR-orphan-run-000002", job_id="BJ-DEAD"
    )
    coordinator = ResearchRunCoordinator(store)
    recovered = await coordinator.mark_stale_running_as_interrupted(
        job_ownership=_probe({"BJ-OWNED": True, "BJ-DEAD": False})
    )
    assert [record.manifest.run_id for record in recovered] == ["RR-orphan-run-000002"]
    owned = await store.get("RR-owned-run-000001")
    orphan = await store.get("RR-orphan-run-000002")
    assert owned is not None
    assert owned.status is ResearchRunStatus.RUNNING
    assert orphan is not None
    assert orphan.status is ResearchRunStatus.INTERRUPTED
