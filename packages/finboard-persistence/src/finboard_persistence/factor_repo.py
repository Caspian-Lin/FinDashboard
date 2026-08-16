"""因子快照与因子值 Repository。"""

from __future__ import annotations

from sqlalchemy import select
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
        """保存完整快照;相同 checksum 复用原记录。"""
        existing = await self._model_by_checksum(snapshot.checksum)
        if existing is not None:
            return existing.id

        row = FactorSnapshotModel(
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
        self._session.add(row)
        await self._session.flush()
        self._session.add_all(
            [
                FactorValueModel(
                    snapshot_id=row.id,
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
        return row.id

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
