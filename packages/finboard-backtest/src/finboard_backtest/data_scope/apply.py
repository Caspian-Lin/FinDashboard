"""提交 run 时的 DataScope 复验与冻结负载(issue #510)。

预览是只读的,提交必须用**同一份** :class:`DataScopeSpec` 重算并比对
``resolution_id``:预览之后若又有新发布上线(``auto`` 模式会选到更新的
release)、或锁定发布被归档,解析结果与 ``resolution_id`` 都会变化,于是
「旧预览 + 新数据」被具名拒绝(409 ``scope_release_drift``)而不是静默
换用最新发布——否则用户看到的数据范围和实际执行的数据范围会不一致。

解析结论里的 ``research_window`` 合并进 ``parameters``(dev #502 的执行窗口
语义),manifest 再按 checksum 冻结这四个日期。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeResolution,
)
from finboard_backtest.data_scope.resolver import (
    ScopeResolutionError,
    resolve_data_scope,
)


class ScopeGateError(ValueError):
    """提交期 scope 门控失败。

    ``status_code`` 由调用端映射:REST 409 只用于「数据已变,需重新预览」,
    其余 422(形状/前置条件/阻塞)。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}


async def apply_scope_to_parameters(
    session: AsyncSession,
    scope: DataScopeSpec,
    *,
    resolution_id: str | None,
    release_ids: Sequence[str],
    strategy_id: str,
    strategy_version: int,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """复验预览结论并把窗口合并进参数,返回 ``(parameters, manifest 区块)``。

    ``release_ids`` 是本次提交冻结的发布集合(已与策略验证计划核对过);
    解析集合必须与它完全一致。
    """

    if resolution_id is None:
        raise ScopeGateError(
            "scope_resolution_required",
            "提交带 data_scope 时必须同时提供预览返回的 data_scope_resolution_id",
        )
    try:
        resolution = await resolve_data_scope(
            session,
            scope,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
        )
    except ScopeResolutionError as exc:
        raise ScopeGateError(exc.code, exc.message) from exc

    _check_resolution(
        resolution,
        resolution_id=resolution_id,
        release_ids=release_ids,
    )
    merged = _merge_window(parameters, resolution)
    return merged, resolution.manifest_payload(scope)

def _check_resolution(
    resolution: ScopeResolution,
    *,
    resolution_id: str,
    release_ids: Sequence[str],
) -> None:
    expected = sorted(set(release_ids))
    actual = sorted(set(resolution.release_ids))
    drift = {
        "expected_resolution_id": resolution_id,
        "actual_resolution_id": resolution.resolution_id,
        "expected_release_ids": expected,
        "actual_release_ids": actual,
    }
    # 先判漂移再判阻塞:漂移时解析可能同时产出 blocker(例如策略规格仍
    # 固定旧发布),但根因是「数据变了」,应先要求重新预览。
    if resolution.resolution_id != resolution_id:
        raise ScopeGateError(
            "scope_release_drift",
            "预览之后数据范围已变化(发布集合或窗口解析不同),请重新预览后再提交",
            status_code=409,
            detail=drift,
        )
    if resolution.blockers:
        joined = "; ".join(
            f"{item.code}: {item.message}" for item in resolution.blockers
        )
        raise ScopeGateError("scope_blocked", joined)
    if expected != actual:
        raise ScopeGateError(
            "scope_release_drift",
            "scope 解析的发布集合与提交冻结的 dataset_release_ids 不一致: "
            f"scope={actual}, 提交={expected}",
            status_code=409,
            detail=drift,
        )


def _merge_window(
    parameters: dict[str, Any], resolution: ScopeResolution
) -> dict[str, Any]:
    window = resolution.run_parameters()
    merged = dict(parameters)
    existing = merged.get("research_window")
    if existing is not None and existing != window:
        raise ScopeGateError(
            "scope_window_conflict",
            "parameters.research_window 与 scope 解析窗口不一致: "
            f"提交={existing}, scope={window}",
        )
    merged["research_window"] = window
    return merged


__all__ = [
    "ScopeGateError",
    "apply_scope_to_parameters",
]
