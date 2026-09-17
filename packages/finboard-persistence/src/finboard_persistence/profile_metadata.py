"""instrument 元数据兜底源:research_instrument_profiles 读取(issue #185,#251 修正)。

``instruments`` 表由 akshare 发现链路写入,本身不带 list_date/industry/sector;
tushare ``stock_basic`` 的 list_date + industry 落在 ``research_instrument_profiles``。
本模块提供「最近一次档案批次 → 按 symbol 取档案」的共享读取能力,供:

* ``data_sync`` 后置 enrichment(profiles → instruments 回填,见
  :meth:`finboard_persistence.repo.InstrumentRepository.backfill_metadata_from_profiles`);
* 发布候选构造兜底(``instruments`` 字段为 null 时用档案补齐,见
  :class:`finboard_persistence.dataset_release_repo.ReleaseInstrumentCatalogRepository`)。

#251:回填与发布兜底的批次口径分离 —— 回填只求数据在表里,取**最近一次实际
摄取过档案的批次**(``require_published=False``,不论批次是否发布);发布兜底
保持「已发布批次」口径不变(``require_published=True``)。此前回填依赖已发布
批次,profiles 摄取过但从未发布时回填永久短路、instruments 元数据全空。
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
    """读取最近一次 ``instrument_profiles`` 批次。

    同一 symbol 在单批次内至多一条(``uq_research_profile_source_version_symbol``
    唯一约束);跨批次时已发布口径取 published_at 最新的整批、数据存在口径取
    有档案行的最大批次 id,均保证整批口径一致。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._batch: ResearchSyncBatchModel | None = None
        self._resolved = False

    async def latest_batch(
        self,
        *,
        source: str | None = None,
        require_published: bool = True,
    ) -> ResearchSyncBatchModel | None:
        """最近一次档案批次;同一会话内只解析一次。

        ``require_published=True``(#185 发布兜底语义):只认 PUBLISHED 批次,
        按 ``published_at`` 取最新。``False``(#251 data_sync 回填语义):只要有
        档案行即算(批次可能仍 running/partial/failed —— upsert 逐切片落库,
        失败批次也可能携带部分数据),按批次 id 取最大。
        """
        if not self._resolved:
            if require_published:
                # #185 原语义:已发布整批,按 published_at 取最新(不加 join,
                # 与既有发布兜底行为逐字一致)。
                stmt = (
                    select(ResearchSyncBatchModel)
                    .where(
                        ResearchSyncBatchModel.dataset
                        == ResearchDataset.INSTRUMENT_PROFILES.value,
                        ResearchSyncBatchModel.status
                        == SyncBatchStatus.PUBLISHED.value,
                    )
                    .order_by(
                        ResearchSyncBatchModel.published_at.desc(),
                        ResearchSyncBatchModel.id.desc(),
                    )
                )
            else:
                # #251 回填语义:档案行存在即算(批次可能 running/partial/failed
                # —— upsert 逐切片落库,失败批次也可能携带部分数据)。
                stmt = (
                    select(ResearchSyncBatchModel)
                    .join(
                        ResearchInstrumentProfileModel,
                        ResearchInstrumentProfileModel.batch_id
                        == ResearchSyncBatchModel.id,
                    )
                    .where(
                        ResearchSyncBatchModel.dataset
                        == ResearchDataset.INSTRUMENT_PROFILES.value,
                    )
                    .order_by(ResearchSyncBatchModel.id.desc())
                )
            if source is not None:
                stmt = stmt.where(ResearchSyncBatchModel.source == source)
            self._batch = (
                (await self._session.execute(stmt.limit(1))).scalar_one_or_none()
            )
            self._resolved = True
        return self._batch

    async def profiles(
        self,
        symbols: Sequence[str],
        *,
        source: str | None = None,
        require_published: bool = True,
    ) -> dict[str, ResearchInstrumentProfileModel]:
        """按 symbol 返回档案行;无批次或无档案的 symbol 不出现。"""
        if not symbols:
            return {}
        batch = await self.latest_batch(
            source=source, require_published=require_published
        )
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
