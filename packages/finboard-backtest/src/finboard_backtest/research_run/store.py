"""ResearchRun 存储端口与内存参考实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from finboard_backtest.research_run.contracts import (
    JsonValue,
    ResearchArtifact,
    ResearchRunConflictError,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunReport,
    ResearchRunStatus,
)


class ResearchRunStore(Protocol):
    async def create_or_get(
        self, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]: ...

    async def get(self, run_id: str) -> ResearchRunRecord | None: ...

    async def list_by_status(
        self, statuses: Iterable[ResearchRunStatus]
    ) -> list[ResearchRunRecord]: ...

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[ResearchRunStatus],
        target: ResearchRunStatus,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunRecord: ...

    async def save_result(
        self,
        run_id: str,
        *,
        report: ResearchRunReport,
        result_checksum: str,
        timing: dict[str, JsonValue] | None = None,
    ) -> ResearchRunRecord: ...

    async def append_artifact(self, artifact: ResearchArtifact) -> bool: ...

    async def list_artifacts(self, run_id: str) -> list[ResearchArtifact]: ...

    async def checkpoint(self) -> None: ...


class InMemoryResearchRunStore:
    """用于纯离线测试/CLI 的幂等存储。

    ``append_artifact`` 以 artifact_id 去重:内容相同返回 ``False``,内容不同
    抛冲突,从而模拟 PostgreSQL 唯一约束和断点重放语义。
    """

    def __init__(self) -> None:
        self._runs: dict[str, ResearchRunRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._artifacts: dict[str, dict[str, ResearchArtifact]] = {}
        self._lock = asyncio.Lock()

    async def create_or_get(
        self, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]:
        async with self._lock:
            existing_id = self._idempotency.get(manifest.idempotency_key)
            if existing_id is not None:
                existing = self._runs[existing_id]
                if existing.manifest.checksum != manifest.checksum:
                    raise ResearchRunConflictError("相同幂等键对应了不同运行清单")
                return existing, False
            if manifest.run_id in self._runs:
                existing = self._runs[manifest.run_id]
                if existing.manifest.checksum != manifest.checksum:
                    raise ResearchRunConflictError("相同 run_id 对应了不同运行清单")
                return existing, False
            record = ResearchRunRecord(manifest=manifest)
            self._runs[manifest.run_id] = record
            self._idempotency[manifest.idempotency_key] = manifest.run_id
            self._artifacts[manifest.run_id] = {}
            return record, True

    async def get(self, run_id: str) -> ResearchRunRecord | None:
        return self._runs.get(run_id)

    async def list_by_status(
        self, statuses: Iterable[ResearchRunStatus]
    ) -> list[ResearchRunRecord]:
        accepted = frozenset(statuses)
        return [record for record in self._runs.values() if record.status in accepted]

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[ResearchRunStatus],
        target: ResearchRunStatus,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunRecord:
        async with self._lock:
            record = self._runs[run_id]
            if record.status not in expected:
                raise ResearchRunConflictError(
                    f"运行 {run_id} 状态为 {record.status.value},"
                    f"期望 {sorted(item.value for item in expected)}"
                )
            now = datetime.now(UTC)
            record.status = target
            record.updated_at = now
            record.error_code = error_code
            record.error_summary = error_summary
            if target is ResearchRunStatus.RUNNING and record.started_at is None:
                record.started_at = now
            if target in {
                ResearchRunStatus.COMPLETED,
                ResearchRunStatus.FAILED,
                ResearchRunStatus.REJECTED,
                ResearchRunStatus.CANCELLED,
            }:
                record.completed_at = now
            return record

    async def save_result(
        self,
        run_id: str,
        *,
        report: ResearchRunReport,
        result_checksum: str,
        timing: dict[str, JsonValue] | None = None,
    ) -> ResearchRunRecord:
        async with self._lock:
            record = self._runs[run_id]
            if record.result_checksum is not None and record.result_checksum != result_checksum:
                raise ResearchRunConflictError("同一次运行产生了不同结果")
            record.result = report
            record.result_checksum = result_checksum
            # issue #285:分段耗时只挂 record,不进 report/checksum。
            record.timing = timing
            record.updated_at = datetime.now(UTC)
            return record

    async def append_artifact(self, artifact: ResearchArtifact) -> bool:
        async with self._lock:
            run_artifacts = self._artifacts[artifact.run_id]
            existing = run_artifacts.get(artifact.artifact_id)
            if existing is not None:
                if existing.checksum != artifact.checksum:
                    raise ResearchRunConflictError(
                        f"artifact {artifact.artifact_id} 断点重放内容不一致"
                    )
                return False
            duplicate_sequence = next(
                (
                    item
                    for item in run_artifacts.values()
                    if item.sequence == artifact.sequence
                ),
                None,
            )
            if duplicate_sequence is not None:
                raise ResearchRunConflictError(
                    f"artifact sequence {artifact.sequence} 已被占用"
                )
            run_artifacts[artifact.artifact_id] = artifact
            return True

    async def list_artifacts(self, run_id: str) -> list[ResearchArtifact]:
        values = self._artifacts.get(run_id, {}).values()
        return sorted(values, key=lambda item: item.sequence)

    async def checkpoint(self) -> None:
        return None


__all__ = ["InMemoryResearchRunStore", "ResearchRunStore"]
