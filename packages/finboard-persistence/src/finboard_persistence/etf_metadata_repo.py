"""ETF 元数据 Repository(issue #97)。

负责把分类器输出的 :class:`~finboard_data.assets.classifier.EtfClassification`
持久化到 ``etf_metadata`` 表,并提供:

* 批量 upsert(自动保留 ``manual_override=True`` 的人工记录,不被覆盖);
* 待复核队列查询 / 批量确认 / 单只人工覆盖(写审计流水);
* dry-run 预览(不写入)。

本模块只修改研究数据表 ``etf_metadata`` / ``etf_metadata_audits``,
不触及实盘 orders / fills / positions。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.assets.classifier import EtfClassification
from finboard_persistence.models import EtfMetadataAuditModel, EtfMetadataModel
from finboard_shared.types import (
    EtfExecutionProfile,
    ReviewStatus,
    asset_class_from_execution_profile,
)

logger = structlog.get_logger(__name__)

_T0_PROFILES = frozenset(
    {
        EtfExecutionProfile.CROSS_BORDER_ETF,
        EtfExecutionProfile.COMMODITY_ETF,
        EtfExecutionProfile.BOND_ETF,
        EtfExecutionProfile.MONEY_MARKET_ETF,
    }
)


@dataclass(frozen=True, slots=True)
class EtfSyncPreview:
    """dry-run 预览结果 —— 不写入,只统计变更范围。"""

    total: int
    to_insert: int
    to_update: int
    skipped_override: int
    needs_review: int
    auto_adopted: int


@dataclass(frozen=True, slots=True)
class EtfMetadataSummary:
    """ETF 元数据分类统计摘要。"""

    total: int
    auto_adopted: int
    needs_review: int
    manually_confirmed: int
    manually_overridden: int
    missing_metadata: int


class EtfMetadataRepository:
    """ETF 元数据的读写与审核入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 -------------------------------------------------------------

    async def get(self, code: str) -> EtfMetadataModel | None:
        stmt = select(EtfMetadataModel).where(EtfMetadataModel.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_review_status(
        self,
        status: ReviewStatus | None = None,
        *,
        limit: int = 500,
    ) -> list[EtfMetadataModel]:
        stmt = select(EtfMetadataModel).order_by(EtfMetadataModel.code)
        if status is not None:
            stmt = stmt.where(EtfMetadataModel.review_status == status.value)
        stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def summary(self, total_etf_instruments: int) -> EtfMetadataSummary:
        """分类统计:各 review_status 的数量 + 缺少元数据的 ETF 数。"""
        rows = await self.list_by_review_status(limit=10000)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.review_status] = counts.get(row.review_status, 0) + 1
        return EtfMetadataSummary(
            total=len(rows),
            auto_adopted=counts.get(ReviewStatus.AUTO_ADOPTED.value, 0),
            needs_review=counts.get(ReviewStatus.NEEDS_REVIEW.value, 0),
            manually_confirmed=counts.get(ReviewStatus.MANUALLY_CONFIRMED.value, 0),
            manually_overridden=counts.get(ReviewStatus.MANUALLY_OVERRIDDEN.value, 0),
            missing_metadata=max(total_etf_instruments - len(rows), 0),
        )

    # -- dry-run 预览 -----------------------------------------------------

    async def preview_upsert(
        self,
        classifications: list[EtfClassification],
    ) -> EtfSyncPreview:
        """不写入,只统计将新增 / 更新 / 跳过(人工覆盖)/ 待复核的数量。"""
        codes = [c.code for c in classifications]
        existing = await self._existing_map(codes)
        to_insert = to_update = skipped = needs_review = auto_adopted = 0
        for cls in classifications:
            row = existing.get(cls.code)
            if row is not None and row.manual_override:
                skipped += 1
                continue
            if row is None:
                to_insert += 1
            else:
                to_update += 1
            if cls.review_status is ReviewStatus.AUTO_ADOPTED:
                auto_adopted += 1
            else:
                needs_review += 1
        return EtfSyncPreview(
            total=len(classifications),
            to_insert=to_insert,
            to_update=to_update,
            skipped_override=skipped,
            needs_review=needs_review,
            auto_adopted=auto_adopted,
        )

    # -- 批量 upsert ------------------------------------------------------

    async def upsert_batch(
        self,
        classifications: list[EtfClassification],
        *,
        source: str = "akshare",
        source_updated_at: datetime | None = None,
    ) -> tuple[int, int, int]:
        """批量写入分类结果。

        ``manual_override=True`` 的人工记录被跳过(不被自动同步覆盖)。
        返回 ``(inserted, updated, skipped)``。
        """
        observed_at = source_updated_at or datetime.now(UTC)
        existing = await self._existing_map([c.code for c in classifications])
        inserted = updated = skipped = 0
        for cls in classifications:
            row = existing.get(cls.code)
            if row is not None and row.manual_override:
                skipped += 1
                continue
            if row is None:
                self._session.add(self._new_row(cls, source, observed_at))
                inserted += 1
            else:
                self._apply_classification(row, cls, source, observed_at)
                updated += 1
        await self._session.flush()
        logger.info(
            "etf_metadata.upsert_batch",
            inserted=inserted,
            updated=updated,
            skipped=skipped,
        )
        return inserted, updated, skipped

    # -- 人工审核 ---------------------------------------------------------

    async def apply_manual_override(
        self,
        code: str,
        *,
        execution_profile: EtfExecutionProfile | None = None,
        underlying_market: str | None = None,
        strategy_type: str | None = None,
        underlying_index: str | None = None,
        changed_by: str = "user",
        reason: str = "",
    ) -> EtfMetadataModel:
        """人工覆盖分类。写审计流水,设 ``manual_override=True``。

        后续自动同步不再覆盖此记录,除非用户显式撤销覆盖。
        """
        row = await self.get(code)
        if row is None:
            row = self._create_skeleton(code)
            self._session.add(row)

        profile = execution_profile
        if profile is not None and profile.value != row.execution_profile:
            self._audit(
                row, "execution_profile", row.execution_profile, profile.value,
                changed_by, reason,
            )
            row.execution_profile = profile.value
            row.category = _category_value(profile)
            row.allows_t_plus_0 = profile in _T0_PROFILES
            new_asset_class = asset_class_from_execution_profile(profile).value
            if new_asset_class != row.underlying_asset_class:
                self._audit(
                    row, "underlying_asset_class",
                    row.underlying_asset_class, new_asset_class,
                    changed_by, reason,
                )
                row.underlying_asset_class = new_asset_class
        if underlying_market and underlying_market != row.underlying_market:
            self._audit(row, "underlying_market", row.underlying_market, underlying_market, changed_by, reason)
            row.underlying_market = underlying_market
        if strategy_type and strategy_type != row.strategy_type:
            self._audit(row, "strategy_type", row.strategy_type, strategy_type, changed_by, reason)
            row.strategy_type = strategy_type
        if underlying_index != row.underlying_index:
            if underlying_index == "":
                underlying_index = None
            self._audit(row, "underlying_index", row.underlying_index, underlying_index, changed_by, reason)
            row.underlying_index = underlying_index

        row.manual_override = True
        row.review_status = ReviewStatus.MANUALLY_OVERRIDDEN.value
        row.source = "manual"
        await self._session.flush()
        return row

    async def batch_confirm(
        self,
        codes: list[str],
        *,
        changed_by: str = "user",
        reason: str = "",
    ) -> int:
        """批量确认:将 ``needs_review`` 转为 ``manually_confirmed``。"""
        if not codes:
            return 0
        stmt = select(EtfMetadataModel).where(
            EtfMetadataModel.code.in_(codes),
            EtfMetadataModel.review_status == ReviewStatus.NEEDS_REVIEW.value,
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            row.review_status = ReviewStatus.MANUALLY_CONFIRMED.value
            self._audit(
                row, "review_status",
                ReviewStatus.NEEDS_REVIEW.value,
                ReviewStatus.MANUALLY_CONFIRMED.value,
                changed_by, reason,
            )
        await self._session.flush()
        return len(rows)

    async def clear_override(self, code: str) -> EtfMetadataModel | None:
        """撤销人工覆盖,允许后续自动同步更新。"""
        row = await self.get(code)
        if row is None:
            return None
        row.manual_override = False
        await self._session.flush()
        return row

    # -- 内部辅助 ---------------------------------------------------------

    async def _existing_map(self, codes: list[str]) -> dict[str, EtfMetadataModel]:
        if not codes:
            return {}
        stmt = select(EtfMetadataModel).where(EtfMetadataModel.code.in_(codes))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.code: row for row in rows}

    def _new_row(
        self,
        cls: EtfClassification,
        source: str,
        observed_at: datetime,
    ) -> EtfMetadataModel:
        row = EtfMetadataModel(
            code=cls.code,
            fund_code=cls.code.split(".", 1)[0],
            category=cls.category.value,
            execution_profile=cls.execution_profile.value,
            underlying_market=cls.underlying_market.value,
            strategy_type=cls.strategy_type.value,
            underlying_asset_class=cls.underlying_asset_class.value,
            underlying_index=cls.tracked_index,
            confidence=cls.confidence,
            review_status=cls.review_status.value,
            evidence=list(cls.evidence),
            rule_version=cls.rule_version,
            source=source,
            source_updated_at=observed_at,
            allows_t_plus_0=cls.execution_profile in _T0_PROFILES,
            dataset_version=f"{source}-{date.today().isoformat()}",
        )
        return row

    def _apply_classification(
        self,
        row: EtfMetadataModel,
        cls: EtfClassification,
        source: str,
        observed_at: datetime,
    ) -> None:
        row.category = cls.category.value
        row.execution_profile = cls.execution_profile.value
        row.underlying_market = cls.underlying_market.value
        row.strategy_type = cls.strategy_type.value
        row.underlying_asset_class = cls.underlying_asset_class.value
        row.underlying_index = cls.tracked_index
        row.confidence = cls.confidence
        row.review_status = cls.review_status.value
        row.evidence = list(cls.evidence)
        row.rule_version = cls.rule_version
        row.source = source
        row.source_updated_at = observed_at
        row.allows_t_plus_0 = cls.execution_profile in _T0_PROFILES
        row.dataset_version = f"{source}-{date.today().isoformat()}"

    def _create_skeleton(self, code: str) -> EtfMetadataModel:
        return EtfMetadataModel(
            code=code,
            fund_code=code.split(".", 1)[0],
            category="equity",
            execution_profile=None,
            underlying_market="domestic",
            strategy_type="index",
            underlying_asset_class="equity",
            iopv_available=False,
            allows_t_plus_0=False,
            dividend_policy="cash",
            confidence=Decimal("0"),
            review_status=ReviewStatus.NEEDS_REVIEW.value,
            rule_version="",
            evidence=[],
            manual_override=False,
            raw_payload={},
            source="manual",
            dataset_version=f"manual-{date.today().isoformat()}",
        )

    def _audit(
        self,
        row: EtfMetadataModel,
        field_name: str,
        old_value: object,
        new_value: object,
        changed_by: str,
        reason: str,
    ) -> None:
        self._session.add(
            EtfMetadataAuditModel(
                code=row.code,
                field_name=field_name,
                old_value=None if old_value is None else str(old_value),
                new_value=None if new_value is None else str(new_value),
                changed_by=changed_by,
                reason=reason,
            )
        )


def _category_value(profile: EtfExecutionProfile) -> str:
    from finboard_shared.types import etf_category_from_execution_profile

    return etf_category_from_execution_profile(profile).value


__all__ = [
    "EtfMetadataRepository",
    "EtfMetadataSummary",
    "EtfSyncPreview",
]
