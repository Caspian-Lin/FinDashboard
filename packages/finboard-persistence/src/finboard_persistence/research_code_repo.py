"""研究代码产物登记仓储(issue #215)。

与 ``research_code_artifacts`` 表交互:submit 追加 active 行并把同名旧 active
行置 retired;list/get 查询;rollback 把历史 commit 重新置 active(现 active
行 retired)。git 对象本体在 bare 仓库(``ResearchCodeRepo``),本仓储只管
引用与生命周期。不控制事务边界,commit 由调用方决定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchCodeArtifactModel

DRAFT = "draft"
ACTIVE = "active"
RETIRED = "retired"
_VALID_STATUSES = {DRAFT, ACTIVE, RETIRED}
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
    ) -> ResearchCodeArtifact:
        """登记一次提交:同名旧 active 行置 retired,追加新行。"""
        _require_kind(kind)
        _require_status(status)
        await self._session.execute(
            update(ResearchCodeArtifactModel)
            .where(
                ResearchCodeArtifactModel.kind == kind,
                ResearchCodeArtifactModel.name == name,
                ResearchCodeArtifactModel.status == ACTIVE,
            )
            .values(status=RETIRED, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session="fetch")
        )
        model = ResearchCodeArtifactModel(
            artifact_id=generate_artifact_id(),
            kind=kind,
            name=name,
            commit=commit,
            path=path,
            checksum=checksum,
            status=status,
            created_by=created_by,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._session.add(model)
        await self._session.flush()
        return _to_record(model)

    async def rollback_to(
        self, *, kind: str, name: str, commit: str
    ) -> ResearchCodeArtifact:
        """把指定历史 commit 重新登记为 active(现 active 行 retired)。"""
        target = await self._get_model(kind=kind, name=name, commit=commit)
        if target is None:
            raise LookupError(
                f"历史版本不存在: kind={kind} name={name} commit={commit}"
            )
        return await self.register(
            kind=kind,
            name=name,
            commit=target.commit,
            path=target.path,
            checksum=target.checksum,
            created_by=target.created_by,
        )

    async def get_active(self, *, kind: str, name: str) -> ResearchCodeArtifact | None:
        model = await self._get_model(kind=kind, name=name, status=ACTIVE)
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
        stmt = stmt.order_by(
            ResearchCodeArtifactModel.created_at.desc()
        ).limit(max(1, min(limit, 500)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(m) for m in rows]

    async def _get_model(
        self,
        *,
        kind: str,
        name: str,
        commit: str | None = None,
        status: str | None = None,
    ) -> ResearchCodeArtifactModel | None:
        stmt = select(ResearchCodeArtifactModel).where(
            ResearchCodeArtifactModel.kind == kind,
            ResearchCodeArtifactModel.name == name,
        )
        if commit is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.commit == commit)
        if status is not None:
            stmt = stmt.where(ResearchCodeArtifactModel.status == status)
        stmt = stmt.order_by(ResearchCodeArtifactModel.created_at.desc())
        return (await self._session.execute(stmt)).scalars().first()


def _to_record(model: ResearchCodeArtifactModel) -> ResearchCodeArtifact:
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
    )


def _require_kind(value: str) -> None:
    if value not in _VALID_KINDS:
        raise ValueError(f"非法 kind {value!r},允许: {sorted(_VALID_KINDS)}")


def _require_status(value: str) -> None:
    if value not in _VALID_STATUSES:
        raise ValueError(f"非法 status {value!r},允许: {sorted(_VALID_STATUSES)}")


__all__ = [
    "ACTIVE",
    "DRAFT",
    "RETIRED",
    "ResearchCodeArtifact",
    "ResearchCodeArtifactRepository",
    "generate_artifact_id",
]
