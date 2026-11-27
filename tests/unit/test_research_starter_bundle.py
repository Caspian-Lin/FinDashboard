from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scripts.build_research_starter import build

from finboard_app.starter_data import STARTER_ID, _read_bundle
from finboard_data.releases import (
    _release_checksum,
    _write_manifest,
    verify_dataset_release,
)


@pytest.mark.asyncio
async def test_rejects_duplicate_parquet_row_even_with_valid_file_checksums(tmp_path: Path) -> None:
    bundle = tmp_path / "sample.zip"
    await build(bundle)
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(unpacked)
    folder = unpacked / STARTER_ID
    release = verify_dataset_release(folder)
    _read_bundle(bundle, tmp_path / "valid")

    item = release.instruments[0]
    parquet = folder / item.artifact_path
    rows = pq.read_table(parquet)
    pq.write_table(pa.concat_tables([rows, rows.slice(0, 1)]), parquet)
    changed = replace(item, artifact_checksum=hashlib.sha256(parquet.read_bytes()).hexdigest())
    release = replace(release, instruments=(changed, *release.instruments[1:]))
    release = replace(release, release_checksum=_release_checksum(release))
    _write_manifest(folder, release)
    assert verify_dataset_release(folder).release_checksum == release.release_checksum

    index_file = unpacked / "starter.json"
    index = json.loads(index_file.read_bytes())
    index["release_checksum"] = release.release_checksum
    for name in index["files"]:
        index["files"][name] = hashlib.sha256((unpacked / name).read_bytes()).hexdigest()
    index_file.write_text(json.dumps(index), encoding="utf-8")
    with zipfile.ZipFile(bundle, "w") as archive:
        for name in sorted([*index["files"], "starter.json"]):
            archive.write(unpacked / name, name)

    with pytest.raises(ValueError, match="starter_bundle_calendar_mismatch"):
        _read_bundle(bundle, tmp_path / "invalid")
