"""全局数据目录的只读查询层(issue #509)。

目录列表/详情只做列投影,**绝不加载 ``research_dataset_releases.manifest``**
(本地全市场发布单份已达 ~90MB;``ResearchDatasetReleaseRepository.list()``
会对每行 ``from_dict`` 解析整份 manifest,目录路径禁用)。

逐标的读模型 ``research_dataset_release_instruments`` 是 manifest 的派生缓存:
新发布在 publish 事务内写入;历史发布由 :meth:`ensure_instruments` 用服务端
SQL 自愈(不占 API 进程内存,也不存在 manifest 回退读路径)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.datasets import (
    CatalogDataset,
    CatalogDatasetSpec,
    catalog_spec,
    catalog_spec_by_release_kind,
)
from finboard_data.releases import ReleaseDatasetKind
from finboard_persistence.models import (
    ResearchDatasetReleaseInstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchSyncBatchModel,
)


@dataclass(frozen=True, slots=True)
class ReleaseHeader:
    """发布头(不含 manifest 与逐标的数组)。"""

    release_id: str
    dataset_name: str
    source: str
    version: str
    dataset_kind: ReleaseDatasetKind
    schema_version: str
    start_date: date
    end_date: date
    adjustment: str
    symbol_count: int
    row_count: int
    coverage_pct: Decimal
    quality_status: str
    published_at: datetime
    release_checksum: str
    storage_uri: str
    capabilities: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class BatchHeader:
    """一次同步批次头。"""

    id: int
    dataset: str
    source: str
    dataset_version: str
    status: str
    quality_status: str | None
    expected_rows: int | None
    received_rows: int
    accepted_rows: int
    rejected_rows: int
    parameters: dict[str, object]
    error_summary: str | None
    started_at: datetime
    completed_at: datetime | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReleaseInstrumentRow:
    """发布内单个标的的覆盖快照(目录读模型)。"""

    release_id: str
    code: str
    name: str
    market: str
    instrument_type: str
    asset_class: str
    exchange: str | None
    listing_board: str
    start_date: date
    end_date: date
    row_count: int
    expected_sessions: int
    missing_sessions: int
    suspended_sessions: int
    anomaly_count: int
    duplicate_count: int
    coverage_pct: Decimal
    ready: bool
    issues: tuple[str, ...]
    list_date: date | None
    delist_date: date | None
    status: str


@dataclass(frozen=True, slots=True)
class PresenceRow:
    """某标的在一份发布中的出现记录。"""

    release_id: str
    dataset_kind: ReleaseDatasetKind
    version: str
    quality_status: str
    published_at: datetime
    start_date: date
    end_date: date
    coverage_pct: Decimal
    ready: bool


@dataclass(frozen=True, slots=True)
class ReleaseReferenceRow:
    """引用某发布的课题/运行/策略。"""

    kind: str
    ref_id: str
    status: str
    created_at: datetime | None


_KIND_EXPR = func.coalesce(
    ResearchDatasetReleaseModel.manifest["dataset_kind"].as_string(), "bars"
)

_RELEASE_COLUMNS = (
    ResearchDatasetReleaseModel.release_id,
    ResearchDatasetReleaseModel.dataset_name,
    ResearchDatasetReleaseModel.source,
    ResearchDatasetReleaseModel.version,
    _KIND_EXPR.label("dataset_kind"),
    ResearchDatasetReleaseModel.schema_version,
    ResearchDatasetReleaseModel.start_date,
    ResearchDatasetReleaseModel.end_date,
    ResearchDatasetReleaseModel.adjustment,
    ResearchDatasetReleaseModel.symbol_count,
    ResearchDatasetReleaseModel.row_count,
    ResearchDatasetReleaseModel.coverage_pct,
    ResearchDatasetReleaseModel.quality_status,
    ResearchDatasetReleaseModel.published_at,
    ResearchDatasetReleaseModel.release_checksum,
    ResearchDatasetReleaseModel.storage_uri,
    ResearchDatasetReleaseModel.capabilities,
)

_USABLE_STATUSES = ("passed", "warnings")

_INSERT_INSTRUMENTS_SQL = text(
    """
    INSERT INTO research_dataset_release_instruments (
        release_id, code, name, market, instrument_type, asset_class,
        exchange, listing_board, start_date, end_date, row_count,
        expected_sessions, missing_sessions, suspended_sessions,
        anomaly_count, duplicate_count, coverage_pct, ready, issues,
        list_date, delist_date, status
    )
    SELECT
        CAST(:release_id AS varchar(128)),
        e ->> 'code',
        COALESCE(e ->> 'name', ''),
        e ->> 'market',
        e ->> 'instrument_type',
        e ->> 'asset_class',
        e ->> 'exchange',
        COALESCE(e ->> 'listing_board', 'unknown'),
        (e ->> 'start_date')::date,
        (e ->> 'end_date')::date,
        COALESCE((e ->> 'row_count')::bigint, 0),
        COALESCE((e ->> 'expected_sessions')::integer, 0),
        COALESCE((e ->> 'missing_sessions')::integer, 0),
        COALESCE((e ->> 'suspended_sessions')::integer, 0),
        COALESCE((e ->> 'anomaly_count')::integer, 0),
        COALESCE((e ->> 'duplicate_count')::integer, 0),
        COALESCE((e ->> 'coverage_pct')::numeric, 0),
        COALESCE((e ->> 'ready')::boolean, TRUE),
        COALESCE(e -> 'issues', '[]'::jsonb),
        NULLIF(e ->> 'list_date', '')::date,
        NULLIF(e ->> 'delist_date', '')::date,
        COALESCE(e ->> 'status', 'unknown')
    FROM research_dataset_releases r,
         jsonb_array_elements(r.manifest::jsonb -> 'instruments') e
    WHERE r.release_id = CAST(:release_id AS varchar(128))
    ON CONFLICT (release_id, code) DO NOTHING
    """
)


class CatalogRepository:
    """目录查询 + 逐标的索引自愈。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 发布
    async def release_header(self, release_id: str) -> ReleaseHeader | None:
        row = (
            await self._session.execute(
                select(*_RELEASE_COLUMNS).where(
                    ResearchDatasetReleaseModel.release_id == release_id
                )
            )
        ).one_or_none()
        return _release_header(row) if row is not None else None

    async def latest_release(
        self,
        *,
        kind: ReleaseDatasetKind,
        source: str | None = None,
        usable_only: bool = True,
    ) -> ReleaseHeader | None:
        stmt = select(*_RELEASE_COLUMNS).where(_KIND_EXPR.in_([kind.value]))
        if source is not None:
            stmt = stmt.where(ResearchDatasetReleaseModel.source == source)
        if usable_only:
            stmt = stmt.where(
                ResearchDatasetReleaseModel.quality_status.in_(_USABLE_STATUSES)
            )
        stmt = stmt.order_by(ResearchDatasetReleaseModel.published_at.desc()).limit(1)
        row = (await self._session.execute(stmt)).one_or_none()
        return _release_header(row) if row is not None else None

    async def list_releases(
        self,
        *,
        kind: ReleaseDatasetKind | None = None,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ReleaseHeader], int]:
        conditions: list[Any] = []
        if kind is not None:
            conditions.append(_KIND_EXPR.in_([kind.value]))
        if source is not None:
            conditions.append(ResearchDatasetReleaseModel.source == source)
        count_stmt = select(func.count()).select_from(ResearchDatasetReleaseModel)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = select(*_RELEASE_COLUMNS)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = (
            stmt.order_by(ResearchDatasetReleaseModel.published_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [_release_header(row) for row in rows], total

    # ------------------------------------------------------------------ 批次
    async def latest_batch(self, *, dataset: str) -> BatchHeader | None:
        stmt = (
            select(ResearchSyncBatchModel)
            .where(ResearchSyncBatchModel.dataset == dataset)
            .order_by(
                func.coalesce(
                    ResearchSyncBatchModel.completed_at,
                    ResearchSyncBatchModel.created_at,
                ).desc(),
                ResearchSyncBatchModel.id.desc(),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _batch_header(row) if row is not None else None

    async def list_batches(
        self,
        *,
        dataset: str,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[BatchHeader], int]:
        conditions = [ResearchSyncBatchModel.dataset == dataset]
        if status is not None:
            conditions.append(ResearchSyncBatchModel.status == status)
        total = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(ResearchSyncBatchModel)
                    .where(*conditions)
                )
            ).scalar_one()
        )
        stmt = (
            select(ResearchSyncBatchModel)
            .where(*conditions)
            .order_by(
                func.coalesce(
                    ResearchSyncBatchModel.completed_at,
                    ResearchSyncBatchModel.created_at,
                ).desc(),
                ResearchSyncBatchModel.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_batch_header(row) for row in rows], total

    async def list_failed_batches(
        self, *, dataset: str | None = None, limit: int = 20
    ) -> list[BatchHeader]:
        stmt = select(ResearchSyncBatchModel).where(
            ResearchSyncBatchModel.status.in_(("failed", "quality_rejected"))
        )
        if dataset is not None:
            stmt = stmt.where(ResearchSyncBatchModel.dataset == dataset)
        stmt = stmt.order_by(
            func.coalesce(
                ResearchSyncBatchModel.completed_at,
                ResearchSyncBatchModel.created_at,
            ).desc()
        ).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_batch_header(row) for row in rows]

    # ------------------------------------------------------------------ 逐标的
    async def count_instruments(self, release_id: str) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(ResearchDatasetReleaseInstrumentModel)
                    .where(
                        ResearchDatasetReleaseInstrumentModel.release_id == release_id
                    )
                )
            ).scalar_one()
        )

    async def ensure_instruments(self, release_id: str) -> bool:
        """确保逐标的读模型完整;缺失时用 manifest 服务端 SQL 重建。

        返回 True 表示当前行数与该发布的 ``symbol_count`` 一致(或发布没有
        标的)。并发首读用事务级 advisory lock 串行化,重复执行幂等。
        """

        expected = (
            await self._session.execute(
                select(ResearchDatasetReleaseModel.symbol_count).where(
                    ResearchDatasetReleaseModel.release_id == release_id
                )
            )
        ).scalar_one_or_none()
        if expected is None:
            return False
        if expected == 0:
            return True
        count = await self.count_instruments(release_id)
        if count == expected:
            return True
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key), 0)"),
            {"key": f"release_instruments:{release_id}"},
        )
        count = await self.count_instruments(release_id)
        if count == expected:
            return True
        await self._session.execute(
            text(
                "DELETE FROM research_dataset_release_instruments "
                "WHERE release_id = :release_id"
            ),
            {"release_id": release_id},
        )
        await self._session.execute(
            _INSERT_INSTRUMENTS_SQL, {"release_id": release_id}
        )
        return await self.count_instruments(release_id) == expected

    async def gap_summary(self, release_id: str) -> tuple[int, int, int]:
        """(有缺口标的数, 缺口交易日总数, 期望交易日总数)。"""

        row = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    ResearchDatasetReleaseInstrumentModel.missing_sessions
                                    > 0,
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            ResearchDatasetReleaseInstrumentModel.missing_sessions
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            ResearchDatasetReleaseInstrumentModel.expected_sessions
                        ),
                        0,
                    ),
                ).where(
                    ResearchDatasetReleaseInstrumentModel.release_id == release_id
                )
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def list_instruments(
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
    ) -> tuple[list[ReleaseInstrumentRow], int]:
        conditions: list[Any] = [
            ResearchDatasetReleaseInstrumentModel.release_id == release_id
        ]
        if ready is not None:
            conditions.append(
                ResearchDatasetReleaseInstrumentModel.ready.is_(ready)
            )
        if missing_only:
            conditions.append(
                ResearchDatasetReleaseInstrumentModel.missing_sessions > 0
            )
        if market is not None:
            conditions.append(ResearchDatasetReleaseInstrumentModel.market == market)
        if instrument_type is not None:
            conditions.append(
                ResearchDatasetReleaseInstrumentModel.instrument_type
                == instrument_type
            )
        if q:
            pattern = f"%{q.strip()}%"
            conditions.append(
                or_(
                    ResearchDatasetReleaseInstrumentModel.code.ilike(pattern),
                    ResearchDatasetReleaseInstrumentModel.name.ilike(pattern),
                )
            )
        total = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(ResearchDatasetReleaseInstrumentModel)
                    .where(*conditions)
                )
            ).scalar_one()
        )
        stmt = (
            select(ResearchDatasetReleaseInstrumentModel)
            .where(*conditions)
            .order_by(ResearchDatasetReleaseInstrumentModel.code)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_instrument_row(row) for row in rows], total

    async def find_presence(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PresenceRow], int]:
        conditions: list[Any] = [ResearchDatasetReleaseInstrumentModel.code == code]
        if start is not None:
            conditions.append(ResearchDatasetReleaseInstrumentModel.end_date >= start)
        if end is not None:
            conditions.append(ResearchDatasetReleaseInstrumentModel.start_date <= end)
        total = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(ResearchDatasetReleaseInstrumentModel)
                    .join(
                        ResearchDatasetReleaseModel,
                        ResearchDatasetReleaseModel.release_id
                        == ResearchDatasetReleaseInstrumentModel.release_id,
                    )
                    .where(*conditions)
                )
            ).scalar_one()
        )
        stmt = (
            select(
                ResearchDatasetReleaseInstrumentModel.release_id,
                _KIND_EXPR,
                ResearchDatasetReleaseModel.version,
                ResearchDatasetReleaseModel.quality_status,
                ResearchDatasetReleaseModel.published_at,
                ResearchDatasetReleaseInstrumentModel.start_date,
                ResearchDatasetReleaseInstrumentModel.end_date,
                ResearchDatasetReleaseInstrumentModel.coverage_pct,
                ResearchDatasetReleaseInstrumentModel.ready,
            )
            .join(
                ResearchDatasetReleaseModel,
                ResearchDatasetReleaseModel.release_id
                == ResearchDatasetReleaseInstrumentModel.release_id,
            )
            .where(*conditions)
            .order_by(ResearchDatasetReleaseModel.published_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            PresenceRow(
                release_id=str(row[0]),
                dataset_kind=ReleaseDatasetKind(str(row[1])),
                version=str(row[2]),
                quality_status=str(row[3]),
                published_at=row[4],
                start_date=row[5],
                end_date=row[6],
                coverage_pct=row[7],
                ready=bool(row[8]),
            )
            for row in rows
        ], total

    async def release_ids_covering(
        self,
        symbols: tuple[str, ...],
        *,
        kind: ReleaseDatasetKind | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[str]:
        """返回同时覆盖全部 symbols(可选窗口/kind)的发布 ID。"""

        if not symbols:
            return []
        normalized = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
        if not normalized:
            return []
        conditions: list[Any] = [
            ResearchDatasetReleaseInstrumentModel.code.in_(normalized)
        ]
        if start is not None:
            conditions.append(ResearchDatasetReleaseInstrumentModel.end_date >= start)
        if end is not None:
            conditions.append(ResearchDatasetReleaseInstrumentModel.start_date <= end)
        stmt = (
            select(ResearchDatasetReleaseInstrumentModel.release_id)
            .where(*conditions)
            .group_by(ResearchDatasetReleaseInstrumentModel.release_id)
            .having(
                func.count(
                    func.distinct(ResearchDatasetReleaseInstrumentModel.code)
                )
                == len(normalized)
            )
        )
        if kind is not None:
            stmt = stmt.join(
                ResearchDatasetReleaseModel,
                ResearchDatasetReleaseModel.release_id
                == ResearchDatasetReleaseInstrumentModel.release_id,
            ).where(_KIND_EXPR.in_([kind.value]))
        return [str(row[0]) for row in (await self._session.execute(stmt)).all()]

    # ------------------------------------------------------------------ 引用
    async def list_references(
        self, release_id: str, *, limit: int = 20
    ) -> list[ReleaseReferenceRow]:
        rows: list[ReleaseReferenceRow] = []

        run_stmt = text(
            """
            SELECT run_id, status, created_at
            FROM research_runs
            WHERE jsonb_path_exists(
                manifest::jsonb,
                '$.dataset_releases[*] ? (@.artifact_id == $rid)',
                jsonb_build_object('rid', CAST(:release_id AS text))
            )
            ORDER BY created_at DESC
            LIMIT CAST(:limit AS integer)
            """
        )
        for row in (await self._session.execute(run_stmt, {
            "release_id": release_id,
            "limit": limit,
        })).all():
            rows.append(
                ReleaseReferenceRow("research_run", str(row[0]), str(row[1]), row[2])
            )

        spec_stmt = text(
            """
            SELECT strategy_id, version, status, published_at, created_at
            FROM research_strategy_specs
            WHERE (payload::jsonb) -> 'validation_plan' -> 'dataset_release_ids'
                  @> jsonb_build_array(CAST(:release_id AS text))
            ORDER BY published_at DESC NULLS LAST, created_at DESC
            LIMIT CAST(:limit AS integer)
            """
        )
        for row in (await self._session.execute(spec_stmt, {
            "release_id": release_id,
            "limit": limit,
        })).all():
            rows.append(
                ReleaseReferenceRow(
                    "strategy_spec",
                    f"{row[0]}@{row[1]}",
                    str(row[2]),
                    row[3] or row[4],
                )
            )

        topic_stmt = text(
            """
            SELECT e.topic_id, e.entry_id,
                   COALESCE(t.payload::jsonb #>> '{topic,status}', 'unknown') AS status,
                   e.created_at
            FROM research_topic_entries e
            JOIN research_topics t ON t.topic_id = e.topic_id
            WHERE (e.payload::jsonb) -> 'source_refs'
                  @> jsonb_build_array(
                      jsonb_build_object(
                          'kind', 'dataset',
                          'ref_id', CAST(:release_id AS text)
                      )
                  )
            ORDER BY e.created_at DESC
            LIMIT CAST(:limit AS integer)
            """
        )
        for row in (await self._session.execute(topic_stmt, {
            "release_id": release_id,
            "limit": limit,
        })).all():
            rows.append(
                ReleaseReferenceRow(
                    "research_topic",
                    f"{row[0]}:{row[1]}",
                    str(row[2]),
                    row[3],
                )
            )

        rows.sort(
            key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return rows[:limit]

    # ------------------------------------------------------------------ 课题数据范围
    async def topic_payload(self, topic_id: str) -> dict[str, Any] | None:
        """课题当前 payload;课题不存在返回 ``None``(目录侧映射 404)。"""
        row = (
            await self._session.execute(
                text("SELECT payload FROM research_topics WHERE topic_id = :topic_id"),
                {"topic_id": topic_id},
            )
        ).first()
        return dict(row[0]) if row is not None else None

    async def topic_source_refs(
        self, topic_id: str, *, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """课题全部轮次的 ``source_refs``(按轮次倒序,未去重)。

        返回 ``(refs, truncated)``;条目数达到 ``limit`` 即视为截断,调用方
        需要把截断暴露在警告里,不能静默当全量。
        """
        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT payload::jsonb -> 'source_refs' AS refs
                    FROM research_topic_entries
                    WHERE topic_id = :topic_id
                      AND jsonb_typeof(payload::jsonb -> 'source_refs') = 'array'
                    ORDER BY id DESC
                    LIMIT CAST(:limit AS integer)
                    """
                ),
                {"topic_id": topic_id, "limit": limit},
            )
        ).all()
        refs: list[dict[str, Any]] = []
        for row in rows:
            for item in row[0] or ():
                if isinstance(item, dict):
                    refs.append(dict(item))
        return refs, len(rows) >= limit

    async def run_dataset_releases(self, run_id: str) -> list[str] | None:
        """研究运行 manifest 冻结的数据发布 ID;运行不存在返回 ``None``。

        只取 ``manifest.dataset_releases[*].artifact_id``,不加载整份 manifest
        (单份可达 ~90MB,课题视图只做有限行投影)。
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT manifest::jsonb -> 'dataset_releases' AS refs "
                    "FROM research_runs WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            )
        ).first()
        if row is None:
            return None
        ids: list[str] = []
        for item in row[0] or ():
            if not isinstance(item, dict):
                continue
            value = item.get("artifact_id") or item.get("release_id")
            if value:
                ids.append(str(value))
        return ids


def _release_header(row: Any) -> ReleaseHeader:
    values = tuple(row)
    return ReleaseHeader(
        release_id=str(values[0]),
        dataset_name=str(values[1]),
        source=str(values[2]),
        version=str(values[3]),
        dataset_kind=ReleaseDatasetKind(str(values[4])),
        schema_version=str(values[5]),
        start_date=values[6],
        end_date=values[7],
        adjustment=str(values[8]),
        symbol_count=int(values[9]),
        row_count=int(values[10]),
        coverage_pct=values[11],
        quality_status=str(values[12]),
        published_at=values[13],
        release_checksum=str(values[14]),
        storage_uri=str(values[15]),
        capabilities=tuple(values[16] or ()),
    )


def _batch_header(row: ResearchSyncBatchModel) -> BatchHeader:
    return BatchHeader(
        id=int(row.id),
        dataset=row.dataset,
        source=row.source,
        dataset_version=row.dataset_version,
        status=row.status,
        quality_status=row.quality_status,
        expected_rows=row.expected_rows,
        received_rows=row.received_rows,
        accepted_rows=row.accepted_rows,
        rejected_rows=row.rejected_rows,
        parameters=dict(row.parameters or {}),
        error_summary=row.error_summary,
        started_at=row.started_at,
        completed_at=row.completed_at,
        published_at=row.published_at,
    )


def _instrument_row(row: ResearchDatasetReleaseInstrumentModel) -> ReleaseInstrumentRow:
    return ReleaseInstrumentRow(
        release_id=row.release_id,
        code=row.code,
        name=row.name,
        market=row.market,
        instrument_type=row.instrument_type,
        asset_class=row.asset_class,
        exchange=row.exchange,
        listing_board=row.listing_board,
        start_date=row.start_date,
        end_date=row.end_date,
        row_count=int(row.row_count),
        expected_sessions=int(row.expected_sessions),
        missing_sessions=int(row.missing_sessions),
        suspended_sessions=int(row.suspended_sessions),
        anomaly_count=int(row.anomaly_count),
        duplicate_count=int(row.duplicate_count),
        coverage_pct=row.coverage_pct,
        ready=bool(row.ready),
        issues=tuple(str(item) for item in (row.issues or ())),
        list_date=row.list_date,
        delist_date=row.delist_date,
        status=row.status,
    )


def catalog_spec_for_kind(kind: ReleaseDatasetKind) -> CatalogDatasetSpec:
    """发布 kind -> 目录规格(便捷包装,避免调用方各自映射)。"""

    return catalog_spec_by_release_kind(kind)


def catalog_spec_for_dataset(dataset: CatalogDataset | str) -> CatalogDatasetSpec:
    """目录 key -> 规格。"""

    return catalog_spec(dataset)


__all__ = [
    "BatchHeader",
    "CatalogRepository",
    "PresenceRow",
    "ReleaseHeader",
    "ReleaseInstrumentRow",
    "ReleaseReferenceRow",
    "catalog_spec_for_dataset",
    "catalog_spec_for_kind",
]
