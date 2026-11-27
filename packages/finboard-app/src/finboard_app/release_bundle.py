"""跨实例冻结发布包的持久化与受控文件目录。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import zipfile
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from finboard_app.research_audit import ResearchAuditEvent, ResearchAuditRecorder
from finboard_data.release_bundle import (
    BundleCalendarDay,
    ReleaseBundle,
    ReleaseBundleError,
    export_release_bundle,
    inspect_release_bundle,
    unpack_release_bundle,
)
from finboard_data.releases import load_dataset_release
from finboard_persistence import ResearchDatasetReleaseRepository
from finboard_persistence.models import (
    InstrumentModel,
    ResearchDatasetReleaseModel,
    TradeCalModel,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_app.research_operation import ResearchOperationContext
    from finboard_data.releases import ResearchDatasetRelease

_ID = re.compile(r"RB-[a-f0-9]{32}")


def release_root() -> Path:
    return Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"))


def new_bundle_id() -> str:
    return f"RB-{uuid4().hex}"


def managed_bundle_path(bundle_id: str) -> Path:
    if not _ID.fullmatch(bundle_id):
        raise ReleaseBundleError("bundle_invalid_file_id")
    root = Path(os.getenv("FINBOARD_DATA_BUNDLE_ROOT", str(release_root().parent / "data_bundles"))).resolve()
    file = root / f"{bundle_id}.zip"
    if file.is_symlink():
        raise ReleaseBundleError("bundle_unsafe_file")
    return file


def bundle_file_checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def _complete_file_io[T](func: Callable[[], T]) -> T:
    """取消等待方后让正在写入暂存目录的线程先收敛。"""
    task = asyncio.create_task(asyncio.to_thread(func))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _calendar(
    session: AsyncSession, releases: Sequence[ResearchDatasetRelease],
) -> tuple[BundleCalendarDay, ...]:
    exchanges = {
        item.exchange.upper()
        for release in releases
        for item in release.instruments
        if item.exchange
    }
    if any(item.market.value == "a_share" for r in releases for item in r.instruments):
        exchanges.update(("SSE", "SZSE"))
    stmt = (
        select(TradeCalModel)
        .where(
            TradeCalModel.exchange.in_(exchanges),
            TradeCalModel.cal_date.between(
                min(release.start_date for release in releases),
                max(release.end_date for release in releases),
            ),
        )
        .order_by(TradeCalModel.exchange, TradeCalModel.cal_date)
    )
    return tuple(
        BundleCalendarDay(row.exchange, row.cal_date, row.is_open, row.source)
        for row in (await session.execute(stmt)).scalars()
    )


async def export_registered_bundle(
    session: AsyncSession, ids: Sequence[str], destination: Path, *, root: Path,
) -> ReleaseBundle:
    if not ids or len(set(ids)) != len(ids):
        raise ReleaseBundleError("bundle_invalid_release_selection")
    repository = ResearchDatasetReleaseRepository(session)
    releases = [await repository.require_usable(release_id) for release_id in ids]
    calendar = await _calendar(session, releases)
    return await _complete_file_io(
        lambda: export_release_bundle(destination, root, releases, calendar)
    )


async def _release_conflicts(
    session: AsyncSession, bundle: ReleaseBundle,
) -> list[str]:
    conflicts = []
    for release in bundle.releases:
        model = ResearchDatasetReleaseModel
        query = select(model.release_id, model.release_checksum).where(
            (model.release_id == release.release_id)
            | (
                (model.dataset_name == release.dataset_name)
                & (model.source == release.source)
                & (model.version == release.version)
            )
        )
        for release_id, checksum in (await session.execute(query)).all():
            if release_id != release.release_id or checksum != release.release_checksum:
                conflicts.append(release.release_id)
                break
    return conflicts


async def _calendar_conflicts(
    session: AsyncSession, calendar: Sequence[BundleCalendarDay],
) -> bool:
    by_exchange: dict[str, dict[date, bool]] = {}
    for item in calendar:
        by_exchange.setdefault(item.exchange, {})[item.cal_date] = item.is_open
    for exchange, days in by_exchange.items():
        query = select(TradeCalModel.cal_date, TradeCalModel.is_open).where(
            TradeCalModel.exchange == exchange,
            TradeCalModel.cal_date.between(min(days), max(days)),
        )
        if any(
            day in days and days[day] != is_open
            for day, is_open in (await session.execute(query)).all()
        ):
            return True
    return False


def _existing_conflicts(root: Path, releases: Iterable[ResearchDatasetRelease]) -> list[str]:
    result = []
    for release in releases:
        final = root / release.release_id
        if final.is_symlink():
            result.append(release.release_id)
        elif final.exists():
            try:
                load_dataset_release(final, expected_checksum=release.release_checksum)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                result.append(release.release_id)
    return result


async def preview_registered_bundle(
    session: AsyncSession, path: Path, *, root: Path,
) -> dict[str, object]:
    bundle = await asyncio.to_thread(inspect_release_bundle, path)
    release_conflicts = await _release_conflicts(session, bundle)
    directory_conflicts = await asyncio.to_thread(_existing_conflicts, root, bundle.releases)
    calendar_conflict = await _calendar_conflicts(session, bundle.calendar)
    return {
        **bundle.summary(),
        "release_conflicts": sorted(set(release_conflicts + directory_conflicts)),
        "calendar_conflict": calendar_conflict,
    }


async def _add_metadata(session: AsyncSession, bundle: ReleaseBundle) -> None:
    calendar = [day.as_dict() | {"cal_date": day.cal_date} for day in bundle.calendar]
    for start in range(0, len(calendar), 500):
        await session.execute(
            insert(TradeCalModel).on_conflict_do_nothing(index_elements=["exchange", "cal_date"]),
            calendar[start:start + 500],
        )

    instruments: dict[str, dict[str, object]] = {}
    # 主 bars 的历史元数据优先, 研究发布只补主发布没有的标的。
    for release in sorted(bundle.releases, key=lambda r: r.dataset_kind.value != "bars"):
        for item in release.instruments:
            instruments.setdefault(
                item.code,
                {
                    "code": item.code,
                    "name": item.name,
                    "market": item.market.value,
                    "instrument_type": item.instrument_type.value,
                    "exchange": item.exchange,
                    "listing_board": item.listing_board,
                    "status": item.status.value,
                    "list_date": item.list_date,
                    "delist_date": item.delist_date,
                    "industry": item.industry,
                },
            )
    rows = list(instruments.values())
    for start in range(0, len(rows), 500):
        await session.execute(
            insert(InstrumentModel).on_conflict_do_nothing(index_elements=["code"]),
            rows[start:start + 500],
        )


def _staging_directory(root: Path) -> tuple[Path, TemporaryDirectory[str]]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary = TemporaryDirectory(prefix=".bundle-import-", dir=root)
    if Path(temporary.name).resolve().parent != root:
        raise ReleaseBundleError("bundle_unsafe_staging")
    return root, temporary


def _activate_files(root: Path, staging: Path, bundle: ReleaseBundle) -> None:
    for release in bundle.releases:
        final = root / release.release_id
        if not final.exists():
            (staging / release.release_id).rename(final)


async def import_registered_bundle(
    sessions: async_sessionmaker[AsyncSession],
    path: Path,
    *,
    root: Path,
    expected_checksum: str | None = None,
    context: ResearchOperationContext | None = None,
    bundle_id: str | None = None,
    job_id: str | None = None,
) -> ReleaseBundle:
    """完整校验后发布目录, 随后一次提交目录索引、日历与标的登记。"""
    root, temporary = _staging_directory(root)
    try:
        staging = Path(temporary.name)
        if expected_checksum is not None and (
            await asyncio.to_thread(bundle_file_checksum, path)
        ) != expected_checksum:
            raise ReleaseBundleError("bundle_preview_changed")
        bundle = await _complete_file_io(lambda: unpack_release_bundle(path, staging))
        async with sessions.begin() as session:
            # 对共享 release 统一排序, 允许同包并发幂等重试。
            for release_id in sorted(release.release_id for release in bundle.releases):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:release_id))"),
                    {"release_id": release_id},
                )
            mismatched = await _release_conflicts(session, bundle)
            mismatched += await asyncio.to_thread(_existing_conflicts, root, bundle.releases)
            if mismatched:
                raise ReleaseBundleError(f"bundle_release_conflict:{','.join(sorted(set(mismatched)))}")
            if await _calendar_conflicts(session, bundle.calendar):
                raise ReleaseBundleError("bundle_calendar_conflict")
            await _add_metadata(session, bundle)
            if await _calendar_conflicts(session, bundle.calendar):
                raise ReleaseBundleError("bundle_calendar_conflict")
            repository = ResearchDatasetReleaseRepository(session)
            for release in bundle.releases:
                await repository.publish(release)
            if context is not None:
                await ResearchAuditRecorder(session_maker=sessions).record(
                    session,
                    ResearchAuditEvent(
                        operation="bundle.import", outcome="succeeded", context=context,
                        entity_type="release_bundle", entity_id=bundle_id,
                        correlation={"job_id": job_id} if job_id else {},
                        params_summary={"release_count": len(bundle.releases)},
                    ),
                )
            await _complete_file_io(lambda: _activate_files(root, staging, bundle))
        return bundle
    finally:
        await _complete_file_io(temporary.cleanup)


def is_legacy_starter(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "starter.json" in archive.namelist()
    except zipfile.BadZipFile as exc:
        raise ReleaseBundleError("bundle_unreadable_archive") from exc
