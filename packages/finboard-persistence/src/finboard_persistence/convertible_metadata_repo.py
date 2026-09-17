"""可转债条款元数据 Repository(issue #265)。

``convertible_metadata`` 表(#58 建表)的第一个**自动写入者**:tushare
cb_basic 快照(dataset_sync ``convertible_profiles`` 数据集)upsert
转股价 / 起息日 / 到期日 / 评级(评级来自 akshare bond_zh_cov 兜底)。

语义:
* 快照刷新 —— cb_basic 是条款的**当前时点权威快照**,已存在行按上游
  非 null 值刷新(转股价下修是真实变动,fill-null-only 会永久保留旧价);
  上游 null 的字段不动,不把已有值清空;
* 缺失可见 —— 无转股价的行无法落库(conversion_price NOT NULL),按行
  跳过并计数;到期日 / 评级缺失计数返回给调用方进质量报告(#251 风格)。

本模块只修改研究主数据表 ``convertible_metadata``,
不触及实盘 orders / fills / positions。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.research import ConvertibleProfile
from finboard_persistence.models import ConvertibleMetadataModel

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ConvertibleMetadataSyncResult:
    """cb_basic → ``convertible_metadata`` upsert 摘要(issue #265)。

    ``skipped_missing_price`` 是因转股价缺失无法落库的行数;
    ``missing_*`` 是落库完成后对应字段仍缺失的行数 —— 缺失可见而非静默。
    """

    total: int
    created: int
    updated: int
    skipped_missing_price: int
    missing_maturity_date: int
    missing_rating: int

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "skipped_missing_price": self.skipped_missing_price,
            "missing_maturity_date": self.missing_maturity_date,
            "missing_rating": self.missing_rating,
        }


class ConvertibleMetadataRepository:
    """``convertible_metadata`` 表的读写入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(
        self,
        profiles: Sequence[ConvertibleProfile],
        *,
        ratings: Mapping[str, str] | None = None,
        source: str = "tushare",
        dataset_version: str = "v1",
    ) -> ConvertibleMetadataSyncResult:
        """按 cb_basic 快照 upsert 转债条款(fail-visible,不静默丢弃)。

        :param profiles: tushare cb_basic 归一化档案
        :param ratings: akshare bond_zh_cov 兜底评级 ``{code: rating}``
        :param source / dataset_version: 溯源字段(落库快照口径)
        """
        rating_map = ratings or {}
        codes = [item.symbol for item in profiles]
        existing: dict[str, ConvertibleMetadataModel] = {}
        if codes:
            stmt = select(ConvertibleMetadataModel).where(
                ConvertibleMetadataModel.code.in_(codes)
            )
            existing = {
                row.code: row
                for row in (await self._session.execute(stmt)).scalars().all()
            }

        created = 0
        updated = 0
        skipped_missing_price = 0
        for item in profiles:
            if item.conversion_price is None:
                # conversion_price NOT NULL:无转股价的快照无法落库,
                # 计数进质量报告而不是抛错中断整批(部分坏行≠全批作废)。
                skipped_missing_price += 1
                continue
            rating = rating_map.get(item.symbol)
            row = existing.get(item.symbol)
            if row is None:
                self._session.add(
                    ConvertibleMetadataModel(
                        code=item.symbol,
                        underlying_stock_code=item.underlying_symbol,
                        conversion_price=item.conversion_price,
                        issue_date=item.issue_date,
                        maturity_date=item.maturity_date,
                        rating=rating,
                        source=source,
                        dataset_version=dataset_version,
                    )
                )
                created += 1
                continue
            # 快照刷新:上游非 null 才覆盖;null 不清空已有值。
            row.underlying_stock_code = item.underlying_symbol
            row.conversion_price = item.conversion_price
            if item.issue_date is not None:
                row.issue_date = item.issue_date
            if item.maturity_date is not None:
                row.maturity_date = item.maturity_date
            if rating is not None:
                row.rating = rating
            updated += 1
        await self._session.flush()

        # 落库后缺失统计:以本批 upsert 涉及的 codes 为口径。
        persisted_codes = [
            item.symbol for item in profiles if item.conversion_price is not None
        ]
        missing_maturity = 0
        missing_rating = 0
        if persisted_codes:
            stats_stmt = select(
                ConvertibleMetadataModel.maturity_date,
                ConvertibleMetadataModel.rating,
            ).where(ConvertibleMetadataModel.code.in_(persisted_codes))
            for maturity_date, rating_value in (
                await self._session.execute(stats_stmt)
            ).all():
                if maturity_date is None:
                    missing_maturity += 1
                if rating_value is None:
                    missing_rating += 1
        result = ConvertibleMetadataSyncResult(
            total=len(profiles),
            created=created,
            updated=updated,
            skipped_missing_price=skipped_missing_price,
            missing_maturity_date=missing_maturity,
            missing_rating=missing_rating,
        )
        logger.info("convertible_metadata.upsert_done", **result.as_dict())
        return result

    async def get(self, code: str) -> ConvertibleMetadataModel | None:
        stmt = select(ConvertibleMetadataModel).where(ConvertibleMetadataModel.code == code)
        return (await self._session.execute(stmt)).scalars().first()


__all__ = [
    "ConvertibleMetadataRepository",
    "ConvertibleMetadataSyncResult",
]
