"""策略参数预设端点。

预设只引用随应用发布的内置策略 kind,并保存经过对应 Pydantic schema 校验的参数。
本路由不接受策略源码、模块路径或可执行表达式。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import (
    StrategyPresetCreate,
    StrategyPresetOut,
    StrategyPresetUpdate,
)
from finboard_api.strategy_validation import validate_strategy_params_for_api
from finboard_persistence import StrategyPresetRepository

router = APIRouter(prefix="/api/strategy-presets", tags=["strategy-presets"])


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="预设名称不能为空")
    return cleaned


async def _ensure_name_available(
    repo: StrategyPresetRepository,
    name: str,
    *,
    exclude_id: int | None = None,
) -> None:
    existing = await repo.get_by_name(name)
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(status_code=409, detail="预设名称已存在")


@router.post("", response_model=StrategyPresetOut, status_code=201)
async def create_preset(
    req: StrategyPresetCreate,
    session: AsyncSession = Depends(get_db_session),
) -> StrategyPresetOut:
    repo = StrategyPresetRepository(session)
    name = _clean_name(req.name)
    await _ensure_name_available(repo, name)
    params = validate_strategy_params_for_api(req.strategy, req.params)
    try:
        row = await repo.create(name=name, strategy=req.strategy, params=params)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="预设名称已存在") from exc
    return StrategyPresetOut.model_validate(row)


@router.get("", response_model=list[StrategyPresetOut])
async def list_presets(
    session: AsyncSession = Depends(get_db_session),
) -> list[StrategyPresetOut]:
    rows = await StrategyPresetRepository(session).list_all()
    return [StrategyPresetOut.model_validate(row) for row in rows]


@router.get("/{preset_id}", response_model=StrategyPresetOut)
async def get_preset(
    preset_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> StrategyPresetOut:
    row = await StrategyPresetRepository(session).get(preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="策略预设不存在")
    return StrategyPresetOut.model_validate(row)


@router.put("/{preset_id}", response_model=StrategyPresetOut)
async def update_preset(
    preset_id: int,
    req: StrategyPresetUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> StrategyPresetOut:
    repo = StrategyPresetRepository(session)
    current = await repo.get(preset_id)
    if current is None:
        raise HTTPException(status_code=404, detail="策略预设不存在")

    name = _clean_name(req.name) if req.name is not None else current.name
    strategy = req.strategy if req.strategy is not None else current.strategy
    raw_params = req.params if req.params is not None else current.params
    await _ensure_name_available(repo, name, exclude_id=preset_id)
    params = validate_strategy_params_for_api(strategy, raw_params)

    try:
        row = await repo.update(
            preset_id,
            name=name,
            strategy=strategy,
            params=params,
        )
        assert row is not None
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="预设名称已存在") from exc
    return StrategyPresetOut.model_validate(row)


@router.delete("/{preset_id}", status_code=204)
async def delete_preset(
    preset_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ok = await StrategyPresetRepository(session).delete(preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="策略预设不存在")
    await session.commit()
