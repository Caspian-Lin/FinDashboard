"""研究代码沙箱执行记录仓储(issue #216)。

与 ``research_code_runs`` 表交互:executor 开始执行时 ``create``(status=
running),终态 ``mark_terminal``(succeeded/failed,附错误分类与容器审计
字段);查询走 ``get`` / ``list_runs``。不控制事务边界,commit 由调用方决定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchCodeRunModel

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
_VALID_STATUSES = {RUNNING, SUCCEEDED, FAILED}
_VALID_KINDS = {"factor", "strategy"}
#: issue #359:factor 双轨执行协议(kind=factor 下的 mode 维度)
_VALID_MODES = {"factor", "factor_series"}


@dataclass(frozen=True)
class ResearchCodeRun:
    run_id: str
    job_id: str | None
    kind: str
    #: factor(单日截面 v1)| factor_series(区间执行 v2,issue #359)
    mode: str
    name: str
    commit: str
    code_checksum: str
    artifact_id: str | None
    dataset_release_ids: list[str]
    dataset_release_checksums: dict[str, str]
    decision_at: datetime
    params: dict[str, Any] | None
    image: str
    image_digest: str
    mount_manifest_checksum: str | None
    scores_checksum: str | None
    # issue #217:成功 run 过质量门后落库的 FeatureSnapshot 引用
    output_snapshot_id: str | None
    status: str
    error_code: str | None
    error_summary: str | None
    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    usage: dict[str, Any] | None
    metrics: dict[str, Any] | None
    artifact_dir: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


def generate_run_id() -> str:
    return f"RCR-{uuid.uuid4().hex[:24]}"


class ResearchCodeRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        kind: str,
        name: str,
        commit: str,
        code_checksum: str,
        dataset_release_ids: list[str],
        dataset_release_checksums: dict[str, str],
        decision_at: datetime,
        image: str,
        image_digest: str,
        artifact_dir: str,
        mode: str = "factor",
        job_id: str | None = None,
        artifact_id: str | None = None,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> ResearchCodeRun:
        """登记一次沙箱执行(status=running),返回 run 记录。"""
        if kind not in _VALID_KINDS:
            raise ValueError(f"非法 kind {kind!r},允许: {sorted(_VALID_KINDS)}")
        if mode not in _VALID_MODES:
            raise ValueError(f"非法 mode {mode!r},允许: {sorted(_VALID_MODES)}")
        now = datetime.now(UTC)
        model = ResearchCodeRunModel(
            run_id=run_id or generate_run_id(),
            job_id=job_id,
            kind=kind,
            mode=mode,
            name=name,
            commit=commit,
            code_checksum=code_checksum,
            artifact_id=artifact_id,
            dataset_release_ids=list(dataset_release_ids),
            dataset_release_checksums=dict(dataset_release_checksums),
            decision_at=decision_at,
            params=params,
            image=image,
            image_digest=image_digest,
            status=RUNNING,
            artifact_dir=artifact_dir,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_record(model)

    async def mark_terminal(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        exit_code: int | None = None,
        timed_out: bool = False,
        oom_killed: bool = False,
        usage: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        scores_checksum: str | None = None,
        mount_manifest_checksum: str | None = None,
        dataset_release_checksums: dict[str, str] | None = None,
        output_snapshot_id: str | None = None,
    ) -> ResearchCodeRun:
        """把 run 置为终态(succeeded/failed)并补齐审计字段。"""
        if status not in _VALID_STATUSES:
            raise ValueError(f"非法 status {status!r},允许: {sorted(_VALID_STATUSES)}")
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "error_summary": error_summary,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "oom_killed": oom_killed,
            "usage": usage,
            "metrics": metrics,
            "scores_checksum": scores_checksum,
            "mount_manifest_checksum": mount_manifest_checksum,
            "output_snapshot_id": output_snapshot_id,
            "updated_at": datetime.now(UTC),
        }
        if dataset_release_checksums is not None:
            values["dataset_release_checksums"] = dataset_release_checksums
        await self._session.execute(
            update(ResearchCodeRunModel)
            .where(ResearchCodeRunModel.run_id == run_id)
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        record = await self.get(run_id)
        if record is None:
            raise LookupError(f"research_code_run 不存在: {run_id}")
        return record

    async def get(self, run_id: str) -> ResearchCodeRun | None:
        stmt = select(ResearchCodeRunModel).where(
            ResearchCodeRunModel.run_id == run_id
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(model) if model else None

    async def list_runs(
        self,
        *,
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ResearchCodeRun]:
        stmt = select(ResearchCodeRunModel)
        if kind is not None:
            if kind not in _VALID_KINDS:
                raise ValueError(f"非法 kind {kind!r},允许: {sorted(_VALID_KINDS)}")
            stmt = stmt.where(ResearchCodeRunModel.kind == kind)
        if name is not None:
            stmt = stmt.where(ResearchCodeRunModel.name == name)
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ValueError(
                    f"非法 status {status!r},允许: {sorted(_VALID_STATUSES)}"
                )
            stmt = stmt.where(ResearchCodeRunModel.status == status)
        stmt = stmt.order_by(
            ResearchCodeRunModel.created_at.desc()
        ).limit(max(1, min(limit, 500)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(m) for m in rows]


def _to_record(model: ResearchCodeRunModel) -> ResearchCodeRun:
    return ResearchCodeRun(
        run_id=model.run_id,
        job_id=model.job_id,
        kind=model.kind,
        mode=model.mode,
        name=model.name,
        commit=model.commit,
        code_checksum=model.code_checksum,
        artifact_id=model.artifact_id,
        dataset_release_ids=[
            str(item) for item in (model.dataset_release_ids or [])
        ],
        dataset_release_checksums={
            str(k): str(v)
            for k, v in (model.dataset_release_checksums or {}).items()
        },
        decision_at=model.decision_at,
        params=model.params,
        image=model.image,
        image_digest=model.image_digest,
        mount_manifest_checksum=model.mount_manifest_checksum,
        scores_checksum=model.scores_checksum,
        output_snapshot_id=model.output_snapshot_id,
        status=model.status,
        error_code=model.error_code,
        error_summary=model.error_summary,
        exit_code=model.exit_code,
        timed_out=model.timed_out,
        oom_killed=model.oom_killed,
        usage=model.usage,
        metrics=model.metrics,
        artifact_dir=model.artifact_dir,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


__all__ = [
    "FAILED",
    "RUNNING",
    "SUCCEEDED",
    "ResearchCodeRun",
    "ResearchCodeRunRepository",
    "generate_run_id",
]
