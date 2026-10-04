"""冻结基线的有限研究压力矩阵;计划、入队与证据查询显式分离 (#503)。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.research_diagnostics import bounded_read, frozen_input
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_app.spec_validation import (
    explicit_fee_base,
    preflight_frozen_policies,
    stress_fee_values,
)
from finboard_backtest.research_run.config_overrides import merge_fee_overrides, section_overrides
from finboard_backtest.research_run.contracts import (
    ResearchActorType,
    ResearchRunManifest,
    manifest_from_json,
    stable_checksum,
    to_json_value,
)
from finboard_persistence import BackgroundJobRepository, ResearchRunRepository
from finboard_persistence.models import ResearchRunModel
from finboard_shared.background_jobs import generate_background_job_id


def stress_manifests(
    source: ResearchRunManifest, *, cost_multipliers: list[float], slippage_bps: list[float],
    capitals: list[str], execution_delay_bars: list[int], plan_key: str,
) -> tuple[list[tuple[str, ResearchRunManifest]], list[dict[str, Any]]]:
    if not plan_key or len(plan_key) > 128:
        raise ValueError("stress_plan_key_required")
    if section_overrides(source.execution_config) or section_overrides(source.validation_config):
        raise ValueError("unsupported_dead_configuration_partition")
    count = len(cost_multipliers) + len(slippage_bps) + len(capitals) + len(execution_delay_bars)
    if not 1 <= count <= 24:
        raise ValueError("stress_budget_exceeded: 1-24 probes")
    execution = merge_fee_overrides(source.strategy_spec.execution_model, section_overrides(source.fee_config))
    variants: list[tuple[str, dict[str, object], Decimal]] = []
    unsupported: list[dict[str, Any]] = []
    for multiplier in cost_multipliers:
        variants.append((f"cost_x{multiplier}", {"cost_multiplier": multiplier}, source.initial_capital))
    for bps in slippage_bps:
        variants.append((f"slippage_{bps}bps", {"slippage_bps": bps}, source.initial_capital))
    for value in capitals:
        try:
            capital = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid_research_capital") from exc
        if not capital.is_finite() or not Decimal(100000) <= capital <= Decimal(500000):
            raise ValueError("research_capital_outside_100000_500000")
        variants.append((f"capital_{capital}", {}, capital))
    for delay in execution_delay_bars:
        if isinstance(delay, bool) or delay not in {1, 2}:
            raise ValueError("invalid_delay_probe")
        if delay == 1:
            variants.append(("delay_1bar", {"execution_delay_bars": 1}, source.initial_capital))
        else:
            unsupported.append({"label": "delay_2bar", "status": "unsupported",
                                "reason": "matching pipeline has no 2-bar execution delay; decision dates unchanged"})
    output = []
    for label, overrides, capital in variants:
        fee = {**source.fee_config, "overrides": {
            **explicit_fee_base(execution),
            **section_overrides(source.fee_config), **stress_fee_values(execution, overrides)}}
        identity = stable_checksum({"baseline": source.input_checksum, "plan_key": plan_key,
                                    "label": label, "fee": fee, "capital": str(capital)})
        child = replace(source, run_id=f"RR-{identity[:24]}", idempotency_key=f"stress:{identity}",
                        fee_config=fee, initial_capital=capital,
                        parameters={**source.parameters, "fee_policy_version": "explicit_overrides_v1", "stress_binding": {
                            "schema_version": "stress_v1", "baseline_run_id": source.run_id,
                            "baseline_input_checksum": source.input_checksum, "plan_key": plan_key,
                            "label": label, "overrides": to_json_value(overrides)}},
                        replay_of_run_id=None, replay_source_status=None)
        output.append((label, child))
    if len({label for label, _ in output}) != len(output):
        raise ValueError("duplicate_stress_probe")
    return output, unsupported


async def stress_matrix(
    session: AsyncSession, *, baseline_run_id: str, plan_key: str,
    operation: str = "plan", cost_multipliers: list[float] | None = None,
    slippage_bps: list[float] | None = None, capitals: list[str] | None = None,
    execution_delay_bars: list[int] | None = None, actor: str = "user:api",
) -> dict[str, Any]:
    if operation not in {"plan", "queue", "get"}:
        raise ValueError("invalid_stress_operation")
    async with bounded_read(session):
        baseline = await frozen_input(session, baseline_run_id)
        if baseline["status"] != "completed":
            raise ValueError("stress_requires_completed_baseline")
        source = manifest_from_json(baseline["manifest"])
        source = replace(source, requested_by=actor,
                         actor_type=ResearchActorType.AGENT if actor.startswith("agent:") else ResearchActorType.HUMAN)
        children, unsupported = stress_manifests(source, plan_key=plan_key,
            cost_multipliers=cost_multipliers or [], slippage_bps=slippage_bps or [],
            capitals=capitals or [], execution_delay_bars=execution_delay_bars or [])
        rows: list[dict[str, Any]] = []
        job_id: str | None
        if operation == "queue":
            # 任意档预检失败时整批不入队;不留下部分已提交矩阵。
            for _, child in children:
                await asyncio.to_thread(preflight_frozen_policies, child)
        for label, child in children:
            manifest_checksum, result_checksum = child.checksum, None
            if operation == "queue":
                record, _ = await SqlAlchemyResearchRunStore(ResearchRunRepository(session)).create_or_get(child)
                job, _ = await BackgroundJobRepository(session).create_or_get(
                    job_id=generate_background_job_id(), idempotency_key=child.idempotency_key,
                    kind="research_run", queue="research", status="queued", priority=0,
                    payload={"run_id": child.run_id}, payload_checksum=stable_checksum({"run_id": child.run_id}),
                    max_attempts=3, requested_by=actor)
                await session.execute(update(ResearchRunModel).where(ResearchRunModel.run_id == child.run_id).values(job_id=job.job_id))
                status, job_id = record.status.value, job.job_id
                manifest_checksum, result_checksum = record.manifest.checksum, record.result_checksum
            else:
                row = (await session.execute(text("""
                    SELECT status,job_id,manifest_checksum,result_checksum
                    FROM research_runs WHERE run_id=:run_id
                """), {"run_id": child.run_id})).first()
                status, job_id = (row.status, row.job_id) if row else ("planned", None)
                if row:
                    # manifest包含创建者;另一个只读通道查询时必须返回已存锚,
                    # 不能用当前查询者重建的计划checksum冒充原运行checksum。
                    manifest_checksum, result_checksum = row.manifest_checksum, row.result_checksum
            rows.append({"label": label, "run_id": child.run_id, "job_id": job_id, "status": status,
                         "manifest_checksum": manifest_checksum, "result_checksum": result_checksum,
                         "input_checksum": child.input_checksum,
                         "overrides": child.parameters["stress_binding"],
                         "fee": {"spec": child.strategy_spec.execution_model.model_dump(mode="json"),
                                 "override": section_overrides(child.fee_config), "effective":
                                 merge_fee_overrides(child.strategy_spec.execution_model, section_overrides(child.fee_config)).model_dump(mode="json")},
                         "capital": str(child.initial_capital)})
        if operation == "queue":
            await session.commit()
    return {"schema_version": "stress_v1", "baseline_run_id": baseline_run_id,
            "baseline_manifest_checksum": baseline["manifest_checksum"], "plan_key": plan_key,
            "operation": operation, "planned_count": len(rows) + len(unsupported),
            "comparison_control": "source uses explicit_overrides_v1" if source.parameters.get("fee_policy_version") == "explicit_overrides_v1" else "legacy baseline: execute cost_x1 control before attributing stress effects",
            "probes": rows + unsupported, "all_completed": not unsupported and all(r["status"] == "completed" for r in rows),
            "limitations": ["cost scales commission rate/minimum and sell tax; slippage stays fixed",
                            "capacity changes capital only; no order-book impact or exact capacity claim",
                            "completed is workflow state; compare inputs and metrics before accepting robustness",
                            "no break-even interpolation from discrete stress levels"]}
