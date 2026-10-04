"""执行预注册的12个回顾研究对照;仅调用研究MCP与隔离队列worker。"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

from scripts.research_evidence_archive import local_round_dir
from sqlalchemy import text

from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
    default_store_factory,
)
from finboard_backtest.background_jobs.registry import JobExecutorRegistry
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.research_run.signal_engine import build_signal_engine_adapter_factory
from finboard_mcp.context import app_lifespan
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.server import build_mcp_server
from finboard_mcp.tools import strategies
from finboard_mcp.tools.diagnostics import call
from finboard_mcp.tools.runs import queue_run

ROOT = Path(__file__).resolve().parents[1]
OUT = local_round_dir("2026-10-04-smallcap-v13")


def save(name: str, value: Any) -> None:
    (OUT / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def data(result: ToolEnvelope) -> dict[str, Any]:
    if result.status != "ok" or not isinstance(result.data, dict):
        raise RuntimeError(f"research tool refused: {result.error}")
    return result.data


async def main() -> None:
    frozen_source = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    source = frozen_source["manifest"]
    dates = [d for d in source["parameters"]["decision_schedule"]["dates"] if "2024-01-01" <= d < "2024-12-31"]
    release = json.loads((ROOT / "data_releases/a-share-cs-20260917-v5/manifest.json").read_text(encoding="utf-8"))
    warmup = release.get("start_date", "2015-01-01")
    variants = ["baseline", "ret5-only", "rsi-only", "bias-only", "macd-only", "macd-positive", "no-overlay"]
    save("preregistration", {"schema_version": "review505_v1", "registered_at": "2026-10-04",
        "baseline_run_id": source["run_id"], "baseline_checksum": frozen_source["manifest_checksum"],
        "window": {"start": "2024-01-01", "end": "2024-12-31", "initial_capital_reset": "300000"},
        "classification": "previously used retrospective sensitivity; not fresh OOS",
        "variants": variants, "cost_multipliers": [2], "slippage_bps": [10, 20], "capitals": ["100000", "500000"],
        "delay_2bar": "unsupported; no run", "run_budget": 12, "automatic_retry_budget": 3,
        "fee_policy_version": "explicit_overrides_v1", "cost_1_overrides": {"commission_rate": .0003, "minimum_commission": 5, "sell_tax_rate": .0005},
        "stop_rule": "record every terminal outcome; no tuning or replacement candidates",
        "historical_reuse": "only v13 completed on v5 in source strategy history; v4 ablations confounded",
        "limits": ["full-history robustness cannot be inferred from one-year reset window", "no fresh future data/factor coverage", "no impact/delay/live validation"]})
    records: list[dict[str, Any]] = []
    async with app_lifespan(build_mcp_server()) as app:
        async def run_jobs(targets: list[dict[str, Any]]) -> None:
            ids = [r["job_id"] for r in targets]
            # 只移动本轮queued研究job到独立研究队列;不领取用户其他任务。
            async with app.session_maker() as session:
                await session.execute(text("UPDATE background_jobs SET queue='review505' WHERE job_id=ANY(:ids) AND kind='research_run' AND status='queued'"), {"ids": ids})
                await session.commit()
            registry = JobExecutorRegistry()
            registry.register("research_run", ResearchRunExecutor(session_maker=app.session_maker,
                store_factory=default_store_factory, adapter_factory=build_signal_engine_adapter_factory(app.session_maker, settings_factory=lambda: app.settings)))
            worker = BackgroundWorker(engine=app.engine, session_maker=app.session_maker, registry=registry,
                config=WorkerConfig(worker_id="review505-v1", poll_interval_seconds=1, max_concurrent=1,
                    lease_timeout_seconds=120, heartbeat_interval_seconds=15, queues=["review505"],
                    kind_concurrency={"research_run": 1}, shutdown_grace_seconds=10))
            task = asyncio.create_task(worker.run())
            try:
                async with asyncio.timeout(10800):
                    while True:
                        async with app.session_maker() as session:
                            rows = [dict(r) for r in (await session.execute(text("SELECT job_id,status,phase,progress_done,progress_total,error_code,error_summary,result_ref,timing FROM background_jobs WHERE job_id=ANY(:ids)"), {"ids": ids})).mappings()]
                        save("job-status", rows)
                        if all(r["status"] in {"succeeded", "failed", "cancelled"} for r in rows) and len(rows) == len(ids):
                            break
                        await asyncio.sleep(10)
            finally:
                worker.request_stop()
                await task
            for target in targets:
                result = data(await call(app, "interval", {"run_id": target["run_id"], "start": "2024-01-01", "end": "2024-12-31", "yearly": False}))
                save(f"result-{target['label']}", result)
                print(f"{target['label']}: {result['provenance']['status']}", flush=True)

        for label in variants:
            spec = copy.deepcopy(source["strategy_spec"])
            strategy_id, version = spec["strategy_id"], source["strategy_version"]
            if label != "baseline":
                strategy_id = f"review505-{label}-v1"
                spec.update(strategy_id=strategy_id, name=f"#505 回顾对照 {label}", description=f"研究对照,非可投资/晋级结论。原v13冻结图;预注册2024窗口;变量={label}。")
                if label.endswith("-only"):
                    index = {"ret5-only": 0, "rsi-only": 1, "bias-only": 2, "macd-only": 3}[label]
                    for node in spec["feature_graph"]["nodes"]:
                        if node["operator"] == "weighted_sum":
                            node["weights"] = [1.0 if j == index else 0.0 for j in range(4)]
                elif label == "macd-positive":
                    for node in spec["feature_graph"]["nodes"]:
                        if node["node_id"] == "macd_rank":
                            node["inputs"] = ["macd_raw"]
                else:
                    for rule in spec["risk_exit_policy"]["rules"]:
                        if rule["rule_type"] in {"portfolio_drawdown_derisk", "cooldown"}:
                            rule["enabled"] = False
                existing = await strategies.strategy_version_get(app, strategy_id=strategy_id, version=1)
                if existing.status != "ok":
                    draft = data(await strategies.strategy_draft_create(app, spec=spec))
                    data(await strategies.strategy_publish(app, strategy_id=strategy_id, version=draft["version"], expected_version=draft["version"]))
                version = 1
            payload = {"idempotency_key": f"review505-v1-{label}", "strategy_id": strategy_id, "strategy_version": version,
                "dataset_release_ids": [r["artifact_id"] for r in source["dataset_releases"]],
                "factor_series_ids": [r["artifact_id"] for r in source["factor_series"]],
                "parameters": {"decision_schedule": {"kind": "custom", "dates": dates},
                    "fee_policy_version": "explicit_overrides_v1", "research_window": {"warmup_start": warmup,
                        "decision_start": "2024-01-01", "decision_end": dates[-1], "valuation_end": "2024-12-31"}},
                "portfolio_config": {"max_risk_contribution": 1}, "fee_config": {"commission_rate": .0003, "minimum_commission": 5, "sell_tax_rate": .0005},
                "benchmark_config": {"symbol": "000852.SH"}, "initial_capital": "300000",
                "code_version": "review505-v1-fee-policy-v1", "requested_by": "agent:mcp"}
            ack = data(await queue_run(app, payload=payload))
            records.append({"label": label, **ack, "payload": payload})
            save("queued", records)
        await run_jobs(records)
        baseline = records[0]
        stress_args = {"baseline_run_id": baseline["run_id"], "plan_key": "review505-v1-stress", "operation": "queue",
                       "cost_multipliers": [2], "slippage_bps": [10, 20], "capitals": ["100000", "500000"], "execution_delay_bars": [2]}
        stress = data(await call(app, "stress", stress_args))
        save("stress-queue", stress)
        targets = [r for r in stress["probes"] if r.get("job_id")]
        await run_jobs(targets)
        save("stress-final", data(await call(app, "stress", {**stress_args, "operation": "get"})))
        for target in [*records[1:], *targets]:
            allowed = ["risk"] if target["label"] == "no-overlay" else ["strategy"] if target in records else ["capital"] if target["label"].startswith("capital_") else ["fee"]
            save(f"compare-{target['label']}", data(await call(app, "compare", {"run_ids": [baseline["run_id"], target["run_id"]], "allowed_differences": allowed})))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
