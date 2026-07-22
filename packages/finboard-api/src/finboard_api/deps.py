"""FastAPI 依赖注入。"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.bootstrap import KernelComponents
from finboard_core import TradingKernel
from finboard_shared.identifiers import AccountId


def get_kernel(request: Request) -> TradingKernel:
    return request.app.state.kernel  # type: ignore[no-any-return]


def get_session(request: Request) -> AsyncSession:
    return request.app.state.session  # type: ignore[no-any-return]


def get_account_id(request: Request) -> AccountId:
    return request.app.state.account_id  # type: ignore[no-any-return]


def get_components(request: Request) -> KernelComponents:
    return request.app.state.components  # type: ignore[no-any-return]
