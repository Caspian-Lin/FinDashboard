"""同参数实现修复复核,保留原运行;不新增研究假设或改变阈值。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
from datetime import UTC, datetime

from scripts.execute_smallcap_review_505 import OUT, ROOT, data, save
from sqlalchemy import text

from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
    default_store_factory,
)
from finboard_backtest.background_jobs.registry import JobExecutorRegistry
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.research_run.signal_engine import build_signal_engine_adapter_factory
from finboard_mcp.context import app_lifespan
from finboard_mcp.server import build_mcp_server
from finboard_mcp.tools.diagnostics import call
from finboard_mcp.tools.runs import queue_run


async def main() -> None:
    originals = json.loads((OUT / "queued.json").read_text(encoding="utf-8"))[:2]
    modules = ["packages/finboard-backtest/src/finboard_backtest/research_run/portfolio_pipeline.py",
               "packages/finboard-backtest/src/finboard_backtest/research_run/signal_engine.py",
               "packages/finboard-backtest/src/finboard_backtest/research_run/window.py"]
    save("window-repair-registration", {"registered_at": datetime.now(UTC).isoformat(),
        "reason": "first two source result summaries included valuation/benchmark outside research_window; selected-window reports retained",
        "classification": "same-parameter engineering reproduction; 12 unique research hypotheses unchanged",
        "additional_execution_budget": 2, "max_attempts_per_execution": 3, "no_parameter_search": True,
        "files_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in modules}})
    async with app_lifespan(build_mcp_server()) as app:
        records = []
        for old in originals:
            payload = copy.deepcopy(old["payload"])
            payload.update(idempotency_key=f"review505-window-final-v2-{old['label']}", code_version="review505-window-final-v2")
            ack = data(await queue_run(app, payload=payload))
            records.append({"label": old["label"], "original_run_id": old["run_id"], **ack, "payload": payload})
        save("window-repair-queued", records)
        ids = [r["job_id"] for r in records]
        async with app.session_maker() as session:
            await session.execute(text("UPDATE background_jobs SET queue='review505replay' WHERE job_id=ANY(:ids) AND kind='research_run' AND status='queued'"), {"ids": ids})
            await session.commit()
        registry = JobExecutorRegistry()
        registry.register("research_run", ResearchRunExecutor(session_maker=app.session_maker, store_factory=default_store_factory,
            adapter_factory=build_signal_engine_adapter_factory(app.session_maker, settings_factory=lambda: app.settings)))
        worker = BackgroundWorker(engine=app.engine, session_maker=app.session_maker, registry=registry,
            config=WorkerConfig(worker_id="review505-window-replay", max_concurrent=1, queues=["review505replay"],
                heartbeat_interval_seconds=15, lease_timeout_seconds=120, poll_interval_seconds=1,
                kind_concurrency={"research_run": 1}, shutdown_grace_seconds=10))
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(3600):
                while True:
                    async with app.session_maker() as session:
                        rows = [dict(r) for r in (await session.execute(text("SELECT job_id,status,attempt,error_code,error_summary,result_ref,timing FROM background_jobs WHERE job_id=ANY(:ids)"), {"ids": ids})).mappings()]
                    save("window-repair-jobs", rows)
                    if len(rows) == len(ids) and all(r["status"] in {"succeeded", "failed", "cancelled"} for r in rows):
                        break
                    await asyncio.sleep(10)
        finally:
            worker.request_stop()
            await task
        reports = []
        for r in records:
            result = data(await call(app, "interval", {"run_id": r["run_id"], "start": "2024-01-01", "end": "2024-12-31", "yearly": False}))
            original = json.loads((OUT / f"result-{r['label']}.json").read_text(encoding="utf-8"))
            fields = ["net_return", "max_drawdown", "final_equity", "fills", "shortfall"]
            reports.append({**r, "diagnostics": result, "same_selected_window": {k: result["period"].get(k) == original["period"].get(k) for k in fields}})
        save("window-repair-results", reports)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
