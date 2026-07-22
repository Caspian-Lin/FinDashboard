"""FinboardError → HTTP 状态码映射。"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from finboard_shared.exceptions import (
    BrokerError,
    BrokerTimeoutError,
    FinboardError,
    KernelNotReadyError,
    KillSwitchActiveError,
    OrderNotFoundError,
    RiskCheckError,
)

_STATUS_MAP: dict[type[FinboardError], int] = {
    KernelNotReadyError: 503,
    RiskCheckError: 422,
    KillSwitchActiveError: 409,
    OrderNotFoundError: 404,
    BrokerTimeoutError: 408,
    BrokerError: 502,
}


async def finboard_error_handler(
    request: Request, exc: FinboardError
) -> JSONResponse:
    status = 500
    for cls, code in _STATUS_MAP.items():
        if isinstance(exc, cls):
            status = code
            break

    detail = str(exc)
    extra: dict[str, str] = {}
    if isinstance(exc, RiskCheckError):
        extra["reject_reason"] = exc.reason.value

    body: dict[str, object] = {"detail": detail}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)
