"""独立 PostgreSQL 上的冻结发布包导入和幂等回归。"""

from __future__ import annotations

import asyncio
import os
from time import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from scripts.build_research_starter import build
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from finboard_api.deps import get_db_session
from finboard_api.routes.data_bundles import router
from finboard_app import release_bundle as bundle_service
from finboard_app.release_bundle import import_registered_bundle, preview_registered_bundle
from finboard_app.release_bundle_jobs import BUNDLE_JOB_KIND, ReleaseBundleExecutor
from finboard_app.starter_data import _read_bundle
from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_data.release_bundle import (
    BundleCalendarDay,
    ReleaseBundleError,
    export_release_bundle,
)
from finboard_persistence import ResearchDatasetReleaseRepository, session_factory
from finboard_persistence.models import (
    InstrumentModel,
    ResearchDatasetReleaseInstrumentModel,
    TradeCalModel,
)
from finboard_persistence.research_audit_repo import ResearchAuditRepository

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.asyncio
async def test_bundle_import_registers_all_indexes_and_keeps_existing_metadata(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    old_zip = tmp_path / "legacy.zip"
    await build(old_zip)
    source = tmp_path / "source"
    release, dates = _read_bundle(old_zip, source)
    first = release.instruments[0]
    days = tuple(BundleCalendarDay("SSE", day, True, "bundle-fixture") for day in sorted(dates))
    archive = tmp_path / "release.zip"
    export_release_bundle(archive, source, [release], days)
    target = tmp_path / "receiver"
    await db_session.execute(insert(TradeCalModel).values(
        exchange=days[0].exchange, cal_date=days[0].cal_date, is_open=False, source="prior",
    ))
    await db_session.commit()

    sessions = session_factory(_engine)
    preview = await preview_registered_bundle(db_session, archive, root=target)
    assert preview["calendar_conflict"] is True
    with pytest.raises(ReleaseBundleError, match="bundle_calendar_conflict"):
        await import_registered_bundle(sessions, archive, root=target)
    assert await ResearchDatasetReleaseRepository(db_session).get(release.release_id) is None
    assert not (target / release.release_id).exists()
    row = await db_session.get(TradeCalModel, (days[0].exchange, days[0].cal_date))
    assert row is not None
    row.is_open = True
    await db_session.execute(insert(InstrumentModel).values(
        code=first.code, name="Receiver metadata", market=first.market.value,
        instrument_type=first.instrument_type.value,
    ))
    await db_session.commit()

    imports = await asyncio.gather(
        import_registered_bundle(sessions, archive, root=target),
        import_registered_bundle(sessions, archive, root=target),
    )
    assert all(item.releases[0].release_checksum == release.release_checksum for item in imports)
    registered = await ResearchDatasetReleaseRepository(db_session).get(release.release_id)
    assert registered is not None
    assert registered.release_checksum == release.release_checksum
    count = await db_session.scalar(
        select(func.count()).select_from(ResearchDatasetReleaseInstrumentModel).where(
            ResearchDatasetReleaseInstrumentModel.release_id == release.release_id,
        ),
    )
    assert count == len(release.instruments)
    fallback = await db_session.scalar(select(InstrumentModel).where(InstrumentModel.code == first.code))
    assert fallback is not None
    assert fallback.name == "Receiver metadata"
    assert (target / release.release_id / "manifest.json").read_bytes() == (
        source / release.release_id / "manifest.json"
    ).read_bytes()


@pytest.mark.asyncio
async def test_bundle_recovers_if_interrupted_after_file_activation(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy.zip"
    await build(legacy)
    fixture = tmp_path / "fixture"
    release, dates = _read_bundle(legacy, fixture)
    archive = tmp_path / "portable.zip"
    calendar = tuple(BundleCalendarDay("SSE", day, True, "fixture") for day in sorted(dates))
    export_release_bundle(archive, fixture, [release], calendar)
    target = tmp_path / "receiver"
    sessions = session_factory(_engine)
    activate = bundle_service._activate_files

    def interrupt_after_activation(*args: Any) -> None:
        activate(*args)
        raise OSError("simulated worker interruption")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(bundle_service, "_activate_files", interrupt_after_activation)
        with pytest.raises(OSError, match="simulated worker interruption"):
            await import_registered_bundle(sessions, archive, root=target)

    assert (target / release.release_id / "manifest.json").is_file()
    assert await ResearchDatasetReleaseRepository(db_session).get(release.release_id) is None
    assert not list(target.glob(".bundle-import-*"))

    artifact = target / release.release_id / release.instruments[0].artifact_path
    expected = artifact.read_bytes()
    for damaged in (None, b"bad parquet"):
        artifact.unlink(missing_ok=True)
        if damaged is not None:
            artifact.write_bytes(damaged)
        preview = await preview_registered_bundle(db_session, archive, root=target)
        assert preview["release_conflicts"] == [release.release_id]
        with pytest.raises(ReleaseBundleError, match="bundle_release_conflict"):
            await import_registered_bundle(sessions, archive, root=target)
        assert await ResearchDatasetReleaseRepository(db_session).get(release.release_id) is None
    artifact.write_bytes(expected)

    result = await import_registered_bundle(sessions, archive, root=target)
    assert result.releases[0].release_checksum == release.release_checksum
    registered = await ResearchDatasetReleaseRepository(db_session).require_usable(release.release_id)
    assert registered.release_checksum == release.release_checksum


@pytest.mark.asyncio
async def test_web_preview_then_explicit_import_runs_data_worker(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "transferred.zip"
    old_zip = tmp_path / "legacy.zip"
    await build(old_zip)
    source = tmp_path / "source"
    release, dates = _read_bundle(old_zip, source)
    export_release_bundle(
        archive, source, [release],
        [BundleCalendarDay("SSE", day, True, "fixture") for day in sorted(dates)],
    )
    target = tmp_path / "receiver-web"
    monkeypatch.setenv("FINBOARD_DATA_RELEASE_ROOT", str(target))
    monkeypatch.setenv("FINBOARD_DATA_BUNDLE_ROOT", str(tmp_path / "managed-archives"))
    api = FastAPI()
    api.state.session_maker = session_factory(_engine)
    api.include_router(router)

    async def database_session() -> AsyncSession:
        return db_session

    api.dependency_overrides[get_db_session] = database_session
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://localhost") as client:
        pending_root = tmp_path / "managed-archives" / "pending"
        pending_root.mkdir(parents=True)
        expired = pending_root / ("RB-" + "1" * 32 + ".zip")
        expired.write_bytes(b"expired")
        past = time() - 48 * 60 * 60
        os.utime(expired, (past, past))
        with monkeypatch.context() as quota:
            quota.setattr(bundle_service, "_PREVIEW_BUDGET_BYTES", 4)
            over_budget = await client.post(
                "/api/data/bundles/preview",
                content=archive.read_bytes(),
                headers={"Content-Type": "application/zip"},
            )
        assert over_budget.status_code == 507
        assert over_budget.json()["detail"] == "bundle_preview_capacity"
        assert not list(pending_root.iterdir())
        response = await client.post(
            "/api/data/bundles/preview",
            content=archive.read_bytes(),
            headers={"Content-Type": "application/zip"},
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["release_conflicts"] == []
        assert preview["releases"][0]["release_id"] == release.release_id
        assert (pending_root / f"{preview['bundle_id']}.zip").is_file()
        assert await ResearchDatasetReleaseRepository(db_session).get(release.release_id) is None

        blocked = await client.post(
            "/api/data/bundles/imports",
            json={"bundle_id": preview["bundle_id"], "checksum": "0" * 64},
        )
        assert blocked.status_code == 422
        assert blocked.json()["detail"] == "bundle_preview_changed"
        assert (pending_root / f"{preview['bundle_id']}.zip").is_file()

        response = await client.post(
            "/api/data/bundles/imports",
            json={"bundle_id": preview["bundle_id"], "checksum": preview["checksum"]},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["kind"] == BUNDLE_JOB_KIND
        assert body["status"] == "queued"
        assert not list(pending_root.iterdir())

    reported = []

    async def progress(done: int, total: int | None, phase: str | None) -> None:
        reported.append((done, total, phase))

    result = await ReleaseBundleExecutor(session_factory(_engine), worker_id="test559").execute(
        JobRecord(body["job_id"], body["kind"], body["queue"], body["payload"], 1, 3, "test559"),
        progress,
    )
    assert result.status == "succeeded"
    assert reported[-1] == (2, 2, "bundle_complete")
    assert await ResearchDatasetReleaseRepository(db_session).get(release.release_id) is not None
    recorded = await ResearchAuditRepository(db_session).list_events(entity_id=preview["bundle_id"])
    assert {(row.operation, row.outcome) for row in recorded} >= {
        ("bundle.preview", "succeeded"), ("bundle.import", "rejected"), ("bundle.import", "succeeded"),
    }
