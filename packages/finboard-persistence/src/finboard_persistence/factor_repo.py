"""因子快照与因子值 Repository。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.factors import (
    FactorName,
    FactorSnapshot,
    FactorSnapshotStatus,
    FactorValue,
)
from finboard_persistence.models import FactorSnapshotModel, FactorValueModel


class FactorSnapshotRepository:
    """按内容校验和幂等保存、回读冻结快照。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_factor_snapshot(self, snapshot: FactorSnapshot) -> int:
        """保存完整快照;相同 checksum 复用原记录。

        INSERT ... ON CONFLICT DO NOTHING 由唯一索引原子仲裁 checksum:
        后台任务并发保存同一快照时(互相看不到对方未提交的行),只有一方
        真正插入,另一方直接复用已存在行;不再出现 SELECT-then-INSERT
        双双 INSERT 撞唯一约束的竞态。仅真正新插入时才写 FactorValue 行。
        """
        statement = (
            pg_insert(FactorSnapshotModel)
            .values(
                decision_at=snapshot.decision_at,
                business_date=snapshot.business_date,
                effective_date=snapshot.effective_date,
                source=snapshot.source,
                dataset_versions=snapshot.dataset_versions,
                factor_version=snapshot.factor_version,
                static_universe=list(snapshot.static_universe),
                selected_symbols=list(snapshot.selected_symbols),
                config=snapshot.config,
                status=snapshot.status.value,
                skip_reason=snapshot.skip_reason,
                warnings=list(snapshot.warnings),
                checksum=snapshot.checksum,
            )
            .on_conflict_do_nothing(
                index_elements=[FactorSnapshotModel.checksum]
            )
            .returning(FactorSnapshotModel.id)
        )
        inserted_id = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        if inserted_id is None:
            # 冲突路径:并发事务(或同事务先前保存)已插入该 checksum。
            existing = await self._model_by_checksum(snapshot.checksum)
            if existing is None:
                # READ COMMITTED 下仲裁通过的冲突行对本事务可见,正常到不了
                # 这里;防御性报错,避免把 None 当成快照 id 返回给调用方。
                raise RuntimeError(
                    "factor snapshot checksum conflict but row not found: "
                    f"{snapshot.checksum}"
                )
            return existing.id

        self._session.add_all(
            [
                FactorValueModel(
                    snapshot_id=inserted_id,
                    symbol=value.symbol,
                    factor_name=value.factor_name.value,
                    factor_version=snapshot.factor_version,
                    value=value.value,
                    global_rank=value.global_rank,
                    industry_rank=value.industry_rank,
                    industry_code=value.industry_code,
                )
                for value in snapshot.values
            ]
        )
        await self._session.flush()
        return inserted_id

    async def get_by_checksum(self, checksum: str) -> FactorSnapshot | None:
        """按内容校验和回读可复现快照。"""
        row = await self._model_by_checksum(checksum)
        if row is None:
            return None
        values = (
            (
                await self._session.execute(
                    select(FactorValueModel)
                    .where(FactorValueModel.snapshot_id == row.id)
                    .order_by(
                        FactorValueModel.factor_name,
                        FactorValueModel.symbol,
                    )
                )
            )
            .scalars()
            .all()
        )
        return FactorSnapshot(
            decision_at=row.decision_at,
            business_date=row.business_date,
            effective_date=row.effective_date,
            source=row.source,
            dataset_versions=row.dataset_versions,
            factor_version=row.factor_version,
            static_universe=tuple(row.static_universe),
            selected_symbols=tuple(row.selected_symbols),
            values=tuple(
                FactorValue(
                    symbol=value.symbol,
                    factor_name=FactorName(value.factor_name),
                    value=value.value,
                    global_rank=value.global_rank,
                    industry_rank=value.industry_rank,
                    industry_code=value.industry_code,
                )
                for value in values
            ),
            status=FactorSnapshotStatus(row.status),
            skip_reason=row.skip_reason,
            config=row.config,
            checksum=row.checksum,
            snapshot_id=row.id,
            warnings=tuple(row.warnings or ()),
        )

    async def _model_by_checksum(
        self,
        checksum: str,
    ) -> FactorSnapshotModel | None:
        stmt = select(FactorSnapshotModel).where(
            FactorSnapshotModel.checksum == checksum
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
