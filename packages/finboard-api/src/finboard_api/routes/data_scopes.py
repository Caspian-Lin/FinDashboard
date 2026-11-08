"""统一数据范围 REST(issue #510)。

只读预览:把 ``DataScopeSpec`` 解析成候选发布 / 窗口 / 缺口 / 阻塞 /
受影响策略。**无状态**——解析结果不落库,``resolution_id`` 只是内容指纹;
把旧预览拿去提交时由入队路由重算复验(数据变了就 409 漂移)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import BaseSchema
from finboard_backtest.data_scope import (
    DataScopeSpec,
    ScopeBlocker,
    ScopeResolution,
    ScopeResolutionError,
    resolve_data_scope,
)

router = APIRouter(prefix="/api/data/scopes", tags=["data-scopes"])


class ScopePreviewIn(BaseModel):
    """预览入参(与 ``ScopePreviewRequest`` 同构,放 API 层便于扩展)。"""

    model_config = ConfigDict(extra="forbid")

    scope: DataScopeSpec
    mode: str = "new_spec"
    strategy_id: str | None = None
    strategy_version: int | None = None


class ScopeValidateIn(BaseModel):
    """提交前复验入参:预览的 scope + 预览返回的 resolution_id + 拟冻结发布。"""

    model_config = ConfigDict(extra="forbid")

    scope: DataScopeSpec
    mode: str = "new_spec"
    strategy_id: str | None = None
    strategy_version: int | None = None
    resolution_id: str = Field(min_length=1, max_length=64)
    dataset_release_ids: list[str] = Field(default_factory=list)


class ScopeDriftOut(BaseSchema):
    expected_resolution_id: str
    actual_resolution_id: str
    expected_release_ids: list[str]
    actual_release_ids: list[str]


class ScopeBlockerOut(BaseSchema):
    code: str
    message: str
    dataset: str | None


class ScopeValidateOut(BaseSchema):
    valid: bool
    resolution_id: str
    drift: ScopeDriftOut | None
    blockers: list[ScopeBlockerOut]
    warnings: list[str]


@router.post("/preview", response_model=ScopeResolution)
async def preview_scope(
    body: ScopePreviewIn,
    session: AsyncSession = Depends(get_db_session),
) -> ScopeResolution:
    """只读解析;不落库、不触发同步或发布。"""

    return await _resolve(session, body)


@router.post("/validate", response_model=ScopeValidateOut)
async def validate_scope(
    body: ScopeValidateIn,
    session: AsyncSession = Depends(get_db_session),
) -> ScopeValidateOut:
    """提交前复验:预览之后数据是否已漂移(不抛错,返回判定)。"""

    resolution = await _resolve(session, body)
    expected = sorted(set(body.dataset_release_ids))
    actual = sorted(set(resolution.release_ids))
    drift = None
    if expected != actual or resolution.resolution_id != body.resolution_id:
        drift = ScopeDriftOut(
            expected_resolution_id=body.resolution_id,
            actual_resolution_id=resolution.resolution_id,
            expected_release_ids=expected,
            actual_release_ids=actual,
        )
    return ScopeValidateOut(
        valid=drift is None and not resolution.blockers,
        resolution_id=resolution.resolution_id,
        drift=drift,
        blockers=[_blocker_out(item) for item in resolution.blockers],
        warnings=list(resolution.warnings),
    )


@router.get("/{resolution_id}")
async def get_scope_resolution(resolution_id: str) -> ScopeResolution:
    """无状态预览不支持按 ID 回读(避免暗示它被持久化)。"""

    raise HTTPException(
        status_code=404,
        detail=(
            f"scope 预览无状态,{resolution_id} 不可回读;"
            "请重新 POST /api/data/scopes/preview"
        ),
    )


async def _resolve(
    session: AsyncSession, body: ScopePreviewIn | ScopeValidateIn
) -> ScopeResolution:
    mode = body.mode
    if mode not in {"new_spec", "existing_spec"}:
        raise HTTPException(status_code=422, detail=f"未知 scope 预览模式: {mode}")
    if mode == "existing_spec" and (
        body.strategy_id is None or body.strategy_version is None
    ):
        raise HTTPException(
            status_code=422,
            detail="existing_spec 模式必须提供 strategy_id 与 strategy_version",
        )
    try:
        return await resolve_data_scope(
            session,
            body.scope,
            strategy_id=body.strategy_id,
            strategy_version=body.strategy_version,
        )
    except ScopeResolutionError as exc:
        raise HTTPException(
            status_code=422, detail=f"{exc.code}: {exc.message}"
        ) from exc


def _blocker_out(item: ScopeBlocker) -> ScopeBlockerOut:
    return ScopeBlockerOut(
        code=item.code,
        message=item.message,
        dataset=item.dataset.value if item.dataset is not None else None,
    )


__all__ = ["router"]
