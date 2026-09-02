"""``finboard.backtest.*`` 工具 —— 回测引擎查询与运行(issue #127 / #174)。

- 只读(3):``backtest_strategy_list``(内置策略 + 已发布规格,含执行入口提示)/
  ``backtest_history_list``(历史回测列表)/ ``backtest_history_get``(历史详情)
- 写(2):``backtest_run`` 双形态——事件驱动回测(同步返回 metrics/equity/fills,
  或 ``run_async=true`` 入队 kind=backtest_run 后台任务返回 job_id,issue #189;
  对应 ``POST /api/backtest/run``)与 strategy_spec 路由入队 research_run
  (返回 run/job 指针,复用 ``finboard.run.queue`` 冻结校验)/ ``backtest_history_delete``

复用现有 ``BacktestEngine`` / ``BacktestRunRepository`` /
``list_strategy_definitions`` / ``get_strategy_definition``,不重复实现。
不触及交易安全红线(回测是纸面撮合,不发真实订单)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from finboard_persistence.models import BacktestRunModel

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


def _validate_backtest_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """校验回测策略参数,要求 ``supports_backtest=True``。

    复用 ``get_strategy_definition`` / ``params_model``,绕过 FastPI 耦合的
    ``validate_strategy_params_for_api``(后者抛 ``HTTPException``)。
    """
    from finboard_app.strategies import get_strategy_definition

    try:
        definition = get_strategy_definition(kind)
    except ValueError as exc:
        raise McpToolError("invalid_argument", f"未知策略: {kind}") from exc
    if not definition.supports_backtest:
        raise McpToolError(
            "invalid_argument",
            f"策略 {kind} 不支持回测(supports_backtest=False)",
        )
    try:
        validated = definition.params_model.model_validate(params or {})
    except ValidationError as exc:
        raise McpToolError("invalid_argument", f"策略参数校验失败: {exc}") from exc
    return validated.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _AsyncDecision:
    """strategy 形态是否走异步的判定结果(issue #189)。"""

    use_async: bool
    #: 判定依据:explicit(显式 run_async=true)/ auto_threshold(自动切换)/
    #: sync_explicit(显式 run_async=false)/ sync_below_threshold(未达阈值)。
    reason: str
    symbol_days_estimate: int = 0


def _estimate_symbol_days(symbols: list[str], start: str, end: str) -> int:
    """估算回测工作量:标的不数 x 近似交易日(start~end,周末 5/7 折算)。

    价格因子的数据拉取 / 撮合成本近似正比于该值,用于自动异步阈值比较。
    """
    try:
        start_date = date_type.fromisoformat(start)
        end_date = date_type.fromisoformat(end)
    except ValueError:
        return 0
    if end_date < start_date:
        return 0
    trading_days = max(1, round((end_date - start_date).days * 5 / 7))
    return max(1, len(symbols or [])) * trading_days


def _resolve_async_mode(
    run_async: bool | None,
    threshold: int,
    symbols: list[str],
    start: str,
    end: str,
) -> _AsyncDecision:
    """决定 strategy 形态是否走异步后台任务(issue #189)。

    * ``run_async=True`` → 显式异步;``run_async=False`` → 显式同步;
    * 省略(None)-> ``threshold`` > 0 时按 标的不数 x 交易日 估算自动切换,
      达到阈值返回异步(避免 MCP 客户端超时后响应丢失)。
    """
    if run_async is True:
        return _AsyncDecision(True, "explicit")
    if run_async is False:
        return _AsyncDecision(False, "sync_explicit")
    estimate = _estimate_symbol_days(symbols, start, end)
    if threshold > 0 and estimate >= threshold:
        return _AsyncDecision(True, "auto_threshold", symbol_days_estimate=estimate)
    return _AsyncDecision(False, "sync_below_threshold", symbol_days_estimate=estimate)


def _strategy_info(definition: Any) -> dict[str, Any]:
    """把 ``StrategyDefinition`` 序列化为参数 schema(复用 API 逻辑)。"""
    from finboard_api.strategy_validation import strategy_info

    return cast(dict[str, Any], to_jsonable(strategy_info(definition).model_dump()))


#: 列表项 symbols 预览长度(issue #206 P2:72 标的级列表只回前 N 只 + 计数)。
_SYMBOLS_PREVIEW_COUNT = 10


def _history_item(
    row: BacktestRunModel, *, symbols_preview: bool = False
) -> dict[str, Any]:
    """历史列表项(摘要,不含完整 equity/fills)。

    ``symbols_preview=True`` 时 symbols 截断为前 10 只并附 symbol_count
    (issue #206 P2);详情视图始终用全量 symbols。
    """
    symbols = list(row.symbols) if row.symbols else []
    item: dict[str, Any] = {
        "id": row.id,
        "strategy": row.strategy,
        "symbols": (
            symbols[:_SYMBOLS_PREVIEW_COUNT] if symbols_preview else symbols
        ),
        "start": row.start,
        "end": row.end,
        "capital": to_jsonable(row.capital),
        "adjust": row.adjust,
        "metrics": dict(row.metrics) if row.metrics else {},
        "factor_version": row.factor_version,
        "created_at": to_jsonable(row.created_at),
    }
    if symbols_preview:
        item["symbol_count"] = len(symbols)
    return item


def _history_detail(
    row: BacktestRunModel,
    *,
    equity_mode: str = "summary",
    max_points: int = 200,
    fills_limit: int | None = None,
    fills_offset: int = 0,
) -> dict[str, Any]:
    """历史详情(equity 按 mode 降采样;fills 按 limit/offset 分页)。"""
    from finboard_mcp.downsample import (
        apply_equity_mode,
        clamp_max_points,
        resolve_equity_mode,
    )

    mode = resolve_equity_mode(equity_mode)
    max_equity_points = clamp_max_points(max_points)
    all_equity = list(row.equity_curve) if row.equity_curve else []
    all_fills = list(row.fills) if row.fills else []
    safe_offset = max(0, fills_offset)
    safe_limit = len(all_fills) if fills_limit is None else max(0, min(len(all_fills), fills_limit))
    page_fills = all_fills[safe_offset : safe_offset + safe_limit]
    detail = _history_item(row)
    detail.update(
        {
            "params": dict(row.params) if row.params else {},
            "selection": dict(row.selection) if row.selection else {},
            "equity_curve": apply_equity_mode(
                all_equity, equity_mode=mode, max_points=max_equity_points
            ),
            "equity_point_count": len(all_equity),
            "fills": page_fills,
            "fills_total": len(all_fills),
            "fills_offset": safe_offset,
            "summary": row.summary,
            "selection_snapshots": (
                list(row.selection_snapshots) if row.selection_snapshots else []
            ),
            "dataset_versions": (dict(row.dataset_versions) if row.dataset_versions else {}),
            "matching_model": (dict(row.matching_model) if row.matching_model else {}),
            "asset_rules": dict(row.asset_rules) if row.asset_rules else None,
            "fee_assumptions": (dict(row.fee_assumptions) if row.fee_assumptions else {}),
            "benchmark_config": (dict(row.benchmark_config) if row.benchmark_config else {}),
        }
    )
    return cast(dict[str, Any], to_jsonable(detail))


# --------------------------------------------------------------------------- #
# finboard.backtest.strategy_list(只读)
# --------------------------------------------------------------------------- #
async def backtest_strategy_list(app: McpAppContext) -> ToolEnvelope:
    """列出支持回测的内置策略与已发布策略规格。

    返回 ``builtin_strategies``(事件驱动引擎,``supports_backtest=true``)、
    ``published_specs``(已发布研究策略规格,走 research_run 管线)与
    ``execution_note`` 执行入口提示(issue #174)。
    """

    async def _do() -> dict[str, Any]:
        from finboard_app.strategies import list_strategy_definitions
        from finboard_persistence import ResearchStrategySpecRepository

        builtin = [_strategy_info(d) for d in list_strategy_definitions() if d.supports_backtest]
        async with app.session_maker() as session:
            repo = ResearchStrategySpecRepository(session)
            published_rows = await repo.list_published()
            specs = []
            for row in published_rows:
                history = await repo.list_history(row.strategy_id)
                specs.append(
                    {
                        "strategy_id": row.strategy_id,
                        "strategy_kind": row.strategy_kind,
                        "name": row.name,
                        "status": row.status,
                        "version": row.version,
                        "version_count": len(history),
                        "execution_hint": (
                            f'backtest_run(strategy_spec={{"strategy_id": '
                            f'"{row.strategy_id}", "version": {row.version}}})'
                        ),
                    }
                )
        return {
            "builtin_strategies": builtin,
            "published_specs": specs,
            "execution_note": (
                "已发布策略规格的正确回测入口是 research_run 管线:用 "
                "backtest_run(strategy_spec=...) 路由入队,或直接 finboard_run_queue "
                "(冻结 dataset_release_ids/factor_snapshot_ids 后异步执行);"
                "builtin_strategies 才是事件驱动 BacktestEngine 的同步回测。"
            ),
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.strategy_list",
        arguments={},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.run(写,双形态:事件驱动同步回测 / 已发布规格路由)
# --------------------------------------------------------------------------- #
async def _run_via_strategy_spec(
    app: McpAppContext,
    strategy_spec: dict[str, Any],
    queue_payload: dict[str, Any],
) -> dict[str, Any]:
    """已发布规格路由:校验 published → 复用 run_queue 冻结/入队 → 返回指针。

    不把 no-code 规格编译进事件驱动 BacktestEngine(与 research_run 组合流水线
    重复建设且绕开 #91 冻结/校验语义);只做轻路由(issue #174)。
    """
    from finboard_mcp.tools.runs import enqueue_research_run, parse_queue_payload
    from finboard_persistence import ResearchStrategySpecRepository

    spec_id = strategy_spec.get("strategy_id")
    version = strategy_spec.get("version")
    if not isinstance(spec_id, str) or not spec_id:
        raise McpToolError("invalid_argument", "strategy_spec 需要 strategy_id(已发布规格 ID)")
    if not isinstance(version, int) or version < 1:
        raise McpToolError("invalid_argument", "strategy_spec.version 必须是正整数")

    async with app.session_maker() as session:
        row = await ResearchStrategySpecRepository(session).get_version(spec_id, version)
    if row is None:
        raise McpToolError("invalid_argument", f"策略规格版本不存在: {spec_id} v{version}")
    if row.status != "published":
        raise McpToolError(
            "invalid_argument",
            f"仅已发布策略规格可以进入研究运行: {spec_id} v{version}(当前状态 {row.status})",
        )

    # strategy_id/version 以 strategy_spec 为准,其余字段复用 run_queue payload
    payload = dict(queue_payload)
    payload["strategy_id"] = spec_id
    payload["strategy_version"] = version
    body = parse_queue_payload(payload)
    ack = await enqueue_research_run(app, body)
    # 返回可跟踪指针(run_id + job_id),不阻塞等待完成
    from finboard_backtest.research_run.contracts import execution_mode_for

    return {
        "run_id": ack["run_id"],
        "job_id": ack.get("job_id"),
        "status": ack["status"],
        "strategy_id": ack["strategy_id"],
        "strategy_kind": ack["strategy_kind"],
        "manifest_checksum": ack["checksum"],
        # issue #183:agent 据此区分单时点决策(single_shot)与全区间回放
        # (multi_period,由 queue_payload.parameters.rebalance_frequency 决定)。
        "execution_mode": execution_mode_for(dict(body.parameters)).value,
        "execution_path": (
            "research_run 管线(已入队,异步执行;用 finboard_run_get 或 finboard_job_get 轮询进度)"
        ),
    }


async def _enqueue_backtest_job(
    app: McpAppContext,
    *,
    request: dict[str, Any],
    idempotency_key: str,
    requested_by: str,
) -> tuple[Any, bool]:
    """登记一个 ``kind=backtest_run`` 后台任务,返回 ``(row, created)``。

    与 REST ``POST /api/backtest/run`` 同队列(``data``)同 payload 形态
    (``{request, provider_name}``),幂等键口径一致;conflict 转
    :class:`McpToolError` conflict。
    """
    from sqlalchemy.exc import IntegrityError

    from finboard_mcp.tools.jobs import _payload_checksum
    from finboard_persistence import BackgroundJobRepository
    from finboard_shared.background_jobs import (
        BackgroundJobStatus,
        generate_background_job_id,
    )

    payload: dict[str, Any] = {
        "request": request,
        "provider_name": getattr(app.settings, "data_provider", "akshare"),
    }
    checksum = _payload_checksum(payload)
    async with app.session_maker() as session:
        # issue #255:research_db 选股必需数据集批次未发布 → 入队秒级拒绝,
        # 不再等 worker 开跑后才以「0 交易成功」收场。
        raw_selection = request.get("selection") or {}
        if raw_selection:
            from finboard_app.selection_schema import FactorSelectionParams
            from finboard_persistence import ResearchDatasetRepository

            try:
                gate_selection = FactorSelectionParams.model_validate(raw_selection)
            except Exception as exc:
                raise McpToolError(
                    "invalid_argument", f"selection 参数校验失败: {exc}"
                ) from exc
            unpublished = await ResearchDatasetRepository(
                session
            ).selection_inputs_gate(gate_selection.to_domain())
            if unpublished:
                raise McpToolError(
                    "invalid_argument",
                    "research_db 选股必需数据集批次未发布,拒绝入队(issue #255): "
                    f"{'、'.join(unpublished)}。请先执行 data_sync 摄取并完成"
                    "批次发布,或改用 inputs_mode=bars/snapshot。",
                )
        try:
            row, created = await BackgroundJobRepository(session).create_or_get(
                job_id=generate_background_job_id(),
                idempotency_key=idempotency_key,
                kind="backtest_run",
                queue="data",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=checksum,
                max_attempts=3,
                requested_by=requested_by,
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise McpToolError("conflict", "重复 idempotency_key") from exc
    return row, created


async def _run_strategy_async(
    app: McpAppContext,
    *,
    strategy: str,
    symbols: list[str],
    start: str,
    end: str,
    capital: Decimal,
    adjust: str,
    validated_params: dict[str, Any],
    selection_dump: dict[str, Any],
    commission_rate: Decimal,
    commission_min: Decimal,
    stamp_tax_rate: Decimal,
    slippage_bps: Decimal,
    benchmark_symbol: str | None,
    requested_by: str,
    reason: str,
    symbol_days_estimate: int,
) -> dict[str, Any]:
    """strategy 形态异步化(issue #189):入队后返回 job 指针,不阻塞等待完成。

    复用 ``BacktestRunExecutor``(worker 消费)执行,与同步形态同一引擎与落库
    语义;参数校验仍在提交前同步完成,非法参数秒级 ``invalid_argument``。
    """
    request: dict[str, Any] = {
        "strategy": strategy,
        "symbols": list(symbols),
        "start": start,
        "end": end,
        "capital": str(capital),
        "adjust": adjust,
        "params": validated_params,
        "selection": selection_dump,
        "commission_rate": str(commission_rate),
        "commission_min": str(commission_min),
        "stamp_tax_rate": str(stamp_tax_rate),
        "slippage_bps": str(slippage_bps),
        "benchmark": (
            {"symbol": benchmark_symbol, "equal_weight_universe": True}
            if benchmark_symbol is not None
            else None
        ),
    }
    params_digest = hashlib.sha256(
        json.dumps(request, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    idempotency_key = f"backtest:{strategy}:{params_digest}"
    row, created = await _enqueue_backtest_job(
        app,
        request=request,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
    )
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "job_id": row.job_id,
                "status": row.status,
                "created": created,
                "idempotency_key": idempotency_key,
                "async_mode": reason,
                "symbol_days_estimate": symbol_days_estimate,
                "auto_async_threshold": int(
                    getattr(app.settings, "backtest_auto_async_symbol_days", 0) or 0
                ),
                "execution_path": (
                    "kind=backtest_run 后台任务(已入队,异步执行;用 finboard_job_get"
                    "(job_id) 轮询:成功后 result_ref=str(run_id),再用 "
                    "finboard_backtest_history_get(run_id=...) 查询完整结果)"
                ),
            }
        ),
    )


async def backtest_run(
    app: McpAppContext,
    *,
    strategy: str | None = None,
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    capital: Decimal = Decimal("100000"),
    adjust: str = "qfq",
    params: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
    commission_rate: Decimal = Decimal("0.0003"),
    commission_min: Decimal = Decimal("1"),
    stamp_tax_rate: Decimal = Decimal("0.0005"),
    slippage_bps: Decimal = Decimal("0"),
    equity_mode: str = "summary",
    max_points: int = 200,
    benchmark_symbol: str | None = None,
    strategy_spec: dict[str, Any] | None = None,
    queue_payload: dict[str, Any] | None = None,
    run_async: bool | None = None,
    requested_by: str | None = None,
) -> ToolEnvelope:
    """运行回测 —— 双形态(issue #174):

    * ``strategy`` 形态:事件驱动回测;默认小规模同步返回结果,``run_async=true``
      或规模达自动阈值时入队 ``kind=backtest_run`` 后台任务返回 job_id(issue
      #189,避免 MCP 客户端超时后响应丢失);
    * ``strategy_spec`` 形态:按已发布 ``{strategy_id, version}`` 路由入队
      research_run 管线,返回 run_id + job_id 指针,不阻塞等待完成。
    两形态互斥,同时给出报 ``invalid_argument``。

    selection.inputs_mode=research_db(默认)必需数据集批次未发布时入队/运行
    秒级拒绝 ``dataset_unpublished:{dataset}``(issue #255,防「0 交易成功」);
    先 research_data_sync 摄取并发布,核验步骤见 docs/research/data-ops.md。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if strategy_spec is not None:
            if strategy is not None:
                raise McpToolError(
                    "invalid_argument",
                    "strategy 与 strategy_spec 互斥,只能二选一",
                )
            return await _run_via_strategy_spec(app, strategy_spec, queue_payload or {})
        if strategy is None or not symbols or not start or not end:
            raise McpToolError(
                "invalid_argument",
                "strategy/symbols/start/end 必填(strategy_spec 形态除外)",
            )

        # issue #189:strategy 形态可异步 —— 显式 run_async 或按估算规模自动切换。
        decision = _resolve_async_mode(
            run_async,
            int(getattr(app.settings, "backtest_auto_async_symbol_days", 0) or 0),
            list(symbols),
            start,
            end,
        )
        if decision.use_async:
            from finboard_app.selection_schema import FactorSelectionParams

            validated_params = _validate_backtest_params(strategy, params or {})
            try:
                selection_model = FactorSelectionParams.model_validate(selection or {})
            except ValidationError as exc:
                raise McpToolError(
                    "invalid_argument", f"selection 参数校验失败: {exc}"
                ) from exc
            return await _run_strategy_async(
                app,
                strategy=strategy,
                symbols=list(symbols),
                start=start,
                end=end,
                capital=capital,
                adjust=adjust,
                validated_params=validated_params,
                selection_dump=selection_model.model_dump(mode="json"),
                commission_rate=commission_rate,
                commission_min=commission_min,
                stamp_tax_rate=stamp_tax_rate,
                slippage_bps=slippage_bps,
                benchmark_symbol=benchmark_symbol,
                requested_by=requested_by or "agent:mcp:backtest_run",
                reason=decision.reason,
                symbol_days_estimate=decision.symbol_days_estimate,
            )

        import json

        from finboard_app.selection_schema import FactorSelectionParams
        from finboard_app.strategies import create_strategy
        from finboard_backtest import (
            BacktestConfig,
            BacktestEngine,
            BenchmarkConfig,
            PointInTimeFactorSelector,
        )
        from finboard_backtest.selection_snapshot import FeatureSnapshotFactorReader
        from finboard_data import (
            AkShareProvider,
            TushareBarProvider,
            YFinanceProvider,
        )
        from finboard_data.factors import InputsMode
        from finboard_mcp.downsample import (
            apply_equity_mode,
            clamp_max_points,
            resolve_equity_mode,
        )
        from finboard_persistence import (
            BacktestRunModel,
            BacktestRunRepository,
            FactorSnapshotRepository,
            FeatureSnapshotRepository,
            ResearchDatasetRepository,
        )

        mode = resolve_equity_mode(equity_mode)
        max_equity_points = clamp_max_points(max_points)

        # 校验参数 + 构建策略
        validated_params = _validate_backtest_params(strategy, params or {})
        strat = create_strategy(strategy, "backtest", **validated_params)

        # 选择数据源
        provider_name = getattr(app.settings, "data_provider", "akshare")
        if provider_name == "akshare":
            data_provider: AkShareProvider | TushareBarProvider | YFinanceProvider = (
                AkShareProvider()
            )
        elif provider_name == "tushare":
            data_provider = TushareBarProvider(
                token=app.settings.tushare_token,
                requests_per_minute=app.settings.tushare_requests_per_minute,
                daily_request_limit=app.settings.tushare_daily_request_limit,
                usage_file=app.settings.tushare_usage_file,
            )
        else:
            data_provider = YFinanceProvider()

        # 解析 selection
        try:
            selection_model = FactorSelectionParams.model_validate(selection or {})
        except ValidationError as exc:
            raise McpToolError("invalid_argument", f"selection 参数校验失败: {exc}") from exc

        config = BacktestConfig(
            symbols=list(symbols),
            start=date_type.fromisoformat(start),
            end=date_type.fromisoformat(end),
            initial_capital=capital,
            adjust=adjust,
            strategy_params=validated_params,
            commission_rate=commission_rate,
            commission_min=commission_min,
            stamp_tax_rate=stamp_tax_rate,
            slippage_bps=slippage_bps,
            selection=selection_model.to_domain(),
            benchmark=(
                BenchmarkConfig(
                    symbol=benchmark_symbol,
                    equal_weight_universe=True,
                )
                if benchmark_symbol is not None
                else BenchmarkConfig()
            ),
        )

        async with app.session_maker() as session:
            # issue #255:research_db 选股必需数据集批次未发布 → 同步路径
            # 秒级拒绝,不让 run 空转成「0 交易成功」。
            unpublished = await ResearchDatasetRepository(
                session
            ).selection_inputs_gate(selection_model.to_domain())
            if unpublished:
                raise McpToolError(
                    "invalid_argument",
                    "research_db 选股必需数据集批次未发布,拒绝运行(issue #255): "
                    f"{'、'.join(unpublished)}。请先执行 data_sync 摄取并完成"
                    "批次发布,或改用 inputs_mode=bars/snapshot。",
                )
            factor_selector = (
                PointInTimeFactorSelector(
                    reader=(
                        FeatureSnapshotFactorReader(
                            snapshot_provider=FeatureSnapshotRepository(session).get,
                            snapshot_ids=tuple(selection_model.snapshot_ids),
                        )
                        if selection_model.inputs_mode is InputsMode.SNAPSHOT
                        else ResearchDatasetRepository(session)
                    ),
                    writer=FactorSnapshotRepository(session),
                )
                if selection_model.enabled
                else None
            )
            engine = BacktestEngine(
                strategy=strat,
                data_provider=data_provider,
                config=config,
                factor_selector=factor_selector,
            )
            try:
                result = await engine.run()
            except Exception as exc:
                raise McpToolError(
                    "unavailable",
                    f"回测执行失败(通常是数据源连接错误): {exc}",
                ) from exc

            # 构建结果字典(与 API 响应字段一一对应)
            equity_curve: list[dict[str, Any]] = [
                {"date": str(d), "equity": float(e)} for d, e in result.equity_curve
            ]
            bench_map: dict[str, float] = {}
            if result.benchmark_curve:
                bench_map = {str(d): float(b) for d, b in result.benchmark_curve}
                for point in equity_curve:
                    point["benchmark"] = bench_map.get(point["date"])
            fills = [
                {
                    "date": str(f.filled_at.date()) if f.filled_at else "",
                    "symbol": f.symbol.code,
                    "side": f.side,
                    "quantity": to_jsonable(f.quantity),
                    "price": to_jsonable(f.price),
                    "commission": to_jsonable(f.commission),
                }
                for f in result.fills
            ]
            metrics = {
                "total_return": result.total_return,
                "annualized_return": result.annualized_return,
                "sharpe_ratio": result.sharpe_ratio,
                # issue #262:rf=0 对照口径 + 主口径 rf 标注随指标序列化;
                # 与 research_run 报告同屏比较 Sharpe 时用 sharpe_rf0。
                "sharpe_rf0": result.sharpe_rf0,
                "risk_free_annual": result.risk_free_annual,
                "max_drawdown": result.max_drawdown,
                "win_rate": result.win_rate,
                "trade_count": result.trade_count,
                "turnover": result.turnover,
                "commission_paid": to_jsonable(result.commission_paid),
                "stamp_tax_paid": to_jsonable(result.stamp_tax_paid),
                "benchmark_return": result.benchmark_return,
                "excess_return": result.excess_return,
                # issue #254:基准曲线实际来源(显式标的/每期选股池等权/静态池等权/首标的)
                "benchmark_source": result.benchmark_source,
                # issue #255:选股逐期诊断(整期 SKIPPED / 数据集未发布可见)
                "selection_diagnostics": result.selection_diagnostics,
                "initial_capital": to_jsonable(result.initial_capital),
                "final_equity": to_jsonable(result.final_equity),
            }
            selection_snapshots = [
                {
                    "decision_at": to_jsonable(s.decision_at),
                    "business_date": str(s.business_date),
                    "effective_date": str(s.effective_date),
                    "selected_symbols": list(s.selected_symbols),
                    "status": s.status.value,
                    "skip_reason": s.skip_reason,
                    "dataset_versions": dict(s.dataset_versions),
                    "factor_version": s.factor_version,
                    "checksum": s.checksum,
                    "warnings": list(s.warnings),
                }
                for s in result.selection_snapshots
            ]

            # 落库
            run_row = BacktestRunModel(
                strategy=strategy,
                symbols=list(symbols),
                start=start,
                end=end,
                capital=capital,
                adjust=adjust,
                params=validated_params,
                selection=selection_model.model_dump(mode="json"),
                metrics=json.loads(json.dumps(metrics, default=str)),
                equity_curve=equity_curve,
                fills=fills,
                summary=result.summary(),
                dataset_versions=result.dataset_versions,
                factor_version=result.factor_version,
                selection_snapshots=selection_snapshots,
                matching_model=result.matching_model,
                asset_rules=result.asset_rules,
                fee_assumptions=result.fee_assumptions,
                benchmark_config=result.benchmark_config,
            )
            repo = BacktestRunRepository(session)
            await repo.save(run_row)
            await session.commit()
            run_id = run_row.id

        # issue #172:返回体积控制 —— 默认 summary(降采样),full 与现状一致;
        # 落库仍是全量(history_get 读取时再按 mode 处理)。
        returned_equity = apply_equity_mode(
            equity_curve, equity_mode=mode, max_points=max_equity_points
        )
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "run_id": run_id,
                    "metrics": metrics,
                    "equity_curve": returned_equity,
                    "equity_point_count": len(equity_curve),
                    "fills": fills,
                    "summary": result.summary(),
                    "selection_snapshots": selection_snapshots,
                    "dataset_versions": result.dataset_versions,
                    "factor_version": result.factor_version,
                    "matching_model": result.matching_model,
                    "asset_rules": result.asset_rules,
                    "fee_assumptions": result.fee_assumptions,
                    "benchmark_config": result.benchmark_config,
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.run",
        arguments={
            "strategy": strategy,
            "strategy_spec": strategy_spec,
            "symbols": symbols,
            "start": start,
            "end": end,
            "capital": str(capital),
            "adjust": adjust,
            "params": params,
            "selection": selection,
            "benchmark_symbol": benchmark_symbol,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_list(只读)
# --------------------------------------------------------------------------- #
async def backtest_history_list(
    app: McpAppContext,
    *,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_persistence import BacktestRunRepository

        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            rows = await repo.list_recent(limit=safe_limit)
            # issue #206 P2:列表 symbols 只回前 10 只 + symbol_count。
            return [_history_item(row, symbols_preview=True) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_list",
        arguments={"limit": limit},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_get(只读)
# --------------------------------------------------------------------------- #
async def backtest_history_get(
    app: McpAppContext,
    run_id: int,
    *,
    equity_mode: str = "summary",
    max_points: int = 200,
    fills_limit: int | None = 200,
    fills_offset: int = 0,
) -> ToolEnvelope:
    """历史详情(equity 降采样;fills 默认有界 200 条,issue #206)。"""
    async def _do() -> dict[str, Any]:
        from finboard_persistence import BacktestRunRepository

        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"回测记录不存在: {run_id}")
            return _history_detail(
                row,
                equity_mode=equity_mode,
                max_points=max_points,
                fills_limit=fills_limit,
                fills_offset=fills_offset,
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_get",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.history_delete(写)
# --------------------------------------------------------------------------- #
async def backtest_history_delete(
    app: McpAppContext,
    run_id: int,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_persistence import BacktestRunRepository

        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            deleted = await repo.delete(run_id)
            if not deleted:
                raise McpToolError("not_found", f"回测记录不存在: {run_id}")
            await session.commit()
            return {"run_id": run_id, "deleted": True}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.history_delete",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """把回测工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_backtest_strategy_list",
        description=(
            "列出回测入口:builtin_strategies(事件驱动引擎,仅 supports_backtest"
            "=true 的内置策略,含参数 schema)与 published_specs(已发布研究策略"
            "规格,含状态/版本数/执行入口提示)。已发布规格的正确回测路径是 "
            "research_run 管线(backtest_run(strategy_spec=...) 或 run_queue),"
            "builtin_strategies 才是事件驱动同步回测。"
        ),
    )
    async def _strategy_list(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_strategy_list(app_context(ctx))

    @mcp.tool(
        name="finboard_backtest_run",
        description=(
            "运行回测,双形态(二选一,互斥):"
            "(1) strategy 形态:事件驱动回测(纸面撮合,不发真实订单),默认同步"
            "返回 metrics/equity_curve/fills/selection_snapshots 并落库 —— 同步"
            "形态适用于小规模(数只标的 x 短区间,总工作量 ≲ 阈值);run_async=true"
            "强制入队 kind=backtest_run 后台任务返回 job_id(异步执行,避免 MCP "
            "客户端超时后响应丢失);省略 run_async 时按估算工作量『标的不数 x "
            "交易日』自动切换:≥ 阈值 backtest_auto_async_symbol_days(settings,"
            "默认 15000,0=关闭自动切换)即异步,否则同步;"
            "参数 strategy(如 ma_cross)、symbols、start/end(ISO 日期)、"
            "capital、adjust(qfq/hfq/none)、params(策略参数)、selection"
            "(因子选股配置,selection.factor_version 仅支持 \"v1\""
            "(选股规则版本);快照 framework_version 是另一契约字段,"
            "不传给 selection)、benchmark_symbol(可选,基准标的代码如 "
            "000300.SH,指数日线自动走 akshare 指数接口;不传则用等权候选池"
            "基准,基准缺失时 benchmark_return=null 而非 0)、equity_mode"
            "(summary 默认:降采样到 max_points 个关键点,首末点保留;full:"
            "完整曲线)、max_points(默认 200)、run_async(可选,true 强制异步/"
            "false 强制同步/省略自动切换,仅 strategy 形态生效)、requested_by"
            "(可选,异步任务归属,默认 agent:mcp:backtest_run)。"
            "(2) strategy_spec 形态:按已发布策略规格 {strategy_id, version} "
            "路由入队 research_run 管线(冻结 dataset_release_ids/因子快照后"
            "异步执行;single_shot 需 factor_snapshot_ids,多期在 queue_payload."
            "parameters 声明 rebalance_frequency,#203),返回 run_id + job_id "
            "指针,不阻塞等待完成;其余入队字段"
            "经 queue_payload 传入(与 finboard_run_queue 同构,不含 "
            "strategy_id/strategy_version)。"
            "同步与异步共用 BacktestEngine + 数据源(akshare/tushare/yfinance);"
            "异步任务用 finboard_job_get 轮询(result_ref=str(run_id)),完成后"
            "用 finboard_backtest_history_get(run_id) 查询完整结果。"
            "Sharpe 口径(issue #262):metrics.sharpe_ratio 为主口径"
            "(rf=risk_free_annual/年默认 3%,按 rf/252 日化,总体标准差 ddof=0,"
            "√252 年化,rf 取值随 risk_free_annual 字段序列化);sharpe_rf0 为"
            "rf=0 对照口径(样本标准差 ddof=1)——与 finboard_run_report 的"
            "sharpe_ratio 同口径,跨报告同屏比较 Sharpe 用 sharpe_rf0,勿用"
            "主口径直比(低收益策略主口径可能被 rf=3% 拖近 0,如 run 277"
            "年化 3.05% 显示 Sharpe 0.05 的误读)。"
        ),
    )
    async def _run(
        strategy: str | None = None,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        capital: str = "100000",
        adjust: str = "qfq",
        params: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        commission_rate: str = "0.0003",
        commission_min: str = "1",
        stamp_tax_rate: str = "0.0005",
        slippage_bps: str = "0",
        equity_mode: str = "summary",
        max_points: int = 200,
        benchmark_symbol: str | None = None,
        strategy_spec: dict[str, Any] | None = None,
        queue_payload: dict[str, Any] | None = None,
        run_async: bool | None = None,
        requested_by: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_run(
            app_context(ctx),
            strategy=strategy,
            symbols=symbols,
            start=start,
            end=end,
            capital=Decimal(capital),
            adjust=adjust,
            params=params,
            selection=selection,
            commission_rate=Decimal(commission_rate),
            commission_min=Decimal(commission_min),
            stamp_tax_rate=Decimal(stamp_tax_rate),
            slippage_bps=Decimal(slippage_bps),
            equity_mode=equity_mode,
            max_points=max_points,
            benchmark_symbol=benchmark_symbol,
            strategy_spec=strategy_spec,
            queue_payload=queue_payload,
            run_async=run_async,
            requested_by=requested_by,
        )

    @mcp.tool(
        name="finboard_backtest_history_list",
        description=(
            "列出最近的回测历史记录(摘要,不含完整 equity/fills;symbols 只回"
            "前 10 只,附 symbol_count,issue #206)。"
        ),
    )
    async def _history_list(
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_list(app_context(ctx), limit=limit)

    @mcp.tool(
        name="finboard_backtest_history_get",
        description=(
            "查询单条回测历史详情。equity_mode(summary 默认:降采样到 "
            "max_points 个关键点,首末点保留;full:完整曲线)、max_points(默认 "
            "200)、fills_limit/fills_offset(fills 分页,默认有界 200 条,"
            "issue #206;fills_limit=null 返回全部)。"
            "返回含 equity_point_count / fills_total / fills_offset 元信息。"
            "Sharpe 口径(#262):sharpe_ratio=主口径(rf 见 risk_free_annual,"
            "默认 3%/年,ddof=0);sharpe_rf0=rf=0 对照口径(ddof=1),与"
            "research_run 报告 sharpe_ratio 同口径,跨报告比较用 sharpe_rf0。"
        ),
    )
    async def _history_get(
        run_id: int,
        equity_mode: str = "summary",
        max_points: int = 200,
        fills_limit: int | None = 200,
        fills_offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_get(
            app_context(ctx),
            run_id,
            equity_mode=equity_mode,
            max_points=max_points,
            fills_limit=fills_limit,
            fills_offset=fills_offset,
        )

    @mcp.tool(
        name="finboard_backtest_history_delete",
        description="删除一条回测历史记录(写操作,mcp_readonly_only=true 时拒绝)。",
    )
    async def _history_delete(
        run_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_history_delete(app_context(ctx), run_id)
