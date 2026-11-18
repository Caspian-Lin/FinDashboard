"""沙箱因子快照落库集成测试(issue #217,需 PostgreSQL)。

覆盖:run 锚定快照(dataset_release_id=None + source_run_id)的 publish/get
幂等往返、research_code_runs.output_snapshot_id 回填、
sandbox_snapshot_dataset_release_ids 的发布集合推导。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.config import Settings
from finboard_backtest.research_sandbox.factor_publish import (
    QualityGateError,
    build_factor_snapshot,
    check_output_quality,
    sandbox_snapshot_dataset_release_ids,
)
from finboard_backtest.research_sandbox.mount_retention import collect_idle_mounts, use_mount
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools.research_code_run import run_get
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeRunRepository,
    create_async_engine,
    session_factory,
)

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


def _snapshot(run_id: str = "RCR-integration0000001"):
    scores = {f"S{i}": float(i) for i in range(10)}
    quality = check_output_quality(scores, universe_size=10, max_nan_ratio=0.5, min_coverage=0.5)
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


async def test_reopen_history_after_dev_mount_reclamation(
    _engine: AsyncEngine,  # noqa: PT019
    tmp_path: Path,
) -> None:
    run_id = "RCR-000000000000000000000546"
    workspace = tmp_path / run_id
    data = workspace / "data"
    with use_mount(data):
        (data / "bars.parquet").write_bytes(b"ephemeral input")
        (data / "mount_manifest.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "decision_at": _TS.isoformat(),
                    "datasets": [
                        {"release_id": "DR-a", "dataset_kind": "bars", "file": "bars.parquet"}
                    ],
                }
            ),
            encoding="utf-8",
        )
    (workspace / "out").mkdir()
    pq.write_table(pa.table({"symbol": ["S0"], "score": [1.0]}), workspace / "out/scores.parquet")
    mount_checksum = "a" * 64
    snapshot = _snapshot(run_id=run_id)
    async with session_factory(_engine)() as session:
        repo = ResearchCodeRunRepository(session)
        await repo.create(
            run_id=run_id,
            kind="factor",
            name="mom20",
            commit="a" * 40,
            code_checksum="b" * 64,
            dataset_release_ids=["DR-a"],
            dataset_release_checksums={"DR-a": "c" * 64},
            decision_at=_TS,
            image="sandbox",
            image_digest="sha256:test",
            artifact_dir=str(workspace),
        )
        await FeatureSnapshotRepository(session).publish(snapshot)
        await repo.mark_terminal(
            run_id,
            status="succeeded",
            exit_code=0,
            mount_manifest_checksum=mount_checksum,
            output_snapshot_id=snapshot.snapshot_id,
        )
        await session.commit()

    now = time.time()
    os.utime(data / ".mount-use", (now, now - 31 * 86400))
    assert collect_idle_mounts(tmp_path, now=now).reclaimed_files == 1
    assert (data / "mount_manifest.json").is_file()

    @asynccontextmanager
    async def restarted() -> AsyncIterator[McpAppContext]:
        engine = create_async_engine(_engine.url.render_as_string(hide_password=False))
        try:
            yield McpAppContext(
                settings=Settings(),
                session_maker=session_factory(engine),
                audit=AuditRecorder(),
                write_tools_enabled=False,
                engine=engine,
                feature_snapshot_jobs=MagicMock(),
            )
        finally:
            await engine.dispose()

    async with restarted() as app:
        detail = await run_get(app, run_id=run_id, view="detail")
        assert detail.status == "ok"
        assert detail.data is not None
        assert detail.data["status"] == "succeeded"
        assert detail.data["scores_preview"] == [{"symbol": "S0", "score": 1.0}]
        assert detail.data["mount_manifest_checksum"] == mount_checksum
        async with app.session_maker() as session:
            restored = await FeatureSnapshotRepository(session).get(snapshot.snapshot_id)
            assert restored is not None
            assert restored.checksum == snapshot.checksum
