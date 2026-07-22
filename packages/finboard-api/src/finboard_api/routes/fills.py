"""成交记录端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_account_id, get_session
from finboard_api.schemas import FillOut, PageResponse
from finboard_persistence import FillRepository
from finboard_shared.identifiers import AccountId

router = APIRouter(prefix="/api/fills", tags=["fills"])


@router.get("", response_model=PageResponse[FillOut])
async def list_fills(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: AccountId = Depends(get_account_id),
    session: AsyncSession = Depends(get_session),
) -> PageResponse[FillOut]:
    repo = FillRepository(session)
    fills, total = await repo.list_by_account(
        str(account_id),
        limit=limit,
        offset=offset,
    )
    return PageResponse(
        items=[
            FillOut(
                fill_id=f.fill_id,
                client_order_id=str(f.client_order_id),
                symbol=f.symbol.code,
                side=f.side.value,
                quantity=f.quantity,
                price=f.price,
                commission=f.commission,
                tax=f.tax,
                broker_order_id=f.broker_order_id,
                filled_at=f.filled_at,
            )
            for f in fills
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
