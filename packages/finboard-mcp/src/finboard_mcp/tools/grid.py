"""``finboard.backtest.grid.*`` 工具 —— 批量参数网格回测(issue #175)。

支撑「合理实验」:一个假设下多组参数对比择优。一次提交 N 组参数,逐组合
复用 ``kind=backtest_run`` job 后台执行,全部到终态后 ``grid_get`` 聚合
对比表(指标矩阵 + 关键指标排名 + 最优标注),部分失败带错误码单独列出,
不影响成功组合返回。

- ``backtest_grid_submit``(写):组合展开(params 显式列表 / 笛卡尔积 x
  selection_grid 选股维度笛卡尔积,#259)+ 上限封顶 → 逐组合参数与
  selection 校验 → 网格定义与 N 个 backtest_run job **同一事务**落库
  (全有或全无)→ 返回 grid_id + job 指针,异步执行;
- ``backtest_grid_get``(只读):按 grid_id 聚合 —— 逐组合读 job 状态 +
  result_ref 对应的回测记录 → 指标矩阵 / 排名 / 最优标注 / 失败清单。

研究域 only(#136 边界):只入队白名单内的 ``backtest_run`` 任务,不连
broker / 账户 / 订单 / 持仓。equity 曲线展示复用 #172 的 summary 降采样形态。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from datetime import date as date_type
from decimal import Decimal
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_mcp.tools.backtest import _require_write_enabled, _validate_backtest_params
from finboard_mcp.tools.jobs import _payload_checksum
from finboard_persistence import (
    BackgroundJobRepository,
    BacktestGridRunModel,
    BacktestGridRunRepository,
    BacktestRunRepository,
)
from finboard_shared.background_jobs import (
    TERMINAL_STATUSES,
    BackgroundJobStatus,
    generate_background_job_id,
)

#: 组合数硬上限 —— 防误操作一次打爆队列;``max_combos`` 参数不能超过它。
_GRID_COMBO_HARD_LIMIT = 50
#: 默认组合数上限(agent 可显式调整)。
_DEFAULT_MAX_COMBOS = 20

#: 指标矩阵列(回测引擎 metrics 的规范顺序,结构稳定可机读)。
_METRIC_FIELDS: tuple[str, ...] = (
    "total_return",
    "annualized_return",
    "sharpe_ratio",
    # issue #262:rf=0 对照口径(老 run 无该键时矩阵自动省略该列)。
    "sharpe_rf0",
    "max_drawdown",
    "win_rate",
    "trade_count",
    "turnover",
    "commission_paid",
    "stamp_tax_paid",
    "benchmark_return",
    "excess_return",
    "initial_capital",
    "final_equity",
)

#: 参与排名 / 最优标注的关键指标(全部「越高越好」;max_drawdown 为负值,
#: 越高 = 回撤越小)。
_RANK_METRICS: tuple[str, ...] = (
    "total_return",
    "annualized_return",
    "sharpe_ratio",
    "max_drawdown",
    "win_rate",
    "excess_return",
)

__all__ = ["register"]


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _expand_combos(
    *,
    params_list: list[dict[str, Any]] | None,
    params_grid: dict[str, list[Any]] | None,
    selection_grid: dict[str, list[Any]] | None,
) -> list[dict[str, Any]]:
    """展开组合覆盖,返回 ``[{label, params, selection}]``。

    params 侧 ``params_list``(显式列表)与 ``params_grid``(笛卡尔积)互斥;
    selection 侧 ``selection_grid``(``{selection字段: 值列表}``,与 params_grid
    同构)是独立的笛卡尔积维度(issue #259)。两侧做笛卡尔积,至少一侧非空;
    组合数上限由调用方在展开后校验。label 是覆盖差异的确定性 JSON:无
    selection_grid 时与历史格式一致(覆盖参数 JSON),有 selection_grid 时为
    ``{"params": ..., "selection": ...}``(空 params 侧省略)。
    """
    if params_list is not None and params_grid is not None:
        raise McpToolError(
            "invalid_argument",
            "params_list(显式列表)与 params_grid(笛卡尔积)必须二选一",
        )
    if params_list is None and params_grid is None and selection_grid is None:
        raise McpToolError(
            "invalid_argument",
            "params_list / params_grid / selection_grid 至少提供一个组合维度",
        )

    params_overrides: list[dict[str, Any]]
    if params_list is not None:
        if not params_list:
            raise McpToolError("invalid_argument", "params_list 不能为空列表")
        for index, item in enumerate(params_list):
            if not isinstance(item, dict):
                raise McpToolError("invalid_argument", f"params_list[{index}] 必须是参数 dict")
        params_overrides = params_list
    elif params_grid is not None:
        if not params_grid:
            raise McpToolError("invalid_argument", "params_grid 不能为空 dict")
        for key, values in params_grid.items():
            if not isinstance(values, list) or not values:
                raise McpToolError(
                    "invalid_argument",
                    f"params_grid[{key!r}] 必须是非空值列表(笛卡尔积维度)",
                )
        keys = list(params_grid)
        params_overrides = [
            dict(zip(keys, values, strict=True))
            for values in itertools.product(*(params_grid[key] for key in keys))
        ]
    else:
        params_overrides = [{}]

    selection_overrides: list[dict[str, Any]]
    if selection_grid is not None:
        if not selection_grid:
            raise McpToolError("invalid_argument", "selection_grid 不能为空 dict")
        for key, values in selection_grid.items():
            if not isinstance(values, list) or not values:
                raise McpToolError(
                    "invalid_argument",
                    f"selection_grid[{key!r}] 必须是非空值列表(笛卡尔积维度)",
                )
        selection_keys = list(selection_grid)
        selection_overrides = [
            dict(zip(selection_keys, values, strict=True))
            for values in itertools.product(*(selection_grid[key] for key in selection_keys))
        ]
    else:
        selection_overrides = [{}]

    has_selection_grid = selection_grid is not None
    combos: list[dict[str, Any]] = []
    for params_override in params_overrides:
        for selection_override in selection_overrides:
            if has_selection_grid:
                label_parts: dict[str, Any] = {}
                if params_override:
                    label_parts["params"] = params_override
                label_parts["selection"] = selection_override
                label = json.dumps(label_parts, sort_keys=True, ensure_ascii=False)
            else:
                label = json.dumps(params_override, sort_keys=True, ensure_ascii=False)
            combos.append(
                {
                    "label": label,
                    "params": params_override,
                    "selection": selection_override,
                }
            )
    return combos


def _combos_checksum(combos: list[dict[str, Any]]) -> str:
    """组合定义(索引 + 标签 + 校验后参数)的确定性指纹,用于幂等重提交校验。

    selection 仅在组合携带时进入指纹(#259)—— 旧网格(无 selection 键)的
    checksum 不因本字段引入而漂移,幂等重提交仍能命中既有网格。
    """

    canonical = json.dumps(
        [
            {
                "index": c["index"],
                "label": c["label"],
                "params": c["params"],
                **({"selection": c["selection"]} if "selection" in c else {}),
            }
            for c in combos
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rank_succeeded(
    succeeded: list[dict[str, Any]],
) -> tuple[dict[str, dict[int, int]], dict[str, dict[str, Any]]]:
    """对成功组合计算每关键指标的竞争排名 + 每指标最优组合。

    返回 ``(rank_by_metric, best_by_metric)``;非数值 / 缺失指标不参与排名。
    ``rank_by_metric[metric][combo_index]`` = 竞争排名(1 起,同值同排名);
    ``best_by_metric[metric]`` = ``{combo_index, label, value}``。
    """
    label_by_index = {row["combo_index"]: row["label"] for row in succeeded}
    rank_by_metric: dict[str, dict[int, int]] = {}
    best_by_metric: dict[str, dict[str, Any]] = {}
    for metric in _RANK_METRICS:
        scored: list[tuple[int, float]] = []
        for row in succeeded:
            try:
                value = float(row["metrics"][metric])
            except (KeyError, TypeError, ValueError):
                continue
            scored.append((row["combo_index"], value))
        if not scored:
            continue
        scored.sort(key=lambda item: item[1], reverse=True)
        ranks: dict[int, int] = {}
        rank = 0
        for position, (combo_index, value) in enumerate(scored):
            if position == 0 or value < scored[position - 1][1]:
                rank = position + 1
            ranks[combo_index] = rank
        rank_by_metric[metric] = ranks
        best_index = scored[0][0]
        best_by_metric[metric] = {
            "combo_index": best_index,
            "label": label_by_index.get(best_index),
            "value": scored[0][1],
        }
    return rank_by_metric, best_by_metric


def _build_request(
    *,
    strategy: str,
    symbols: list[str],
    start: str,
    end: str,
    capital: Decimal,
    adjust: str,
    params: dict[str, Any],
    selection: dict[str, Any],
    commission_rate: Decimal,
    commission_min: Decimal,
    stamp_tax_rate: Decimal,
    slippage_bps: Decimal,
) -> dict[str, Any]:
    """构建单组合的 ``BacktestRunRequest`` 兼容 dict(job payload 用)。"""

    return {
        "strategy": strategy,
        "symbols": list(symbols),
        "start": start,
        "end": end,
        "capital": str(capital),
        "adjust": adjust,
        "params": params,
        "selection": selection,
        "commission_rate": str(commission_rate),
        "commission_min": str(commission_min),
        "stamp_tax_rate": str(stamp_tax_rate),
        "slippage_bps": str(slippage_bps),
    }


# --------------------------------------------------------------------------- #
# finboard.backtest.grid_submit(写)
# --------------------------------------------------------------------------- #
async def backtest_grid_submit(
    app: McpAppContext,
    *,
    strategy: str | None,
    symbols: list[str] | None,
    start: str | None,
    end: str | None,
    capital: Decimal,
    adjust: str,
    params: dict[str, Any] | None,
    selection: dict[str, Any] | None,
    params_list: list[dict[str, Any]] | None,
    params_grid: dict[str, list[Any]] | None,
    selection_grid: dict[str, list[Any]] | None = None,
    max_combos: int,
    grid_idempotency_key: str,
    requested_by: str,
    commission_rate: Decimal,
    commission_min: Decimal,
    stamp_tax_rate: Decimal,
    slippage_bps: Decimal,
) -> ToolEnvelope:
    """批量参数网格回测提交:展开 + 上限 + 校验 → 与 N 个 job 同一事务落库。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if not grid_idempotency_key or len(grid_idempotency_key) < 8:
            raise McpToolError("invalid_argument", "grid_idempotency_key 至少 8 个字符")
        if not (1 <= max_combos <= _GRID_COMBO_HARD_LIMIT):
            raise McpToolError(
                "invalid_argument",
                f"max_combos 必须在 1..{_GRID_COMBO_HARD_LIMIT} 之间(硬上限,防打爆队列)",
            )
        if strategy is None or not symbols or not start or not end:
            raise McpToolError("invalid_argument", "strategy/symbols/start/end 必填")
        try:
            date_type.fromisoformat(start)
            date_type.fromisoformat(end)
        except ValueError as exc:
            raise McpToolError("invalid_argument", f"start/end 必须是 ISO 日期: {exc}") from exc

        # 1) 组合展开 + 上限封顶(在任何入队/落库之前)
        raw_combos = _expand_combos(
            params_list=params_list,
            params_grid=params_grid,
            selection_grid=selection_grid,
        )
        if len(raw_combos) > max_combos:
            raise McpToolError(
                "invalid_argument",
                f"组合数 {len(raw_combos)} 超过 max_combos={max_combos}"
                f"(硬上限 {_GRID_COMBO_HARD_LIMIT};组合数 = params 覆盖 x selection "
                f"覆盖的笛卡尔积,收敛网格或提高 max_combos)",
            )

        # 2) 逐组合参数与 selection 校验(与 backtest_run 同一校验入口,失败即拒)
        from finboard_app.selection_schema import FactorSelectionParams

        validated_base = _validate_backtest_params(strategy, params or {})
        try:
            selection_model = FactorSelectionParams.model_validate(selection or {})
        except ValidationError as exc:
            raise McpToolError("invalid_argument", f"selection 参数校验失败: {exc}") from exc
        selection_dump = selection_model.model_dump(mode="json")

        combos: list[dict[str, Any]] = []
        for index, item in enumerate(raw_combos):
            merged = {**validated_base, **item["params"]}
            validated = _validate_backtest_params(strategy, merged)
            entry: dict[str, Any] = {"index": index, "label": item["label"], "params": validated}
            # issue #259:selection 覆盖只在组合携带时合并校验;
            # 基础 selection 已在上方整体验证过。
            if item["selection"]:
                merged_selection = {**selection_dump, **item["selection"]}
                try:
                    combo_selection = FactorSelectionParams.model_validate(merged_selection)
                except ValidationError as exc:
                    raise McpToolError(
                        "invalid_argument",
                        f"组合 {index}({item['label']}) selection 参数校验失败: {exc}",
                    ) from exc
                entry["selection"] = combo_selection.model_dump(mode="json")
            combos.append(entry)
        checksum = _combos_checksum(combos)
        provider_name = getattr(app.settings, "data_provider", "akshare")

        # 3) 网格定义 + N 个 backtest_run job 同一事务落库(全有或全无)
        async with app.session_maker() as session:
            grid_repo = BacktestGridRunRepository(session)
            existing = await grid_repo.get_by_idempotency_key(grid_idempotency_key)
            if existing is not None:
                if existing.combos_checksum != checksum:
                    raise McpToolError(
                        "conflict",
                        "相同 grid_idempotency_key 对应不同组合"
                        f"(checksum 不一致): {grid_idempotency_key}",
                    )
                return _grid_submit_out(existing, created=False)

            grid_id = f"BTG-{uuid.uuid4().hex[:16].upper()}"
            row = BacktestGridRunModel(
                grid_id=grid_id,
                idempotency_key=grid_idempotency_key,
                combos_checksum=checksum,
                strategy=strategy,
                symbols=list(symbols),
                start=start,
                end=end,
                capital=capital,
                adjust=adjust,
                base_params=validated_base,
                selection=selection_dump,
                combos=[],
                combo_count=len(combos),
                max_combos=max_combos,
                requested_by=requested_by,
            )
            session.add(row)
            try:
                await session.flush()
            except Exception as exc:
                await session.rollback()
                raise McpToolError("conflict", f"网格提交并发冲突,请重试: {exc}") from exc

            job_repo = BackgroundJobRepository(session)
            jobs: list[dict[str, Any]] = []
            for combo in combos:
                # issue #259:组合携带 selection 覆盖时 payload 用组合级
                # selection(已校验合并),否则用网格级基础 selection。
                combo_selection = combo.get("selection", selection_dump)
                payload: dict[str, Any] = {
                    "request": _build_request(
                        strategy=strategy,
                        symbols=list(symbols),
                        start=start,
                        end=end,
                        capital=capital,
                        adjust=adjust,
                        params=combo["params"],
                        selection=combo_selection,
                        commission_rate=commission_rate,
                        commission_min=commission_min,
                        stamp_tax_rate=stamp_tax_rate,
                        slippage_bps=slippage_bps,
                    ),
                    "provider_name": provider_name,
                    "grid": {"grid_id": grid_id, "combo_index": combo["index"]},
                }
                try:
                    job_row, _created = await job_repo.create_or_get(
                        job_id=generate_background_job_id(),
                        idempotency_key=f"btg:{grid_id}:{combo['index']}",
                        kind="backtest_run",
                        queue="default",
                        status=BackgroundJobStatus.QUEUED.value,
                        priority=0,
                        payload=payload,
                        payload_checksum=_payload_checksum(payload),
                        max_attempts=3,
                        requested_by=requested_by,
                    )
                except Exception as exc:
                    await session.rollback()
                    raise McpToolError("conflict", f"网格任务登记冲突,请重试: {exc}") from exc
                combo["job_id"] = job_row.job_id
                job_entry: dict[str, Any] = {
                    "combo_index": combo["index"],
                    "label": combo["label"],
                    "job_id": job_row.job_id,
                    "params": combo["params"],
                }
                if "selection" in combo:
                    job_entry["selection"] = combo["selection"]
                jobs.append(job_entry)
            row.combos = combos
            await session.commit()
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "grid_id": grid_id,
                    "created": True,
                    "strategy": strategy,
                    "symbols": list(symbols),
                    "start": start,
                    "end": end,
                    "capital": str(capital),
                    "adjust": adjust,
                    "combo_count": len(combos),
                    "max_combos": max_combos,
                    "jobs": jobs,
                    "polling_note": (
                        "用 finboard_backtest_grid_get(grid_id=...) 查询聚合对比表"
                        "(指标矩阵/排名/最优标注/失败清单),或 finboard_job_get "
                        "逐任务查询;任务由独立 worker 消费"
                    ),
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.grid_submit",
        arguments={
            "strategy": strategy,
            "symbols": symbols,
            "start": start,
            "end": end,
            "capital": str(capital),
            "adjust": adjust,
            "max_combos": max_combos,
            "params_list": params_list,
            "params_grid": params_grid,
            "selection_grid": selection_grid,
            "params": params,
            "selection": selection,
        },
        handler=_do,
        idempotency_key=grid_idempotency_key,
    )


def _grid_submit_out(row: BacktestGridRunModel, *, created: bool) -> dict[str, Any]:
    """幂等命中时返回已存在的网格定义(created=False)。"""

    jobs = []
    for combo in row.combos or []:
        entry: dict[str, Any] = {
            "combo_index": combo["index"],
            "label": combo.get("label"),
            "job_id": combo.get("job_id"),
            "params": combo.get("params") or {},
        }
        if combo.get("selection") is not None:
            entry["selection"] = combo["selection"]
        jobs.append(entry)
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "grid_id": row.grid_id,
                "created": created,
                "strategy": row.strategy,
                "symbols": list(row.symbols or []),
                "start": row.start,
                "end": row.end,
                "capital": str(row.capital),
                "adjust": row.adjust,
                "combo_count": row.combo_count,
                "max_combos": row.max_combos,
                "jobs": jobs,
                "polling_note": (
                    "用 finboard_backtest_grid_get(grid_id=...) 查询聚合对比表,"
                    "或 finboard_job_get 逐任务查询"
                ),
            }
        ),
    )


