"""研究版入门包的离线校验及登记。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import tempfile
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pyarrow.parquet as pq
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from finboard_data.releases import verify_dataset_release
from finboard_persistence import ResearchDatasetReleaseRepository, TradeCalRepository
from finboard_persistence.models import InstrumentModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_data.releases import ResearchDatasetRelease

STARTER_ID = "synthetic-starter-v1"
STARTER_SOURCE = "synthetic-starter"
STARTER_SYMBOLS = tuple(f"FBDEMO{i:02d}.SH" for i in range(1, 9))
STARTER_BENCHMARK = "FBDEMOIDX.SH"
_SIZE_LIMIT = 32 * 1024 * 1024
_FILE_LIMIT = 100


def _read_bundle(bundle: Path, staging: Path) -> tuple[ResearchDatasetRelease, set[date]]:
    if bundle.stat().st_size > _SIZE_LIMIT:
        raise ValueError("starter_bundle_too_large")
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        if (
            len(names) != len(members)
            or len(names) > _FILE_LIMIT
            or sum(member.file_size for member in members) > _SIZE_LIMIT
            or "starter.json" not in names
        ):
            raise ValueError("starter_bundle_invalid_index")
        index = json.loads(archive.read("starter.json"))
        if index["format"] != STARTER_ID or set(index["files"]) != names - {"starter.json"}:
            raise ValueError("starter_bundle_invalid_index")
        for member in members:
            if member.filename == "starter.json":
                continue
            path = PurePosixPath(member.filename)
            if (
                len(path.parts) < 2
                or path.parts[0] != STARTER_ID
                or path.as_posix() != member.filename
                or ".." in path.parts
                or ":" in member.filename
                or "\\" in member.filename
                or member.is_dir()
                or stat.S_ISLNK(member.external_attr >> 16)
                or path.suffix not in {".json", ".parquet"}
            ):
                raise ValueError("starter_bundle_unsafe_path")
            content = archive.read(member)
            if hashlib.sha256(content).hexdigest() != index["files"][member.filename]:
                raise ValueError("starter_bundle_file_checksum_mismatch")
            target = staging.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        days = {date.fromisoformat(day) for day in index["trading_days"]}
    release = verify_dataset_release(staging / STARTER_ID)
    if (
        release.release_id != STARTER_ID
        or release.source != STARTER_SOURCE
        or release.release_checksum != index["release_checksum"]
        or not release.is_usable
        or release.symbol_codes() != {*STARTER_SYMBOLS, STARTER_BENCHMARK}
        or not days or min(days) != release.start_date or max(days) != release.end_date
        or any(item.row_count != len(days) for item in release.instruments)
    ):
        raise ValueError("starter_bundle_release_mismatch")
    for item in release.instruments:
        table = pq.read_table(staging / STARTER_ID / item.artifact_path, columns=["timestamp"])
        if {stamp.date() for stamp in table["timestamp"].to_pylist()} != days:
            raise ValueError("starter_bundle_calendar_mismatch")
    return release, days


async def import_starter_bundle(
    bundle: Path, *, root: Path, sessions: async_sessionmaker[AsyncSession],
) -> ResearchDatasetRelease:
    """先验证整包, 随后发布目录与 PostgreSQL 索引。可重试未登记的完整目录。"""
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".starter-", dir=root) as directory:
        staging = Path(directory)
        release, days = await asyncio.to_thread(_read_bundle, bundle, staging)
        final = root / STARTER_ID
        async with sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:package))"), {"package": STARTER_ID}
            )
            if final.exists() and (
                await asyncio.to_thread(verify_dataset_release, final)
            ).release_checksum != release.release_checksum:
                raise ValueError("starter_bundle_version_conflict")
            await ResearchDatasetReleaseRepository(session).publish(release)
            await TradeCalRepository(session).upsert_trading_days(days, source=STARTER_SOURCE)
            await session.execute(
                insert(InstrumentModel).on_conflict_do_nothing(index_elements=["code"]),
                [
                    {
                        "code": item.code, "name": item.name, "market": item.market.value,
                        "instrument_type": item.instrument_type.value, "exchange": item.exchange,
                        "listing_board": item.listing_board, "status": item.status.value,
                        "list_date": item.list_date, "industry": item.industry,
                    }
                    for item in release.instruments
                ],
            )
            if not final.exists():
                (staging / STARTER_ID).rename(final)
        return release
