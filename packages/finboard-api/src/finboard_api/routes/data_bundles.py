"""冻结发布包操作。预览仅暂存上传文件, 提交后才登记发布。"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobOut
from finboard_api.schemas import BaseSchema
from finboard_app.release_bundle import (
    managed_bundle_path,
    new_bundle_id,
    preview_registered_bundle,
    release_root,
)
from finboard_app.release_bundle_jobs import (
    bundle_receipt,
    submit_bundle_export,
    submit_bundle_import,
)
from finboard_app.research_operation import ResearchOperationContext
from finboard_data.release_bundle import MAX_BUNDLE_BYTES, ReleaseBundleError

router = APIRouter(prefix="/api/data/bundles", tags=["data-bundles"])


class BundleImportIn(BaseSchema):
    bundle_id: str = Field(pattern=r"^RB-[a-f0-9]{32}$")
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")


class BundleExportIn(BaseSchema):
    release_ids: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        min_length=1, max_length=32,
    )


def _error(exc: ReleaseBundleError) -> HTTPException:
    message = str(exc)
    if message == "bundle_file_not_found":
        return HTTPException(status_code=404, detail=message)
    if message == "bundle_quota_exceeded":
        return HTTPException(status_code=413, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/preview")
async def preview_upload(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    mime = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if mime not in {"application/zip", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="bundle_archive_required")
    bundle_id = new_bundle_id()
    path = anyio.Path(managed_bundle_path(bundle_id))
    await path.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    count = 0
    retained = False
    try:
        async with await path.open("xb") as output:
            async for chunk in request.stream():
                count += len(chunk)
                if count > MAX_BUNDLE_BYTES:
                    raise ReleaseBundleError("bundle_quota_exceeded")
                checksum.update(chunk)
                await output.write(chunk)
        preview = await preview_registered_bundle(
            session, managed_bundle_path(bundle_id), root=release_root(),
        )
        retained = True
        return {**preview, "bundle_id": bundle_id, "checksum": checksum.hexdigest()}
    except ReleaseBundleError as exc:
        raise _error(exc) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail="bundle_storage_unavailable") from exc
    finally:
        if not retained:
            with anyio.CancelScope(shield=True):
                await path.unlink(missing_ok=True)


@router.post("/imports", status_code=202, response_model=JobOut)
async def import_archive(
    body: BundleImportIn,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    try:
        job = await submit_bundle_import(
            session,
            bundle_id=body.bundle_id,
            checksum=body.checksum,
            context=ResearchOperationContext.for_web(),
        )
    except ReleaseBundleError as exc:
        raise _error(exc) from exc
    await session.commit()
    return JobOut.model_validate(job)


@router.post("/exports", status_code=202)
async def export_archive(
    body: BundleExportIn,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    if len(set(body.release_ids)) != len(body.release_ids):
        raise HTTPException(status_code=422, detail="bundle_duplicate_release")
    job, bundle_id = await submit_bundle_export(
        session, release_ids=body.release_ids, context=ResearchOperationContext.for_web(),
    )
    await session.commit()
    return {"job_id": job.job_id, "bundle_id": bundle_id, "status": job.status}


@router.get("/{bundle_id}")
async def get_bundle(bundle_id: str) -> dict[str, Any]:
    try:
        return await bundle_receipt(bundle_id)
    except ReleaseBundleError as exc:
        raise _error(exc) from exc


@router.get("/{bundle_id}/download")
async def download_bundle(bundle_id: str) -> FileResponse:
    try:
        path = managed_bundle_path(bundle_id)
        if not await anyio.Path(path).is_file():
            raise ReleaseBundleError("bundle_file_not_found")
    except ReleaseBundleError as exc:
        raise _error(exc) from exc
    return FileResponse(path, media_type="application/zip", filename=f"{bundle_id}.zip")
