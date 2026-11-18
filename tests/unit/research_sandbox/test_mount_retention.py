from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from finboard_backtest.research_sandbox.mount_retention import collect_idle_mounts, use_mount

_NOW = 2_000_000_000.0
_CUTOFF = _NOW - 30 * 86400
_FSP = "FSP-000000000001"
_RCR = "RCR-000000000000000000000001"


def _input(mount: Path, *, finished: float | None = None) -> Path:
    mount.mkdir(parents=True, exist_ok=True)
    manifest = mount / "mount_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 3,
                "window": {"window_end": "2024-07-01"},
                "datasets": [
                    {"release_id": "DR-a", "dataset_kind": "bars", "file": "bars.parquet"}
                ],
            }
        ),
        encoding="utf-8",
    )
    data = mount / "bars.parquet"
    data.write_bytes(b"frozen bars")
    if finished is not None:
        with use_mount(mount):
            pass
        os.utime(mount / ".mount-use", (_NOW, finished))
    return data


def test_retention_boundary_layout_and_idempotence(tmp_path: Path) -> None:
    main = _input(tmp_path / _FSP / "data", finished=_CUTOFF - 1)
    cut = _input(tmp_path / _FSP / "data-cut-2024-07-01", finished=_CUTOFF)
    decision = _input(tmp_path / "RR-one" / "D0000" / "data", finished=_CUTOFF - 1)
    unrelated = _input(tmp_path / "unknown" / "data", finished=_CUTOFF - 1)
    archive = main.parent.parent / "out"
    archive.mkdir()
    scores = archive / "scores.parquet"
    scores.write_bytes(b"formal scores")
    manifest = main.parent / "mount_manifest.json"
    before = manifest.read_bytes()
    extra = main.parent / "other.parquet"
    extra.write_bytes(b"unregistered file")

    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert (report.scanned_mounts, report.reclaimed_mounts, report.reclaimed_files) == (3, 2, 2)
    assert report.released_bytes == 2 * len(b"frozen bars")
    assert report.skipped["within_retention"] == 1
    assert not main.exists()
    assert not decision.exists()
    assert extra.exists()
    assert cut.exists()
    assert unrelated.exists()
    assert manifest.read_bytes() == before
    assert scores.read_bytes() == b"formal scores"
    assert collect_idle_mounts(tmp_path, now=_NOW).reclaimed_files == 0


def test_legacy_finished_container_and_ambiguous_runs(tmp_path: Path) -> None:
    old = _input(tmp_path / _RCR / "data")
    for path in old.parent.iterdir():
        os.utime(path, (_NOW, _CUTOFF - 2))
    for name in ("container.json", "stdout.txt", "stderr.txt"):
        path = old.parent.parent / name
        path.write_text('{"exit_code": 0, "container_id": "abc"}', encoding="utf-8")
        os.utime(path, (_NOW, _CUTOFF - 1))
    ambiguous = _input(tmp_path / _FSP / "data")
    aborted = _input(tmp_path / "FSE-000000000001" / "data")
    try:
        with use_mount(aborted.parent):
            raise RuntimeError("interrupted")
    except RuntimeError:
        pass
    os.utime(aborted.parent / ".mount-use", (_NOW, _CUTOFF - 1))
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert not old.exists()
    assert ambiguous.exists()
    assert aborted.exists()
    assert report.skipped["completion_unconfirmed"] == 2


def test_path_alias_shares_active_lock(tmp_path: Path) -> None:
    physical = tmp_path / "actual"
    item = _input(physical / _FSP / "data", finished=_CUTOFF - 1)
    alias = tmp_path / "worktree"
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(physical)],
            check=True,
            capture_output=True,
        )
    else:
        alias.symlink_to(physical, target_is_directory=True)
    outside = _input(tmp_path / "elsewhere", finished=_CUTOFF - 1)
    indirect = physical / "FSE-000000000001"
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(indirect), str(outside.parent)],
            check=True,
            capture_output=True,
        )
    else:
        indirect.symlink_to(outside.parent, target_is_directory=True)

    with use_mount(item.parent):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from finboard_backtest.research_sandbox.mount_retention import collect_idle_mounts; "
                "r = collect_idle_mounts(Path(sys.argv[1]), now=float(sys.argv[2])); "
                "assert r.skipped['active_mount'] == 1 and r.skipped['unsafe_path'] == 1, r",
                str(alias),
                str(_NOW),
            ],
            check=True,
            capture_output=True,
        )
        assert result.returncode == 0
        assert item.exists()
        assert outside.exists()

    stamp = item.parent / ".mount-use"
    os.utime(stamp, (_NOW, _CUTOFF - 1))
    assert collect_idle_mounts(alias, now=_NOW).reclaimed_files == 1
    assert outside.exists()


def test_unavailable_input_and_invalid_manifest(tmp_path: Path, monkeypatch) -> None:
    assert collect_idle_mounts(tmp_path / "absent").skipped["root_missing"] == 1
    item = _input(tmp_path / _FSP / "data", finished=_CUTOFF - 1)
    cut = _input(tmp_path / _FSP / "data-cut-2024-07-01", finished=_CUTOFF - 1)
    (cut.parent / "mount_manifest.json").write_text('{"datasets":["../bars.parquet"]}')
    unlink = Path.unlink

    def locked(self: Path, *args, **kwargs) -> None:
        if self == item.resolve():
            raise PermissionError("file in use")
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert report.skipped["file_unavailable"] == 1
    assert report.skipped["unverified_manifest"] == 1
    assert item.exists()
    assert cut.exists()
