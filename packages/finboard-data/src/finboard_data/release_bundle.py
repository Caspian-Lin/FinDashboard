"""版本化冻结发布包的预览、流式校验及导出。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq

from finboard_data.releases import (
    RELEASE_FIELDS,
    RELEASE_MANIFEST_FILENAME,
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import IO

BUNDLE_FORMAT = "finboard-release-bundle-v1"
BUNDLE_INDEX = "bundle.json"
MAX_BUNDLE_BYTES = 64 * 1024**3
_MAX_INDEX_BYTES = 32 * 1024**2
_MAX_MANIFEST_BYTES = 32 * 1024**2
_MAX_RELEASES = 32
_MAX_FILES = 100_000
_MAX_CALENDAR_DAYS = 150_000
_BLOCK_SIZE = 1024 * 1024
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}")
_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", re.I)
_KINDS = {
    ReleaseDatasetKind.BARS,
    ReleaseDatasetKind.DAILY_METRICS,
    ReleaseDatasetKind.FINANCIAL_INDICATORS,
    ReleaseDatasetKind.CONVERTIBLE_METRICS,
}


class ReleaseBundleError(ValueError):
    """包内容无法作为冻结发布登记。"""


@dataclass(frozen=True, slots=True)
class BundleCalendarDay:
    exchange: str
    cal_date: date
    is_open: bool
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "exchange": self.exchange,
            "cal_date": self.cal_date.isoformat(),
            "is_open": self.is_open,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBundle:
    releases: tuple[ResearchDatasetRelease, ...]
    calendar: tuple[BundleCalendarDay, ...]
    unpacked_bytes: int
    archive_bytes: int

    def summary(self) -> dict[str, object]:
        return {
            "format": BUNDLE_FORMAT,
            "archive_bytes": self.archive_bytes,
            "unpacked_bytes": self.unpacked_bytes,
            "calendar_days": len(self.calendar),
            "releases": [
                {
                    "release_id": release.release_id,
                    "release_checksum": release.release_checksum,
                    "dataset_kind": release.dataset_kind.value,
                    "dataset_name": release.dataset_name,
                    "source": release.source,
                    "adjustment": release.adjustment,
                    "start_date": release.start_date.isoformat(),
                    "end_date": release.end_date.isoformat(),
                    "symbol_count": release.symbol_count,
                }
                for release in self.releases
            ],
        }


def _valid_component(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _COMPONENT.fullmatch(value)
        or value.endswith(".")
        or _WINDOWS_DEVICE.fullmatch(value.split(".")[0])
    ):
        raise ReleaseBundleError("bundle_unsafe_path")
    return value


def _relative_name(value: str) -> str:
    return "/".join(_valid_component(part) for part in value.split("/"))


def _checksum(value: object) -> str:
    if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
        raise ReleaseBundleError("bundle_invalid_checksum")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _object(data: bytes) -> dict[str, Any]:
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseBundleError("bundle_invalid_json") from exc
    if not isinstance(result, dict):
        raise ReleaseBundleError("bundle_invalid_json")
    return result


def _release_from_wire(data: bytes, release_id: str, checksum: str) -> ResearchDatasetRelease:
    raw = _object(data)
    try:
        release = ResearchDatasetRelease.from_dict(raw)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise ReleaseBundleError("bundle_invalid_release_manifest") from exc
    if (
        release.release_id != release_id
        or release.release_checksum != checksum
        or release.dataset_kind not in _KINDS
        or not release.is_usable
        or not release.instruments
    ):
        raise ReleaseBundleError("bundle_release_identity_mismatch")
    # 用原始 manifest 重算, 避免对旧发布补入当前 from_dict 的默认字段。
    raw["release_checksum"] = ""
    if release.dataset_kind is ReleaseDatasetKind.BARS:
        raw.pop("dataset_kind", None)
    if hashlib.sha256(_json(raw)).hexdigest() != checksum:
        raise ReleaseBundleError("bundle_release_checksum_mismatch")
    return release


def _calendar_from_wire(data: object) -> tuple[BundleCalendarDay, ...]:
    if not isinstance(data, list) or len(data) > _MAX_CALENDAR_DAYS:
        raise ReleaseBundleError("bundle_invalid_calendar")
    days: dict[tuple[str, date], BundleCalendarDay] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ReleaseBundleError("bundle_invalid_calendar")
        exchange = raw.get("exchange")
        source = raw.get("source")
        is_open = raw.get("is_open")
        day = raw.get("cal_date")
        if (
            not isinstance(exchange, str)
            or not re.fullmatch(r"[A-Z0-9]{1,16}", exchange)
            or not isinstance(source, str)
            or not 0 < len(source) <= 32
            or not isinstance(is_open, bool)
            or not isinstance(day, str)
        ):
            raise ReleaseBundleError("bundle_invalid_calendar")
        try:
            cal_date = date.fromisoformat(day)
        except ValueError as exc:
            raise ReleaseBundleError("bundle_invalid_calendar") from exc
        key = (exchange, cal_date)
        if key in days:
            raise ReleaseBundleError("bundle_duplicate_calendar_day")
        days[key] = BundleCalendarDay(exchange, cal_date, is_open, source)
    return tuple(days[key] for key in sorted(days))


def _read_small(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    if archive.getinfo(name).file_size > limit:
        raise ReleaseBundleError("bundle_metadata_too_large")
    return archive.read(name)


def _archive_index(
    archive: zipfile.ZipFile, archive_bytes: int, limit: int,
) -> tuple[ReleaseBundle, dict[str, str]]:
    members = archive.infolist()
    names = {item.filename for item in members}
    unpacked = sum(item.file_size for item in members)
    if archive_bytes > limit or unpacked > limit or len(members) > _MAX_FILES:
        raise ReleaseBundleError("bundle_quota_exceeded")
    if BUNDLE_INDEX not in names or len({name.casefold() for name in names}) != len(members):
        raise ReleaseBundleError("bundle_invalid_index")
    for member in members:
        _relative_name(member.filename)
        if stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG):
            raise ReleaseBundleError("bundle_unsafe_path")
    index = _object(_read_small(archive, BUNDLE_INDEX, _MAX_INDEX_BYTES))
    refs = index.get("releases")
    raw_files = index.get("files")
    if (
        index.get("format") != BUNDLE_FORMAT
        or not isinstance(refs, list)
        or not 0 < len(refs) <= _MAX_RELEASES
        or not isinstance(raw_files, dict)
        or set(raw_files) != names - {BUNDLE_INDEX}
    ):
        raise ReleaseBundleError("bundle_invalid_index")
    files = {name: _checksum(value) for name, value in raw_files.items()}
    expected_files: set[str] = set()
    releases = []
    for entry in refs:
        if not isinstance(entry, dict):
            raise ReleaseBundleError("bundle_invalid_index")
        release_id = _valid_component(entry.get("release_id"))
        manifest_name = f"{release_id}/{RELEASE_MANIFEST_FILENAME}"
        if manifest_name in expected_files or manifest_name not in files:
            raise ReleaseBundleError("bundle_invalid_release_index")
        manifest = _read_small(archive, manifest_name, _MAX_MANIFEST_BYTES)
        if hashlib.sha256(manifest).hexdigest() != files[manifest_name]:
            raise ReleaseBundleError("bundle_manifest_file_checksum_mismatch")
        release = _release_from_wire(manifest, release_id, _checksum(entry.get("release_checksum")))
        expected_files.add(manifest_name)
        for item in release.instruments:
            name = f"{release_id}/{_relative_name(item.artifact_path)}"
            if name in expected_files or files.get(name) != item.artifact_checksum:
                raise ReleaseBundleError("bundle_artifact_index_mismatch")
            expected_files.add(name)
        releases.append(release)
    calendar = _calendar_from_wire(index.get("calendar"))
    if any(r.dataset_kind is ReleaseDatasetKind.BARS for r in releases) and not calendar:
        raise ReleaseBundleError("bundle_calendar_missing")
    if expected_files != set(files):
        raise ReleaseBundleError("bundle_unreferenced_files")
    return ReleaseBundle(tuple(releases), calendar, unpacked, archive_bytes), files


def inspect_release_bundle(path: Path, *, limit: int = MAX_BUNDLE_BYTES) -> ReleaseBundle:
    """验证归档结构与冻结清单, 预览阶段不解包数据文件。"""
    try:
        with zipfile.ZipFile(path) as archive:
            bundle, _ = _archive_index(archive, path.stat().st_size, limit)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ReleaseBundleError("bundle_unreadable_archive") from exc
    return bundle


def _copy_hashed(source: IO[bytes], output: IO[bytes], limit: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    while block := source.read(_BLOCK_SIZE):
        count += len(block)
        if count > limit:
            raise ReleaseBundleError("bundle_quota_exceeded")
        output.write(block)
        digest.update(block)
    return count, digest.hexdigest()


def _verify_parquet(staging: Path, bundle: ReleaseBundle) -> None:
    for release in bundle.releases:
        columns = set(release.fields)
        if release.dataset_kind is ReleaseDatasetKind.BARS:
            columns.update(RELEASE_FIELDS)
        else:
            columns.add("available_at")
            columns.add(
                "report_period"
                if release.dataset_kind is ReleaseDatasetKind.FINANCIAL_INDICATORS
                else "trade_date"
            )
        for item in release.instruments:
            with pq.ParquetFile(staging / release.release_id / item.artifact_path) as parquet:
                if (
                    parquet.metadata.num_rows != item.row_count
                    or not columns.issubset(parquet.schema_arrow.names)
                ):
                    raise ReleaseBundleError(f"bundle_dataset_contract_mismatch:{item.code}")


def unpack_release_bundle(
    path: Path, staging: Path, *, limit: int = MAX_BUNDLE_BYTES,
) -> ReleaseBundle:
    """在专用暂存目录逐块校验和解包。停牌与新上市等缺行沿用冻结清单。"""
    try:
        with zipfile.ZipFile(path) as archive:
            bundle, files = _archive_index(archive, path.stat().st_size, limit)
            if bundle.unpacked_bytes > shutil.disk_usage(staging).free:
                raise ReleaseBundleError("bundle_insufficient_disk")
            for name, digest in files.items():
                item = archive.getinfo(name)
                target = staging.joinpath(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("xb") as output:
                    count, actual = _copy_hashed(source, output, item.file_size)
                if count != item.file_size or actual != digest:
                    raise ReleaseBundleError(f"bundle_file_checksum_mismatch:{name}")
        _verify_parquet(staging, bundle)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ReleaseBundleError("bundle_unreadable_archive") from exc
    return bundle


def export_release_bundle(
    destination: Path,
    root: Path,
    releases: Sequence[ResearchDatasetRelease],
    calendar: Sequence[BundleCalendarDay],
    *,
    limit: int = MAX_BUNDLE_BYTES,
) -> ReleaseBundle:
    """从已登记发布导出, 流式复验文件并在成功后原子发布包。"""
    if not 0 < len(releases) <= _MAX_RELEASES:
        raise ReleaseBundleError("bundle_invalid_release_index")
    root = root.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".release-bundle-", dir=destination.parent, delete=False,
    ) as temp:
        temporary = destination.parent / os.path.basename(temp.name)
    try:
        files: dict[str, str] = {}
        refs = []
        total = 0
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            for registered in releases:
                release_id = _valid_component(registered.release_id)
                claim = _checksum(registered.release_checksum)
                release_root = root / release_id
                manifest_path = release_root / RELEASE_MANIFEST_FILENAME
                if not manifest_path.resolve().is_relative_to(root):
                    raise ReleaseBundleError("bundle_unsafe_path")
                with manifest_path.open("rb") as manifest_file:
                    manifest = manifest_file.read(_MAX_MANIFEST_BYTES + 1)
                if len(manifest) > _MAX_MANIFEST_BYTES:
                    raise ReleaseBundleError("bundle_metadata_too_large")
                release = _release_from_wire(manifest, release_id, claim)
                key = f"{release_id}/{RELEASE_MANIFEST_FILENAME}"
                if key in files:
                    raise ReleaseBundleError("bundle_duplicate_release")
                archive.writestr(key, manifest)
                files[key] = hashlib.sha256(manifest).hexdigest()
                total += len(manifest)
                refs.append({"release_id": release_id, "release_checksum": claim})
                for item in release.instruments:
                    relative = _relative_name(item.artifact_path)
                    path = (release_root / relative).resolve()
                    key = f"{release_id}/{relative}"
                    if key in files or not path.is_relative_to(release_root):
                        raise ReleaseBundleError("bundle_unsafe_path")
                    with path.open("rb") as source, archive.open(
                        key, "w", force_zip64=True,
                    ) as output:
                        count, digest = _copy_hashed(source, output, limit - total)
                    if digest != item.artifact_checksum:
                        raise ReleaseBundleError(f"bundle_file_checksum_mismatch:{key}")
                    files[key] = digest
                    total += count
            archive.writestr(BUNDLE_INDEX, _json({
                "format": BUNDLE_FORMAT,
                "releases": refs,
                "calendar": [day.as_dict() for day in calendar],
                "files": files,
            }))
        bundle = inspect_release_bundle(temporary, limit=limit)
        os.link(temporary, destination)
        return bundle
    finally:
        temporary.unlink(missing_ok=True)
