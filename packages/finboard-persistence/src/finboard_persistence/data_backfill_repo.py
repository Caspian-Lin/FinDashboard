"""补数请求仓储(issue #511)。

两张表的分工与语义:

* ``data_backfill_requests`` —— 请求头:冻结的 ``spec`` / ``plan``、幂等键、
  fingerprint、状态机、复用与重试血缘、job 与产出发布;
* ``data_backfill_request_items`` —— 逐数据集明细:动作、窗口、冻结标的、
  缺口估算、批次/发布产物与逐项错误。

并发要点:
* 明细领取用 ``UPDATE ... WHERE status IN ('pending','failed') RETURNING``
  原子完成(与 worker 的 ``FOR UPDATE SKIP LOCKED`` 同精神):重复执行时已被
 同请求其他执行器领走的 item 直接跳过,不会重复消耗上游配额;
* 状态迁移只在允许的源状态上生效,非法迁移返回 None 而不是静默改状态。

边界:本仓储只读写补数两张表;``job_id`` / ``release_ids`` 只做字符串引用,
不建指向实线表(``background_jobs`` / ``research_dataset_releases``)的外键。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Select, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
)

#: 可被同幂等键新建取代的死状态(与 background_jobs #371 同语义)。
_REQUEST_RECREATABLE_STATUSES = frozenset({"failed", "cancelled"})

#: 明细可被原子重领的状态(失败项允许在同一请求内重建)。
_ITEM_CLAIMABLE_STATUSES = ("pending", "failed")


class DataBackfillRepository:
    """补数请求读写入口;``__init__`` 只持有 session,不自己 commit。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ create
    async def create_request(
        self,
        *,
        request_id: str,
        idempotency_key: str,
        fingerprint: str,
        scope_checksum: str,
        status: str,
        spec: dict[str, Any],
        plan: dict[str, Any],
        requested_by: str,
        actor_type: str,
        reused_request_ids: Sequence[str] = (),
        retry_of_request_id: str | None = None,
        job_id: str | None = None,
        blockers: Sequence[Mapping[str, Any]] = (),
        error_code: str | None = None,
        error_summary: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> DataBackfillRequestModel:
        row = DataBackfillRequestModel(
            request_id=request_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            scope_checksum=scope_checksum,
            status=status,
            spec=dict(spec),
            plan=dict(plan),
            reused_request_ids=list(reused_request_ids),
            retry_of_request_id=retry_of_request_id,
            job_id=job_id,
            release_ids=[],
            blockers=[dict(item) for item in blockers],
            requested_by=requested_by,
            actor_type=actor_type,
            error_code=error_code,
            error_summary=error_summary,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_items(
        self, request_id: str, items: Sequence[Mapping[str, Any]]
    ) -> list[DataBackfillRequestItemModel]:
        rows = [
            DataBackfillRequestItemModel(
                request_id=request_id,
                dataset=str(item["dataset"]),
                source=str(item["source"]),
                adjustment=str(item.get("adjustment") or "none"),
                action=str(item["action"]),
                start_date=item["start_date"],
                end_date=item["end_date"],
                symbols=list(item.get("symbols") or ()),
                status=str(item.get("status") or "pending"),
                estimated_requests=int(item.get("estimated_requests") or 0),
                batch_ids=list(item.get("batch_ids") or ()),
                release_id=item.get("release_id"),
                accepted_rows=int(item.get("accepted_rows") or 0),
                rejected_rows=int(item.get("rejected_rows") or 0),
                error_code=item.get("error_code"),
                error_summary=item.get("error_summary"),
            )
            for item in items
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    # -------------------------------------------------------------------- read
    async def get(
        self, request_id: str, *, for_update: bool = False
    ) -> DataBackfillRequestModel | None:
        stmt = select(DataBackfillRequestModel).where(
            DataBackfillRequestModel.request_id == request_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> DataBackfillRequestModel | None:
        """按幂等键取「当前」请求:活跃行优先,无活跃行时取最新死行。"""

        stmt = (
            select(DataBackfillRequestModel)
            .where(DataBackfillRequestModel.idempotency_key == idempotency_key)
            .order_by(
                case(
                    (
                        DataBackfillRequestModel.status.in_(
                            _REQUEST_RECREATABLE_STATUSES
                        ),
                        1,
                    ),
                    else_=0,
                ),
                DataBackfillRequestModel.created_at.desc(),
                DataBackfillRequestModel.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_by_fingerprint(
        self,
        fingerprint: str,
        *,
        statuses: Sequence[str],
        exclude_request_id: str | None = None,
    ) -> list[DataBackfillRequestModel]:
        """同 fingerprint 的在途请求(去重复用;不含已终态失败行)。"""

        stmt = select(DataBackfillRequestModel).where(
            DataBackfillRequestModel.fingerprint == fingerprint,
            DataBackfillRequestModel.status.in_(tuple(statuses)),
        )
        if exclude_request_id is not None:
            stmt = stmt.where(
                DataBackfillRequestModel.request_id != exclude_request_id
            )
        stmt = stmt.order_by(DataBackfillRequestModel.created_at.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_requests(
        self,
        *,
        statuses: Sequence[str] | None = None,
        dataset: str | None = None,
        requested_by: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[DataBackfillRequestModel], int]:
        conditions: list[Any] = []
        if statuses:
            conditions.append(DataBackfillRequestModel.status.in_(tuple(statuses)))
        if requested_by:
            conditions.append(DataBackfillRequestModel.requested_by == requested_by)
        if created_from is not None:
            conditions.append(DataBackfillRequestModel.created_at >= created_from)
        if created_to is not None:
            conditions.append(DataBackfillRequestModel.created_at <= created_to)
        if dataset:
            conditions.append(
                select(DataBackfillRequestItemModel.id)
                .where(
                    DataBackfillRequestItemModel.request_id
                    == DataBackfillRequestModel.request_id,
                    DataBackfillRequestItemModel.dataset == dataset,
                )
                .exists()
            )
        count_stmt: Select[Any] = select(func.count()).select_from(
            DataBackfillRequestModel
        )
        stmt: Select[Any] = select(DataBackfillRequestModel)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            stmt = stmt.where(*conditions)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = (
            stmt.order_by(
                DataBackfillRequestModel.created_at.desc(),
                DataBackfillRequestModel.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get_many(
        self, request_ids: Sequence[str]
    ) -> list[DataBackfillRequestModel]:
        """按 ID 批量取请求头(在途覆盖的来源状态用)。"""

        if not request_ids:
            return []
        stmt = select(DataBackfillRequestModel).where(
            DataBackfillRequestModel.request_id.in_(tuple(request_ids))
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_items(self, request_id: str) -> list[DataBackfillRequestItemModel]:
        stmt = (
            select(DataBackfillRequestItemModel)
            .where(DataBackfillRequestItemModel.request_id == request_id)
            .order_by(
                DataBackfillRequestItemModel.dataset,
                DataBackfillRequestItemModel.source,
                DataBackfillRequestItemModel.id,
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def inflight_items(
        self,
        *,
        datasets: Sequence[str],
        statuses: Sequence[str],
        window_end: date | None = None,
    ) -> list[DataBackfillRequestItemModel]:
        """在途请求的明细(用于缺口 diff 的「排队中」半边)。

        只取请求仍处 ``statuses`` 的 item;``window_end`` 用于把明显不相关的
        历史窗口滤掉(区间交集判断仍在 planner 里做,SQL 只做粗筛)。
        """

        if not datasets:
            return []
        stmt = (
            select(DataBackfillRequestItemModel)
            .join(
                DataBackfillRequestModel,
                DataBackfillRequestModel.request_id
                == DataBackfillRequestItemModel.request_id,
            )
            .where(
                DataBackfillRequestModel.status.in_(tuple(statuses)),
                DataBackfillRequestItemModel.dataset.in_(tuple(datasets)),
            )
        )
        if window_end is not None:
            stmt = stmt.where(DataBackfillRequestItemModel.start_date <= window_end)
        stmt = stmt.order_by(DataBackfillRequestItemModel.id.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    # --------------------------------------------------------------- transition
    async def claim_item(
        self, item_id: int
    ) -> DataBackfillRequestItemModel | None:
        """原子领取一个明细(``pending|failed -> running``);领不到返回 None。"""

        stmt = (
            update(DataBackfillRequestItemModel)
            .where(
                DataBackfillRequestItemModel.id == item_id,
                DataBackfillRequestItemModel.status.in_(_ITEM_CLAIMABLE_STATUSES),
            )
            .values(status="running", updated_at=datetime.now(UTC))
            .returning(DataBackfillRequestItemModel)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update_item(
        self, item_id: int, *, status: str | None = None, **values: Any
    ) -> DataBackfillRequestItemModel | None:
        payload: dict[str, Any] = dict(values)
        if status is not None:
            payload["status"] = status
        payload["updated_at"] = datetime.now(UTC)
        stmt = (
            update(DataBackfillRequestItemModel)
            .where(DataBackfillRequestItemModel.id == item_id)
            .values(**payload)
            .returning(DataBackfillRequestItemModel)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def set_request_status(
        self,
        request_id: str,
        *,
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        job_id: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> DataBackfillRequestModel | None:
        row = await self.get(request_id, for_update=True)
        if row is None:
            return None
        now = datetime.now(UTC)
        row.status = status
        row.error_code = error_code
        row.error_summary = error_summary
        if job_id is not None:
            row.job_id = job_id
        if started and row.started_at is None:
            row.started_at = now
        if finished:
            row.completed_at = now
        row.updated_at = now
        await self._session.flush()
        return row

    async def add_release_id(
        self, request_id: str, release_id: str
    ) -> DataBackfillRequestModel | None:
        """登记产出发布(幂等:同一 release_id 只追加一次)。"""

        row = await self.get(request_id, for_update=True)
        if row is None:
            return None
        existing = list(row.release_ids or [])
        if release_id not in existing:
            existing.append(release_id)
            row.release_ids = existing
            row.updated_at = datetime.now(UTC)
            await self._session.flush()
        return row

    async def mark_cancelled_items(self, request_id: str) -> int:
        """把仍未完成的明细标 ``cancelled``(已成功的保留其发布)。"""

        stmt = (
            update(DataBackfillRequestItemModel)
            .where(
                DataBackfillRequestItemModel.request_id == request_id,
                DataBackfillRequestItemModel.status.in_(("pending", "running")),
            )
            .values(status="cancelled", updated_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        return int(cast(CursorResult[Any], result).rowcount or 0)


__all__ = ["DataBackfillRepository"]
