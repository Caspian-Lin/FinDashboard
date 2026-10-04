"""只整理真实模型日志与可核验回执;文字关键错误须另做逐项人工式复核。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.research_evidence_archive import local_round_dir

OUT = local_round_dir("2026-10-04-agent-evaluation-504")


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]


def audit(batch: str) -> None:
    results = []
    for name in [f"{batch}-{i}" for i in (1, 2, 3)] + [f"{batch}-continued"]:
        events_file = OUT / f"{name}-events.jsonl"
        if not events_file.exists():
            continue
        events = records(events_file)
        texts = [e["part"]["text"] for e in events if e.get("type") == "text"]
        answer = texts[-1] if texts else ""
        (OUT / f"{name}-answer.md").write_text(answer, encoding="utf-8")
        stem = f"{batch}-interrupted" if name.endswith("continued") else name
        tools = records(OUT / f"{stem}-tools.jsonl")

        def called(tool: str, *, captured: list[dict[str, Any]] = tools, **wanted: Any) -> list[dict[str, Any]]:
            return [r for r in captured if r["tool"] == tool and all(r["arguments"].get(k) == v for k, v in wanted.items())]

        def ok(tool: str, **wanted: Any) -> bool:
            return any(r["response"]["status"] == "ok" for r in called(tool, **wanted))

        projections = called("finboard_decision_projection", run_id="RR-fixture-v13", decision_id="RR-fixture-D1")
        waits = called("finboard_job_wait", job_id="BJ-fixture")
        topics = called("finboard_topic_write", operation="append", topic_id="RT-fixture")
        receipts = {
            "baseline": (ok("finboard_run_get", run_id="RR-fixture-v13") or ok("finboard_report_run", run_id="RR-fixture-v13", view="summary")) and ok("finboard_memory_list") and ok("finboard_strategy_explain", run_id="RR-fixture-v13"),
            "confounded_comparison": ok("finboard_run_compare"),
            "carrier_not_supported": ok("finboard_validation_experiment_get", experiment_id="EXP-fixture-carrier"),
            "delay_unsupported": ok("finboard_research_stress") and not any(r["response"]["status"] == "ok" for r in called("finboard_run_queue")),
            "oversize_then_filtered_pages": any(r["response"].get("error", {}).get("kind") == "payload_too_large" for r in called("finboard_report_run", view="detail", decision_id="RR-fixture-D1")) and {0, 1}.issubset({r["arguments"]["offset"] for r in projections if r["response"]["status"] == "ok"}),
            "same_job_timeout_and_cache": any(r["response"].get("data", {}).get("completed") is False for r in waits) and any(r["response"].get("data", {}).get("result_ref") == "RR-fixture-cache" and r["response"]["data"].get("idempotent_cache_hit") is True for r in waits) and any(r["response"]["status"] == "ok" and any(ref.get("ref_id") == "RR-fixture-cache" for ref in r["arguments"]["payload"].get("source_refs", [])) for r in topics),
            "memory_source_check": ok("finboard_source_check") and ok("finboard_memory_list"),
            "planned_stress": ok("finboard_research_stress"),
            "service_metrics": ok("finboard_run_diagnostics", run_id="RR-fixture-v13"),
            "goal_and_round": ok("finboard_topic_read") and any(r["response"]["status"] == "ok" and r["arguments"]["payload"].get("goal_version") == "fixture-v1" for r in topics) and ok("finboard_memory_remember"),
        }
        steps = [e["part"] for e in events if e.get("type") == "step_finish"]
        billing = {"input_tokens": sum(s.get("tokens", {}).get("input", 0) for s in steps),
                   "output_tokens": sum(s.get("tokens", {}).get("output", 0) for s in steps),
                   "cache_read_tokens": sum(s.get("tokens", {}).get("cache", {}).get("read", 0) for s in steps),
                   "runtime_estimated_cost": sum(s.get("cost", 0) for s in steps),
                   "cost_currency": "not supplied in events; runtime estimate is not an account invoice"}
        if name.endswith("continued"):
            before = records(OUT / f"{batch}-interrupted-events.jsonl")
            prior_steps = [e["part"] for e in before if e.get("type") == "step_finish"]
            billing["interrupted_phase_runtime_estimated_cost"] = sum(s.get("cost", 0) for s in prior_steps)
        results.append({"round": name, "session_id": next((e.get("sessionID") for e in events if e.get("sessionID")), None),
            "receipt_checks": receipts, "all_receipt_checks": all(receipts.values()),
            "manual_text_review_required": True, "tool_call_count": len(tools),
            "tool_error_count": sum(r["response"]["status"] == "error" for r in tools), **billing})
    (OUT / f"{batch}-audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: r[k] for k in ("round", "session_id", "all_receipt_checks", "receipt_checks")} for r in results], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="scoped")
    audit(parser.parse_args().batch)
