"""沙箱因子快照落库集成测试(issue #217,需 PostgreSQL)。

覆盖:run 锚定快照(dataset_release_id=None + source_run_id)的 publish/get
幂等往返、research_code_runs.output_snapshot_id 回填、
sandbox_snapshot_dataset_release_ids 的发布集合推导。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finboard_backtest.research_sandbox.factor_publish import (
    QualityGateError,
    build_factor_snapshot,
    check_output_quality,
    sandbox_snapshot_dataset_release_ids,
)
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeRunRepository,
)

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


def _snapshot(run_id: str = "RCR-integration0000001"):
    scores = {f"S{i}": float(i) for i in range(10)}
    quality = check_output_quality(
        scores, universe_size=10, max_nan_ratio=0.5, min_coverage=0.5
    )
    assert quality.passed
    return build_factor_snapshot(
        factor_artifact_name="mom20",
        run_id=run_id,
        decision_at=_TS,
        commit="a" * 40,
        mount_manifest_checksum="m" * 64,
        scores=scores,
        quality=quality,
    )


class TestSandboxFactorSnapshotPersistence:
    async def test_publish_and_get_roundtrip(self, db_session):
        repo = FeatureSnapshotRepository(db_session)
        snapshot = _snapshot()
        row = await repo.publish(snapshot)
        await db_session.commit()
        assert row.source_run_id == "RCR-integration0000001"
        assert row.dataset_release_id is None

        restored = await repo.get(snapshot.snapshot_id)
        assert restored is not None
        assert restored.source_run_id == "RCR-integration0000001"
        assert restored.checksum == snapshot.checksum
        assert {o.feature_name for o in restored.observations} == {"u_mom20"}

        # 幂等重发同一快照不重复落库
        again = await repo.publish(snapshot)
        assert again.snapshot_id == row.snapshot_id

    async def test_run_output_snapshot_backfill(self, db_session):
        snapshot = _snapshot()
        run_repo = ResearchCodeRunRepository(db_session)
        run = await run_repo.create(
            kind="factor",
            name="mom20",
            commit="a" * 40,
            code_checksum="deadbeef",
            dataset_release_ids=["DR-a", "DR-b"],
            dataset_release_checksums={"DR-a": "1", "DR-b": "2"},
            decision_at=_TS,
            image="finboard-research-sandbox:0.1.0",
            image_digest="sha256:abc",
            artifact_dir="data_cache/research_sandbox/x",
        )
        done = await run_repo.mark_terminal(
            run.run_id,
            status="succeeded",
            exit_code=0,
            metrics={"coverage": 1.0},
            scores_checksum="ab" * 32,
            output_snapshot_id=snapshot.snapshot_id,
        )
        await db_session.commit()
        assert done.output_snapshot_id == snapshot.snapshot_id

    async def test_release_ids_resolution_for_gate(self, db_session):
        run_repo = ResearchCodeRunRepository(db_session)
        run = await run_repo.create(
            kind="factor",
            name="mom20",
            commit="a" * 40,
            code_checksum="deadbeef",
            dataset_release_ids=["DR-a", "DR-b"],
            dataset_release_checksums={},
            decision_at=_TS,
            image="img",
            image_digest="sha256:x",
            artifact_dir="dir",
        )
        snapshot = _snapshot(run_id=run.run_id)
        await FeatureSnapshotRepository(db_session).publish(snapshot)
        await db_session.commit()
        resolved = await sandbox_snapshot_dataset_release_ids(db_session, snapshot)
        assert resolved == {"DR-a", "DR-b"}

    async def test_missing_anchor_rejected(self, db_session):
        snapshot = _snapshot(run_id="RCR-nonexistent0000001")
        # 不落 run 记录,直接解析 → fail-visible
        with pytest.raises(QualityGateError, match="不存在"):
            await sandbox_snapshot_dataset_release_ids(db_session, snapshot)
