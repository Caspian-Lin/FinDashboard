"""有界、只读的研究诊断;REST、MCP 与 Markdown 共用此契约 (#501)。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.research_explanation import _FIELDS, _KEYS, _key_case, explain
from finboard_backtest.metrics import (
    annualized_return,
    max_drawdown,
    sharpe_ratio_rf0,
    total_return,
)
from finboard_backtest.research_run.contracts import stable_checksum

MAX_POINTS = 10000
MAX_BYTES = 262144


@asynccontextmanager
async def bounded_read(session: AsyncSession):  # type: ignore[no-untyped-def]
    async with asyncio.timeout(150):
        await session.execute(text("SET LOCAL statement_timeout = '120s'"))
        yield


async def frozen_input(session: AsyncSession, run_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("""
        SELECT status, manifest_checksum, result_checksum,
        CASE WHEN octet_length(manifest::text)<=1048576 THEN manifest END AS manifest
        FROM research_runs WHERE run_id=:run_id
    """),
                {"run_id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError("research_run_not_found")
    if row["manifest"] is None:
        raise ValueError("payload_too_large: manifest > 1MiB")
    return dict(row)


def interval_metrics(
    points: list[tuple[date, Decimal]],
    start: date,
    end: date,
    initial_capital: Decimal,
) -> dict[str, Any]:
    """闭区间收益包含首日变动;优先用首日前最后一条权益作分母。"""
    if start > end:
        raise ValueError("invalid_window: start > end")
    selected = [(d, v) for d, v in points if start <= d <= end]
    if not selected:
        return {"status": "unsupported", "reason": "empty_equity_window"}
    previous = [(d, v) for d, v in points if d < selected[0][0]]
    anchor = previous[-1] if previous else (selected[0][0], initial_capital)
    if any(not v.is_finite() or v <= 0 for _, v in [anchor, *selected]):
        raise ValueError("invalid_equity: positive finite values required")
    curve = [anchor, *selected] if previous or selected[0][1] != initial_capital else selected
    peak = curve[0][1]
    peak_day = curve[0][0]
    longest = 0
    underwater = False
    for d, value in selected:
        if underwater or value < peak:
            longest = max(longest, (d - peak_day).days)
        underwater = value < peak
        if value >= peak:
            peak, peak_day = value, d
    return {
        "status": "completed",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "observed_start": selected[0][0].isoformat(),
        "observed_end": selected[-1][0].isoformat(),
        "observations": len(selected),
        "anchor_date": anchor[0].isoformat(),
        "anchor_equity": str(anchor[1]),
        "net_return": total_return(curve),
        "annualized_return": (1 + total_return(curve)) ** (252 / (len(curve) - 1)) - 1 if len(curve) > 1 else 0.0,
        "annualized_return_calendar": annualized_return(curve),
        "max_drawdown": abs(max_drawdown(curve)),
        "drawdown_duration_calendar_days": longest,
        "sharpe_rf0_ddof1": sharpe_ratio_rf0(curve),
        "final_equity": str(selected[-1][1]),
    }


async def equity_points(session: AsyncSession, run_id: str) -> list[tuple[date, Decimal]]:
    # 只在服务器检查点数与字节后读取投影;不加载 ORM result/artifact。
    size = (
        await session.execute(
            text("""
        SELECT jsonb_array_length(COALESCE(result::jsonb->'equity_curve','[]'::jsonb)),
        octet_length(COALESCE(result::jsonb->'equity_curve','[]'::jsonb)::text)
        FROM research_runs WHERE run_id=:run_id
    """),
            {"run_id": run_id},
        )
    ).first()
    if size is None:
        raise LookupError("research_run_not_found")
    if size[0] > MAX_POINTS or size[1] > 2097152:
        raise ValueError("payload_too_large: equity point/byte guard")
    rows = (
        await session.execute(
            text("""
        SELECT p->>'trade_date' AS day, p->>'equity' AS equity
        FROM research_runs CROSS JOIN LATERAL jsonb_array_elements(
          COALESCE(result::jsonb->'equity_curve','[]'::jsonb)) p
        WHERE run_id=:run_id ORDER BY p->>'trade_date'
    """),
            {"run_id": run_id},
        )
    ).all()
    points = [(date.fromisoformat(r.day), Decimal(r.equity)) for r in rows]
    if len({d for d, _ in points}) != len(points):
        raise ValueError("duplicate_equity_dates")
    return points


async def interval_report(
    session: AsyncSession,
    *,
    run_id: str,
    start: str,
    end: str,
    yearly: bool = True,
) -> dict[str, Any]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid_window: start > end")
    async with bounded_read(session):
        frozen = await frozen_input(session, run_id)
        points = await equity_points(session, run_id)
        manifest = frozen["manifest"]
        capital = Decimal(str(manifest["initial_capital"]))
        periods = [(first, last)]
        if yearly:
            periods += [
                (max(first, date(y, 1, 1)), min(last, date(y, 12, 31)))
                for y in range(first.year, last.year + 1)
            ]
        if len(periods) > 101:
            raise ValueError("window_too_large: at most 100 years")
        reports = []
        for lo, hi in periods:
            metrics = interval_metrics(points, lo, hi, capital)
            fills = (
                (
                    await session.execute(
                        text("""
                SELECT count(*) AS fill_count,
                  COALESCE(sum((f->>'quantity')::numeric*(f->>'price')::numeric),0) AS notional,
                  COALESCE(sum((f->>'commission')::numeric),0) AS commission,
                  COALESCE(sum((f->>'tax')::numeric),0) AS tax,
                  COALESCE(sum((f->>'slippage')::numeric),0) AS slippage
                FROM research_run_artifacts a CROSS JOIN LATERAL
                  jsonb_array_elements(a.payload::jsonb->'fills') f
                WHERE a.run_id=:run_id AND a.stage='fills'
                  AND left(f->>'filled_at',10) BETWEEN :start AND :end
            """),
                        {"run_id": run_id, "start": lo.isoformat(), "end": hi.isoformat()},
                    )
                )
                .mappings()
                .one()
            )
            metrics["fills"] = {
                k: str(v) if isinstance(v, Decimal) else v for k, v in fills.items()
            }
            shortfall = (await session.execute(text("""
                WITH ledgers AS (
                  SELECT decision_id,payload::jsonb->>'business_date' AS day,
                  (payload::jsonb->'ledger'->>'fill_shortfall')::numeric AS cumulative,
                  lag((payload::jsonb->'ledger'->>'fill_shortfall')::numeric,1,0)
                    OVER (ORDER BY sequence) AS previous
                  FROM research_run_artifacts WHERE run_id=:run_id AND stage='ledger'
                ), selected AS (SELECT * FROM ledgers WHERE day BETWEEN :start AND :end),
                plans AS (
                  SELECT COALESCE(sum(abs((i->>'estimated_value')::numeric)),0) AS requested
                  FROM research_run_artifacts a JOIN selected s USING(decision_id)
                  CROSS JOIN LATERAL jsonb_array_elements(a.payload::jsonb->'instructions') i
                  WHERE a.run_id=:run_id AND a.stage='rebalance_plan'
                )
                SELECT COALESCE(sum(cumulative-previous),0) AS shortfall,
                  (SELECT requested FROM plans) AS requested_notional FROM selected
            """), {"run_id": run_id, "start": lo.isoformat(), "end": hi.isoformat()})).mappings().one()
            reasons = (await session.execute(text("""
                SELECT COALESCE(o->>'reject_reason',o->>'status','unknown') AS reason,count(*) AS count
                FROM research_run_artifacts a CROSS JOIN LATERAL jsonb_array_elements(a.payload::jsonb->'orders') o
                WHERE a.run_id=:run_id AND a.stage='orders' AND
                  a.payload::jsonb->>'business_date' BETWEEN :start AND :end
                GROUP BY 1 ORDER BY 2 DESC LIMIT 30
            """), {"run_id": run_id, "start": lo.isoformat(), "end": hi.isoformat()})).mappings().all()
            denominator = shortfall["requested_notional"]
            metrics["shortfall"] = {
                "amount": str(shortfall["shortfall"]), "requested_notional": str(denominator),
                "ratio": float(shortfall["shortfall"] / denominator) if denominator else None,
                "axis": "business_date of matching decision ledger/rebalance plan; fills above use actual filled_at",
                "denominator_definition": "sum(abs(rebalance instruction estimated_value)); includes rejected and partial orders",
                "order_reasons": [dict(r) for r in reasons],
                "limitation": "shortfall is modeled unfilled value, not PnL, realized slippage or capacity limit",
            }
            reports.append(metrics)
        boundaries = (
            (
                await session.execute(
                    text("""
            SELECT min(payload::jsonb->>'business_date') AS first_decision,
            max(payload::jsonb->>'business_date') AS last_decision
            FROM research_run_artifacts WHERE run_id=:run_id AND stage='universe'
        """),
                    {"run_id": run_id},
                )
            )
            .mappings()
            .one()
        )
        screen = await session.scalar(
            text("""
            SELECT CASE WHEN octet_length((result::jsonb->'factor_screen')::text)<=65536
              THEN result::jsonb->'factor_screen' END FROM research_runs WHERE run_id=:run_id
        """),
            {"run_id": run_id},
        )
    output = {
        "schema_version": "diagnostics_v1",
        "run_id": run_id,
        "provenance": {k: frozen[k] for k in ("status", "manifest_checksum", "result_checksum")},
        "period": reports[0],
        "years": reports[1:],
        "decision_boundaries": dict(boundaries),
        "performance_window": manifest.get("parameters", {}).get("research_window"),
        "factor_quality": {
            "status": "existing_evidence" if screen else "unsupported",
            "evidence": screen,
        },
        "definitions": {
            "annualized_return": "research pipeline: (end/anchor) ** (252 / daily_return_count) - 1; zero for no return observations",
            "annualized_return_calendar": "secondary: (end/anchor) ** (365.25 / elapsed_calendar_days) - 1; zero for zero elapsed days",
            "sharpe": "rf=0, sample standard deviation ddof=1, sqrt(252)",
            "drawdown": "positive loss fraction; peak restarts at interval anchor",
            "fees": "fills selected by actual filled_at; slippage is model cost, not observed execution residual",
            "warmup": "equity alone cannot identify feature warmup; explicit window contract required",
        },
        "unsupported": [
            "new_rolling_ic",
            "industry_neutral_alpha",
            "order_book_impact",
            "break_even_cost",
        ],
        "warnings": [
            "现金期与尾段估值可能来自全发布日历;不自动等同实验窗口",
            "事后分段是敏感性分析,不是新 OOS",
            "累计fill_shortfall不是盈亏或实测滑点",
        ],
    }
    output["markdown"] = render_interval(output)
    return output


def render_interval(report: dict[str, Any]) -> str:
    rows = ["| 区间 | 净收益 | 年化 | 回撤 |", "|---|---:|---:|---:|"]
    for p in [report["period"], *report["years"]]:
        if p["status"] == "completed":
            rows.append(
                f"| {p['observed_start']}-{p['observed_end']} | {p['net_return']:.2%} | "
                f"{p['annualized_return']:.2%} | {p['max_drawdown']:.2%} |"
            )
        else:
            rows.append(f"| {p['reason']} | 缺证据 | 缺证据 | 缺证据 |")
    return "\n".join(rows)


def differences(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            d
            for key in sorted(left.keys() | right.keys())
            for d in differences(left.get(key), right.get(key), f"{path}.{key}".strip("."))
        ]
    def bounded(value: Any) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) <= 2048:
            return value
        return {"checksum": stable_checksum(value), "bytes": len(encoded.encode("utf-8")),
                "preview": encoded[:200], "notice": "bounded difference; frozen source retained"}
    return [{"field": path, "baseline": bounded(left), "comparison": bounded(right)}]


def semantic_policy(value: Any) -> Any:
    """说明文字不改变执行规则;原文仍保留在冻结manifest与校验和中。"""
    if isinstance(value, dict):
        return {key: semantic_policy(item) for key, item in value.items() if key != "rationale"}
    if isinstance(value, list):
        return [semantic_policy(item) for item in value]
    return value


async def compare_runs(
    session: AsyncSession,
    *,
    run_ids: list[str],
    allowed_differences: list[str] | None = None,
) -> dict[str, Any]:
    if not 2 <= len(run_ids) <= 6 or len(set(run_ids)) != len(run_ids):
        raise ValueError("compare requires 2-6 distinct runs")
    allowed = allowed_differences or []
    valid = {
        "datasets",
        "factors",
        "schedule",
        "capital",
        "strategy",
        "portfolio",
        "risk",
        "fee",
        "benchmark",
        "code",
    }
    if set(allowed) - valid:
        raise ValueError("unknown comparison dimension")
    inputs = []
    async with bounded_read(session):
        for run_id in run_ids:
            frozen = await frozen_input(session, run_id)
            m = frozen["manifest"]
            explanation = await explain(session, run_id=run_id)
            spec = dict(m["strategy_spec"])
            for k in [
                "name",
                "description",
                "validation_plan",
                "compatibility",
                "strategy_id",
                "execution_model",
                "portfolio_policy",
                "risk_exit_policy",
            ]:
                spec.pop(k, None)
            policies = explanation["effective_policies"]
            parameters = dict(m.get("parameters", {}))
            schedule = {k: parameters.pop(k) for k in ("decision_schedule", "rebalance_frequency", "research_window") if k in parameters}
            fee_version = parameters.pop("fee_policy_version", "legacy_instrument_first")
            for key in ("stress_binding", "validation_binding"):
                parameters.pop(key, None)
            inputs.append(
                {
                    "run_id": run_id,
                    "frozen": frozen,
                    "dimensions": {
                        "datasets": m.get("dataset_releases"),
                        "factors": {
                            "series": m.get("factor_series", []),
                            "snapshots": m.get("factor_snapshots", []),
                        },
                        "schedule": schedule,
                        "capital": m.get("initial_capital"),
                        "strategy": {"spec": semantic_policy(spec), "parameters": parameters},
                        "portfolio": semantic_policy(policies.get("portfolio_constraints")),
                        "risk": semantic_policy(policies.get("risk_exit_policy")),
                        "fee": {"model": policies.get("execution_model"), "policy_version": fee_version},
                        "benchmark": m.get("benchmark_config"),
                        "code": m.get("code_version"),
                    },
                    "gaps": explanation["gaps"],
                }
            )
    comparisons = []
    for other in inputs[1:]:
        delta = differences(inputs[0]["dimensions"], other["dimensions"])
        changed = sorted({d["field"].split(".")[0] for d in delta})
        invalid = any(i["frozen"]["status"] != "completed" for i in [inputs[0], other])
        missing = any(i["dimensions"]["portfolio"] is None for i in [inputs[0], other])
        # 数据变化可报告为显式对照,永远不能称单变量策略消融。
        status = (
            "incomparable"
            if invalid or missing
            else (
                "confounded"
                if "datasets" in changed or set(changed) - set(allowed)
                else "controlled_difference"
            )
        )
        comparisons.append(
            {
                "run_id": other["run_id"],
                "status": status,
                "changed_dimensions": changed,
                "differences": delta[:100],
                "differences_total": len(delta),
                "reason": "missing_or_incomplete_evidence"
                if invalid or missing
                else "data_or_undeclared_differences"
                if status == "confounded"
                else "declared_control",
                "provenance": {
                    k: other["frozen"][k] for k in ["manifest_checksum", "result_checksum"]
                },
            }
        )
    return {
        "baseline": run_ids[0],
        "allowed_differences": allowed,
        "comparisons": comparisons,
        "notice": "控制实验只报告绩效差异,不证明因果归因;指数基准不能证明微盘alpha",
    }


async def decision_projection(
    session: AsyncSession,
    *,
    run_id: str,
    decision_id: str | None = None,
    symbol: str | None = None,
    stage: str = "features",
    fields: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    chosen = fields if fields is not None else _FIELDS
    if stage not in _KEYS or not chosen or set(chosen) - set(_FIELDS):
        raise ValueError("invalid_projection: unknown stage/field")
    if not 1 <= limit <= 200 or not 0 <= offset <= 100000 or (symbol and len(symbol) > 32):
        raise ValueError("invalid_projection_bounds")
    pairs = ", ".join(f"'{key}',e.value->'{key}'" for key in chosen)
    base = f"""
      SELECT a.decision_id,a.stage,a.sequence,a.trace_id,a.checksum,e.ordinality,
      jsonb_strip_nulls(jsonb_build_object({pairs})) AS item
      FROM research_run_artifacts a CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.payload::jsonb->({_key_case}))='array'
        THEN a.payload::jsonb->({_key_case}) ELSE '[]'::jsonb END) WITH ORDINALITY e(value,ordinality)
      WHERE a.run_id=:run_id AND a.stage=:stage
        AND (CAST(:decision_id AS text) IS NULL OR a.decision_id=:decision_id)
        AND (CAST(:symbol AS text) IS NULL OR e.value->>'symbol'=:symbol)
    """
    args = {
        "run_id": run_id,
        "stage": stage,
        "decision_id": decision_id,
        "symbol": symbol,
        "limit": limit,
        "offset": offset,
    }
    async with bounded_read(session):
        await frozen_input(session, run_id)
        total = await session.scalar(text(f"SELECT count(*) FROM ({base}) t"), args)
        page = f"{base} ORDER BY a.sequence,e.ordinality LIMIT :limit OFFSET :offset"
        size = await session.scalar(
            text(f"SELECT COALESCE(sum(octet_length(item::text)),0) FROM ({page}) t"), args
        )
        if size > MAX_BYTES:
            raise ValueError("payload_too_large: projection > 256KiB; reduce fields/limit")
        rows = (await session.execute(text(page), args)).mappings().all()
    return {
        "schema_version": "projection_v1",
        "run_id": run_id,
        "stage": stage,
        "items": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
        "fields": chosen,
        "definitions": {
            "source": "frozen artifact SQL projection",
            "checksum": "original artifact checksum; item is projection",
            "trace_id": "original execution trace",
        },
    }
