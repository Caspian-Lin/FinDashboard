"""策略规格 MCP 工具(issue #126)。

版本化无代码研究策略的完整生命周期:registry / template / validate /
draft / supersede / publish / rollback / diff / preset CRUD。

- 只读工具(8 个):registry / template / list / history / version_get / diff /
  preset_list / preset_get
- 写工具(8 个):validate(纯计算,不持久化)/ draft_create / supersede /
  publish / rollback / preset_create / preset_update / preset_delete

所有工具复用现有 repository / compiler / 注册表函数,不重复业务逻辑。
不触及交易安全红线(下单 / 持仓 / Kill Switch);不接受 Python 源码、模块路径或
可执行表达式(规格由白名单组件组合而成,经 Pydantic schema 与编译器双重校验)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


__all__ = ["register"]


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _version_to_dict(row: Any) -> dict[str, Any]:
    """把 ``ResearchStrategySpecModel`` ORM 行映射为 JSON 可序列化字典。

    与 API 路由的 ``_version_out`` 字段一一对应;``spec`` 字段从 payload 重建为
    ``ResearchStrategySpec`` 后再导出,保证结构化输出。
    """
    from finboard_backtest.strategy_spec import ResearchStrategySpec

    spec = ResearchStrategySpec.model_validate(row.payload)
    return {
        "strategy_id": row.strategy_id,
        "version": row.version,
        "schema_version": row.schema_version,
        "name": row.name,
        "strategy_kind": row.strategy_kind,
        "status": row.status,
        "change_type": row.change_type,
        "checksum": row.checksum,
        "spec": cast(dict[str, Any], to_jsonable(spec.canonical_payload())),
        "validation_errors": list(row.validation_errors),
        "parent_version": row.parent_version,
        "rollback_of_version": row.rollback_of_version,
        "created_at": to_jsonable(row.created_at),
        "published_at": to_jsonable(row.published_at),
    }


def _version_ack(row: Any) -> dict[str, Any]:
    """写操作精简回执(issue #206 P1):id/version/status/checksum/created_at。

    全量详情(含完整 spec payload)走 ``finboard_strategy_version_get``。
    """
    return {
        "strategy_id": row.strategy_id,
        "version": row.version,
        "status": row.status,
        "checksum": row.checksum,
        "created_at": to_jsonable(row.created_at),
        "view": "ack",
        "detail_hint": (
            f"finboard_strategy_version_get(strategy_id={row.strategy_id!r}, "
            f"version={row.version})"
        ),
    }


def _validation_to_dict(plan: Any) -> dict[str, Any]:
    """把 ``ResolvedStrategyPlan`` 映射为 validate 工具返回字典。"""
    preview = getattr(plan, "universe_precheck", None)
    return {
        "valid": True,
        "checksum": plan.checksum,
        "feature_order": list(plan.feature_order),
        "required_factor_sources": list(plan.required_factor_sources),
        "required_datasets": list(plan.required_datasets),
        "dataset_release_ids": list(plan.dataset_release_ids),
        "lifecycle_stages": list(plan.lifecycle_stages),
        "can_execute": plan.can_execute,
        # issue #186:universe 预检(universe_precheck.as_dict);无发布信息时为空。
        "universe_precheck": preview.as_dict() if preview is not None else None,
    }


def _preset_to_dict(row: Any) -> dict[str, Any]:
    """把 ``StrategyPresetModel`` ORM 行映射为 JSON 可序列化字典。"""
    return {
        "id": row.id,
        "name": row.name,
        "strategy": row.strategy,
        "params": dict(row.params) if row.params else {},
        "selection": dict(row.selection) if row.selection else {},
        "created_at": to_jsonable(row.created_at),
        "updated_at": to_jsonable(row.updated_at),
    }


async def _compile_with_releases(
    spec: Any,
    session: AsyncSession,
    *,
    disabled_factors: frozenset[str] = frozenset(),
) -> Any:
    """复用 API 路由的编译逻辑:校验数据集发布可用 → 编译注册策略规格。

    将 ``ReleaseCapabilityError`` / ``StrategySpecError`` / ``ValueError`` 统一映射为
    ``McpToolError(invalid_argument)``(对应 HTTP 422)。

    issue #186:编译后附加 universe 静态预检(依赖字段存在性 + 候选池空池
    诊断),与 REST ``/validate`` 同一评估函数,挂在 ``ResolvedStrategyPlan``。
    """
    from dataclasses import replace

    from finboard_backtest.research_code import active_user_factor_names
    from finboard_backtest.strategy_spec import (
        StrategySpecError,
        compile_registered_strategy_spec,
    )
    from finboard_backtest.strategy_spec.universe_precheck import (
        preview_universe_pool,
        resolvable_feature_names,
    )
    from finboard_data.releases import ReleaseCapabilityError
    from finboard_persistence import ResearchDatasetReleaseRepository

    release_repo = ResearchDatasetReleaseRepository(session)
    releases: list[Any] = []
    try:
        for release_id in spec.validation_plan.dataset_release_ids:
            releases.append(await release_repo.require_usable(release_id))
        # issue #217:用户因子(u_ 前缀)按 artifact status=active 名单校验。
        user_factors = await active_user_factor_names(session)
        plan = compile_registered_strategy_spec(
            spec,
            disabled_factors=disabled_factors,
            available_dataset_release_ids=frozenset(
                release.release_id for release in releases
            ),
            user_factor_sources=user_factors,
        )
    except (ReleaseCapabilityError, StrategySpecError, ValueError) as exc:
        raise McpToolError("invalid_argument", f"策略规格校验失败: {exc}") from exc
    if not releases:
        return plan
    primary = releases[0]
    return replace(
        plan,
        universe_precheck=preview_universe_pool(
            spec.universe,
            primary.instruments,
            decision_date=primary.end_date,
            available_features=resolvable_feature_names(
                feature_graph_sources=[
                    node.source for node in spec.feature_graph.nodes if node.source is not None
                ],
                research_release_kinds=[release.dataset_kind for release in releases],
            ),
        ),
    )


def _validate_preset_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """用内置策略定义的 params_model 校验参数,返回规范化 JSON。

    与 API 路由的 ``validate_strategy_params_for_api`` 等价,但不依赖 FastAPI
    ``HTTPException``;``ValueError``/``ValidationError`` 统一映射为
    ``McpToolError(invalid_argument)``。
    """
    from finboard_app.strategies import get_strategy_definition

    try:
        definition = get_strategy_definition(kind)
        normalized = definition.params_model.model_validate(params)
        return cast(dict[str, Any], to_jsonable(normalized.model_dump(mode="json")))
    except ValueError as exc:
        raise McpToolError("invalid_argument", f"未知策略 kind: {kind}") from exc
    except ValidationError as exc:
        raise McpToolError(
            "invalid_argument", f"策略参数校验失败: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# 只读工具
# --------------------------------------------------------------------------- #
async def strategy_registry(app: McpAppContext) -> ToolEnvelope:
    """返回前端/agent 可安全生成控件的注册表(不返回 Python 类或模块路径)。"""

    async def _do() -> dict[str, Any]:
        from finboard_backtest.strategy_spec import (
            FEATURE_SOURCE_CATALOG,
            LIFECYCLE_STAGES,
            FeatureOperator,
            list_strategy_capabilities,
        )

        sources = [
            {
                "name": item.name,
                "kind": item.kind.value,
                "required_datasets": list(item.required_datasets),
                "description": item.description,
            }
            for item in sorted(
                FEATURE_SOURCE_CATALOG.values(), key=lambda value: value.name
            )
        ]
        operators = [
            {"name": operator.value, "executable_expression": False}
            for operator in FeatureOperator
        ]
        return {
            "strategies": [
                cast(dict[str, Any], to_jsonable(item.as_dict()))
                for item in list_strategy_capabilities()
            ],
            "feature_sources": sources,
            "operators": operators,
            "lifecycle_stages": list(LIFECYCLE_STAGES),
            "publication_starts_run": False,
            "accepts_python": False,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.registry",
        arguments={},
        handler=_do,
    )


async def strategy_template(
    app: McpAppContext,
    *,
    kind: str,
    strategy_id: str,
    dataset_release_ids: list[str],
) -> ToolEnvelope:
    """生成指定 kind 的策略规格模板(无代码,白名单组件组合)。"""

    async def _do() -> dict[str, Any]:
        from finboard_backtest.strategy_spec import (
            StrategySpecError,
            build_strategy_template,
        )

        try:
            spec = build_strategy_template(
                kind,
                strategy_id=strategy_id,
                dataset_release_ids=tuple(dataset_release_ids),
            )
        except (StrategySpecError, ValueError) as exc:
            raise McpToolError(
                "invalid_argument", f"无法生成策略模板: {exc}"
            ) from exc
        except ValidationError as exc:
            raise McpToolError(
                "invalid_argument", f"策略模板校验失败: {exc}"
            ) from exc
        return cast(dict[str, Any], to_jsonable(spec.canonical_payload()))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.template",
        arguments={
            "kind": kind,
            "strategy_id": strategy_id,
            "dataset_release_ids": dataset_release_ids,
        },
        handler=_do,
    )


async def strategy_list(
    app: McpAppContext, *, limit: int = 100
) -> ToolEnvelope:
    """列出策略规格(每个 strategy_id 的最新版本)。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import ResearchStrategySpecRepository

        async with app.session_maker() as session:
            rows = await ResearchStrategySpecRepository(session).list_latest(
                limit=limit
            )
            return [_version_to_dict(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.list",
        arguments={"limit": limit},
        handler=_do,
    )


async def strategy_history(
    app: McpAppContext, *, strategy_id: str
) -> ToolEnvelope:
    """查询指定策略的全部版本历史。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import ResearchStrategySpecRepository

        async with app.session_maker() as session:
            rows = await ResearchStrategySpecRepository(session).list_history(
                strategy_id
            )
        if not rows:
            raise McpToolError("not_found", f"策略规格不存在: {strategy_id}")
        return [_version_to_dict(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.history",
        arguments={"strategy_id": strategy_id},
        handler=_do,
    )


async def strategy_version_get(
    app: McpAppContext, *, strategy_id: str, version: int
) -> ToolEnvelope:
    """查询指定策略的单个版本详情。"""

    async def _do() -> dict[str, Any]:
        from finboard_persistence import ResearchStrategySpecRepository

        async with app.session_maker() as session:
            row = await ResearchStrategySpecRepository(session).get_version(
                strategy_id, version
            )
        if row is None:
            raise McpToolError(
                "not_found",
                f"策略版本不存在: {strategy_id} v{version}",
            )
        return _version_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.version_get",
        arguments={"strategy_id": strategy_id, "version": version},
        handler=_do,
    )


async def strategy_diff(
    app: McpAppContext,
    *,
    strategy_id: str,
    from_version: int,
    to_version: int,
) -> ToolEnvelope:
    """计算两个版本间的结构化 diff。"""

    async def _do() -> dict[str, Any]:
        from finboard_backtest.strategy_spec import structured_diff
        from finboard_persistence import ResearchStrategySpecRepository

        async with app.session_maker() as session:
            repo = ResearchStrategySpecRepository(session)
            before = await repo.get_version(strategy_id, from_version)
            after = await repo.get_version(strategy_id, to_version)
        if before is None or after is None:
            raise McpToolError("not_found", "对比版本不存在")
        changes = structured_diff(before.payload, after.payload)
        return {
            "strategy_id": strategy_id,
            "from_version": from_version,
            "to_version": to_version,
            "changes": [
                cast(dict[str, Any], to_jsonable(change.as_dict()))
                for change in changes
            ],
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.diff",
        arguments={
            "strategy_id": strategy_id,
            "from_version": from_version,
            "to_version": to_version,
        },
        handler=_do,
    )


async def preset_list(app: McpAppContext) -> ToolEnvelope:
    """列出全部策略参数预设。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import StrategyPresetRepository

        async with app.session_maker() as session:
            rows = await StrategyPresetRepository(session).list_all()
            return [_preset_to_dict(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.preset.list",
        arguments={},
        handler=_do,
    )


async def preset_get(app: McpAppContext, *, preset_id: int) -> ToolEnvelope:
    """查询单个策略参数预设。"""

    async def _do() -> dict[str, Any]:
        from finboard_persistence import StrategyPresetRepository

        async with app.session_maker() as session:
            row = await StrategyPresetRepository(session).get(preset_id)
        if row is None:
            raise McpToolError("not_found", f"策略预设不存在: {preset_id}")
        return _preset_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.preset.get",
        arguments={"preset_id": preset_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 写工具
# --------------------------------------------------------------------------- #
async def strategy_validate(
    app: McpAppContext,
    *,
    spec: dict[str, Any],
    disabled_factors: list[str] | None = None,
) -> ToolEnvelope:
    """纯计算:编译/校验策略规格,返回 checksum/feature_order/can_execute(不持久化)。

    agent 可反复修改规格 → validate 预览 → 满意后 ``strategy_draft_create``。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.strategy_spec import ResearchStrategySpec

        try:
            parsed = ResearchStrategySpec.model_validate(spec)
        except ValidationError as exc:
            raise McpToolError(
                "invalid_argument", f"策略规格校验失败: {exc}"
            ) from exc
        async with app.session_maker() as session:
            plan = await _compile_with_releases(
                parsed,
                session,
                disabled_factors=frozenset(disabled_factors or []),
            )
        return _validation_to_dict(plan)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.validate",
        arguments={"spec": spec, "disabled_factors": disabled_factors},
        handler=_do,
    )


async def strategy_draft_create(
    app: McpAppContext,
    *,
    spec: dict[str, Any],
    expected_version: int | None = None,
) -> ToolEnvelope:
    """保存策略规格草稿版本(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.strategy_spec import ResearchStrategySpec
        from finboard_persistence import (
            ResearchStrategySpecRepository,
            StrategySpecVersionConflictError,
        )

        try:
            parsed = ResearchStrategySpec.model_validate(spec)
        except ValidationError as exc:
            raise McpToolError(
                "invalid_argument", f"策略规格校验失败: {exc}"
            ) from exc
        async with app.session_maker() as session:
            plan = await _compile_with_releases(parsed, session)
            repo = ResearchStrategySpecRepository(session)
            try:
                row = await repo.create_draft(
                    strategy_id=parsed.strategy_id,
                    schema_version=parsed.schema_version,
                    name=parsed.name,
                    strategy_kind=parsed.strategy_kind,
                    checksum=plan.checksum,
                    payload=parsed.canonical_payload(),
                    expected_version=expected_version,
                )
                await session.commit()
            except StrategySpecVersionConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except Exception:
                await session.rollback()
                raise
            # issue #206 P1:写操作返回精简回执,全文走 version_get。
            return _version_ack(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.draft_create",
        arguments={"spec": spec, "expected_version": expected_version},
        handler=_do,
    )


async def strategy_supersede(
    app: McpAppContext,
    *,
    strategy_id: str,
    spec: dict[str, Any],
    expected_version: int,
) -> ToolEnvelope:
    """为已存在的策略创建后继草稿版本(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.strategy_spec import ResearchStrategySpec
        from finboard_persistence import (
            ResearchStrategySpecRepository,
            StrategySpecChangeType,
            StrategySpecVersionConflictError,
        )

        try:
            parsed = ResearchStrategySpec.model_validate(spec)
        except ValidationError as exc:
            raise McpToolError(
                "invalid_argument", f"策略规格校验失败: {exc}"
            ) from exc
        if parsed.strategy_id != strategy_id:
            raise McpToolError(
                "invalid_argument", "路径 strategy_id 与规格不一致"
            )
        async with app.session_maker() as session:
            plan = await _compile_with_releases(parsed, session)
            repo = ResearchStrategySpecRepository(session)
            try:
                row = await repo.create_draft(
                    strategy_id=strategy_id,
                    schema_version=parsed.schema_version,
                    name=parsed.name,
                    strategy_kind=parsed.strategy_kind,
                    checksum=plan.checksum,
                    payload=parsed.canonical_payload(),
                    expected_version=expected_version,
                    change_type=StrategySpecChangeType.SUPERSEDE,
                )
                await session.commit()
            except StrategySpecVersionConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except Exception:
                await session.rollback()
                raise
            return _version_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.supersede",
        arguments={
            "strategy_id": strategy_id,
            "spec": spec,
            "expected_version": expected_version,
        },
        handler=_do,
    )


async def strategy_publish(
    app: McpAppContext,
    *,
    strategy_id: str,
    version: int,
    expected_version: int,
) -> ToolEnvelope:
    """发布策略规格的指定版本(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.strategy_spec import ResearchStrategySpec
        from finboard_persistence import (
            ResearchStrategySpecRepository,
            StrategySpecTransitionError,
            StrategySpecVersionConflictError,
        )

        async with app.session_maker() as session:
            repo = ResearchStrategySpecRepository(session)
            target = await repo.get_version(strategy_id, version)
            if target is None:
                raise McpToolError("not_found", "策略版本不存在")
            try:
                spec = ResearchStrategySpec.model_validate(target.payload)
                await _compile_with_releases(spec, session)
                row = await repo.publish(
                    strategy_id,
                    version,
                    expected_version=expected_version,
                )
                await session.commit()
            except StrategySpecVersionConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except StrategySpecTransitionError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except Exception:
                await session.rollback()
                raise
            # issue #206 P1:写操作返回精简回执,全文走 version_get。
            return _version_ack(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.publish",
        arguments={
            "strategy_id": strategy_id,
            "version": version,
            "expected_version": expected_version,
        },
        handler=_do,
    )


async def strategy_rollback(
    app: McpAppContext,
    *,
    strategy_id: str,
    target_version: int,
    expected_version: int,
) -> ToolEnvelope:
    """回滚策略规格到指定版本(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.strategy_spec import ResearchStrategySpec
        from finboard_persistence import (
            ResearchStrategySpecRepository,
            StrategySpecTransitionError,
            StrategySpecVersionConflictError,
        )

        async with app.session_maker() as session:
            repo = ResearchStrategySpecRepository(session)
            target = await repo.get_version(strategy_id, target_version)
            if target is None:
                raise McpToolError("not_found", "回滚目标版本不存在")
            try:
                spec = ResearchStrategySpec.model_validate(target.payload)
                await _compile_with_releases(spec, session)
                row = await repo.rollback(
                    strategy_id,
                    target_version,
                    expected_version=expected_version,
                )
                await session.commit()
            except StrategySpecVersionConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except StrategySpecTransitionError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except Exception:
                await session.rollback()
                raise
            return _version_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.strategy.rollback",
        arguments={
            "strategy_id": strategy_id,
            "target_version": target_version,
            "expected_version": expected_version,
        },
        handler=_do,
    )


async def preset_create(
    app: McpAppContext,
    *,
    name: str,
    strategy: str,
    params: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> ToolEnvelope:
    """创建策略参数预设(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from sqlalchemy.exc import IntegrityError

        from finboard_persistence import StrategyPresetRepository

        cleaned_name = name.strip()
        if not cleaned_name:
            raise McpToolError("invalid_argument", "预设名称不能为空")
        normalized_params = _validate_preset_params(
            strategy, params or {}
        )
        async with app.session_maker() as session:
            repo = StrategyPresetRepository(session)
            existing = await repo.get_by_name(cleaned_name)
            if existing is not None:
                raise McpToolError("conflict", "预设名称已存在")
            try:
                row = await repo.create(
                    name=cleaned_name,
                    strategy=strategy,
                    params=normalized_params,
                    selection=selection or {},
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "预设名称已存在") from exc
            except Exception:
                await session.rollback()
                raise
            return _preset_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.preset.create",
        arguments={
            "name": name,
            "strategy": strategy,
            "params": params,
            "selection": selection,
        },
        handler=_do,
    )


async def preset_update(
    app: McpAppContext,
    *,
    preset_id: int,
    name: str | None = None,
    strategy: str | None = None,
    params: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> ToolEnvelope:
    """更新策略参数预设(写操作);仅传入的字段会被更新。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from sqlalchemy.exc import IntegrityError

        from finboard_persistence import StrategyPresetRepository

        async with app.session_maker() as session:
            repo = StrategyPresetRepository(session)
            current = await repo.get(preset_id)
            if current is None:
                raise McpToolError("not_found", f"策略预设不存在: {preset_id}")

            resolved_name = (
                name.strip() if name is not None else current.name
            )
            if not resolved_name:
                raise McpToolError("invalid_argument", "预设名称不能为空")
            resolved_strategy = (
                strategy if strategy is not None else current.strategy
            )
            raw_params = params if params is not None else current.params
            resolved_selection = (
                selection if selection is not None else current.selection
            )
            normalized_params = _validate_preset_params(
                resolved_strategy, raw_params or {}
            )

            existing = await repo.get_by_name(resolved_name)
            if existing is not None and existing.id != preset_id:
                raise McpToolError("conflict", "预设名称已存在")
            try:
                row = await repo.update(
                    preset_id,
                    name=resolved_name,
                    strategy=resolved_strategy,
                    params=normalized_params,
                    selection=resolved_selection,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "预设名称已存在") from exc
            except Exception:
                await session.rollback()
                raise
            assert row is not None
            return _preset_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.preset.update",
        arguments={
            "preset_id": preset_id,
            "name": name,
            "strategy": strategy,
            "params": params,
            "selection": selection,
        },
        handler=_do,
    )


async def preset_delete(
    app: McpAppContext, *, preset_id: int
) -> ToolEnvelope:
    """删除策略参数预设(写操作)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_persistence import StrategyPresetRepository

        async with app.session_maker() as session:
            repo = StrategyPresetRepository(session)
            ok = await repo.delete(preset_id)
            if not ok:
                raise McpToolError("not_found", f"策略预设不存在: {preset_id}")
            await session.commit()
        return {"deleted": True, "preset_id": preset_id}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.preset.delete",
        arguments={"preset_id": preset_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """注册策略规格工具(8 只读 + 8 写)。"""

    # --- 只读 ---

    @mcp.tool(
        name="finboard_strategy_registry",
        description=(
            "查询策略规格注册表:可用的策略 kind(含资产类别/做空/模板支持)、"
            "特征源(feature sources)、算子(operators)、生命周期阶段。"
            "用于了解 agent 能组合哪些白名单组件构建无代码策略规格。"
            "只读,直接调用。对应 GET /api/research/strategy-specs/registry。"
        ),
    )
    async def _strategy_registry(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_registry(app_context(ctx))

    @mcp.tool(
        name="finboard_strategy_template",
        description=(
            "生成指定 kind 的策略规格模板(无代码,白名单组件组合)。"
            "参数:kind(策略类型,如 multi_factor)、strategy_id(1-64 字符)、"
            "dataset_release_ids(至少 1 个数据集发布 ID)。"
            "用于作为草稿起点,agent 填充/修改后用 strategy_validate 预览。"
            "只读,直接调用。对应 GET /api/research/strategy-specs/templates/{kind}。"
        ),
    )
    async def _strategy_template(
        kind: str,
        strategy_id: str,
        dataset_release_ids: list[str],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_template(
            app_context(ctx),
            kind=kind,
            strategy_id=strategy_id,
            dataset_release_ids=dataset_release_ids,
        )

    @mcp.tool(
        name="finboard_strategy_list",
        description=(
            "列出策略规格(每个 strategy_id 的最新版本,默认上限 100)。"
            "只读,直接调用。对应 GET /api/research/strategy-specs。"
        ),
    )
    async def _strategy_list(
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_list(app_context(ctx), limit=limit)

    @mcp.tool(
        name="finboard_strategy_history",
        description=(
            "查询指定策略的全部版本历史(draft/published/superseded/rollback)。"
            "只读,直接调用。对应 GET /api/research/strategy-specs/{id}/history。"
        ),
    )
    async def _strategy_history(
        strategy_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_history(
            app_context(ctx), strategy_id=strategy_id
        )

    @mcp.tool(
        name="finboard_strategy_version_get",
        description=(
            "查询指定策略的单个版本详情(含完整 spec 与 checksum)。"
            "只读,直接调用。"
            "对应 GET /api/research/strategy-specs/{id}/versions/{version}。"
        ),
    )
    async def _strategy_version_get(
        strategy_id: str,
        version: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_version_get(
            app_context(ctx), strategy_id=strategy_id, version=version
        )

    @mcp.tool(
        name="finboard_strategy_diff",
        description=(
            "计算指定策略两个版本间的结构化 diff(path/before/after 列表)。"
            "只读,直接调用。对应 GET /api/research/strategy-specs/{id}/diff。"
        ),
    )
    async def _strategy_diff(
        strategy_id: str,
        from_version: int,
        to_version: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_diff(
            app_context(ctx),
            strategy_id=strategy_id,
            from_version=from_version,
            to_version=to_version,
        )

    @mcp.tool(
        name="finboard_preset_list",
        description=(
            "列出全部策略参数预设(内置策略 kind 的已保存参数组合)。"
            "只读,直接调用。对应 GET /api/strategy-presets。"
        ),
    )
    async def _preset_list(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await preset_list(app_context(ctx))

    @mcp.tool(
        name="finboard_preset_get",
        description=(
            "查询单个策略参数预设(按 ID)。"
            "只读,直接调用。对应 GET /api/strategy-presets/{id}。"
        ),
    )
    async def _preset_get(
        preset_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await preset_get(app_context(ctx), preset_id=preset_id)

    # --- 写 ---

    @mcp.tool(
        name="finboard_strategy_validate",
        description=(
            "纯计算:编译/校验策略规格,返回 checksum/feature_order/"
            "required_factor_sources/required_datasets/dataset_release_ids/"
            "lifecycle_stages/can_execute。**不持久化**,agent 可反复修改规格 → "
            "validate 预览 → 满意后 strategy_draft_create。"
            "研究写操作(#122,自主执行)。对应 POST /api/research/strategy-specs/validate。"
        ),
    )
    async def _strategy_validate(
        spec: dict[str, Any],
        disabled_factors: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_validate(
            app_context(ctx),
            spec=spec,
            disabled_factors=disabled_factors,
        )

    @mcp.tool(
        name="finboard_strategy_draft_create",
        description=(
            "保存策略规格草稿版本(change_type=create,首版本)。"
            "先编译校验(validate)再持久化;冲突(版本号)返回 conflict。"
            "返回精简回执(strategy_id/version/status/checksum/created_at,"
            "issue #206),完整 spec 走 finboard_strategy_version_get。"
            "研究写操作(#122,自主执行)。对应 POST /api/research/strategy-specs/drafts。"
        ),
    )
    async def _strategy_draft_create(
        spec: dict[str, Any],
        expected_version: int | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_draft_create(
            app_context(ctx),
            spec=spec,
            expected_version=expected_version,
        )

    @mcp.tool(
        name="finboard_strategy_supersede",
        description=(
            "为已存在的策略创建后继草稿版本(change_type=supersede)。"
            "路径 strategy_id 必须与 spec.strategy_id 一致。"
            "研究写操作(#122,自主执行)。"
            "对应 POST /api/research/strategy-specs/{id}/supersede。"
        ),
    )
    async def _strategy_supersede(
        strategy_id: str,
        spec: dict[str, Any],
        expected_version: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_supersede(
            app_context(ctx),
            strategy_id=strategy_id,
            spec=spec,
            expected_version=expected_version,
        )

    @mcp.tool(
        name="finboard_strategy_publish",
        description=(
            "发布策略规格的指定版本(draft→published)。"
            "发布前会重新编译校验;版本不存在返回 not_found,状态转换非法返回 conflict。"
            "返回精简回执(strategy_id/version/status/checksum/created_at,"
            "issue #206),完整 spec 走 finboard_strategy_version_get。"
            "研究写操作(#122,自主执行)。"
            "对应 POST /api/research/strategy-specs/{id}/publish。"
        ),
    )
    async def _strategy_publish(
        strategy_id: str,
        version: int,
        expected_version: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_publish(
            app_context(ctx),
            strategy_id=strategy_id,
            version=version,
            expected_version=expected_version,
        )

    @mcp.tool(
        name="finboard_strategy_rollback",
        description=(
            "回滚策略规格到指定目标版本(change_type=rollback)。"
            "回滚前会重新编译校验目标版本;目标不存在返回 not_found。"
            "研究写操作(#122,自主执行)。"
            "对应 POST /api/research/strategy-specs/{id}/rollback。"
        ),
    )
    async def _strategy_rollback(
        strategy_id: str,
        target_version: int,
        expected_version: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await strategy_rollback(
            app_context(ctx),
            strategy_id=strategy_id,
            target_version=target_version,
            expected_version=expected_version,
        )

    @mcp.tool(
        name="finboard_preset_create",
        description=(
            "创建策略参数预设(内置策略 kind 的参数组合)。"
            "参数经对应策略的 params_model 校验;名称唯一,冲突返回 conflict。"
            "研究写操作(#122,自主执行)。对应 POST /api/strategy-presets。"
        ),
    )
    async def _preset_create(
        name: str,
        strategy: str,
        params: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await preset_create(
            app_context(ctx),
            name=name,
            strategy=strategy,
            params=params,
            selection=selection,
        )

    @mcp.tool(
        name="finboard_preset_update",
        description=(
            "更新策略参数预设(仅传入字段会被更新,未传字段保留原值)。"
            "预设不存在返回 not_found;名称冲突返回 conflict。"
            "研究写操作(#122,自主执行)。对应 PUT /api/strategy-presets/{id}。"
        ),
    )
    async def _preset_update(
        preset_id: int,
        name: str | None = None,
        strategy: str | None = None,
        params: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await preset_update(
            app_context(ctx),
            preset_id=preset_id,
            name=name,
            strategy=strategy,
            params=params,
            selection=selection,
        )

    @mcp.tool(
        name="finboard_preset_delete",
        description=(
            "删除策略参数预设(按 ID)。"
            "预设不存在返回 not_found。"
            "研究写操作(#122,自主执行)。对应 DELETE /api/strategy-presets/{id}。"
        ),
    )
    async def _preset_delete(
        preset_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await preset_delete(app_context(ctx), preset_id=preset_id)
