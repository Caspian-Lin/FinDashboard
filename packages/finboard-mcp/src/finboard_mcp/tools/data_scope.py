"""``finboard.data_scope.*`` 工具 —— 统一数据范围预览(只读,issue #510)。

与 REST ``POST /api/data/scopes/preview`` 同源(:func:`resolve_data_scope`):
把一份 ``DataScopeSpec`` 解析成候选发布 / 交易日窗口 / 单位与复权交集 /
缺口 / 阻塞 / 候选替代 / 受影响策略。

**只读**(除 #509 逐标的读模型自愈外):不落库、不触发 ``dataset_sync``、
不发布数据集。写侧不新增工具 ——
提交 run 仍走 ``finboard_research_run_queue``(入队 payload 增加
``data_scope`` + ``data_scope_resolution_id``,提交期由同一门控函数复验)。
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_scope import (
    DataScopeSpec,
    ScopeResolution,
    ScopeResolutionError,
    resolve_data_scope,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

_SCOPE_MODES = {"new_spec", "existing_spec"}
_SCOPE_VIEWS = {"summary", "detail"}


async def data_scope_preview(
    app: McpAppContext,
    *,
    scope: dict[str, Any],
    mode: str = "new_spec",
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    view: str = "summary",
) -> ToolEnvelope:
    """只读解析统一数据范围(scope → release → 窗口)。"""

    if mode not in _SCOPE_MODES:
        raise McpToolError(
            "invalid_argument", f"mode 仅支持 {sorted(_SCOPE_MODES)},收到 {mode!r}"
        )
    if view not in _SCOPE_VIEWS:
        raise McpToolError(
            "invalid_argument", f"view 仅支持 {sorted(_SCOPE_VIEWS)},收到 {view!r}"
        )
    if mode == "existing_spec" and (
        strategy_id is None or strategy_version is None
    ):
        raise McpToolError(
            "invalid_argument", "existing_spec 模式必须提供 strategy_id/version"
        )
    try:
        spec = DataScopeSpec.model_validate(scope)
    except ValidationError as exc:
        raise McpToolError("invalid_argument", f"scope 非法: {exc}") from exc

    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            resolution = await _resolve(
                session,
                spec,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            )
        payload = _resolution_to_dict(resolution, view=view)
        return cast(dict[str, Any], to_jsonable(payload))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_scope.preview",
        arguments={
            "scope": scope,
            "mode": mode,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "view": view,
        },
        handler=_do,
    )


async def _resolve(
    session: AsyncSession,
    spec: DataScopeSpec,
    *,
    strategy_id: str | None,
    strategy_version: int | None,
) -> ScopeResolution:
    try:
        return await resolve_data_scope(
            session,
            spec,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
        )
    except ScopeResolutionError as exc:
        raise McpToolError("invalid_argument", f"{exc.code}: {exc.message}") from exc


def _resolution_to_dict(
    resolution: ScopeResolution, *, view: str
) -> dict[str, Any]:
    window = resolution.window
    payload: dict[str, Any] = {
        "resolution_id": resolution.resolution_id,
        "scope_checksum": resolution.scope_checksum,
        "selection_mode": resolution.selection_mode,
        "resolver_version": resolution.resolver_version,
        "as_of": resolution.as_of,
        "window": {
            "warmup_start": window.warmup_start,
            "decision_start": window.decision_start,
            "decision_end": window.decision_end,
            "valuation_end": window.valuation_end,
            "warmup_sessions": window.warmup_sessions,
            "decision_sessions": window.decision_sessions,
            "tail_sessions": window.tail_sessions,
            "calendar_markets": list(window.calendar_markets),
            "calendar_source": window.calendar_source,
        },
        "releases": [
            {
                "artifact_id": item.artifact_id,
                "dataset": item.dataset.value,
                "role": item.role,
                "version": item.version,
                "checksum": item.checksum,
                "published_at": item.published_at,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "coverage_pct": float(item.coverage_pct),
                "symbol_count": item.symbol_count,
                "reason": item.reason,
            }
            for item in resolution.releases
        ],
        "universe": {
            "requested_count": resolution.universe.requested_count,
            "resolved_count": resolution.universe.resolved_count,
            "truncated": resolution.universe.truncated,
            "symbols_preview": list(resolution.universe.symbols_preview),
            "excluded": dict(resolution.universe.excluded),
        },
        "gaps": {
            "missing_in_bars": list(resolution.gaps.missing_in_bars),
            "missing_in_research": {
                key: list(value)
                for key, value in resolution.gaps.missing_in_research.items()
            },
            "window_shortfall": dict(resolution.gaps.window_shortfall),
        },
        "warnings": list(resolution.warnings),
        "blockers": [
            {
                "code": item.code,
                "message": item.message,
                "dataset": item.dataset.value if item.dataset is not None else None,
            }
            for item in resolution.blockers
        ],
    }
    if view == "detail":
        payload["alternatives"] = [
            {
                "dataset": item.dataset.value,
                "release_id": item.release_id,
                "version": item.version,
                "reason": item.reason,
            }
            for item in resolution.alternatives
        ]
        payload["affected_strategies"] = [
            {
                "strategy_id": item.strategy_id,
                "version": item.version,
                "name": item.name,
                "reason": item.reason,
            }
            for item in resolution.affected_strategies
        ]
    else:
        payload["alternative_count"] = len(resolution.alternatives)
        payload["affected_strategy_count"] = len(resolution.affected_strategies)
    return payload


def register(mcp: MCPServer) -> None:
    """把统一数据范围预览工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_data_scope_preview",
        description=(
            "只读:预览统一数据范围(scope → 冻结发布 → 执行窗口)。入参 "
            "scope 为 DataScopeSpec JSON:{universe{markets/asset_classes/"
            "instrument_types/explicit_symbols/filters/selection_limit},"
            "decision{start,end,schedule?},warmup_sessions,tail_sessions,"
            "benchmarks,datasets{required,optional},fields,bars_adjustment,"
            "release_selection(auto|locked),locked_release_ids}。返回 "
            "resolution_id(提交 run 时要带回来)/scope_checksum/window"
            "{warmup_start,decision_start,decision_end,valuation_end,"
            "warmup_sessions,decision_sessions,tail_sessions}/releases"
            "[{artifact_id,dataset,role,version,checksum,reason}]/universe/"
            "gaps/warnings/blockers。view=summary(默认)只回计数;"
            "view=detail 才回 alternatives(候选替代)与 affected_strategies"
            "(引用这些发布的策略)。mode=existing_spec 时需提供 "
            "strategy_id/version,并核对 scope 与策略 validation_plan 一致。"
            "只读:不落库、不触发同步、不发布数据集;提交仍走 "
            "finboard_research_run_queue 的 data_scope + "
            "data_scope_resolution_id 字段。"
        ),
    )
    async def _data_scope_preview(
        scope: dict[str, Any],
        mode: str = "new_spec",
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_scope_preview(
            app_context(ctx),
            scope=scope,
            mode=mode,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            view=view,
        )


__all__ = ["data_scope_preview", "register"]
