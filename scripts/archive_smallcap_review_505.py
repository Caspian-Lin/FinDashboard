"""仅有界读取#505原产物/本轮任务,追加数据对账与复现证据;不启动研究。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.research_evidence_archive import local_round_dir
from sqlalchemy import text

from finboard_app.config import load_settings
from finboard_app.research_diagnostics import bounded_read, compare_runs, frozen_input
from finboard_app.spec_validation import preflight_frozen_policies
from finboard_backtest.research_run.contracts import manifest_from_json
from finboard_backtest.validation.contracts import derive_oos_outcome, describe_final_test_state
from finboard_persistence import create_async_engine, session_factory
from finboard_persistence.validation_repo import (
    ResearchExperimentRepository,
    ResearchTrialRepository,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = local_round_dir("2026-10-04-smallcap-v13")


def save(name: str, value: Any) -> None:
    (OUT / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def release_diff() -> None:
    releases = [json.loads((ROOT / "data_releases" / name / "manifest.json").read_text(encoding="utf-8"))
                for name in ("a-share-cs-20260908-v4", "a-share-cs-20260917-v5")]
    indices = [{i["code"]: i for i in r["instruments"]} for r in releases]
    common = indices[0].keys() & indices[1].keys()
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for symbol in sorted(common):
        for field in indices[0][symbol].keys() | indices[1][symbol].keys():
            if indices[0][symbol].get(field) != indices[1][symbol].get(field):
                counts[field] += 1
                examples.setdefault(field, [])
                if len(examples[field]) < 10:
                    examples[field].append(symbol)
    projection = [{**{k: r[k] for k in ("release_id", "release_checksum", "source", "adjustment", "period",
                    "start_date", "end_date", "fields", "availability_rules", "code_version", "known_limitations", "quality_status")},
                   "instrument_counts": dict(Counter(i["instrument_type"] for i in r["instruments"])),
                   "quality_report": {k: v for k, v in r["quality_report"].items() if k != "source_by_instrument"}}
                  for r in releases]
    save("release-diff", {"releases": projection, "common_instruments": len(common),
        "added": sorted(indices[1].keys() - indices[0].keys()), "removed": sorted(indices[0].keys() - indices[1].keys()),
        "changed_fields_counts": dict(counts), "changed_fields_examples": examples,
        "stock_parquet_checksum_same_count": sum(indices[0][s]["artifact_checksum"] == indices[1][s]["artifact_checksum"] for s in common if indices[0][s]["instrument_type"] == "stock"),
        "evidence_level": "frozen per-instrument manifest checksums; no independent full-file rehash in this comparison",
        "attribution": "same adjustment/PIT declarations do not make runs comparable when benchmark/factors/calendar/code differ"})


def producer_warnings() -> None:
    records = []
    for name in [".review505-execution.log", ".review505-window-replay.log"]:
        path = ROOT / name
        if not path.exists():
            continue
        groups: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "research_run.universe_filter_degraded" not in line:
                continue
            code = line.split("code=", 1)[1].split(" ", 1)[0]
            item = groups.setdefault(code, {"count": 0, "first_observation": line, "last_observation": line})
            item["count"] += 1
            item["last_observation"] = line
        records.append({"source_log": name, "log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "warnings": groups})
    save("producer-warning-evidence", {"scope": "this retrospective execution and implementation replay, not an assertion about original v13 stdout", "records": records})


async def archive() -> None:
    release_diff()
    producer_warnings()
    queued = json.loads((OUT / "queued.json").read_text(encoding="utf-8"))
    stress_file = OUT / "stress-queue.json"
    if stress_file.exists():
        queued += [r for r in json.loads(stress_file.read_text(encoding="utf-8"))["probes"] if r.get("job_id")]
    engine = create_async_engine(load_settings().db_url)
    try:
        async with session_factory(engine)() as session, bounded_read(session):
            jobs = [dict(r) for r in (await session.execute(text("""
                SELECT job_id,kind,queue,status,attempt,max_attempts,phase,result_ref,error_code,error_summary,
                  created_at,started_at,finished_at,timing FROM background_jobs WHERE job_id=ANY(:ids)
                ORDER BY created_at,job_id
            """), {"ids": [r["job_id"] for r in queued]})).mappings()]
            save("attempt-ledger", jobs)
            sources = []
            baseline = queued[0]["run_id"]
            comparisons = []
            for target in queued:
                frozen = await frozen_input(session, target["run_id"])
                if frozen["status"] == "completed":
                    metrics = (await session.execute(text("""
                        SELECT (result::jsonb - 'equity_curve') AS metrics FROM research_runs
                        WHERE run_id=:id AND octet_length((result::jsonb-'equity_curve')::text)<=262144
                    """), {"id": target["run_id"]})).scalar_one()
                    sources.append({"label": target["label"], **frozen, "metrics": metrics})
                    if target["run_id"] != baseline:
                        allowed = ["risk"] if target["label"] == "no-overlay" else ["strategy"] if target["label"] in {"ret5-only", "rsi-only", "bias-only", "macd-only", "macd-positive"} else ["capital"] if target["label"].startswith("capital_") else ["fee"]
                        comparisons.append({"label": target["label"], **await compare_runs(session,
                            run_ids=[baseline, target["run_id"]], allowed_differences=allowed)})
            save("executed-sources", sources)
            save("executed-comparisons", comparisons)
            replay_path = OUT / "window-repair-queued.json"
            if replay_path.exists():
                repaired = []
                for target in json.loads(replay_path.read_text(encoding="utf-8")):
                    frozen = await frozen_input(session, target["run_id"])
                    metrics = (await session.execute(text("SELECT (result::jsonb-'equity_curve') AS metrics FROM research_runs WHERE run_id=:id AND octet_length((result::jsonb-'equity_curve')::text)<=262144"), {"id": target["run_id"]})).scalar_one_or_none()
                    repaired.append({"label": target["label"], **frozen, "metrics": metrics})
                save("window-repair-native", repaired)
            source = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
            await asyncio.to_thread(preflight_frozen_policies, manifest_from_json(source["manifest"]))
            save("preflight", {"baseline": source["manifest"]["run_id"], "manifest_checksum": source["manifest_checksum"],
                               "portfolio_policy_gate": "passed", "runtime_hard_constraints": "remain fail-closed"})
            exp = await ResearchExperimentRepository(session).get("bc9fc5af85e84173")
            assert exp is not None
            trials = await ResearchTrialRepository(session).list_by_experiment(exp.experiment_id)
            save("carrier-outcome", {"experiment_id": exp.experiment_id, "version_checksum": exp.version_stamp.checksum(),
                 "version_stamp": exp.version_stamp.as_dict(), "status": exp.status.value,
                 "final_test_unsealed": exp.final_test_unsealed, "final_test_state": describe_final_test_state(exp.final_test_unsealed),
                 "oos_outcome": derive_oos_outcome(exp, trials).value,
                 "trial_ids": [t.trial_id for t in trials], "formal_spec_equivalence": False})
        modules = ["packages/finboard-backtest/src/finboard_backtest/research_run/portfolio_pipeline.py",
                   "packages/finboard-backtest/src/finboard_backtest/research_run/signal_engine.py",
                   "packages/finboard-backtest/src/finboard_backtest/research_run/window.py"]
        save("reproduction-code", {"code_version_label": "review505-v1-fee-policy-v1", "files_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in modules},
            "notice": "development branch evidence; hashes captured for reproduction, not a claim of an immutable deployed commit"})
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(archive())
