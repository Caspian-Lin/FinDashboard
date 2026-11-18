"""互斥保护可重建的研究挂载, 并回收已结束的挂载输入。"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

_IDLE_SECONDS = 30 * 24 * 60 * 60
_LEASE = ".mount.lock"
_STATE = ".mount-use"
_MANIFEST = "mount_manifest.json"
_RUN = re.compile(r"(?:RCR-[0-9a-f]{24}|FS[CPE]-[0-9a-f]{12}|RR-[A-Za-z0-9-]+)")
_DECISION = re.compile(r"D\d{4,}")
_CUT = re.compile(r"data-cut-\d{4}-\d{2}-\d{2}")
_DATA_FILES = frozenset(
    f"{kind}.parquet"
    for kind in (
        "bars",
        "daily_metrics",
        "financial_indicators",
        "income_statements",
        "balance_sheets",
        "cashflow_statements",
        "dividends",
    )
)


class UnsafeMountPathError(OSError):
    """路径不是普通文件或目录, 或存在重解析点。"""


def _plain_stat(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or (not directory and info.st_nlink != 1)
    ):
        raise UnsafeMountPathError(str(path))
    return info


@contextmanager
def _lock(mount: Path) -> Iterator[None]:
    path = mount / _LEASE
    with suppress(FileNotFoundError):
        _plain_stat(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not os.path.samestat(os.fstat(fd), _plain_stat(path)):
            raise UnsafeMountPathError(str(path))
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BlockingIOError(str(path)) from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def use_mount(mount: Path) -> Iterator[None]:
    """从物化或读入开始持有锁, 成功结束时登记最后使用时间。"""
    mount.parent.mkdir(parents=True, exist_ok=True)
    mount = mount.parent.resolve(strict=True) / mount.name
    mount.mkdir(exist_ok=True)
    _plain_stat(mount, directory=True)
    with _lock(mount):
        state = mount / _STATE
        pending = mount / f"{_STATE}.tmp"
        for path in (state, pending):
            with suppress(FileNotFoundError):
                _plain_stat(path)
        pending.write_text("in_progress\n", encoding="ascii")
        pending.replace(state)
        yield
        pending.write_text("finished\n", encoding="ascii")
        pending.replace(state)


@dataclass
class MountCleanup:
    scanned_mounts: int = 0
    reclaimed_mounts: int = 0
    reclaimed_files: int = 0
    released_bytes: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


def _safe_dir(root: Path, path: Path) -> None:
    parts = path.relative_to(root).parts
    _plain_stat(root, directory=True)
    for part in parts:
        root /= part
        _plain_stat(root, directory=True)


def _read_object(path: Path) -> dict[str, object]:
    _plain_stat(path)
    with path.open(encoding="utf-8") as source:
        item = json.load(source)
    if not isinstance(item, dict):
        raise ValueError("expected object")
    return item


def _mount_files(manifest: Path) -> set[str]:
    payload = _read_object(manifest)
    version = payload.get("version")
    datasets = payload.get("datasets")
    if version not in (2, 3) or not isinstance(datasets, list) or not datasets:
        raise ValueError("unrecognized mount manifest")
    if version == 2 and not isinstance(payload.get("decision_at"), str):
        raise ValueError("missing decision anchor")
    if version == 3 and not isinstance(payload.get("window"), dict):
        raise ValueError("missing window anchor")
    files: set[str] = set()
    for item in datasets:
        if not isinstance(item, dict):
            raise ValueError("unrecognized dataset")
        name, kind, release = (item.get(key) for key in ("file", "dataset_kind", "release_id"))
        if (
            not isinstance(name, str)
            or name not in _DATA_FILES
            or name != f"{kind}.parquet"
            or not isinstance(release, str)
            or not release
        ):
            raise ValueError("unrecognized dataset file")
        files.add(name)
    return files


def _last_used(mount: Path, files: set[str]) -> float | None:
    state = mount / _STATE
    try:
        state_mtime = _plain_stat(state).st_mtime
    except FileNotFoundError:
        return _legacy_finished_at(mount, files)
    return state_mtime if state.read_text(encoding="ascii") == "finished\n" else None


def _legacy_finished_at(mount: Path, files: set[str]) -> float | None:
    # 旧版本只在容器返回后归档三个文件。原生 FSP/FSE 旧版没有终态证据。
    if mount.name != "data" or not (
        mount.parent.name.startswith(("RCR-", "FSC-")) or _DECISION.fullmatch(mount.parent.name)
    ):
        return None
    archive = mount.parent / "container.json"
    try:
        payload = _read_object(archive)
        if type(payload.get("exit_code")) is not int or not payload.get("container_id"):
            return None
        ended_at = max(
            _plain_stat(mount.parent / name).st_mtime
            for name in ("container.json", "stdout.txt", "stderr.txt")
        )
    except (FileNotFoundError, ValueError):
        return None
    # 终态归档必须晚于全部输入, 拒绝已被重建或重新开始使用的挂载。
    for name in (*files, _MANIFEST):
        try:
            if _plain_stat(mount / name).st_mtime > ended_at:
                return None
        except FileNotFoundError:
            continue
    return ended_at


def _reclaim(root: Path, mount: Path, cutoff: float, report: MountCleanup) -> None:
    report.scanned_mounts += 1
    try:
        _safe_dir(root, mount)
        with _lock(mount):
            _safe_dir(root, mount)
            files = _mount_files(mount / _MANIFEST)
            last_used = _last_used(mount, files)
            if last_used is None:
                report.skipped["completion_unconfirmed"] += 1
                return
            if last_used >= cutoff:
                report.skipped["within_retention"] += 1
                return
            removed = False
            for name in sorted(files):
                try:
                    _safe_dir(root, mount)
                    path = mount / name
                    size = _plain_stat(path).st_size
                    path.unlink()
                except FileNotFoundError:
                    continue
                except UnsafeMountPathError:
                    report.skipped["unsafe_path"] += 1
                    continue
                except OSError:
                    report.skipped["file_unavailable"] += 1
                    continue
                report.reclaimed_files += 1
                report.released_bytes += size
                removed = True
            if removed:
                report.reclaimed_mounts += 1
            else:
                report.skipped["no_files_reclaimed"] += 1
    except UnsafeMountPathError:
        report.skipped["unsafe_path"] += 1
    except BlockingIOError:
        report.skipped["active_mount"] += 1
    except (FileNotFoundError, ValueError):
        report.skipped["unverified_manifest"] += 1
    except OSError:
        report.skipped["mount_unavailable"] += 1


def collect_idle_mounts(root: Path, *, now: float | None = None) -> MountCleanup:
    """只扫已知布局。根目录可经 worktree junction 指向同一个物理目录。"""
    report = MountCleanup()
    cutoff = (time.time() if now is None else now) - _IDLE_SECONDS
    try:
        root = root.resolve(strict=True)
        _plain_stat(root, directory=True)
        runs = list(root.iterdir())
    except FileNotFoundError:
        report.skipped["root_missing"] += 1
        return report
    except (OSError, RuntimeError):
        report.skipped["root_unavailable"] += 1
        return report

    for run in runs:
        if not _RUN.fullmatch(run.name):
            report.skipped["unrecognized_layout"] += 1
            continue
        try:
            _safe_dir(root, run)
            for child in run.iterdir():
                if child.name == "data" or _CUT.fullmatch(child.name):
                    _reclaim(root, child, cutoff, report)
                elif run.name.startswith("RR-") and _DECISION.fullmatch(child.name):
                    try:
                        _safe_dir(root, child)
                        if (child / "data").lstat():
                            _reclaim(root, child / "data", cutoff, report)
                    except FileNotFoundError:
                        continue
                    except UnsafeMountPathError:
                        report.skipped["unsafe_path"] += 1
        except UnsafeMountPathError:
            report.skipped["unsafe_path"] += 1
        except OSError:
            report.skipped["run_unavailable"] += 1
    return report
