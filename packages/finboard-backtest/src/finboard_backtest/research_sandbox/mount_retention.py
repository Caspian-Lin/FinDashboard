"""互斥保护可重建的研究挂载, 并回收已结束的挂载输入。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TextIO

from finboard_backtest.research_sandbox.mount_cleanup_paths import (
    UnsafeMountPathError,
    pin_mount,
    plain_stat,
    unlink_input,
)

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


class _MountBusyError(BlockingIOError):
    """另一个进程正在使用或回收同一个物理挂载。"""


@contextmanager
def _open_regular(path: Path, flags: int, dir_fd: int | None) -> Iterator[int]:
    with suppress(FileNotFoundError):
        plain_stat(path, dir_fd=dir_fd)
    fd = os.open(
        path.name if dir_fd is not None else path,
        flags | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=dir_fd,
    )
    try:
        if not os.path.samestat(os.fstat(fd), plain_stat(path, dir_fd=dir_fd)):
            raise UnsafeMountPathError(str(path))
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _lock(mount: Path, dir_fd: int | None = None) -> Iterator[None]:
    path = mount / _LEASE
    with _open_regular(path, os.O_CREAT | os.O_RDWR, dir_fd) as fd:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise _MountBusyError(str(path)) from exc
            raise
        yield


@asynccontextmanager
async def use_mount(mount: Path) -> AsyncIterator[None]:
    """从物化或读入开始持有锁, 成功结束时登记最后使用时间。"""
    mount.parent.mkdir(parents=True, exist_ok=True)
    mount = mount.parent.resolve(strict=True) / mount.name
    mount.mkdir(exist_ok=True)
    plain_stat(mount, directory=True)
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(_lock(mount))
            except _MountBusyError:
                await asyncio.sleep(0.05)
            else:
                break
        state = mount / _STATE
        pending = mount / f"{_STATE}.tmp"
        for path in (state, pending):
            with suppress(FileNotFoundError):
                plain_stat(path)
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


@dataclass(frozen=True)
class _ContainerArchive:
    bound_mount: Path
    ended_at: float
    manifest_checksum: str | None


def _safe_dir(root: Path, path: Path) -> None:
    parts = path.relative_to(root).parts
    plain_stat(root, directory=True)
    for part in parts:
        root /= part
        plain_stat(root, directory=True)


@contextmanager
def _read_text(path: Path, dir_fd: int | None) -> Iterator[TextIO]:
    with (
        _open_regular(path, os.O_RDONLY, dir_fd) as fd,
        open(fd, encoding="utf-8", closefd=False) as source,
    ):
        yield source


def _read_object(path: Path, dir_fd: int | None) -> dict[str, object]:
    with _read_text(path, dir_fd) as source:
        item = json.load(source)
    if not isinstance(item, dict):
        raise ValueError("expected object")
    return item


def _mount_files(manifest: Path, dir_fd: int | None) -> set[str]:
    payload = _read_object(manifest, dir_fd)
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


def _last_used(
    mount: Path,
    files: set[str],
    dir_fd: int | None,
    parent_fd: int | None,
    cut_archive: _ContainerArchive | None,
) -> float | None:
    state = mount / _STATE
    try:
        with _read_text(state, dir_fd) as source:
            last_used = (
                os.fstat(source.fileno()).st_mtime if source.read() == "finished\n" else None
            )
    except FileNotFoundError:
        last_used = _legacy_finished_at(mount, dir_fd, parent_fd, cut_archive)
    if last_used is None:
        return None
    for name in (*files, _MANIFEST):
        with suppress(FileNotFoundError):
            if plain_stat(mount / name, dir_fd=dir_fd).st_mtime > last_used:
                return None
    return last_used


def _legacy_finished_at(
    mount: Path,
    dir_fd: int | None,
    parent_fd: int | None,
    cut_archive: _ContainerArchive | None,
) -> float | None:
    # 旧版 RCR/FSC 容器使用唯一目录。RR 决策目录可复用, 旧进程不持有挂载锁。
    archive = cut_archive
    if mount.name == "data" and mount.parent.name.startswith(("RCR-", "FSC-")):
        archive = _container_archive(mount.parent, parent_fd)
    if archive is None or archive.bound_mount != mount:
        return None
    if archive.manifest_checksum:
        # 构建端对 UTF-8 文本计算校验和, Windows 落盘回读统一换行。
        with _read_text(mount / _MANIFEST, dir_fd) as source:
            checksum = hashlib.sha256(source.read().encode("utf-8")).hexdigest()
        if checksum != archive.manifest_checksum:
            return None
    return archive.ended_at


def _container_archive(run: Path, dir_fd: int | None) -> _ContainerArchive | None:
    try:
        with _read_text(run / "container.json", dir_fd) as source:
            payload = json.load(source)
            ended_at = os.fstat(source.fileno()).st_mtime
        if not isinstance(payload, dict):
            return None
        container = payload.get("container_id")
        command = payload.get("command")
        if (
            type(payload.get("exit_code")) is not int
            or not isinstance(container, str)
            or not container
            or not isinstance(command, list)
        ):
            return None
        if any(
            plain_stat(run / name, dir_fd=dir_fd).st_mtime > ended_at
            for name in ("stdout.txt", "stderr.txt")
        ):
            return None
    except (FileNotFoundError, ValueError):
        return None
    prefix = "type=bind,source="
    suffix = ",target=/data,readonly"
    sources = [
        Path(argument[len(prefix) : -len(suffix)])
        for flag, argument in pairwise(command)
        if flag == "--mount"
        and isinstance(argument, str)
        and argument.startswith(prefix)
        and argument.endswith(suffix)
    ]
    if len(sources) != 1 or not sources[0].is_absolute():
        return None
    checksum = payload.get("mount_manifest_checksum")
    return _ContainerArchive(sources[0], ended_at, checksum if isinstance(checksum, str) else None)


def _legacy_cut_archives(root: Path, runs: list[Path]) -> dict[Path, _ContainerArchive]:
    archives: dict[Path, _ContainerArchive] = {}
    for run in runs:
        if not run.name.startswith("FSC-") or not _RUN.fullmatch(run.name):
            continue
        try:
            _safe_dir(root, run)
            with pin_mount(run) as (dir_fd, _):
                archive = _container_archive(run, dir_fd)
        except OSError:
            continue
        if archive is None or not archive.manifest_checksum:
            continue
        mount = archive.bound_mount
        if (
            mount.parent.parent == root
            and mount.parent.name.startswith("FSC-")
            and _RUN.fullmatch(mount.parent.name)
            and _CUT.fullmatch(mount.name)
        ):
            previous = archives.get(mount)
            if previous is None or previous.ended_at < archive.ended_at:
                archives[mount] = archive
    return archives


def _reclaim(
    root: Path,
    mount: Path,
    cutoff: float,
    report: MountCleanup,
    cut_archive: _ContainerArchive | None = None,
) -> None:
    report.scanned_mounts += 1
    try:
        _safe_dir(root, mount)
        manifest = mount / _MANIFEST
        if plain_stat(manifest).st_mtime >= cutoff:
            report.skipped["within_retention"] += 1
            return
        if (
            cut_archive is None
            and not (mount / _STATE).exists()
            and (mount.name != "data" or not mount.parent.name.startswith(("RCR-", "FSC-")))
        ):
            report.skipped["completion_unconfirmed"] += 1
            return
        with pin_mount(mount) as (dir_fd, parent_fd), _lock(mount, dir_fd):
            files = _mount_files(manifest, dir_fd)
            last_used = _last_used(mount, files, dir_fd, parent_fd, cut_archive)
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
                    size = unlink_input(mount / name, dir_fd)
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
    except _MountBusyError:
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
        plain_stat(root, directory=True)
        runs = list(root.iterdir())
    except FileNotFoundError:
        report.skipped["root_missing"] += 1
        return report
    except (OSError, RuntimeError):
        report.skipped["root_unavailable"] += 1
        return report

    cut_archives = _legacy_cut_archives(root, runs)
    for run in runs:
        if not _RUN.fullmatch(run.name):
            report.skipped["unrecognized_layout"] += 1
            continue
        try:
            _safe_dir(root, run)
            for child in run.iterdir():
                if child.name == "data" or _CUT.fullmatch(child.name):
                    _reclaim(root, child, cutoff, report, cut_archives.get(child))
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
