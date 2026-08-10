"""``finboard.sim.*`` 工具 —— 持久化模拟盘(issue #127,边界 #83)。

- 只读(9):sim_account_list / sim_account_get / sim_session_list /
  sim_session_get / sim_orders / sim_fills / sim_positions / sim_ledger /
  sim_audit / sim_report
- 写(8):sim_account_create / sim_session_create / sim_session_start /
  sim_session_pause / sim_session_stop / sim_session_reset /
  sim_decision_submit / sim_order_cancel

复用现有 ``SimulationService`` / ``SimulationRepository`` +
``simulation_schemas`` 的 Pydantic 模型(复用 ``to_domain()`` /
``model_validate``),不重复实现业务逻辑。

模拟盘边界(#83)不变:
- 只接受结构化目标仓位决策(→ 生成订单),**不直接创建订单 / 修改持仓**;
- 不连接实盘 broker / QMT / CTP;
- 不自动晋级影子盘 / 实盘。
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

__all__ = ["register"]


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _map_sim_error(exc: Exception) -> McpToolError:
    """把模拟域异常映射为 ``McpToolError``(与 API ``_raise_http`` 一致)。"""
    from finboard_simulation import (
        SimulationConflictError,
        SimulationError,
        SimulationIsolationError,
        SimulationNotFoundError,
        SimulationRiskError,
        SimulationTransitionError,
    )

    if isinstance(exc, McpToolError):
        raise exc
    if isinstance(exc, SimulationNotFoundError):
        return McpToolError("not_found", str(exc))
    if isinstance(
        exc,
        (
            SimulationConflictError,
            SimulationTransitionError,
            SimulationIsolationError,
        ),
    ):
        return McpToolError("conflict", str(exc))
    if isinstance(exc, SimulationRiskError):
        return McpToolError("invalid_argument", str(exc))
    if isinstance(exc, SimulationError):
        return McpToolError("invalid_argument", str(exc))
    return McpToolError("invalid_argument", str(exc))


def _out_row(schema_cls: Any, row: Any) -> dict[str, Any]:
    """ORM 行 → Pydantic *Out → dict(字段名与 API 一致)。"""
    return cast(dict[str, Any], to_jsonable(schema_cls.model_validate(row).model_dump(mode="json")))


def _validate_id(entity: str, value: str) -> None:
    """校验模拟 ID 前缀(与路由一致):账户 ``SIM-A-`` / 会话 ``SIM-S-`` /
    验证运行 ``RR-``。"""
    prefix_map = {"account": "SIM-A-", "session": "SIM-S-", "run": "RR-"}
    prefix = prefix_map.get(entity)
    if prefix is not None and not value.startswith(prefix):
        raise McpToolError(
            "invalid_argument",
            f"模拟接口只接受 {prefix} 前缀的 {entity} ID",
        )


# --------------------------------------------------------------------------- #
# 账户(只读 list/get + 写 create)
# --------------------------------------------------------------------------- #
async def sim_account_list(
    app: McpAppContext,
    *,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationAccountOut
        from finboard_simulation import SimulationRepository

        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            rows = await SimulationRepository(session).list_accounts(limit=safe_limit)
            return [_out_row(SimulationAccountOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.account_list",
        arguments={"limit": limit},
        handler=_do,
    )


async def sim_account_get(
    app: McpAppContext,
    account_id: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_api.simulation_schemas import SimulationAccountOut
        from finboard_simulation import SimulationRepository

        _validate_id("account", account_id)
        async with app.session_maker() as session:
            row = await SimulationRepository(session).get_account(account_id)
            if row is None:
                raise McpToolError("not_found", f"模拟账户不存在: {account_id}")
            return _out_row(SimulationAccountOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.account_get",
        arguments={"account_id": account_id},
        handler=_do,
    )


async def sim_account_create(
    app: McpAppContext,
    *,
    name: str,
    initial_cash: str,
    actor: str,
    currency: str = "CNY",
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from decimal import Decimal

        from finboard_api.simulation_schemas import SimulationAccountCreateIn, SimulationAccountOut
        from finboard_simulation import SimulationRepository, SimulationService

        try:
            body = SimulationAccountCreateIn(
                name=name,
                initial_cash=Decimal(initial_cash),
                actor=actor,
                currency=currency,
            )
        except Exception as exc:
            raise _map_sim_error(exc) from exc

        async with app.session_maker() as session:
            try:
                row = await SimulationService(SimulationRepository(session)).create_account(
                    name=body.name,
                    initial_cash=body.initial_cash,
                    actor=body.actor,
                    currency=body.currency,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            return _out_row(SimulationAccountOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.account_create",
        arguments={"name": name, "initial_cash": initial_cash, "actor": actor},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 会话(只读 list/get + 写 create/start/pause/stop/reset)
# --------------------------------------------------------------------------- #
async def sim_session_list(
    app: McpAppContext,
    *,
    account_id: str | None = None,
    status: list[str] | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationSessionOut
        from finboard_simulation import SimulationRepository, SimulationSessionStatus

        if status is not None:
            valid = {item.value for item in SimulationSessionStatus}
            if not set(status).issubset(valid):
                raise McpToolError("invalid_argument", f"未知模拟会话状态: {status}")
        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            rows = await SimulationRepository(session).list_sessions(
                account_id=account_id,
                statuses=status,
                limit=safe_limit,
            )
            return [_out_row(SimulationSessionOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.session_list",
        arguments={"account_id": account_id, "status": status, "limit": limit},
        handler=_do,
    )


async def sim_session_get(
    app: McpAppContext,
    session_id: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_api.simulation_schemas import SimulationSessionOut
        from finboard_simulation import SimulationRepository

        _validate_id("session", session_id)
        async with app.session_maker() as session:
            row = await SimulationRepository(session).get_session(session_id)
            if row is None:
                raise McpToolError("not_found", f"模拟会话不存在: {session_id}")
            return _out_row(SimulationSessionOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.session_get",
        arguments={"session_id": session_id},
        handler=_do,
    )


async def sim_session_create(
    app: McpAppContext,
    *,
    simulation_account_id: str,
    strategy_id: str,
    strategy_version: int,
    validation_run_id: str,
    data_release_id: str,
    source_mode: str,
    matching: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    clock_speed: str = "1",
    actor: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)

        from finboard_api.simulation_schemas import (
            SimulationSessionCreateIn,
            SimulationSessionOut,
        )
        from finboard_simulation import SimulationRepository, SimulationService

        _validate_id("account", simulation_account_id)
        _validate_id("run", validation_run_id)
        try:
            body = SimulationSessionCreateIn.model_validate(
                {
                    "simulation_account_id": simulation_account_id,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "validation_run_id": validation_run_id,
                    "data_release_id": data_release_id,
                    "source_mode": source_mode,
                    "matching": matching or {},
                    "risk": risk or {},
                    "clock_speed": clock_speed,
                    "actor": actor,
                }
            )
        except Exception as exc:
            raise _map_sim_error(exc) from exc

        async with app.session_maker() as session:
            try:
                row = await SimulationService(SimulationRepository(session)).create_session(
                    account_id=body.simulation_account_id,
                    strategy_id=body.strategy_id,
                    strategy_version=body.strategy_version,
                    validation_run_id=body.validation_run_id,
                    data_release_id=body.data_release_id,
                    source_mode=body.source_mode,
                    config=body.to_config(),
                    actor=body.actor,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            return _out_row(SimulationSessionOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.session_create",
        arguments={
            "simulation_account_id": simulation_account_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "validation_run_id": validation_run_id,
            "data_release_id": data_release_id,
            "source_mode": source_mode,
            "actor": actor,
        },
        handler=_do,
    )


async def _session_transition(
    app: McpAppContext,
    session_id: str,
    target: str,
    actor: str,
) -> ToolEnvelope:
    """会话状态转换的共享实现(start / pause / stop 共用)。"""
    from finboard_api.simulation_schemas import SimulationSessionOut
    from finboard_simulation import (
        SimulationRepository,
        SimulationService,
        SimulationSessionStatus,
    )

    target_enum = SimulationSessionStatus(target)

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        _validate_id("session", session_id)
        async with app.session_maker() as session:
            try:
                row = await SimulationService(
                    SimulationRepository(session)
                ).transition_session(session_id, target=target_enum, actor=actor)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            return _out_row(SimulationSessionOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name=f"finboard.sim.session_{target}",
        arguments={"session_id": session_id, "actor": actor},
        handler=_do,
    )


async def sim_session_start(
    app: McpAppContext, session_id: str, *, actor: str
) -> ToolEnvelope:
    return await _session_transition(app, session_id, "running", actor)


async def sim_session_pause(
    app: McpAppContext, session_id: str, *, actor: str
) -> ToolEnvelope:
    return await _session_transition(app, session_id, "paused", actor)


async def sim_session_stop(
    app: McpAppContext, session_id: str, *, actor: str
) -> ToolEnvelope:
    return await _session_transition(app, session_id, "stopped", actor)


async def sim_session_reset(
    app: McpAppContext,
    session_id: str,
    *,
    actor: str,
    initial_cash: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from decimal import Decimal

        from finboard_api.simulation_schemas import (
            SimulationAccountOut,
            SimulationResetOut,
            SimulationSessionOut,
        )
        from finboard_simulation import SimulationRepository, SimulationService

        _validate_id("session", session_id)
        cash_value = Decimal(initial_cash) if initial_cash is not None else None
        async with app.session_maker() as session:
            try:
                account, new_session = await SimulationService(
                    SimulationRepository(session)
                ).reset_session(session_id, actor=actor, initial_cash=cash_value)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            out = SimulationResetOut(
                account=SimulationAccountOut.model_validate(account),
                session=SimulationSessionOut.model_validate(new_session),
            )
            return cast(
                dict[str, Any],
                to_jsonable(out.model_dump(mode="json")),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.session_reset",
        arguments={"session_id": session_id, "actor": actor, "initial_cash": initial_cash},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 决策提交(写,核心:结构化目标仓位 → 生成订单)
# --------------------------------------------------------------------------- #
async def sim_decision_submit(
    app: McpAppContext,
    session_id: str,
    *,
    decision: dict[str, Any],
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_api.simulation_schemas import (
            SimulationDecisionIn,
            SimulationDecisionOut,
            SimulationDecisionResultOut,
            SimulationOrderOut,
        )
        from finboard_simulation import SimulationRepository, SimulationService

        _validate_id("session", session_id)
        try:
            body = SimulationDecisionIn.model_validate(decision)
        except Exception as exc:
            raise _map_sim_error(exc) from exc

        async with app.session_maker() as session:
            try:
                decision_row, orders, duplicate = await SimulationService(
                    SimulationRepository(session)
                ).submit_decision(session_id, body.to_domain())
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            out = SimulationDecisionResultOut(
                decision=SimulationDecisionOut.model_validate(decision_row),
                orders=[
                    SimulationOrderOut.model_validate(o) for o in orders
                ],
                duplicate=duplicate,
            )
            return cast(
                dict[str, Any],
                to_jsonable(out.model_dump(mode="json")),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.decision_submit",
        arguments={"session_id": session_id, "decision": decision},
        handler=_do,
    )


async def sim_order_cancel(
    app: McpAppContext,
    session_id: str,
    order_id: str,
    *,
    actor: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_api.simulation_schemas import SimulationOrderOut
        from finboard_simulation import SimulationRepository, SimulationService

        _validate_id("session", session_id)
        async with app.session_maker() as session:
            try:
                row = await SimulationService(
                    SimulationRepository(session)
                ).cancel_order(session_id, order_id, actor=actor)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise _map_sim_error(exc) from exc
            return _out_row(SimulationOrderOut, row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.order_cancel",
        arguments={"session_id": session_id, "order_id": order_id, "actor": actor},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 会话内只读查询(orders / fills / positions / ledger / audit)
# --------------------------------------------------------------------------- #
async def _require_sim_session(repo: Any, session_id: str) -> Any:
    """复用路由的 _require_session 逻辑(前缀校验 + 存在性)。"""
    _validate_id("session", session_id)
    row = await repo.get_session(session_id)
    if row is None:
        raise McpToolError("not_found", f"模拟会话不存在: {session_id}")
    return row


async def sim_orders(
    app: McpAppContext,
    session_id: str,
    *,
    status: list[str] | None = None,
    symbol: str | None = None,
    limit: int = 1000,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationOrderOut
        from finboard_simulation import SimulationRepository

        safe_limit = max(1, min(10000, limit))
        async with app.session_maker() as session:
            repo = SimulationRepository(session)
            await _require_sim_session(repo, session_id)
            rows = await repo.list_orders(
                session_id, statuses=status, symbol=symbol, limit=safe_limit
            )
            return [_out_row(SimulationOrderOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.orders",
        arguments={
            "session_id": session_id,
            "status": status,
            "symbol": symbol,
            "limit": limit,
        },
        handler=_do,
    )


async def sim_fills(
    app: McpAppContext,
    session_id: str,
    *,
    limit: int = 1000,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationFillOut
        from finboard_simulation import SimulationRepository

        safe_limit = max(1, min(10000, limit))
        async with app.session_maker() as session:
            repo = SimulationRepository(session)
            await _require_sim_session(repo, session_id)
            rows = await repo.list_fills(session_id, limit=safe_limit)
            return [_out_row(SimulationFillOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.fills",
        arguments={"session_id": session_id, "limit": limit},
        handler=_do,
    )


async def sim_positions(app: McpAppContext, session_id: str) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationPositionOut
        from finboard_simulation import SimulationRepository

        async with app.session_maker() as session:
            repo = SimulationRepository(session)
            sim_session = await _require_sim_session(repo, session_id)
            rows = await repo.list_positions(sim_session.simulation_account_id)
            return [_out_row(SimulationPositionOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.positions",
        arguments={"session_id": session_id},
        handler=_do,
    )


async def sim_ledger(
    app: McpAppContext,
    session_id: str,
    *,
    limit: int = 1000,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationLedgerOut
        from finboard_simulation import SimulationRepository

        safe_limit = max(1, min(10000, limit))
        async with app.session_maker() as session:
            repo = SimulationRepository(session)
            await _require_sim_session(repo, session_id)
            rows = await repo.list_ledger(session_id, limit=safe_limit)
            return [_out_row(SimulationLedgerOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.ledger",
        arguments={"session_id": session_id, "limit": limit},
        handler=_do,
    )


async def sim_audit(
    app: McpAppContext,
    session_id: str,
    *,
    limit: int = 1000,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_api.simulation_schemas import SimulationAuditOut
        from finboard_simulation import SimulationRepository

        safe_limit = max(1, min(10000, limit))
        async with app.session_maker() as session:
            repo = SimulationRepository(session)
            await _require_sim_session(repo, session_id)
            rows = await repo.list_audit(session_id, limit=safe_limit)
            return [_out_row(SimulationAuditOut, r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.audit",
        arguments={"session_id": session_id, "limit": limit},
        handler=_do,
    )


async def sim_report(app: McpAppContext, session_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_simulation import SimulationRepository, SimulationService

        _validate_id("session", session_id)
        async with app.session_maker() as session:
            try:
                report = await SimulationService(
                    SimulationRepository(session)
                ).build_report(session_id)
            except Exception as exc:
                raise _map_sim_error(exc) from exc
            return cast(dict[str, Any], to_jsonable(report))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.sim.report",
        arguments={"session_id": session_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """把模拟盘工具注册到 MCP server(9 只读 + 8 写 = 17 个)。"""

    @mcp.tool(
        name="finboard_sim_account_list",
        description="列出模拟账户(只读)。返回 simulation_account_id/状态/资金/权益。",
    )
    async def _account_list(
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_account_list(app_context(ctx), limit=limit)

    @mcp.tool(
        name="finboard_sim_account_get",
        description="查询单个模拟账户详情(只读)。ID 须以 SIM-A- 开头。",
    )
    async def _account_get(
        account_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_account_get(app_context(ctx), account_id)

    @mcp.tool(
        name="finboard_sim_account_create",
        description=(
            "创建模拟账户(写)。参数:name、initial_cash(>0)、actor、currency(CNY)。"
            "生成 SIM-A- 前缀 ID。"
        ),
    )
    async def _account_create(
        name: str,
        initial_cash: str,
        actor: str,
        currency: str = "CNY",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_account_create(
            app_context(ctx),
            name=name,
            initial_cash=initial_cash,
            actor=actor,
            currency=currency,
        )

    @mcp.tool(
        name="finboard_sim_session_list",
        description="列出模拟会话(只读),可按 account_id / status 过滤。",
    )
    async def _session_list(
        account_id: str | None = None,
        status: list[str] | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_list(
            app_context(ctx),
            account_id=account_id,
            status=status,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_sim_session_get",
        description="查询单个模拟会话详情(只读)。ID 须以 SIM-S- 开头。",
    )
    async def _session_get(
        session_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_get(app_context(ctx), session_id)

    @mcp.tool(
        name="finboard_sim_session_create",
        description=(
            "创建模拟会话(写)。绑定已发布策略版本 + completed ResearchRun(RR- 前缀)"
            "+ 数据发布。源模式 source_mode(bar_replay / event_driven)。"
            "matching/risk 为撮合与风控配置(省略用默认)。"
        ),
    )
    async def _session_create(
        simulation_account_id: str,
        strategy_id: str,
        strategy_version: int,
        validation_run_id: str,
        data_release_id: str,
        source_mode: str,
        actor: str,
        matching: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
        clock_speed: str = "1",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_create(
            app_context(ctx),
            simulation_account_id=simulation_account_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            validation_run_id=validation_run_id,
            data_release_id=data_release_id,
            source_mode=source_mode,
            matching=matching,
            risk=risk,
            clock_speed=clock_speed,
            actor=actor,
        )

    @mcp.tool(
        name="finboard_sim_session_start",
        description="启动模拟会话(created/paused → running,写)。",
    )
    async def _session_start(
        session_id: str,
        actor: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_start(app_context(ctx), session_id, actor=actor)

    @mcp.tool(
        name="finboard_sim_session_pause",
        description="暂停模拟会话(running → paused,写)。",
    )
    async def _session_pause(
        session_id: str,
        actor: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_pause(app_context(ctx), session_id, actor=actor)

    @mcp.tool(
        name="finboard_sim_session_stop",
        description="停止模拟会话(→ stopped,撤全部活动单 + 重估,写)。",
    )
    async def _session_stop(
        session_id: str,
        actor: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_stop(app_context(ctx), session_id, actor=actor)

    @mcp.tool(
        name="finboard_sim_session_reset",
        description=(
            "重置模拟会话(stopped/archived → 新账户+新会话,原会话不动,"
            "经 reset_of_session_id 关联,写)。可选 initial_cash 重置资金。"
        ),
    )
    async def _session_reset(
        session_id: str,
        actor: str,
        initial_cash: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_session_reset(
            app_context(ctx),
            session_id,
            actor=actor,
            initial_cash=initial_cash,
        )

    @mcp.tool(
        name="finboard_sim_decision_submit",
        description=(
            "提交结构化目标仓位决策(写)。输入 decision(含 decision_id/"
            "source_run_id(RR-)/targets 列表),模拟 runner 据此生成订单——"
            "agent 不直接创建订单。targets 含 symbol/target_quantity(期望总仓位,"
            "非增量)/signal_trace_id/reason。"
        ),
    )
    async def _decision_submit(
        session_id: str,
        decision: dict[str, Any],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_decision_submit(
            app_context(ctx), session_id, decision=decision
        )

    @mcp.tool(
        name="finboard_sim_order_cancel",
        description="撤销模拟订单(running/paused 会话内的活动单,写)。",
    )
    async def _order_cancel(
        session_id: str,
        order_id: str,
        actor: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_order_cancel(
            app_context(ctx), session_id, order_id, actor=actor
        )

    @mcp.tool(
        name="finboard_sim_orders",
        description="列出模拟会话的订单(只读),可按 status / symbol 过滤。",
    )
    async def _orders(
        session_id: str,
        status: list[str] | None = None,
        symbol: str | None = None,
        limit: int = 1000,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_orders(
            app_context(ctx),
            session_id,
            status=status,
            symbol=symbol,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_sim_fills",
        description="列出模拟会话的成交(只读)。",
    )
    async def _fills(
        session_id: str,
        limit: int = 1000,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_fills(app_context(ctx), session_id, limit=limit)

    @mcp.tool(
        name="finboard_sim_positions",
        description="列出模拟会话(账户)的持仓(只读)。",
    )
    async def _positions(
        session_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_positions(app_context(ctx), session_id)

    @mcp.tool(
        name="finboard_sim_ledger",
        description="列出模拟会话的账本流水(只读)。",
    )
    async def _ledger(
        session_id: str,
        limit: int = 1000,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_ledger(app_context(ctx), session_id, limit=limit)

    @mcp.tool(
        name="finboard_sim_audit",
        description="列出模拟会话的审计事件(只读)。",
    )
    async def _audit(
        session_id: str,
        limit: int = 1000,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_audit(app_context(ctx), session_id, limit=limit)

    @mcp.tool(
        name="finboard_sim_report",
        description=(
            "生成模拟会话绩效报告(只读)。含初始资金/最终权益/收益率/"
            "回撤/手续费/订单数/成交数,以及 simulation_is_not_return_proof="
            "true / automatic_live_promotion=false。"
        ),
    )
    async def _report(
        session_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await sim_report(app_context(ctx), session_id)
