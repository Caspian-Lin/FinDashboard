"""instrument 元数据兜底源:research_instrument_profiles 读取(issue #185)。

``instruments`` 表由 akshare 发现链路写入,本身不带 list_date/industry/sector;
tushare ``stock_basic`` 的 list_date + industry 落在 ``research_instrument_profiles``。
本模块提供「最近一次已发布档案批次 → 按 symbol 取档案」的共享读取能力,供:

* ``data_sync`` 后置 enrichment(profiles → instruments 回填,见
  :meth:`finboard_persistence.repo.InstrumentRepository.backfill_metadata_from_profiles`);
* 发布候选构造兜底(``instruments`` 字段为 null 时用档案补齐,见
  :class:`finboard_persistence.dataset_release_repo.ReleaseInstrumentCatalogRepository`)。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    ResearchInstrumentProfileModel,
    ResearchSyncBatchModel,
)
from finboard_persistence.research_repo import ResearchDataset, SyncBatchStatus


class ProfileMetadataLookup:
    """读取最近一次已发布的 ``instrument_profiles`` 批次。

    同一 symbol 在单批次内至多一条(``uq_research_profile_source_version_symbol``
    唯一约束);跨批次时始终取 published_at 最新的整批,保证口径一致。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._batch: ResearchSyncBatchModel | None = None
        self._resolved = False

    async def latest_batch(
        self,
        *,
        source: str | None = None,
    ) -> ResearchSyncBatchModel | None:
        """最近一次已发布的档案批次;同一会话内只解析一次。"""
        if not self._resolved:
            stmt = (
                select(ResearchSyncBatchModel)
                .where(
                    ResearchSyncBatchModel.dataset
                    == ResearchDataset.INSTRUMENT_PROFILES.value,
                    ResearchSyncBatchModel.status == SyncBatchStatus.PUBLISHED.value,
                )
                .order_by(
                    ResearchSyncBatchModel.published_at.desc(),
                    ResearchSyncBatchModel.id.desc(),
                )
                .limit(1)
            )
            if source is not None:
                stmt = stmt.where(ResearchSyncBatchModel.source == source)
            self._batch = (await self._session.execute(stmt)).scalar_one_or_none()
            self._resolved = True
        return self._batch

    async def profiles(
        self,
        symbols: Sequence[str],
        *,
        source: str | None = None,
    ) -> dict[str, ResearchInstrumentProfileModel]:
        """按 symbol 返回档案行;无已发布批次或无档案的 symbol 不出现。"""
        if not symbols:
            return {}
        batch = await self.latest_batch(source=source)
        if batch is None:
            return {}
        rows = (
            await self._session.execute(
                select(ResearchInstrumentProfileModel).where(
                    ResearchInstrumentProfileModel.batch_id == batch.id,
                    ResearchInstrumentProfileModel.symbol.in_(symbols),
                )
            )
        ).scalars().all()
        return {row.symbol: row for row in rows}


__all__ = ["ProfileMetadataLookup"]
