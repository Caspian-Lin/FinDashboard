"""沙箱因子快照落库集成测试(issue #217,需 PostgreSQL)。

覆盖:run 锚定快照(dataset_release_id=None + source_run_id)的 publish/get
幂等往返、research_code_runs.output_snapshot_id 回填、
sandbox_snapshot_dataset_release_ids 的发布集合推导。
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.cli import dev
from finboard_app.config import Settings
from finboard_backtest.research_sandbox.data_mount import build_data_mount
from finboard_backtest.research_sandbox.factor_publish import (
    QualityGateError,
    build_factor_snapshot,
    check_output_quality,
    sandbox_snapshot_dataset_release_ids,
)
from finboard_backtest.research_sandbox.mount_retention import use_mount
from finboard_data import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
    verify_dataset_release,
)
from finboard_data.cache import ParquetCache
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools.research_code_run import run_get
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeRunRepository,
    ResearchDatasetReleaseRepository,
    create_async_engine,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


def _snapshot(run_id: str = "RCR-integration0000001", mount_checksum: str = "m" * 64):
    scores = {f"S{i}": float(i) for i in range(10)}
    quality = check_output_quality(scores, universe_size=10, max_nan_ratio=0.5, min_coverage=0.5)
    assert quality.passed
    return build_factor_snapshot(
        factor_artifact_name="mom20",
        run_id=run_id,
        decision_at=_TS,
        commit="a" * 40,
        mount_manifest_checksum=mount_checksum,
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = "RCR-000000000000000000000546"
    release_id = "DR-mount-retention-546"
    release_root = tmp_path / "releases"
    mount_root = tmp_path / "sandbox"
    workspace = mount_root / run_id
    data = workspace / "data"
    dates = (date(2024, 5, 31), _TS.date(), date(2024, 6, 4))
    monkeypatch.setattr("finboard_data.trading_calendar._CACHE", set(dates))
    instrument = Symbol("TST546.SH", Market.A_SHARE)
    await ParquetCache(tmp_path / "cache").write(
        instrument,
        BarPeriod.D1,
        "qfq",
        [
            Bar(
                symbol=instrument,
                period=BarPeriod.D1,
                timestamp=datetime(day.year, day.month, day.day, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=Decimal("1000"),
                amount=Decimal("10500"),
                source="fixed_sample",
            )
            for day in dates
        ],
    )
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache", release_root=release_root
    ).publish(
        DatasetReleaseSpec(
            release_id=release_id,
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version="mount-retention-v1",
            start_date=dates[0],
            end_date=dates[-1],
            code_version="integration-test",
            required_capabilities=("stock",),
        ),
        [
            ReleaseInstrumentSpec(
                code=str(instrument.code),
                name="研究挂载回归样本",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                asset_class=AssetClass.EQUITY,
                available_at=datetime(2020, 1, 1, tzinfo=UTC),
                list_date=date(2020, 1, 1),
                execution=default_execution_metadata(
                    market=Market.A_SHARE, instrument_type=InstrumentType.STOCK
                ),
            )
        ],
    )
    async with use_mount(data):
        mount = await build_data_mount(
            providers=[FrozenReleaseProvider(release_root=release_root, release_id=release_id)],
            decision_at=_TS,
            out_root=data,
        )
    manifest = mount.manifest_path.read_bytes()
    bars = pq.read_table(data / "bars.parquet")
    assert mount.datasets[0].max_data_date == dates[0]
    assert bars.num_rows == 1
    (workspace / "out").mkdir()
    pq.write_table(pa.table({"symbol": ["S0"], "score": [1.0]}), workspace / "out/scores.parquet")
    snapshot = _snapshot(run_id=run_id, mount_checksum=mount.manifest_checksum)
    async with session_factory(_engine)() as session:
        await ResearchDatasetReleaseRepository(session).publish(release)
        repo = ResearchCodeRunRepository(session)
        await repo.create(
            run_id=run_id,
            kind="factor",
            name="mom20",
            commit="a" * 40,
            code_checksum="b" * 64,
            dataset_release_ids=[release_id],
            dataset_release_checksums={release_id: release.release_checksum},
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
            mount_manifest_checksum=mount.manifest_checksum,
            output_snapshot_id=snapshot.snapshot_id,
        )
        await session.commit()

    now = time.time()
    for path in (data / "bars.parquet", mount.manifest_path, data / ".mount-use"):
        os.utime(path, (now, now - 31 * 86400))
    settings = Settings(
        research_sandbox_workspace_root=str(mount_root),
        research_sandbox_cleanup_on_dev=True,
        mcp_enabled=False,
    )
    ctx = MagicMock(obj=settings)
    with (
        patch("finboard_app.cli.shutil.which", return_value="npm"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli.sweep_stale_workers", return_value=[]),
        patch("finboard_app.cli._start_frontend_when_api_ready"),
        patch("finboard_app.cli._spawn_dev_worker", return_value=None),
        patch("finboard_app.cli.serve") as serve,
    ):
        dev(ctx, web_dir=tmp_path)
    serve.assert_called_once()
    assert "回收 1 个挂载的 1 个文件" in capsys.readouterr().out
    assert not (data / "bars.parquet").is_file()
    assert mount.manifest_path.read_bytes() == manifest

    @asynccontextmanager
    async def restarted() -> AsyncIterator[McpAppContext]:
        engine = create_async_engine(_engine.url.render_as_string(hide_password=False))
        try:
            yield McpAppContext(
                settings=settings,
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
        assert detail.data["mount_manifest_checksum"] == mount.manifest_checksum
        async with app.session_maker() as session:
            restored = await FeatureSnapshotRepository(session).get(snapshot.snapshot_id)
            assert restored is not None
            assert restored.checksum == snapshot.checksum

    provider = FrozenReleaseProvider(release_root=release_root, release_id=release_id)
    assert (
        verify_dataset_release(release_root / release_id).release_checksum
        == release.release_checksum
    )
    reloaded = tmp_path / "rebuilt-mount"
    async with use_mount(reloaded):
        rebuilt = await build_data_mount(providers=[provider], decision_at=_TS, out_root=reloaded)
    assert rebuilt.manifest_dict() == mount.manifest_dict()
    assert pq.read_table(reloaded / "bars.parquet").equals(bars)
