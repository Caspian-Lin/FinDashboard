from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from scripts.package_research_desktop import archive_image_ids

_APP = "finboard-research-desktop/app:test"
_POSTGRES = "finboard-research-desktop/postgres:test"
_PLATFORM = {"os": "linux", "architecture": "amd64"}


def _blob(files: dict[str, bytes], content: dict[str, object]) -> str:
    data = json.dumps(content).encode()
    hash_value = hashlib.sha256(data).hexdigest()
    files[f"blobs/sha256/{hash_value}"] = data
    return f"sha256:{hash_value}"


def _archive(
    path: Path, *, swap_targets: bool = False, alternate_amd64: bool = False,
) -> dict[str, set[str]]:
    files: dict[str, bytes] = {}
    config_app = _blob(files, {**_PLATFORM, "config": {"User": "finboard"}})
    config_pg = _blob(files, {**_PLATFORM, "config": {"User": "postgres"}})
    alternate = {"os": "linux", "architecture": "amd64" if alternate_amd64 else "arm64"}
    config_alternate = _blob(files, {**alternate, "config": {"User": "other"}})
    app = _blob(files, {"config": {"digest": config_app}})
    postgres_amd64 = _blob(files, {"config": {"digest": config_pg}})
    postgres_alternate = _blob(files, {"config": {"digest": config_alternate}})
    postgres_index = _blob(files, {"manifests": [
        {"digest": postgres_amd64, "platform": _PLATFORM},
        {"digest": postgres_alternate, "platform": alternate},
    ]})
    manifest = [
        {"RepoTags": [tag], "Config": f"blobs/sha256/{config[7:]}"}
        for tag, config in ((_APP, config_app), (_POSTGRES, config_pg))
    ]
    index = {"manifests": [
        {"digest": digest, "annotations": {
            "io.containerd.image.name": f"docker.io/{tag}",
            "org.opencontainers.image.ref.name": "test",
        }}
        for tag, digest in ((_APP, postgres_index if swap_targets else app), (_POSTGRES, postgres_index))
    ]}
    files["manifest.json"] = json.dumps(manifest).encode()
    files["index.json"] = json.dumps(index).encode()
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))
    return {_APP: {config_app, app}, _POSTGRES: {config_pg, postgres_amd64, postgres_index}}


def test_archived_ids_cover_classic_and_containerd_without_mixing_tags(tmp_path: Path) -> None:
    path = tmp_path / "images.tar"
    expected = _archive(path)
    assert archive_image_ids(path, (_APP, _POSTGRES)) == expected


def test_reject_archive_linking_a_tag_to_another_image_config(tmp_path: Path) -> None:
    path = tmp_path / "images.tar"
    _archive(path, swap_targets=True)
    with pytest.raises(ValueError, match="different image config"):
        archive_image_ids(path, (_APP, _POSTGRES))


def test_reject_index_with_two_different_configs_for_amd64(tmp_path: Path) -> None:
    path = tmp_path / "images.tar"
    _archive(path, alternate_amd64=True)
    with pytest.raises(ValueError, match="different image config"):
        archive_image_ids(path, (_APP, _POSTGRES))
