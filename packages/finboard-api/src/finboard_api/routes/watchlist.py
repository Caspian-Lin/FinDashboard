"""标的组(watchlist)端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_session
from finboard_api.schemas import (
    WatchlistAddSymbols,
    WatchlistCreate,
    WatchlistDetailOut,
    WatchlistOut,
    WatchlistUpdate,
)
from finboard_persistence import WatchlistRepository

router = APIRouter(prefix="/api/watchlists", tags=["watchlist"])


def _require(session: AsyncSession) -> WatchlistRepository:
    return WatchlistRepository(session)


@router.get("", response_model=list[WatchlistOut])
async def list_watchlists(
    session: AsyncSession = Depends(get_session),
) -> list[WatchlistOut]:
    """列出全部标的组(含成员数)。"""
    repo = _require(session)
    rows = await repo.list_all()
    result: list[WatchlistOut] = []
    for w in rows:
        items = await repo.items(w.id)
        result.append(
            WatchlistOut(
                id=w.id,
                name=w.name,
                description=w.description,
                item_count=len(items),
                created_at=w.created_at,
            )
        )
    await session.commit()
    return result


@router.post("", response_model=WatchlistOut, status_code=201)
async def create_watchlist(
    body: WatchlistCreate,
    session: AsyncSession = Depends(get_session),
) -> WatchlistOut:
    """创建标的组。"""
    repo = _require(session)
    row = await repo.create(body.name, body.description)
    await session.commit()
    return WatchlistOut(
        id=row.id,
        name=row.name,
        description=row.description,
        item_count=0,
        created_at=row.created_at,
    )


@router.get("/{watchlist_id}", response_model=WatchlistDetailOut)
async def get_watchlist(
    watchlist_id: int,
    session: AsyncSession = Depends(get_session),
) -> WatchlistDetailOut:
    """获取标的组详情(含成员列表)。"""
    repo = _require(session)
    row = await repo.get(watchlist_id)
    if row is None:
        raise HTTPException(status_code=404, detail="标的组不存在")
    items = await repo.items(watchlist_id)
    await session.commit()
    return WatchlistDetailOut(
        id=row.id,
        name=row.name,
        description=row.description,
        item_count=len(items),
        created_at=row.created_at,
        symbols=[it.symbol_code for it in items],
    )


@router.put("/{watchlist_id}", response_model=WatchlistOut)
async def update_watchlist(
    watchlist_id: int,
    body: WatchlistUpdate,
    session: AsyncSession = Depends(get_session),
) -> WatchlistOut:
    """重命名 / 更新描述。"""
    repo = _require(session)
    row = await repo.rename(
        watchlist_id, body.name or "", body.description
    )
    if row is None:
        raise HTTPException(status_code=404, detail="标的组不存在")
    items = await repo.items(watchlist_id)
    await session.commit()
    return WatchlistOut(
        id=row.id,
        name=row.name,
        description=row.description,
        item_count=len(items),
        created_at=row.created_at,
    )


@router.delete("/{watchlist_id}", status_code=204)
async def delete_watchlist(
    watchlist_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除标的组(级联删除成员)。"""
    repo = _require(session)
    ok = await repo.delete(watchlist_id)
    if not ok:
        raise HTTPException(status_code=404, detail="标的组不存在")
    await session.commit()


@router.post("/{watchlist_id}/symbols", response_model=WatchlistDetailOut)
async def add_symbols(
    watchlist_id: int,
    body: WatchlistAddSymbols,
    session: AsyncSession = Depends(get_session),
) -> WatchlistDetailOut:
    """向标的组添加标的(自动去重)。"""
    repo = _require(session)
    if await repo.get(watchlist_id) is None:
        raise HTTPException(status_code=404, detail="标的组不存在")
    await repo.add_symbols(watchlist_id, body.symbols)
    row = await repo.get(watchlist_id)
    items = await repo.items(watchlist_id)
    await session.commit()
    assert row is not None
    return WatchlistDetailOut(
        id=row.id,
        name=row.name,
        description=row.description,
        item_count=len(items),
        created_at=row.created_at,
        symbols=[it.symbol_code for it in items],
    )


@router.delete("/{watchlist_id}/symbols/{symbol_code}", response_model=WatchlistDetailOut)
async def remove_symbol(
    watchlist_id: int,
    symbol_code: str,
    session: AsyncSession = Depends(get_session),
) -> WatchlistDetailOut:
    """从标的组移除单个标的。"""
    repo = _require(session)
    if await repo.get(watchlist_id) is None:
        raise HTTPException(status_code=404, detail="标的组不存在")
    await repo.remove_symbol(watchlist_id, symbol_code)
    row = await repo.get(watchlist_id)
    items = await repo.items(watchlist_id)
    await session.commit()
    assert row is not None
    return WatchlistDetailOut(
        id=row.id,
        name=row.name,
        description=row.description,
        item_count=len(items),
        created_at=row.created_at,
        symbols=[it.symbol_code for it in items],
    )
