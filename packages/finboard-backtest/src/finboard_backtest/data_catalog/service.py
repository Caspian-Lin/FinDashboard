"""全局数据目录服务(issue #509)。

只做 DB 列投影 + 有界文件系统扫描;逐标的明细走
``research_dataset_release_instruments`` 读模型,缺失时由
:class:`CatalogRepository.ensure_instruments` 用服务端 SQL 自愈。
不加载发布 manifest,不扫描 release parquet 全目录。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_catalog.contracts import (
    STATUS_BLOCKED,
    STATUS_INGESTED_NOT_PUBLISHED,
    STATUS_NOT_INGESTED,
    STATUS_STALE,
    STATUS_USABLE,
    CalendarView,
    CatalogEntry,
    CatalogError,
    CatalogQuery,
    CoverageReport,
    DatasetDetail,
    InstrumentPage,
    PresencePage,
    ReferencePage,
)
from finboard_data.datasets import (
    CatalogDataset,
    CatalogDatasetSpec,
    CatalogStorage,
    catalog_spec,
    catalog_specs,
)
from finboard_persistence.catalog_repo import (
    BatchHeader,
    CatalogRepository,
    ReleaseHeader,
)
from finboard_persistence.trade_calendar_repo import TradeCalRepository

#: 超过该天数未更新即视为 stale(目录只提示,不阻断运行)。
FRESHNESS_DAYS = 30

_DEEP_LINKS = {
    "research_run": "/research/runs?run={ref}",
    "strategy_spec": "/research/strategy?strategy={ref}",
    "research_topic": "/research/topics?topic={ref}",
}


class DataCatalogService:
    """目录列表 / 数据集详情 / 覆盖报告 / 发布逐标的 / 标的反查。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cache_dir: str | Path = "data_cache",
        release_root: str | Path = "data_releases",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._repo = CatalogRepository(session)
        self._cache_dir = Path(cache_dir)
        self._release_root = Path(release_root)
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ 列表
    async def list_entries(
        self, query: CatalogQuery
    ) -> tuple[tuple[CatalogEntry, ...], int]:
        specs = catalog_specs()
        if query.dataset is not None:
            specs = tuple(
                spec for spec in specs if spec.key is query.dataset
            )
        entries: list[CatalogEntry] = []
        for spec in specs:
            entry = await self._build_entry(spec)
            entries.append(entry)
        if query.status is not None:
            entries = [entry for entry in entries if entry.status == query.status]
        if query.q:
            needle = query.q.strip().lower()
            entries = [
                entry
                for entry in entries
                if needle in entry.key.value
                or needle in entry.title.lower()
                or needle in entry.title_en.lower()
            ]
        total = len(entries)
        start = max(query.offset, 0)
        return tuple(entries[start : start + query.limit]), total

    async def get_dataset(self, key: CatalogDataset | str) -> DatasetDetail:
        spec = catalog_spec(key)
        entry = await self._build_entry(spec)
        releases = (
            await self._repo.list_releases(kind=spec.release_kind, limit=20)
            if spec.release_kind is not None
            else ([], 0)
        )
        batches = (
            await self._repo.list_batches(dataset=spec.sync_dataset, limit=20)
            if spec.sync_dataset is not None
            else ([], 0)
        )
        failures = (
            await self._repo.list_failed_batches(
                dataset=spec.sync_dataset, limit=20
            )
            if spec.sync_dataset is not None
            else []
        )
        return DatasetDetail(
            entry=entry,
            spec=spec,
            releases=tuple(releases[0]),
            batches=tuple(batches[0]),
            failures=tuple(failures),
            adjustment=spec.adjustment,
        )

    async def list_batches(
        self,
        key: CatalogDataset | str,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[tuple[BatchHeader, ...], int]:
        spec = catalog_spec(key)
        if spec.sync_dataset is None:
            return (), 0
        rows, total = await self._repo.list_batches(
            dataset=spec.sync_dataset,
            status=status,
            limit=limit,
            offset=offset,
        )
        return tuple(rows), total

    async def release_header(self, release_id: str) -> ReleaseHeader | None:
        return await self._repo.release_header(release_id)

    # ------------------------------------------------------------------ 覆盖
    async def get_coverage(
        self,
        key: CatalogDataset | str,
        *,
        release_id: str | None = None,
        limit: int = 200,
    ) -> CoverageReport:
        spec = catalog_spec(key)
        if spec.release_kind is None:
            batch = (
                await self._repo.latest_batch(dataset=spec.sync_dataset)
                if spec.sync_dataset
                else None
            )
            if batch is None:
                return CoverageReport(
                    dataset=spec.key,
                    denominator=spec.gap_denominator,
                    warnings=("尚无同步批次",),
                )
            coverage = None
            if batch.received_rows:
                coverage = (
                    Decimal(batch.accepted_rows) / Decimal(batch.received_rows)
                )
            return CoverageReport(
                dataset=spec.key,
                denominator=spec.gap_denominator,
                batch=batch,
                coverage_pct=coverage,
                warnings=tuple(
                    [batch.error_summary] if batch.error_summary else []
                ),
            )

        release = (
            await self._repo.release_header(release_id)
            if release_id
            else await self._repo.latest_release(kind=spec.release_kind)
        )
        if release is None:
            return CoverageReport(
                dataset=spec.key,
                denominator=spec.gap_denominator,
                warnings=("尚无可用发布",),
            )
        if not await self._repo.ensure_instruments(release.release_id):
            return CoverageReport(
                dataset=spec.key,
                denominator=spec.gap_denominator,
                release=release,
                warnings=("逐标的索引重建失败: manifest 与发布行不一致",),
            )
        symbols_with_gaps, missing_sessions, expected_sessions = (
            await self._repo.gap_summary(release.release_id)
        )
        preview, _ = await self._repo.list_instruments(
            release.release_id, missing_only=True, limit=limit
        )
        return CoverageReport(
            dataset=spec.key,
            denominator=spec.gap_denominator,
            release=release,
            coverage_pct=release.coverage_pct,
            expected_sessions=expected_sessions,
            missing_sessions=missing_sessions,
            symbols_with_gaps=symbols_with_gaps,
            missing_preview=tuple(preview),
        )

    # ------------------------------------------------------------------ 发布
    async def list_release_instruments(
        self,
        release_id: str,
        *,
        ready: bool | None = None,
        missing_only: bool = False,
        market: str | None = None,
        instrument_type: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> InstrumentPage:
        release = await self._repo.release_header(release_id)
        if release is None:
            raise CatalogError("release_not_found", f"未找到研究数据发布: {release_id}")
        if not await self._repo.ensure_instruments(release_id):
            raise CatalogError(
                "release_index_unavailable",
                f"发布 {release_id} 的逐标的索引不可重建",
            )
        rows, total = await self._repo.list_instruments(
            release_id,
            ready=ready,
            missing_only=missing_only,
            market=market,
            instrument_type=instrument_type,
            q=q,
            limit=limit,
            offset=offset,
        )
        return InstrumentPage(
            release_id=release_id, items=tuple(rows), total=total
        )

    async def find_instrument_presence(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PresencePage:
        headers, _ = await self._repo.list_releases(limit=200)
        warnings: list[str] = []
        for header in headers:
            if start is not None and header.end_date < start:
                continue
            if end is not None and header.start_date > end:
                continue
            try:
                if not await self._repo.ensure_instruments(header.release_id):
                    warnings.append(
                        f"发布 {header.release_id} 的逐标的索引不可重建"
                    )
            except Exception as exc:
                warnings.append(f"发布 {header.release_id} 索引重建失败: {exc}")
        rows, total = await self._repo.find_presence(
            code, start=start, end=end, limit=limit, offset=offset
        )
        return PresencePage(
            code=code, items=tuple(rows), total=total, warnings=tuple(warnings)
        )

    async def list_release_references(
        self, release_id: str, *, limit: int = 20
    ) -> ReferencePage:
        release = await self._repo.release_header(release_id)
        if release is None:
            raise CatalogError("release_not_found", f"未找到研究数据发布: {release_id}")
        items = await self._repo.list_references(release_id, limit=limit)
        links: dict[str, str] = {}
        for item in items:
            template = _DEEP_LINKS.get(item.kind)
            if template is None:
                continue
            ref = item.ref_id.split("@", 1)[0].split(":", 1)[0]
            links[f"{item.kind}:{item.ref_id}"] = template.format(ref=ref)
        return ReferencePage(
            release_id=release_id, items=tuple(items), deep_links=links
        )

    # ------------------------------------------------------------------ 日历
    async def get_calendar(
        self, market: str, start: date, end: date
    ) -> CalendarView:
        days = await TradeCalRepository(self._session).list_trading_days(
            exchange=market
        )
        selected = tuple(sorted(day for day in days if start <= day <= end))
        return CalendarView(
            market=market,
            start=start,
            end=end,
            days=selected,
            source="trade_cal" if days else "trade_cal_missing",
        )

    # ------------------------------------------------------------------ 内部
    async def _build_entry(self, spec: CatalogDatasetSpec) -> CatalogEntry:
        release = (
            await self._repo.latest_release(kind=spec.release_kind)
            if spec.release_kind is not None
            else None
        )
        batch = (
            await self._repo.latest_batch(dataset=spec.sync_dataset)
            if spec.sync_dataset is not None
            else None
        )
        cache_files = 0
        cache_bytes = 0
        cache_updated: datetime | None = None
        if spec.storage is CatalogStorage.BARS_CACHE:
            cache_files, cache_bytes, cache_updated = await asyncio.to_thread(
                _scan_parquet_cache, self._cache_dir
            )

        warnings: list[str] = []
        if batch is not None and batch.error_summary:
            warnings.append(batch.error_summary)
        if spec.storage is CatalogStorage.BARS_CACHE and cache_files == 0:
            warnings.append(f"缓存目录无 parquet: {self._cache_dir}")

        updated_at = _max_datetime(
            release.published_at if release else None,
            batch.completed_at if batch else None,
            cache_updated,
        )
        status = _status_for(release=release, batch=batch, cache_files=cache_files)
        if status == STATUS_STALE:
            warnings.append(f"最近更新超过 {FRESHNESS_DAYS} 天")
        if spec.storage is CatalogStorage.BARS_CACHE and release is None:
            status = (
                STATUS_INGESTED_NOT_PUBLISHED
                if cache_files
                else STATUS_NOT_INGESTED
            )

        local_start, local_end = _local_window(release=release, batch=batch)
        return CatalogEntry(
            key=spec.key,
            title=spec.title,
            title_en=spec.title_en,
            storage=spec.storage,
            publishable=spec.publishable,
            source=spec.source,
            status=status,
            release=release,
            batch=batch,
            coverage_pct=release.coverage_pct if release else None,
            symbol_count=release.symbol_count if release else None,
            row_count=release.row_count if release else None,
            denominator=spec.gap_denominator,
            local_start=local_start,
            local_end=local_end,
            updated_at=updated_at,
            cache_files=cache_files,
            cache_bytes=cache_bytes,
            warnings=tuple(warnings),
        )


def _status_for(
    *,
    release: ReleaseHeader | None,
    batch: BatchHeader | None,
    cache_files: int,
) -> str:
    now = datetime.now(UTC)
    if release is None and batch is None:
        if cache_files:
            return STATUS_INGESTED_NOT_PUBLISHED
        return STATUS_NOT_INGESTED
    if batch is not None and batch.status in ("failed", "quality_rejected"):
        batch_time = batch.completed_at or batch.started_at
        if release is None or batch_time > release.published_at:
            return STATUS_BLOCKED
    if release is None:
        return STATUS_INGESTED_NOT_PUBLISHED
    if now - release.published_at > timedelta(days=FRESHNESS_DAYS):
        return STATUS_STALE
    return STATUS_USABLE


def _local_window(
    *, release: ReleaseHeader | None, batch: BatchHeader | None
) -> tuple[date | None, date | None]:
    if batch is not None:
        params = batch.parameters
        trade_date = params.get("trade_date")
        if isinstance(trade_date, str):
            try:
                day = date.fromisoformat(trade_date[:10])
            except ValueError:
                day = None
            if day is not None:
                return day, day
        start = params.get("start_date")
        end = params.get("end_date")
        if isinstance(start, str) and isinstance(end, str):
            try:
                return (
                    date.fromisoformat(start[:10]),
                    date.fromisoformat(end[:10]),
                )
            except ValueError:
                pass
    if release is not None:
        return release.start_date, release.end_date
    return None, None


def _max_datetime(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)


def _scan_parquet_cache(cache_dir: Path) -> tuple[int, int, datetime | None]:
    if not cache_dir.is_dir():
        return 0, 0, None
    files = 0
    total = 0
    latest: float | None = None
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                if not entry.is_file() or not entry.name.endswith(".parquet"):
                    continue
                files += 1
                stat = entry.stat()
                total += stat.st_size
                if latest is None or stat.st_mtime > latest:
                    latest = stat.st_mtime
    except OSError:
        return 0, 0, None
    updated = (
        datetime.fromtimestamp(latest, tz=UTC) if latest is not None else None
    )
    return files, total, updated


__all__ = ["FRESHNESS_DAYS", "DataCatalogService"]
