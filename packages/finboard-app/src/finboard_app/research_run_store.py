"""SQLAlchemy Repository 到 ResearchRunStore 端口的组装层。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import cast

from finboard_backtest.research_run import (
    ArtifactDigest,
    ResearchArtifact,
    ResearchRunConflictError,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunReport,
    ResearchRunStage,
    ResearchRunStatus,
    ResearchRunStore,
    manifest_from_json,
    report_from_json,
    to_json_value,
)
from finboard_backtest.research_run.contracts import JsonValue
from finboard_persistence import (
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
)


class SqlAlchemyResearchRunStore(ResearchRunStore):
    """保持离线研究域与 ORM 分层,绝不复用实盘 Repository。"""

    def __init__(self, repository: ResearchRunRepository) -> None:
        self._repository = repository

    async def create_or_get(
        self, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]:
        payload = to_json_value(manifest)
        assert isinstance(payload, dict)
        try:
            row, created = await self._repository.create_or_get(
                run_id=manifest.run_id,
                idempotency_key=manifest.idempotency_key,
                replay_of_run_id=manifest.replay_of_run_id,
                strategy_id=manifest.strategy_spec.strategy_id,
                strategy_kind=manifest.strategy_kind,
                status=ResearchRunStatus.QUEUED.value,
                schema_version=manifest.schema_version,
                manifest_checksum=manifest.checksum,
                manifest=cast(dict[str, object], payload),
                requested_by=manifest.requested_by,
            )
        except ResearchRunPersistenceConflictError as exc:
            raise ResearchRunConflictError(str(exc)) from exc
        return _record_from_model(row), created

    async def get(self, run_id: str) -> ResearchRunRecord | None:
        row = await self._repository.get(run_id)
        return None if row is None else _record_from_model(row)

    async def list_by_status(
        self, statuses: Iterable[ResearchRunStatus]
    ) -> list[ResearchRunRecord]:
        rows = await self._repository.list_recent(
            statuses=[item.value for item in statuses],
            limit=10_000,
        )
        return [_record_from_model(row) for row in rows]

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[ResearchRunStatus],
        target: ResearchRunStatus,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunRecord:
        try:
            row = await self._repository.transition(
                run_id,
                expected=frozenset(item.value for item in expected),
                target=target.value,
                error_code=error_code,
                error_summary=error_summary,
            )
        except ResearchRunPersistenceConflictError as exc:
            raise ResearchRunConflictError(str(exc)) from exc
        return _record_from_model(row)

    async def save_result(
        self,
        run_id: str,
        *,
        report: ResearchRunReport,
        result_checksum: str,
        timing: dict[str, JsonValue] | None = None,
        partial_failure: dict[str, JsonValue] | None = None,
    ) -> ResearchRunRecord:
        payload = to_json_value(report)
        assert isinstance(payload, dict)
        if timing is not None:
            # issue #285:分段耗时与 report 同存于 result JSON,但不是 report
            # 字段、不参与任何 artifact/checksum(重放耗时不同不判漂移)。
            payload = {**payload, "timing": timing}
        if partial_failure is not None:
            # issue #304:组合阶段拒绝但保留部分证据时,result JSON 顶层标注
            # partial 与失败决策定位(constraint_failure);成功路径不注入,
            # result 序列化与既有输出逐字节一致。report_from_json 忽略未知键,
            # 读回 record 不受影响。
            payload = {
                **payload,
                "partial": True,
                "constraint_failure": partial_failure,
            }
        try:
            row = await self._repository.save_result(
                run_id,
                result=cast(dict[str, object], payload),
                result_checksum=result_checksum,
            )
        except ResearchRunPersistenceConflictError as exc:
            raise ResearchRunConflictError(str(exc)) from exc
        return _record_from_model(row)

    async def append_artifact(self, artifact: ResearchArtifact) -> bool:
        try:
            _, created = await self._repository.append_artifact(
                run_id=artifact.run_id,
                artifact_id=artifact.artifact_id,
                decision_id=artifact.decision_id,
                sequence=artifact.sequence,
                stage=artifact.stage.value,
                trace_id=artifact.trace_id,
                parent_trace_ids=list(artifact.parent_trace_ids),
                payload=cast(dict[str, object], artifact.payload),
                checksum=artifact.checksum,
                # issue #472:canonical 文本直写(非 None 时权威),落库不再
                # 对 dict 二次 json.dumps。
                payload_json=artifact.payload_json,
            )
        except ResearchRunPersistenceConflictError as exc:
            raise ResearchRunConflictError(str(exc)) from exc
        return created

    async def append_artifacts(
        self, artifacts: Sequence[ResearchArtifact]
    ) -> list[bool]:
        """批内逐 artifact 幂等追加(同一 session,不逐个 commit;提交边界
        由调用方决定 —— SessionPerOperation 包装在批后一次 commit)。语义
        与逐个调用 :meth:`append_artifact` 完全一致:批内任一冲突即整批
        抛错(该 session 已 flush 的前序行随会话终结一并回滚)。"""

        created: list[bool] = []
        try:
            for artifact in artifacts:
                _, artifact_created = await self._repository.append_artifact(
                    run_id=artifact.run_id,
                    artifact_id=artifact.artifact_id,
                    decision_id=artifact.decision_id,
                    sequence=artifact.sequence,
                    stage=artifact.stage.value,
                    trace_id=artifact.trace_id,
                    parent_trace_ids=list(artifact.parent_trace_ids),
                    payload=cast(dict[str, object], artifact.payload),
                    checksum=artifact.checksum,
                    payload_json=artifact.payload_json,
                )
                created.append(artifact_created)
        except ResearchRunPersistenceConflictError as exc:
            raise ResearchRunConflictError(str(exc)) from exc
        return created

    async def list_artifacts(self, run_id: str) -> list[ResearchArtifact]:
        rows = await self._repository.list_artifacts(run_id)
        return [
            ResearchArtifact(
                artifact_id=row.artifact_id,
                run_id=row.run_id,
                decision_id=row.decision_id,
                sequence=row.sequence,
                stage=ResearchRunStage(row.stage),
                trace_id=row.trace_id,
                parent_trace_ids=tuple(row.parent_trace_ids),
                payload=cast(dict[str, JsonValue], row.payload),
                checksum=row.checksum,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def iter_artifacts(self, run_id: str) -> AsyncIterator[ResearchArtifact]:
        """流式遍历 artifact(#470 前半场;行序 = sequence 升序)。

        repo 侧服务端游标 + 分块;本层只做行 → 契约对象映射,不物化整表。
        """

        async def _stream() -> AsyncIterator[ResearchArtifact]:
            async for row in self._repository.iter_artifacts(run_id):
                yield ResearchArtifact(
                    artifact_id=row.artifact_id,
                    run_id=row.run_id,
                    decision_id=row.decision_id,
                    sequence=row.sequence,
                    stage=ResearchRunStage(row.stage),
                    trace_id=row.trace_id,
                    parent_trace_ids=tuple(row.parent_trace_ids),
                    payload=cast(dict[str, JsonValue], row.payload),
                    checksum=row.checksum,
                    created_at=row.created_at,
                )

        return _stream()

    async def list_artifact_digests(self, run_id: str) -> list[ArtifactDigest]:
        rows = await self._repository.list_artifact_digests(run_id)
        return [
            ArtifactDigest(stage=ResearchRunStage(stage), decision_id=decision_id, checksum=checksum)
            for stage, decision_id, checksum in rows
        ]

    async def checkpoint(self) -> None:
        await self._repository.checkpoint()


def _record_from_model(row: object) -> ResearchRunRecord:
    from finboard_persistence import ResearchRunModel

    assert isinstance(row, ResearchRunModel)
    manifest = manifest_from_json(row.manifest)
    # issue #285:result JSON 中的 "timing" 键不是 report 字段(report_from_json
    # 忽略未知键),读回时单独提取挂到 record 上。
    raw_result = row.result if isinstance(row.result, dict) else {}
    timing_raw = raw_result.get("timing")
    timing = dict(timing_raw) if isinstance(timing_raw, dict) else None
    return ResearchRunRecord(
        manifest=manifest,
        status=ResearchRunStatus(row.status),
        result=report_from_json(row.result) if row.result is not None else None,
        result_checksum=row.result_checksum,
        error_code=row.error_code,
        error_summary=row.error_summary,
        job_id=getattr(row, "job_id", None),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
        timing=cast(dict[str, JsonValue] | None, timing),
    )


__all__ = ["SqlAlchemyResearchRunStore"]
