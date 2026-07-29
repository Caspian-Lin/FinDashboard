"""无代码研究策略的版本历史仓储(issue #79)。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchStrategySpecModel


class StrategySpecStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class StrategySpecChangeType(StrEnum):
    CREATE = "create"
    SUPERSEDE = "supersede"
    ROLLBACK = "rollback"


class StrategySpecVersionConflictError(RuntimeError):
    """客户端基于过期版本写入。"""


class StrategySpecTransitionError(RuntimeError):
    """请求的生命周期转换不合法。"""


class ResearchStrategySpecRepository:
    """追加式版本仓储。

    payload/checksum 一经创建不再修改。发布只改变状态。调用方必须在同一事务内
    commit。异常 rollback 后不会留下半发布状态。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_draft(
        self,
        *,
        strategy_id: str,
        schema_version: str,
        name: str,
        strategy_kind: str,
        checksum: str,
        payload: dict[str, Any],
        validation_errors: list[dict[str, object]] | None = None,
        expected_version: int | None = None,
        change_type: StrategySpecChangeType = StrategySpecChangeType.CREATE,
    ) -> ResearchStrategySpecModel:
        latest = await self.get_latest(strategy_id, for_update=True)
        self._check_expected_version(latest, expected_version)
        next_version = 1 if latest is None else latest.version + 1
        if latest is not None and latest.status == StrategySpecStatus.DRAFT.value:
            latest.status = StrategySpecStatus.SUPERSEDED.value

        row = ResearchStrategySpecModel(
            strategy_id=strategy_id,
            version=next_version,
            schema_version=schema_version,
            name=name,
            strategy_kind=strategy_kind,
            status=StrategySpecStatus.DRAFT.value,
            change_type=change_type.value,
            checksum=checksum,
            payload=payload,
            validation_errors=validation_errors or [],
            parent_version=latest.version if latest is not None else None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def publish(
        self,
        strategy_id: str,
        version: int,
        *,
        expected_version: int,
    ) -> ResearchStrategySpecModel:
        history = await self._locked_history(strategy_id)
        if not history:
            raise StrategySpecTransitionError("策略规格不存在")
        latest = history[0]
        self._check_expected_version(latest, expected_version)
        target = next((item for item in history if item.version == version), None)
        if target is None:
            raise StrategySpecTransitionError(f"策略版本不存在: v{version}")
        if target.version != latest.version or target.status != StrategySpecStatus.DRAFT.value:
            raise StrategySpecTransitionError("只能发布当前最新 draft")
        if target.validation_errors:
            raise StrategySpecTransitionError("存在校验错误的 draft 不能发布")

        for item in history:
            if item.status == StrategySpecStatus.PUBLISHED.value:
                item.status = StrategySpecStatus.SUPERSEDED.value
        target.status = StrategySpecStatus.PUBLISHED.value
        target.published_at = datetime.now(UTC)
        await self._session.flush()
        return target

    async def rollback(
        self,
        strategy_id: str,
        target_version: int,
        *,
        expected_version: int,
    ) -> ResearchStrategySpecModel:
        history = await self._locked_history(strategy_id)
        if not history:
            raise StrategySpecTransitionError("策略规格不存在")
        latest = history[0]
        self._check_expected_version(latest, expected_version)
        target = next((item for item in history if item.version == target_version), None)
        if target is None:
            raise StrategySpecTransitionError(f"回滚目标版本不存在: v{target_version}")
        if target.status == StrategySpecStatus.DRAFT.value:
            raise StrategySpecTransitionError("不能回滚到未发布 draft")

        for item in history:
            if item.status in {
                StrategySpecStatus.PUBLISHED.value,
                StrategySpecStatus.DRAFT.value,
            }:
                item.status = StrategySpecStatus.SUPERSEDED.value
        now = datetime.now(UTC)
        row = ResearchStrategySpecModel(
            strategy_id=strategy_id,
            version=latest.version + 1,
            schema_version=target.schema_version,
            name=target.name,
            strategy_kind=target.strategy_kind,
            status=StrategySpecStatus.PUBLISHED.value,
            change_type=StrategySpecChangeType.ROLLBACK.value,
            checksum=target.checksum,
            payload=dict(target.payload),
            validation_errors=[],
            parent_version=latest.version,
            rollback_of_version=target.version,
            published_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_latest(
        self,
        strategy_id: str,
        *,
        for_update: bool = False,
    ) -> ResearchStrategySpecModel | None:
        stmt = (
            select(ResearchStrategySpecModel)
            .where(ResearchStrategySpecModel.strategy_id == strategy_id)
            .order_by(ResearchStrategySpecModel.version.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_version(
        self,
        strategy_id: str,
        version: int,
    ) -> ResearchStrategySpecModel | None:
        stmt = select(ResearchStrategySpecModel).where(
            ResearchStrategySpecModel.strategy_id == strategy_id,
            ResearchStrategySpecModel.version == version,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_published(
        self,
        strategy_id: str,
    ) -> ResearchStrategySpecModel | None:
        stmt = select(ResearchStrategySpecModel).where(
            ResearchStrategySpecModel.strategy_id == strategy_id,
            ResearchStrategySpecModel.status == StrategySpecStatus.PUBLISHED.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_history(
        self,
        strategy_id: str,
    ) -> list[ResearchStrategySpecModel]:
        stmt = (
            select(ResearchStrategySpecModel)
            .where(ResearchStrategySpecModel.strategy_id == strategy_id)
            .order_by(ResearchStrategySpecModel.version.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_latest(self, *, limit: int = 100) -> list[ResearchStrategySpecModel]:
        latest_versions = (
            select(
                ResearchStrategySpecModel.strategy_id,
                ResearchStrategySpecModel.version.label("max_version"),
            )
            .group_by(ResearchStrategySpecModel.strategy_id)
            .subquery()
        )
        stmt = (
            select(ResearchStrategySpecModel)
            .join(
                latest_versions,
                (ResearchStrategySpecModel.strategy_id == latest_versions.c.strategy_id)
                & (ResearchStrategySpecModel.version == latest_versions.c.max_version),
            )
            .order_by(ResearchStrategySpecModel.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _locked_history(
        self,
        strategy_id: str,
    ) -> list[ResearchStrategySpecModel]:
        stmt = (
            select(ResearchStrategySpecModel)
            .where(ResearchStrategySpecModel.strategy_id == strategy_id)
            .order_by(ResearchStrategySpecModel.version.desc())
            .with_for_update()
        )
        return list((await self._session.execute(stmt)).scalars().all())

    @staticmethod
    def _check_expected_version(
        latest: ResearchStrategySpecModel | None,
        expected_version: int | None,
    ) -> None:
        actual = None if latest is None else latest.version
        if actual != expected_version:
            raise StrategySpecVersionConflictError(
                f"策略版本冲突: expected={expected_version}, actual={actual}"
            )


__all__ = [
    "ResearchStrategySpecRepository",
    "StrategySpecChangeType",
    "StrategySpecStatus",
    "StrategySpecTransitionError",
    "StrategySpecVersionConflictError",
]
