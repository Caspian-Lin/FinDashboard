"""冻结包在统一数据队列中的提交和执行。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from finboard_app.background_jobs import enqueue_job_core
from finboard_app.release_bundle import (
    available_preview_bytes,
    bundle_file_checksum,
    export_registered_bundle,
    import_registered_bundle,
    lock_bundle_previews,
    managed_bundle_path,
    new_bundle_id,
    release_root,
)
from finboard_app.research_audit import ResearchAuditEvent, ResearchAuditRecorder
from finboard_app.research_operation import ResearchOperationContext
from finboard_backtest.background_jobs.contracts import ExecutorError, JobResult
from finboard_data.release_bundle import (
    ReleaseBundleError,
    inspect_release_bundle,
    verify_release_bundle,
)
from finboard_data.releases import DatasetReleaseError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_backtest.background_jobs.contracts import JobRecord, ProgressCallback
    from finboard_persistence.models import BackgroundJobModel

BUNDLE_JOB_KIND = "release_bundle"


async def submit_bundle_export(
    session: AsyncSession,
    *,
    release_ids: Sequence[str],
    context: ResearchOperationContext,
) -> tuple[BackgroundJobModel, str]:
    bundle_id = new_bundle_id()
    row, _ = await enqueue_job_core(
        session,
        kind=BUNDLE_JOB_KIND,
        queue="data",
        idempotency_key=f"bundle-export:{bundle_id}",
        payload={"action": "export", "release_ids": list(release_ids), "bundle_id": bundle_id},
        requested_by=context.actor,
        context=context,
    )
    return row, bundle_id


async def submit_bundle_import(
    session: AsyncSession,
    *,
    bundle_id: str,
    checksum: str,
    context: ResearchOperationContext,
) -> BackgroundJobModel:
    path = managed_bundle_path(bundle_id)
    pending = managed_bundle_path(bundle_id, pending=True)
    await lock_bundle_previews(session)
    await asyncio.to_thread(available_preview_bytes, pending.parent)
    if pending.is_file():
        verified = await asyncio.to_thread(bundle_file_checksum, pending)
        if verified != checksum:
            raise ReleaseBundleError("bundle_preview_changed")
    elif not path.is_file():
        raise ReleaseBundleError("bundle_file_not_found")
    row, _ = await enqueue_job_core(
        session,
        kind=BUNDLE_JOB_KIND,
        queue="data",
        idempotency_key=f"bundle-import:{bundle_id}:{checksum}",
        payload={"action": "import", "bundle_id": bundle_id, "checksum": checksum},
        requested_by=context.actor,
        context=context,
    )
    if pending.is_file():
        await asyncio.to_thread(pending.rename, path)
    return row


async def bundle_receipt(bundle_id: str) -> dict[str, Any]:
    path = managed_bundle_path(bundle_id)
    if not path.is_file():
        path = managed_bundle_path(bundle_id, pending=True)
        if not path.is_file():
            raise ReleaseBundleError("bundle_file_not_found")
    preview = await asyncio.to_thread(inspect_release_bundle, path)
    checksum = await asyncio.to_thread(bundle_file_checksum, path)
    return {"bundle_id": bundle_id, "checksum": checksum, **preview.summary()}


class ReleaseBundleExecutor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, worker_id: str) -> None:
        self._sessions = sessions
        self._worker_id = worker_id
        self._audit = ResearchAuditRecorder(session_maker=sessions)

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        bundle_id = str(job.payload.get("bundle_id", ""))
        action = job.payload.get("action")
        context = ResearchOperationContext.for_worker(self._worker_id)
        try:
            path = managed_bundle_path(bundle_id)
            await progress(0, 2, "bundle_validate")
            if action == "export":
                ids = job.payload.get("release_ids")
                if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
                    raise ReleaseBundleError("bundle_invalid_release_selection")
                release_ids = cast(list[str], ids)
                await progress(1, 2, "bundle_export")
                # 进程若在原子发布包后中断, 复验现有结果即可续接。
                if path.is_file():
                    archive = await asyncio.to_thread(verify_release_bundle, path)
                    if [release.release_id for release in archive.releases] != release_ids:
                        raise ReleaseBundleError("bundle_export_identity_mismatch")
                else:
                    async with self._sessions() as session:
                        await export_registered_bundle(
                            session, release_ids, path, root=release_root(),
                        )
                async with self._sessions.begin() as session:
                    await self._audit.record(session, ResearchAuditEvent(
                        operation="bundle.export", outcome="succeeded", context=context,
                        entity_type="release_bundle", entity_id=bundle_id,
                        correlation={"job_id": job.job_id},
                        params_summary={"release_count": len(release_ids)},
                    ))
            elif action == "import":
                checksum = job.payload.get("checksum")
                if not isinstance(checksum, str):
                    raise ReleaseBundleError("bundle_invalid_checksum")
                await progress(1, 2, "bundle_import")
                await import_registered_bundle(
                    self._sessions, path, root=release_root(), expected_checksum=checksum,
                    context=context, bundle_id=bundle_id, job_id=job.job_id,
                )
            else:
                raise ReleaseBundleError("bundle_invalid_action")
        except (DatasetReleaseError, ValueError) as exc:
            if action in ("import", "export"):
                await self._audit.record_detached(ResearchAuditEvent(
                    operation=f"bundle.{action}", outcome="rejected", context=context,
                    entity_type="release_bundle", entity_id=bundle_id,
                    correlation={"job_id": job.job_id},
                    result_code="release_bundle_invalid",
                ))
            raise ExecutorError(
                code="release_bundle_invalid",
                summary=str(exc) if isinstance(exc, ReleaseBundleError) else "冻结发布不可用",
                retryable=False,
            ) from exc
        except OSError as exc:
            if action in ("import", "export"):
                await self._audit.record_detached(ResearchAuditEvent(
                    operation=f"bundle.{action}", outcome="failed", context=context,
                    entity_type="release_bundle", entity_id=bundle_id,
                    correlation={"job_id": job.job_id},
                    result_code="release_bundle_io",
                ))
            raise ExecutorError(
                code="release_bundle_io",
                summary="文件读写失败。请检查共享存储和磁盘余量",
                retryable=True,
            ) from exc
        await progress(2, 2, "bundle_complete")
        return JobResult(status="succeeded", result_ref=bundle_id, progress_total=2)
