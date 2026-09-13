"""离线 ResearchRun 与逐阶段 artifact 仓储(issue #80)。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from finboard_persistence.models import (
    ResearchRunArtifactModel,
    ResearchRunModel,
)

#: artifact 流式读取的行缓冲(#470 前半场):决策级 13 行/决策,26 行 ≈ 2 决策。
_ARTIFACT_STREAM_CHUNK = 26


class ResearchRunPersistenceConflictError(RuntimeError):
    """数据库中的幂等内容或状态与请求冲突。"""


class ResearchRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def checkpoint(self) -> None:
        await self._session.commit()

    async def create_or_get(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        replay_of_run_id: str | None,
        strategy_id: str,
        strategy_kind: str,
        status: str,
        schema_version: str,
        manifest_checksum: str,
        manifest: dict[str, object],
        requested_by: str,
        job_id: str | None = None,
    ) -> tuple[ResearchRunModel, bool]:
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:
            existing = await self.get(run_id)
        if existing is not None:
            if existing.manifest_checksum != manifest_checksum:
                raise ResearchRunPersistenceConflictError(
                    "相同 run_id/idempotency_key 对应不同 manifest"
                )
            return existing, False
        row = ResearchRunModel(
            run_id=run_id,
            idempotency_key=idempotency_key,
            replay_of_run_id=replay_of_run_id,
            strategy_id=strategy_id,
            strategy_kind=strategy_kind,
            status=status,
            schema_version=schema_version,
            manifest_checksum=manifest_checksum,
            manifest=manifest,
            requested_by=requested_by,
            job_id=job_id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
            return row, True
        except IntegrityError:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                existing = await self.get(run_id)
            if existing is None:
                raise
            if existing.manifest_checksum != manifest_checksum:
                raise ResearchRunPersistenceConflictError(
                    "并发创建命中相同身份但 manifest 不同"
                ) from None
            return existing, False

    async def get(
        self, run_id: str, *, for_update: bool = False
    ) -> ResearchRunModel | None:
        stmt = select(ResearchRunModel).where(ResearchRunModel.run_id == run_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> ResearchRunModel | None:
        stmt = select(ResearchRunModel).where(
            ResearchRunModel.idempotency_key == idempotency_key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(
        self,
        *,
        statuses: Iterable[str] | None = None,
        strategy_kind: str | None = None,
        limit: int = 100,
    ) -> list[ResearchRunModel]:
        stmt = select(ResearchRunModel)
        if statuses is not None:
            stmt = stmt.where(ResearchRunModel.status.in_(tuple(statuses)))
        if strategy_kind is not None:
            stmt = stmt.where(ResearchRunModel.strategy_kind == strategy_kind)
        stmt = stmt.order_by(
            ResearchRunModel.created_at.desc(), ResearchRunModel.id.desc()
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_status_by_job_id(self, job_id: str) -> str | None:
        """按关联 job_id 取 run 状态(issue #306);无关联 run 返回 None。

        供 job 视图(``GET /api/jobs/{id}`` / ``finboard_job_get``)把
        ``research_runs.status`` 透传为 ``run_status``,让「run interrupted 但
        job 仍 running」的两表不一致一眼可见。轻量投影只取 status 列。
        """

        stmt = (
            select(ResearchRunModel.status)
            .where(ResearchRunModel.job_id == job_id)
            .order_by(ResearchRunModel.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[str],
        target: str,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunModel:
        row = await self.get(run_id, for_update=True)
        if row is None:
            raise ResearchRunPersistenceConflictError(f"研究运行不存在: {run_id}")
        if row.status not in expected:
            raise ResearchRunPersistenceConflictError(
                f"运行 {run_id} 当前状态 {row.status} 不在 {sorted(expected)}"
            )
        now = datetime.now(UTC)
        row.status = target
        row.error_code = error_code
        row.error_summary = error_summary
        row.updated_at = now
        if target == "running" and row.started_at is None:
            row.started_at = now
        if target in {"completed", "failed", "rejected", "cancelled"}:
            row.completed_at = now
        await self._session.flush()
        return row

    async def save_result(
        self,
        run_id: str,
        *,
        result: dict[str, object],
        result_checksum: str,
    ) -> ResearchRunModel:
        row = await self.get(run_id, for_update=True)
        if row is None:
            raise ResearchRunPersistenceConflictError(f"研究运行不存在: {run_id}")
        if row.result_checksum is not None and row.result_checksum != result_checksum:
            raise ResearchRunPersistenceConflictError("同一次运行产生了不同结果")
        row.result = result
        row.result_checksum = result_checksum
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def append_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        decision_id: str | None,
        sequence: int,
        stage: str,
        trace_id: str,
        parent_trace_ids: list[str],
        payload: dict[str, object],
        checksum: str,
    ) -> tuple[ResearchRunArtifactModel, bool]:
        # 幂等命中分支只比对 checksum,不消费 payload —— load_only 让既有行
        # 的 payload 列保持 deferred(#470 前半场:断点续算种子逐决策重持久化
        # 时,每个命中行省掉一次全量 payload JSONB → Python 物化,features
        # 期均 ~8.5MB)。命中行除 checksum 外的字段未加载,调用方只读 created
        # 标志;新插入行不受影响。
        stmt = (
            select(ResearchRunArtifactModel)
            .where(
                ResearchRunArtifactModel.run_id == run_id,
                ResearchRunArtifactModel.artifact_id == artifact_id,
            )
            .options(load_only(ResearchRunArtifactModel.checksum))
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            if existing.checksum != checksum:
                raise ResearchRunPersistenceConflictError(
                    f"artifact {artifact_id} checkpoint 内容冲突"
                )
            return existing, False
        row = ResearchRunArtifactModel(
            run_id=run_id,
            artifact_id=artifact_id,
            decision_id=decision_id,
            sequence=sequence,
            stage=stage,
            trace_id=trace_id,
            parent_trace_ids=parent_trace_ids,
            payload=payload,
            checksum=checksum,
        )
        self._session.add(row)
        await self._session.flush()
        return row, True

    async def list_artifacts(self, run_id: str) -> list[ResearchRunArtifactModel]:
        stmt = (
            select(ResearchRunArtifactModel)
            .where(ResearchRunArtifactModel.run_id == run_id)
            .order_by(ResearchRunArtifactModel.sequence)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def iter_artifacts(self, run_id: str) -> AsyncIterator[ResearchRunArtifactModel]:
        """按 sequence 流式遍历 artifact 行(#470 前半场)。

        服务端游标 + ``yield_per`` 分块:全历史 run 的 payload 总量达数 GB
        (features 期均 ~8.5MB),一次性 ``list_artifacts`` 物化会在断点续算
        读回时把整个前缀的 payload 钉进内存(272 期前缀实测 ≈8GB)。流式
        读取让消费方(逐决策分组 → 复验 → 重建 bundle → 释放)任意时刻只
        持有 O(1) 个决策的载荷。行序与 ``list_artifacts`` 一致(sequence
        升序)。
        """

        async def _stream() -> AsyncIterator[ResearchRunArtifactModel]:
            stmt = (
                select(ResearchRunArtifactModel)
                .where(ResearchRunArtifactModel.run_id == run_id)
                .order_by(ResearchRunArtifactModel.sequence)
            )
            result = await self._session.stream(
                stmt, execution_options={"yield_per": _ARTIFACT_STREAM_CHUNK}
            )
            async for row in result:
                yield row[0]

        return _stream()

    async def list_artifact_digests(
        self, run_id: str
    ) -> list[tuple[str, str | None, str]]:
        """artifact 瘦指纹(stage, decision_id, checksum),不取 payload 列。

        result_checksum / 拒绝路径归档只消费这三个字段(#470 前半场);列级
        SELECT 避免 run 收尾把全量 payload 再物化一遍(556 期全历史 run
        ≈ 5GB JSON 文本)。行序与 ``list_artifacts`` 一致。
        """

        stmt = (
            select(
                ResearchRunArtifactModel.stage,
                ResearchRunArtifactModel.decision_id,
                ResearchRunArtifactModel.checksum,
            )
            .where(ResearchRunArtifactModel.run_id == run_id)
            .order_by(ResearchRunArtifactModel.sequence)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(stage, decision_id, checksum) for stage, decision_id, checksum in rows]

    async def get_artifact_by_trace(
        self, run_id: str, trace_id: str
    ) -> ResearchRunArtifactModel | None:
        stmt = select(ResearchRunArtifactModel).where(
            ResearchRunArtifactModel.run_id == run_id,
            ResearchRunArtifactModel.trace_id == trace_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def model_payload(row: ResearchRunModel) -> dict[str, Any]:
        return {
            "run_id": row.run_id,
            "status": row.status,
            "manifest": row.manifest,
            "result": row.result,
            "result_checksum": row.result_checksum,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
            "job_id": row.job_id,
        }


__all__ = [
    "ResearchRunPersistenceConflictError",
    "ResearchRunRepository",
]
