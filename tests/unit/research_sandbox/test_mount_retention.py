from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finboard_backtest.research_sandbox import mount_retention
from finboard_backtest.research_sandbox.mount_cleanup_paths import unlink_input
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
        state = mount / ".mount-use"
        state.write_text("finished\n", encoding="ascii")
        os.utime(state, (_NOW, finished))
    return data


def _legacy_archive(
    run: Path, mount: Path, finished: float, *, manifest_checksum: str | None = None
) -> None:
    run.mkdir(parents=True, exist_ok=True)
    record = {
        "exit_code": 0,
        "container_id": "abc",
        "command": ["--mount", f"type=bind,source={mount.resolve()},target=/data,readonly"],
        "mount_manifest_checksum": manifest_checksum,
    }
    (run / "container.json").write_text(json.dumps(record), encoding="utf-8")
    for name in ("stdout.txt", "stderr.txt"):
        (run / name).write_text("container output", encoding="utf-8")
    for name in ("stdout.txt", "stderr.txt", "container.json"):
        os.utime(run / name, (_NOW, finished))


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


async def test_legacy_finished_container_and_ambiguous_runs(tmp_path: Path) -> None:
    old = _input(tmp_path / _RCR / "data")
    for path in old.parent.iterdir():
        os.utime(path, (_NOW, _CUTOFF - 2))
    _legacy_archive(old.parent.parent, old.parent, _CUTOFF - 1)
    ambiguous = _input(tmp_path / _FSP / "data")
    aborted = _input(tmp_path / "FSE-000000000001" / "data")
    with pytest.raises(RuntimeError, match="interrupted"):
        async with use_mount(aborted.parent):
            raise RuntimeError("interrupted")
    os.utime(aborted.parent / ".mount-use", (_NOW, _CUTOFF - 1))
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert not old.exists()
    assert ambiguous.exists()
    assert aborted.exists()
    assert report.skipped["completion_unconfirmed"] == 2


def test_legacy_cut_needs_matching_checksum_and_latest_terminal_archive(tmp_path: Path) -> None:
    baseline = tmp_path / "FSC-000000000001"
    valid = _input(baseline / "data-cut-2024-07-01")
    different = _input(baseline / "data-cut-2024-07-02")
    recently_used = _input(baseline / "data-cut-2024-07-03")
    for item in (valid, different, recently_used):
        manifest = item.parent / "mount_manifest.json"
        for path in (manifest, item):
            os.utime(path, (_NOW, _CUTOFF - 30))
    unchanged = (valid.parent / "mount_manifest.json").read_text(encoding="utf-8")
    checksum = hashlib.sha256(unchanged.encode("utf-8")).hexdigest()
    _legacy_archive(
        tmp_path / "FSC-000000000002", valid.parent, _CUTOFF - 10, manifest_checksum=checksum
    )
    _legacy_archive(
        tmp_path / "FSC-000000000003",
        different.parent,
        _CUTOFF - 10,
        manifest_checksum="unexpected",
    )
    for n, ended in ((4, _CUTOFF - 10), (5, _CUTOFF + 10)):
        _legacy_archive(
            tmp_path / f"FSC-{n:012x}", recently_used.parent, ended, manifest_checksum=checksum
        )
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert report.reclaimed_files == 1
    assert report.skipped["completion_unconfirmed"] == 1
    assert report.skipped["within_retention"] == 1
    assert not valid.exists()
    assert different.exists()
    assert recently_used.exists()
    assert (valid.parent / "mount_manifest.json").read_text(encoding="utf-8") == unchanged


async def test_path_alias_shares_active_lock(tmp_path: Path) -> None:
    physical = tmp_path / "actual"
    item = _input(physical / _FSP / "data", finished=_CUTOFF - 1)
    alias = tmp_path / "worktree"
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "mklink", "/J", str(alias), str(physical)],
            check=True,
            capture_output=True,
        )
    else:
        alias.symlink_to(physical, target_is_directory=True)
    outside = _input(tmp_path / "elsewhere", finished=_CUTOFF - 1)
    indirect = physical / "FSE-000000000001"
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "mklink", "/J", str(indirect), str(outside.parent)],
            check=True,
            capture_output=True,
        )
    else:
        indirect.symlink_to(outside.parent, target_is_directory=True)

    async with use_mount(item.parent):
        result = await asyncio.to_thread(
            subprocess.run,
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

    def locked(path: Path, dir_fd: int | None) -> int:
        if path == item.resolve():
            raise PermissionError("file in use")
        return unlink_input(path, dir_fd)

    monkeypatch.setattr(mount_retention, "unlink_input", locked)
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert report.skipped["file_unavailable"] == 1
    assert report.skipped["unverified_manifest"] == 1
    assert item.exists()
    assert cut.exists()


async def test_mount_waits_for_contended_lease_and_can_cancel(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    ready = asyncio.Event()

    async def use() -> None:
        ready.set()
        async with use_mount(mount):
            pass

    async with use_mount(mount):
        waiting = asyncio.create_task(use())
        await ready.wait()
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        ready.clear()
        waiting = asyncio.create_task(use())
        await ready.wait()
        assert not waiting.done()

    await asyncio.wait_for(waiting, timeout=3)
    assert (mount / ".mount-use").read_text(encoding="ascii") == "finished\n"


def test_rebuilt_mount_ignores_stale_finish_marker(tmp_path: Path) -> None:
    item = _input(tmp_path / _FSP / "data", finished=_CUTOFF - 100)
    os.utime(item, (_NOW, _CUTOFF - 10))
    report = collect_idle_mounts(tmp_path, now=_NOW)
    assert report.skipped["completion_unconfirmed"] == 1
    assert item.is_file()


def test_mount_replacement_cannot_redirect_file_removal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "cache"
    item = _input(root / _FSP / "data", finished=_CUTOFF - 1)
    outside = _input(tmp_path / "unrelated" / "data", finished=_CUTOFF - 1)
    moved = root / "moved"
    run = item.parent.parent

    def replace_then_unlink(path: Path, dir_fd: int | None) -> int:
        if sys.platform == "win32":
            with pytest.raises(PermissionError):
                run.rename(moved)
        else:
            run.rename(moved)
            run.symlink_to(outside.parent.parent, target_is_directory=True)
        return unlink_input(path, dir_fd)

    monkeypatch.setattr(mount_retention, "unlink_input", replace_then_unlink)
    report = collect_idle_mounts(root, now=_NOW)
    assert outside.read_bytes() == b"frozen bars"
    if sys.platform == "win32":
        assert report.reclaimed_files == 1
        assert not item.exists()
    else:
        assert report.skipped["unsafe_path"] == 1
        assert report.reclaimed_files == 0
        assert (moved / "data" / item.name).read_bytes() == b"frozen bars"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mandatory file sharing")
def test_windows_open_file_is_skipped_until_released(tmp_path: Path) -> None:
    item = _input(tmp_path / _FSP / "data", finished=_CUTOFF - 1)
    with item.open("rb"):
        report = collect_idle_mounts(tmp_path, now=_NOW)
        assert report.skipped["file_unavailable"] == 1
        assert item.exists()
    assert collect_idle_mounts(tmp_path, now=_NOW).reclaimed_files == 1