# --------------------------------------------------------------------------- #
# finboard.backtest.grid_get(只读)
# --------------------------------------------------------------------------- #
async def backtest_grid_get(
    app: McpAppContext,
    *,
    grid_id: str,
    equity_mode: str = "none",
    max_points: int = 200,
) -> ToolEnvelope:
    """聚合网格对比表:指标矩阵 + 排名/最优标注 + 失败清单(默认不含曲线)。"""

    async def _do() -> dict[str, Any]:
        from finboard_mcp.downsample import (
            apply_equity_mode,
            clamp_max_points,
            resolve_equity_mode,
        )

        mode = resolve_equity_mode(equity_mode)
        max_equity_points = clamp_max_points(max_points)

        async with app.session_maker() as session:
            grid_row = await BacktestGridRunRepository(session).get_by_grid_id(grid_id)
            if grid_row is None:
                raise McpToolError("not_found", f"网格回测不存在: {grid_id}")
            job_repo = BackgroundJobRepository(session)
            run_repo = BacktestRunRepository(session)

            rows: list[dict[str, Any]] = []
            failures: list[dict[str, Any]] = []
            for combo in grid_row.combos or []:
                index = combo["index"]
                label = combo.get("label")
                job = await job_repo.get(str(combo.get("job_id") or ""))
                if job is None:
                    failures.append(
                        {
                            "combo_index": index,
                            "label": label,
                            "job_id": combo.get("job_id"),
                            "job_status": "missing",
                            "error_code": "job_missing",
                            "error_summary": "组合对应的后台任务不存在",
                        }
                    )
                    continue
                entry: dict[str, Any] = {
                    "combo_index": index,
                    "label": label,
                    "job_id": job.job_id,
                    "job_status": job.status,
                    # issue #206 P1:公共参数上提网格头部 base_params,
                    # combo 不重复;组合差异由 label(覆盖参数 JSON)承载。
                }
                if job.status == BackgroundJobStatus.SUCCEEDED.value:
                    run = None
                    if job.result_ref:
                        try:
                            run = await run_repo.get(int(job.result_ref))
                        except ValueError:
                            run = None
                    if run is None:
                        failures.append(
                            {
                                "combo_index": index,
                                "label": label,
                                "job_id": job.job_id,
                                "job_status": job.status,
                                "error_code": "run_missing",
                                "error_summary": (
                                    f"回测记录缺失(result_ref={job.result_ref},可能已被删除)"
                                ),
                            }
                        )
                        continue
                    equity = list(run.equity_curve or [])
                    row_payload: dict[str, Any] = {
                        "run_id": run.id,
                        "metrics": dict(run.metrics or {}),
                        # issue #190:默认(none)不返回曲线,只保留点数提示;
                        # 显式 equity_mode=summary/full 才带 equity_curve。
                        "equity_point_count": len(equity),
                    }
                    if mode != "none":
                        row_payload["equity_curve"] = apply_equity_mode(
                            equity,
                            equity_mode=mode,
                            max_points=max_equity_points,
                        )
                    entry.update(row_payload)
                    rows.append(entry)
                elif job.status in TERMINAL_STATUSES:
                    # failed / cancelled / interrupted —— 终态失败,带错误码单列
                    failures.append(
                        {
                            "combo_index": index,
                            "label": label,
                            "job_id": job.job_id,
                            "job_status": job.status,
                            "error_code": job.error_code,
                            "error_summary": job.error_summary,
                        }
                    )
                else:
                    # queued / running / retry_waiting / cancel_requested —— 未到终态
                    rows.append(entry)

            succeeded = [row for row in rows if "metrics" in row]
            rank_by_metric, best_by_metric = _rank_succeeded(succeeded)
            for row in rows:
                if "metrics" not in row:
                    continue
                ranks = {
                    metric: metric_ranks[row["combo_index"]]
                    for metric, metric_ranks in rank_by_metric.items()
                    if row["combo_index"] in metric_ranks
                }
                row["rank"] = ranks
                row["best"] = {metric: rank == 1 for metric, rank in ranks.items()}

            statuses = [row["job_status"] for row in rows]
            statuses.extend(item["job_status"] for item in failures)
            complete = bool(statuses) and all(status in TERMINAL_STATUSES for status in statuses)
            metric_fields = [
                field
                for field in _METRIC_FIELDS
                if any(field in row["metrics"] for row in succeeded)
            ]

        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "grid_id": grid_row.grid_id,
                    "strategy": grid_row.strategy,
                    "symbols": list(grid_row.symbols or []),
                    "start": grid_row.start,
                    "end": grid_row.end,
                    "capital": str(grid_row.capital),
                    "adjust": grid_row.adjust,
                    # issue #206 P1:网格级基础参数在头部只出现一次;
                    # combo 完整参数 = base_params + label(覆盖参数)。
                    "params": dict(getattr(grid_row, "base_params", None) or {}),
                    # issue #259:基础 selection 同样只在头部出现一次,
                    # 组合级 selection 差异由 label(覆盖 JSON)承载。
                    "selection": dict(getattr(grid_row, "selection", None) or {}),
                    "combo_count": grid_row.combo_count,
                    "complete": complete,
                    "completed_count": sum(1 for status in statuses if status in TERMINAL_STATUSES),
                    "pending_count": sum(
                        1 for status in statuses if status not in TERMINAL_STATUSES
                    ),
                    "failed_count": len(failures),
                    "metric_fields": metric_fields,
                    "combos": rows,
                    "ranking": best_by_metric,
                    "failures": failures,
                    "polling_note": (
                        "complete=false 表示还有组合未到终态,稍后重试;"
                        "失败组合在 failures 中带错误码单列"
                    ),
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.backtest.grid_get",
        arguments={"grid_id": grid_id, "equity_mode": equity_mode},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(mcp: MCPServer) -> None:
    """把批量参数网格回测工具注册到 MCP server(1 写 + 1 只读)。"""

    @mcp.tool(
        name="finboard_backtest_grid_submit",
        description=(
            "[写] 批量参数网格回测提交:一次提交 N 组参数,逐组合展开 + 上限"
            "封顶 + 参数校验后,各登记一个 kind=backtest_run 后台任务(网格定义"
            "与任务同一事务落库,全有或全无),返回 grid_id + job 指针,异步执行。"
            "公共参数:strategy/symbols/start/end/capital/adjust/params/selection/"
            "commission_rate/commission_min/stamp_tax_rate/slippage_bps;"
            "组合维度:params_list(显式参数列表)与 params_grid(参数笛卡尔积"
            " {字段: 值列表})互斥二选一;selection_grid(#259,选股维度笛卡尔积 "
            "{selection字段: 值列表},如 ranking_factor/momentum_lookback/max_symbols)"
            "为独立维度,与 params 侧做笛卡尔积,可单独使用(纯选股扫描,如因子"
            "x窗口);三者至少提供一个,总组合数受 max_combos(默认 20,硬上限 "
            "50,超限 invalid_argument)约束;逐组合 selection 过 FactorSelectionParams "
            "校验,非法组合具名 invalid_argument 不落库不入队。"
            "grid_idempotency_key(幂等键,重提交返回同一网格);"
            "requested_by。完成后用 finboard_backtest_grid_get(grid_id) 查询聚合"
            "对比表,或 finboard_job_get 逐任务查询。研究域 only,不连实盘。"
        ),
    )
    async def _grid_submit(
        strategy: str,
        symbols: list[str],
        start: str,
        end: str,
        grid_idempotency_key: str,
        requested_by: str,
        capital: str = "100000",
        adjust: str = "qfq",
        params: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        params_list: list[dict[str, Any]] | None = None,
        params_grid: dict[str, list[Any]] | None = None,
        selection_grid: dict[str, list[Any]] | None = None,
        max_combos: int = _DEFAULT_MAX_COMBOS,
        commission_rate: str = "0.0003",
        commission_min: str = "1",
        stamp_tax_rate: str = "0.0005",
        slippage_bps: str = "0",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_grid_submit(
            app_context(ctx),
            strategy=strategy,
            symbols=symbols,
            start=start,
            end=end,
            capital=Decimal(capital),
            adjust=adjust,
            params=params,
            selection=selection,
            params_list=params_list,
            params_grid=params_grid,
            selection_grid=selection_grid,
            max_combos=max_combos,
            grid_idempotency_key=grid_idempotency_key,
            requested_by=requested_by,
            commission_rate=Decimal(commission_rate),
            commission_min=Decimal(commission_min),
            stamp_tax_rate=Decimal(stamp_tax_rate),
            slippage_bps=Decimal(slippage_bps),
        )

    @mcp.tool(
        name="finboard_backtest_grid_get",
        description=(
            "查询批量参数网格回测的聚合对比表:逐组合指标矩阵(收益/回撤/夏普/"
            "胜率/超额/换手等)+ 关键指标竞争排名与最优标注(ranking/best)+ "
            "失败组合错误清单(failures 带错误码单列,不影响成功组合返回)。"
            "Sharpe 口径(issue #262):矩阵 sharpe_ratio 列为主口径(rf=3%/年,"
            "ddof=0),sharpe_rf0 列为 rf=0 对照口径(ddof=1,与 research_run "
            "报告同口径);跨策略/报告比较用 sharpe_rf0;老 run 无该键时列自动"
            "省略。排名指标不变(仍用主口径 sharpe_ratio,组合间同口径可比)。"
            "另 risk_free_annual 键在逐组合 metrics 里可见,矩阵不单列。"

            "公共字段(strategy/symbols/start/end/capital/adjust/params=base_params/"
            "selection=基础选股配置)在网格头部只出现一次(issue #206/#259);"
            "combo 只含 combo_index/label/"
            "job_id/job_status/指标/权益,组合差异由 label(覆盖参数 JSON,"
            "含 selection 覆盖时为 {params, selection} 结构)承载,"
            "完整组合参数 = params/selection + label。"
            "参数:grid_id、equity_mode(none 默认:不返回 equity 曲线,响应最轻,"
            "只保留 equity_point_count 点数提示;summary:equity 降采样到 "
            "max_points 个关键点,首末点保留;full:完整曲线)、max_points(默认 "
            "200)。9 组合 x 200 点曲线约 100-200KB,对比择优建议默认 none,"
            "确需曲线再显式请求。complete=false 表示还有组合未到终态"
            "(queued/running),可稍后重试。只读。"
        ),
    )
    async def _grid_get(
        grid_id: str,
        equity_mode: str = "none",
        max_points: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await backtest_grid_get(
            app_context(ctx),
            grid_id=grid_id,
            equity_mode=equity_mode,
            max_points=max_points,
        )
