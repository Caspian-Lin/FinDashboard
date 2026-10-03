"""Read-side explanation of frozen inputs and bounded decision evidence (#499)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from sqlalchemy import literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.factors.predefined.registry import (
    get_predefined_factor,
    predefined_factor_commit,
)
from finboard_backtest.research_run.config_overrides import (
    merge_fee_overrides,
    merge_risk_exit_policy,
    section_overrides,
)
from finboard_backtest.research_run.contracts import manifest_from_json
from finboard_backtest.research_run.portfolio_pipeline import _constraints_from_manifest
from finboard_persistence.models import ResearchFactorSeriesModel, ResearchRunModel
from finboard_persistence.strategy_spec_repo import ResearchStrategySpecRepository


def graph_direction(nodes: list[dict[str, Any]], output: str, source: str) -> str:
    """Derivative sign through monotone transforms; ambiguous paths stay unknown."""
    by_id = {
        n["node_id"]: n for n in nodes if isinstance(n, dict) and isinstance(n.get("node_id"), str)
    }

    def walk(key: str, seen: frozenset[str]) -> int | None:
        if key in seen or key not in by_id:
            return None
        n = by_id[key]
        if n.get("source") == source:
            return 1
        inputs = n.get("inputs", [])
        signs = [walk(i, seen | {key}) for i in inputs]
        op = n.get("operator")
        if not inputs:
            return 0
        if op in {"identity", "negate", "cross_section_rank", "winsorize"} and len(signs) == 1:
            return None if signs[0] is None else signs[0] * (-1 if op == "negate" else 1)
        if op == "weighted_sum":
            if len(n.get("weights", [])) != len(signs):
                return None
            weighted = [
                None if s is None else s * (1 if w > 0 else -1 if w < 0 else 0)
                for s, w in zip(signs, n.get("weights", []), strict=True)
            ]
            nonzero = set(weighted) - {0}
            return next(iter(nonzero)) if len(nonzero) == 1 else 0 if not nonzero else None
        return None

    sign = walk(output, frozenset())
    return {1: "原值越高,输出分数越高", -1: "原值越低,输出分数越高", 0: "不影响此输出"}.get(
        sign if sign is not None else 2, "方向不能由单调路径确定,需查看完整规则"
    )


async def explain(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    strategy_id: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    gaps: list[str] = []
    manifest: dict[str, Any] | None = None
    if run_id:
        row: Any = (
            await session.execute(
                select(
                    literal_column(
                        "CASE WHEN octet_length(manifest::text)<=1048576 THEN manifest ELSE NULL END"
                    ).label("manifest"),
                    ResearchRunModel.manifest_checksum,
                    ResearchRunModel.status,
                    literal_column("result::jsonb->>'execution_mode'").label("execution_mode"),
                ).where(ResearchRunModel.run_id == run_id)
            )
        ).first()
        if row is None:
            raise LookupError("研究运行不存在")
        if row.manifest is None:
            raise ValueError("冻结输入超出说明书载荷上限(1MiB),请缩小引用范围")
        manifest = row.manifest
        spec = dict(manifest.get("strategy_spec", {}))
        version = manifest.get("strategy_version")
        provenance = {
            "run_id": run_id,
            "manifest_checksum": row.manifest_checksum,
            "strategy_checksum": manifest.get("strategy_spec_checksum"),
            "version": version,
            "source": "frozen_manifest",
            "status": row.status,
            "execution_mode": row.execution_mode,
        }
    elif strategy_id and version:
        spec_row = await ResearchStrategySpecRepository(session).get_version(strategy_id, version)
        if spec_row is None:
            raise LookupError("指定策略版本不存在")
        spec = dict(spec_row.payload)
        provenance = {
            "strategy_id": strategy_id,
            "version": version,
            "strategy_checksum": spec_row.checksum,
            "source": "versioned_spec",
            "status": spec_row.status,
        }
        gaps.append("未指定运行,仅解释此版本规则,因子参数与运行覆盖尚未冻结")
    else:
        raise ValueError("须指定 run_id 或 strategy_id + version,禁止默认最新版本")
    if not spec:
        gaps.append("历史记录缺少冻结策略,无法解释")
    policies: dict[str, Any] = {}
    if manifest:
        try:
            frozen = manifest_from_json(manifest)
            policies = {
                "portfolio_constraints": asdict(_constraints_from_manifest(frozen)),
                "risk_exit_policy": merge_risk_exit_policy(
                    frozen.strategy_spec.risk_exit_policy, section_overrides(frozen.risk_config)
                ).model_dump(mode="json"),
                "execution_model": merge_fee_overrides(
                    frozen.strategy_spec.execution_model, section_overrides(frozen.fee_config)
                ).model_dump(mode="json"),
            }
        except (ValueError, KeyError, TypeError) as exc:
            gaps.append(f"历史消费契约无法解析,覆盖生效值未知: {type(exc).__name__}")
    graph = spec.get("feature_graph")
    if not isinstance(graph, dict):
        graph = {}
        gaps.append("历史特征图缺少可识别结构")
    nodes = [
        n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and isinstance(n.get("node_id"), str)
    ]
    outputs = [o for o in (graph.get("outputs") or []) if isinstance(o, str)]
    references = [
        r
        for r in ((manifest or {}).get("factor_series") or [])
        if isinstance(r, dict)
        and isinstance(r.get("artifact_id"), str)
        and isinstance(r.get("checksum"), str)
    ]
    f = ResearchFactorSeriesModel
    series_rows = (
        (
            await session.execute(
                select(
                    f.series_id,
                    f.code_artifact,
                    f.code_commit,
                    f.content_checksum,
                    literal_column(
                        "CASE WHEN octet_length(params::text)<=16384 THEN params ELSE NULL END"
                    ).label("params"),
                    f.kind,
                ).where(f.series_id.in_([r["artifact_id"] for r in references]))
            )
        )
        .mappings()
        .all()
        if references
        else []
    )
    series_by_id = {r["series_id"]: r for r in series_rows}
    factors: list[dict[str, Any]] = []
    for node in nodes:
        name = node.get("source")
        if not name or node.get("kind") not in {"factor", "risk_factor"}:
            continue
        sources: list[dict[str, Any]] = []
        for ref in references:
            series = series_by_id.get(ref["artifact_id"])
            if series and series["code_artifact"] in {
                name,
                name.removeprefix("p_"),
                name.removeprefix("u_"),
            }:
                sources.append(
                    {
                        **dict(series),
                        "integrity": "matched"
                        if series["content_checksum"] == ref["checksum"]
                        else "checksum_mismatch",
                    }
                )
        info: dict[str, Any] = {
            "name": name,
            "node_id": node["node_id"],
            "sources": sources,
            "parameters": sources[0]["params"]
            if len(sources) == 1 and sources[0]["integrity"] == "matched"
            else None,
            "formula": None,
            "unit": "缺少可核验单位证据",
            "raw_direction": "未知",
            "effective_direction": {o: graph_direction(nodes, o, name) for o in outputs},
            "evidence": "missing",
        }
        if name.startswith("p_") and len(sources) == 1 and sources[0]["integrity"] == "matched":
            try:
                definition = get_predefined_factor(name.removeprefix("p_"))
                if predefined_factor_commit(definition.name) == sources[0]["code_commit"]:
                    info.update(
                        formula=definition.title,
                        window=definition.window,
                        implementation_version=definition.implementation_version,
                        raw_direction=definition.direction.value,
                        dependencies=definition.data_dependencies,
                        evidence="commit_matched_definition",
                    )
                    if (
                        definition.name.startswith(("return_", "rsi_", "bias_"))
                        or definition.name == "macd_hist_norm"
                    ):
                        info["unit"] = (
                            "RSI 点(0-100)"
                            if definition.name.startswith("rsi_")
                            else "比例(1 = 100%);详见公式"
                        )
            except KeyError:
                pass
        if info["parameters"] is None:
            gaps.append(f"{name}: 冻结参数缺记录、多源或超出16KiB上限,不能推断参数")
        if info["evidence"] == "missing":
            gaps.append(f"{name}: 历史公式/参数未完全核验,当前目录不能代替冻结定义")
        factors.append(info)
    return {
        "provenance": provenance,
        "spec": spec,
        "factors": factors,
        "effective_policies": policies,
        "overrides": {
            k: (manifest or {}).get(k, {})
            for k in ["portfolio_config", "risk_config", "fee_config"]
        },
        "schedule": (manifest or {}).get("parameters", {}),
        "gaps": gaps,
        "warnings": [
            "覆盖值按当前消费契约解析;历史版本是否实际执行须核对当时的账本与成交证据",
            "发布与运行完成仅表示流程状态,不代表 OOS 支持或可晋级",
            "风险规则在决策时点评估,按后续执行时点撮合;回撤触发阈值不是最大回撤保证",
            "交易对手身份不能由日线行情识别,经济机制必须作为待验证假设并附来源",
            "目标权重不等于实际成交,需查看约束、拒单、部分成交及费用证据",
        ],
        "mechanism_hypotheses": [
            {
                "claim": "反转信号可能补偿短期流动性供给,也可能是微盘暴露、样本选择或执行近似",
                "level": "external_hypothesis",
                "source": "https://www.nber.org/papers/w30917",
                "counterparty": "未验证;无账户身份数据",
                "failure": "交易成本、冲击、流动性收缩或方向漂移可能吞噬收益;外部研究不验证本策略",
            }
        ]
        if any(
            f["evidence"] == "commit_matched_definition"
            and (
                f["name"].startswith(("p_return_", "p_bias_", "p_rsi_"))
                or f["name"] == "p_macd_hist_norm"
            )
            and "原值越低" in str(f["effective_direction"])
            for f in factors
        )
        else [],
    }


_DECISIONS = text(
    """SELECT decision_id, payload::jsonb->>'business_date' AS business_date, payload::jsonb->>'decision_at' AS decision_at FROM research_run_artifacts WHERE run_id=:run_id AND stage='universe' ORDER BY sequence LIMIT :limit OFFSET :offset"""
)
_FIELDS = [
    "symbol",
    "feature_id",
    "value",
    "available_at",
    "source_artifact_ids",
    "score",
    "action",
    "rule_id",
    "rationale",
    "included",
    "reasons",
    "weight",
    "constraint",
    "passed",
    "before_value",
    "after_value",
    "limit",
    "reason",
    "status",
    "quantity",
    "filled_quantity",
    "price",
    "side",
    "commission",
    "tax",
    "slippage",
    "execution_at",
    "order_id",
    "rule_type",
    "triggered",
    "target_weight",
    "research_order_id",
    "research_fill_id",
    "reject_reason",
    "filled_at",
    "metric",
    "threshold",
    "before_weight",
    "after_weight",
    "current_quantity",
    "target_quantity",
    "delta_quantity",
    "lot_size",
    "average_price",
    "market_price",
    "market_value",
    "position_side",
]
_KEYS = {
    "universe": "candidates",
    "features": "features",
    "signals": "signals",
    "targets_before_constraints": "targets",
    "constraints": "constraints",
    "targets_after_constraints": "targets",
    "risk_exits": "outcomes",
    "targets_after_risk": "targets",
    "rebalance_plan": "instructions",
    "orders": "orders",
    "fills": "fills",
    "ledger": "positions",
}
_key_case = (
    "CASE a.stage "
    + " ".join(f"WHEN '{stage}' THEN '{key}'" for stage, key in _KEYS.items())
    + " END"
)
_field_pairs = ", ".join(f"'{k}', e.value->'{k}'" for k in _FIELDS)
_EVIDENCE = text(f"""
WITH projected AS (
 SELECT a.stage, a.sequence, a.checksum, a.trace_id, e.ordinality,
 jsonb_strip_nulls(jsonb_build_object({_field_pairs})) AS item
 FROM research_run_artifacts a CROSS JOIN LATERAL jsonb_array_elements(
 CASE WHEN jsonb_typeof(a.payload::jsonb->({_key_case}))='array'
 THEN a.payload::jsonb->({_key_case}) ELSE '[]'::jsonb END
 ) WITH ORDINALITY e(value, ordinality)
 WHERE a.run_id=:run_id AND (CAST(:decision_id AS text) IS NULL OR a.decision_id=:decision_id)
 AND (CAST(:business_date AS text) IS NULL OR a.payload::jsonb->>'business_date'=:business_date)
 AND (e.value->>'symbol'=:symbol OR (a.stage IN ('constraints','risk_exits') AND e.value->>'symbol' IS NULL))
 ORDER BY a.sequence, e.ordinality LIMIT :limit OFFSET :offset
)
SELECT stage, checksum, trace_id,
CASE WHEN octet_length(item::text)<=16384 THEN item ELSE '{{"evidence_missing":"projected_item_too_large"}}'::jsonb END AS item
FROM projected ORDER BY sequence, ordinality
""")


async def decision_evidence(
    session: AsyncSession,
    *,
    run_id: str,
    symbol: str | None = None,
    decision_id: str | None = None,
    business_date: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
        raise ValueError("limit 1-200,offset 0-100000")
    if not await session.scalar(
        select(ResearchRunModel.run_id).where(ResearchRunModel.run_id == run_id)
    ):
        raise LookupError("研究运行不存在")
    params = {
        "run_id": run_id,
        "symbol": symbol,
        "decision_id": decision_id,
        "business_date": business_date,
        "limit": limit + 1,
        "offset": offset,
    }
    if symbol and (not (decision_id or business_date) or len(symbol) > 32):
        raise ValueError("标的下钻须指定 decision_id 或 business_date")
    async with asyncio.timeout(150):
        await session.execute(text("SET LOCAL statement_timeout = '120s'"))
        rows = (await session.execute(_EVIDENCE if symbol else _DECISIONS, params)).mappings().all()
    return {
        "run_id": run_id,
        "symbol": symbol,
        "items": [dict(r) for r in rows[:limit]],
        "has_more": len(rows) > limit,
        "limit": limit,
        "offset": offset,
        "notice": "只投影所选标的与组合级约束;空阶段可能缺记录,不据此猜测原因。目标不等于成交。",
    }
