"""核对端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_components, get_session
from finboard_api.schemas import ReconcileResult
from finboard_app.bootstrap import KernelComponents

router = APIRouter(prefix="/api/reconcile", tags=["reconcile"])


@router.post("", response_model=ReconcileResult)
async def trigger_reconcile(
    components: KernelComponents = Depends(get_components),
    session: AsyncSession = Depends(get_session),
) -> ReconcileResult:
    """触发一次本地↔券商核对(只读)。"""
    recon = components.new_reconciler(session)
    report = await recon.run()
    return ReconcileResult(
        ok=report.ok,
        summary=report.summary(),
        total_checks=getattr(report, "total_checks", 0),
        mismatches=len(getattr(report, "mismatches", [])),
    )
