"""研究代码产物登记仓储(issue #215)。

与 ``research_code_artifacts`` 表交互:submit 追加 draft/pending 行;晋级通过后
同名旧 active 行置 retired;list/get 查询;rollback 建立待验证 draft。git
对象本体在 bare 仓库(``ResearchCodeRepo`),本仓储只管引用与生命周期。不控制
事务边界,commit 由调用方决定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchCodeArtifactModel

DRAFT = "draft"
ACTIVE = "active"
RETIRED = "retired"
PROMOTION_PENDING = "pending"
PROMOTION_PASSED = "passed"
PROMOTION_FAILED = "failed"
_VALID_STATUSES = {DRAFT, ACTIVE, RETIRED}
_VALID_PROMOTION_STATUSES = {
    PROMOTION_PENDING,
    PROMOTION_PASSED,
    PROMOTION_FAILED,
}
_VALID_KINDS = {"factor", "strategy"}


@dataclass(frozen=True)
class ResearchCodeArtifact:
    artifact_id: str
    kind: str
    name: str
    commit: str
    path: str
    checksum: str
    status: str
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    promotion_status: str = PROMOTION_PENDING
    validation_experiment_id: str | None = None
    screen_run_id: str | None = None
    promotion_evidence: dict[str, Any] | None = None
    promoted_at: datetime | None = None
    retired_at: datetime | None = None


def generate_artifact_id() -> str:
    return f"RC-{uuid.uuid4().hex[:24]}"


class ResearchCodeArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        *,
        kind: str,
        name: str,
        commit: str,
        path: str,
        checksum: str,
        created_by: str,
        status: str = ACTIVE,
        promotion_status: str | None = None,
        validation_experiment_id: str | None = None,
        screen_run_id: str | None = None,
        promotion_evidence: dict[str, Any] | None = None,
    ) -> ResearchCodeArtifact:
        """登记一次提交或草稿。

        新的 MCP 提交使用 ``status=draft``,不会影响当前 active 版本;
        ``status=active`` 保留 #215 直接登记调用的兼容语义,并视为已通过
        晋级门(历史测试/迁移数据没有机器证据,正式新流程不应调用该默认值)。
        """
        _require_kind(kind)
        _require_status(status)
        resolved_promotion_status = promotion_status or (
            PROMOTION_PASSED if status == ACTIVE else PROMOTION_PENDING
        )
        _require_promotion_status(resolved_promotion_status)
        now = datetime.now(UTC)
        if status == ACTIVE:
            await self._retire_active(kind=kind, name=name, now=now)
        model = ResearchCodeArtifactModel(
            artifact_id=generate_artifact_id(),
            kind=kind,
            name=name,
            commit=commit,
            path=path,
            checksum=checksum,
            status=status,
            promotion_status=resolved_promotion_status,
            validation_experiment_id=validation_experiment_id,
            screen_run_id=screen_run_id,
            promotion_evidence=promotion_evidence,
            promoted_at=now if resolved_promotion_status == PROMOTION_PASSED else None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_record(model)

    async def rollback_to(self, *, kind: str, name: str, commit: str) -> ResearchCodeArtifact:
        """兼容旧仓储 API:把指定历史 commit 重新登记为 active。

        正式 MCP 回滚走 :meth:`rollback_to_draft`,旧版本必须重新执行
        screen + OOS 后才能晋级;保留此方法是为了不破坏 #215/#216 的
        低层调用方与历史测试,新的正式入口不会绕过 #219 晋级门。
        """
        target = await self._get_model(kind=kind, name=name, commit=commit)
        if target is None:
            raise LookupError(f"历史版本不存在: kind={kind} name={name} commit={commit}")
        return await self.register(
            kind=kind,
            name=name,
            commit=target.commit,
            path=target.path,
            checksum=target.checksum,
            created_by=target.created_by,
        )

    async def rollback_to_draft(self, *, kind: str, name: str, commit: str) -> ResearchCodeArtifact:
        """按旧 commit 建立待验证草稿,不绕过晋级门。"""
        target = await self._get_model(kind=kind, name=name, commit=commit)
        if target is None:
            raise LookupError(f"历史版本不存在: kind={kind} name={name} commit={commit}")
        return await self.register(
            kind=kind,
            name=name,
            commit=target.commit,
            path=target.path,
            checksum=target.checksum,
            created_by=target.created_by,
            status=DRAFT,
        )

    async def promote(
        self,
        artifact_id: str,
        *,
        validation_experiment_id: str,
        screen_run_id: str,
        evidence: dict[str, Any],
    ) -> ResearchCodeArtifact:
        """原子地把已通过机器验证的 draft 晋级为 active。"""
        target = await self._get_model_by_id(artifact_id, for_update=True)
        if target is None:
            raise LookupError(f"研究代码产物不存在: {artifact_id}")
        current_status = target.status
        current_promotion = _promotion_status(target)
        if current_status == ACTIVE and current_promotion == PROMOTION_PASSED:
            # 重复 promote 是幂等操作;不重写审计证据。
            return _to_record(target)
        if current_status != DRAFT:
            raise ValueError(
                f"只有 draft 产物可以晋级: artifact_id={artifact_id} "
                f"status={current_status} promotion_status={current_promotion}"
            )
        now = datetime.now(UTC)
        await self._retire_active(kind=target.kind, name=target.name, now=now)
        target.status = ACTIVE
        target.promotion_status = PROMOTION_PASSED
        target.validation_experiment_id = validation_experiment_id
        target.screen_run_id = screen_run_id
        target.promotion_evidence = evidence
        target.promoted_at = now
        target.retired_at = None
        target.updated_at = now
        await self._session.flush()
        return _to_record(target)

    async def mark_promotion_failed(
        self,
        artifact_id: str,
        *,
        validation_experiment_id: str,
        screen_run_id: str,
        evidence: dict[str, Any],
    ) -> ResearchCodeArtifact:
        """记录晋级门失败,保留 draft 生命周期供修复后重新验证。"""
        target = await self._get_model_by_id(artifact_id, for_update=True)
        if target is None:
            raise LookupError(f"研究代码产物不存在: {artifact_id}")
        if target.status != DRAFT:
            raise ValueError(
                f"只有 draft 产物可以记录晋级失败: artifact_id={artifact_id} status={target.status}"
            )
        now = datetime.now(UTC)
        target.promotion_status = PROMOTION_FAILED
        target.validation_experiment_id = validation_experiment_id
        target.screen_run_id = screen_run_id
        target.promotion_evidence = evidence
        target.promoted_at = None
        target.updated_at = now
        await self._session.flush()
        return _to_record(target)

    async def retire(self, artifact_id: str) -> ResearchCodeArtifact:
        """显式退役一个 active 版本。"""
        target = await self._get_model_by_id(artifact_id, for_update=True)
        if target is None:
            raise LookupError(f"研究代码产物不存在: {artifact_id}")
        if target.status == RETIRED:
            return _to_record(target)
        if target.status != ACTIVE:
            raise ValueError(
                f"只有 active 产物可以退役: artifact_id={artifact_id} status={target.status}"
            )
        now = datetime.now(UTC)
        target.status = RETIRED
        target.retired_at = now
        target.updated_at = now
        await self._session.flush()
        return _to_record(target)

    async def get_active(self, *, kind: str, name: str) -> ResearchCodeArtifact | None:
        model = await self._get_model(
            kind=kind,
            name=name,
            status=ACTIVE,
            promotion_status=PROMOTION_PASSED,
        )
        return _to_record(model) if model else None

    async def get(self, artifact_id: str) -> ResearchCodeArtifact | None:
        stmt = select(ResearchCodeArtifactModel).where(
            ResearchCodeArtifactModel.artifact_id == artifact_id
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(model) if model else None

    async def list_artifacts(
        self,
        *,
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
        promotion_status: str | None = None,
        limit: int = 100,
    ) -> list[ResearchCodeArtifact]:
        stmt = select(ResearchCodeArtifactModel)
        if kind is not None:
            _require_kind(kind)
            stmt = stmt.where(ResearchCodeArtifactModel.kind == kind)
        if name is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.name == name)
        if status is not None:
            _require_status(status)
            stmt = stmt.where(ResearchCodeArtifactModel.status == status)
        if promotion_status is not None:
            _require_promotion_status(promotion_status)
            stmt = stmt.where(ResearchCodeArtifactModel.promotion_status == promotion_status)
        stmt = stmt.order_by(ResearchCodeArtifactModel.created_at.desc()).limit(
            max(1, min(limit, 500))
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(m) for m in rows]

    async def _get_model(
        self,
        *,
        kind: str,
        name: str,
        commit: str | None = None,
        status: str | None = None,
        promotion_status: str | None = None,
    ) -> ResearchCodeArtifactModel | None:
        stmt = select(ResearchCodeArtifactModel).where(
            ResearchCodeArtifactModel.kind == kind,
            ResearchCodeArtifactModel.name == name,
        )
        if commit is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.commit == commit)
        if status is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.status == status)
        if promotion_status is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.promotion_status == promotion_status)
        stmt = stmt.order_by(ResearchCodeArtifactModel.created_at.desc())
        return (await self._session.execute(stmt)).scalars().first()

    async def _get_model_by_id(
        self, artifact_id: str, *, for_update: bool = False
    ) -> ResearchCodeArtifactModel | None:
        stmt = select(ResearchCodeArtifactModel).where(
            ResearchCodeArtifactModel.artifact_id == artifact_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _retire_active(self, *, kind: str, name: str, now: datetime) -> None:
        await self._session.execute(
            update(ResearchCodeArtifactModel)
            .where(
                ResearchCodeArtifactModel.kind == kind,
                ResearchCodeArtifactModel.name == name,
                ResearchCodeArtifactModel.status == ACTIVE,
            )
            .values(status=RETIRED, retired_at=now, updated_at=now)
            .execution_options(synchronize_session="fetch")
        )


def _to_record(model: ResearchCodeArtifactModel) -> ResearchCodeArtifact:
    promotion_status = _promotion_status(model)
    return ResearchCodeArtifact(
        artifact_id=model.artifact_id,
        kind=model.kind,
        name=model.name,
        commit=model.commit,
        path=model.path,
        checksum=model.checksum,
        status=model.status,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        promotion_status=promotion_status,
        validation_experiment_id=getattr(model, "validation_experiment_id", None),
        screen_run_id=getattr(model, "screen_run_id", None),
        promotion_evidence=getattr(model, "promotion_evidence", None),
        promoted_at=getattr(model, "promoted_at", None),
        retired_at=getattr(model, "retired_at", None),
    )


def _promotion_status(model: ResearchCodeArtifactModel) -> str:
    """旧数据库/测试 double 没有晋级列时安全回退。

    迁移会把既有 active 行回填为 passed;这里的回退只服务于尚未迁移的
    mock/旧读取路径,draft/retired 默认仍不可正式消费。
    """
    raw = getattr(model, "promotion_status", None)
    if raw in _VALID_PROMOTION_STATUSES:
        return str(raw)
    return PROMOTION_PASSED if model.status == ACTIVE else PROMOTION_PENDING


def _require_kind(value: str) -> None:
    if value not in _VALID_KINDS:
        raise ValueError(f"非法 kind {value!r},允许: {sorted(_VALID_KINDS)}")


def _require_status(value: str) -> None:
    if value not in _VALID_STATUSES:
        raise ValueError(f"非法 status {value!r},允许: {sorted(_VALID_STATUSES)}")


def _require_promotion_status(value: str) -> None:
    if value not in _VALID_PROMOTION_STATUSES:
        raise ValueError(
            f"非法 promotion_status {value!r},允许: {sorted(_VALID_PROMOTION_STATUSES)}"
        )


__all__ = [
    "ACTIVE",
    "DRAFT",
    "PROMOTION_FAILED",
    "PROMOTION_PASSED",
    "PROMOTION_PENDING",
    "RETIRED",
    "ResearchCodeArtifact",
    "ResearchCodeArtifactRepository",
    "generate_artifact_id",
]
