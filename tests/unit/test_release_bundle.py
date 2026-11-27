"""冻结发布包的可复核文件契约。"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scripts.build_research_starter import build

from finboard_app.starter_data import _read_bundle
from finboard_data.release_bundle import (
    BUNDLE_INDEX,
    BundleCalendarDay,
    ReleaseBundleError,
    export_release_bundle,
    inspect_release_bundle,
    unpack_release_bundle,
)
from finboard_data.releases import (
    ReleaseDatasetKind,
    _release_checksum,
    _sha256_file,
    _write_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

    from finboard_data.releases import ResearchDatasetRelease


async def _fixture(root: Path) -> tuple[Path, ResearchDatasetRelease, tuple[BundleCalendarDay, ...]]:
    starter = root / "starter.zip"
    await build(starter)
    source = root / "source"
    release, days = _read_bundle(starter, source)
    calendar = tuple(BundleCalendarDay("SSE", day, True, "fixture") for day in sorted(days))
    return source, release, calendar


@pytest.mark.asyncio
async def test_group_roundtrip_keeps_release_checksums_and_known_bar_gaps(tmp_path: Path) -> None:
    source, bars, calendar = await _fixture(tmp_path)
    first = bars.instruments[0]
    artifact = source / bars.release_id / first.artifact_path
    existing = pq.read_table(artifact)
    pq.write_table(pa.concat_tables([existing.slice(0, 1), existing.slice(2)]), artifact)
    item = replace(
        first, row_count=first.row_count - 1, suspended_sessions=first.suspended_sessions + 1,
        artifact_checksum=_sha256_file(artifact),
    )
    bars = replace(bars, instruments=(item, *bars.instruments[1:]))
    bars = replace(bars, release_checksum=_release_checksum(bars))
    _write_manifest(source / bars.release_id, bars)
    metrics_root = source / "test-daily-metrics"
    metrics_root.mkdir()
    date = calendar[0].cal_date
    observed = datetime.combine(date, datetime.min.time(), tzinfo=UTC)
    pq.write_table(
        pa.table({
            "trade_date": [date], "available_at": [observed],
            "observed_at": [observed], "source": ["fixture"], "pb": [1.25],
        }),
        metrics_root / "metric.parquet",
    )
    metric_item = replace(
        item, artifact_path="metric.parquet", row_count=1,
        artifact_checksum=_sha256_file(metrics_root / "metric.parquet"),
    )
    metrics = replace(
        bars, release_id=metrics_root.name, storage_uri=metrics_root.name,
        dataset_name="daily_metrics", dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        fields=("trade_date", "pb"), instruments=(metric_item,),
    )
    metrics = replace(metrics, release_checksum=_release_checksum(metrics))
    _write_manifest(metrics_root, metrics)
    archive = tmp_path / "portable.zip"
    preview = export_release_bundle(archive, source, [bars, metrics], calendar)
    assert inspect_release_bundle(archive).summary() == preview.summary()
    target = tmp_path / "target"
    target.mkdir()
    result = unpack_release_bundle(archive, target)
    assert [r.release_checksum for r in result.releases] == [bars.release_checksum, metrics.release_checksum]
    assert result.calendar == calendar
    assert result.releases[0].instruments[0].suspended_sessions == item.suspended_sessions
    for release in result.releases:
        manifest = f"{release.release_id}/manifest.json"
        assert (target / manifest).read_bytes() == (source / manifest).read_bytes()


@pytest.mark.asyncio
async def test_bad_digest_extra_file_and_quota_fail_closed(tmp_path: Path) -> None:
    source, release, calendar = await _fixture(tmp_path)
    clean = tmp_path / "portable.zip"
    export_release_bundle(clean, source, [release], calendar)
    with pytest.raises(ReleaseBundleError, match="bundle_quota_exceeded"):
        inspect_release_bundle(clean, limit=100)

    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(clean) as original, zipfile.ZipFile(corrupted, "w") as output:
        corrupted_member = f"{release.release_id}/{release.instruments[0].artifact_path}"
        for name in original.namelist():
            data = original.read(name)
            if name == corrupted_member:
                data = bytes([data[0] ^ 1]) + data[1:]
            output.writestr(name, data)
    target = tmp_path / "target"
    target.mkdir()
    assert inspect_release_bundle(corrupted).releases[0].release_id == release.release_id
    with pytest.raises(ReleaseBundleError, match="bundle_file_checksum_mismatch"):
        unpack_release_bundle(corrupted, target)

    with zipfile.ZipFile(clean, "a") as output:
        output.writestr("unexpected.parquet", b"outside manifest")
    with pytest.raises(ReleaseBundleError, match="bundle_invalid_index"):
        inspect_release_bundle(clean)


@pytest.mark.asyncio
async def test_export_rejects_conflict_without_replacing_existing_file(tmp_path: Path) -> None:
    source, release, calendar = await _fixture(tmp_path)
    archive = tmp_path / "portable.zip"
    export_release_bundle(archive, source, [release], calendar)
    with pytest.raises(FileExistsError):
        export_release_bundle(archive, source, [release], calendar)
    with zipfile.ZipFile(archive) as bundle:
        assert json.loads(bundle.read(BUNDLE_INDEX))["releases"][0]["release_id"] == release.release_id
