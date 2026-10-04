"""#505 固定基线的有界读侧证据归档;研究写操作另由明确命令启动。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from scripts.research_evidence_archive import local_round_dir
from sqlalchemy import text

from finboard_app.config import load_settings
from finboard_app.research_diagnostics import compare_runs, frozen_input, interval_report
from finboard_app.research_explanation import explain
from finboard_persistence import create_async_engine, session_factory

ROOT = Path(__file__).resolve().parents[1]
OUT = local_round_dir("2026-10-04-smallcap-v13")
BASELINE = "RR-e909662f7b2c9f7ad30e89c4"
OLD = "RR-e1976d09cb26576d4cd9fb9f"
OLD_COST = "RR-a2f0ad6f035d7eada1538227"


def save(name: str, value: Any) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def snapshot() -> None:
    engine = create_async_engine(load_settings().db_url)
    try:
        async with session_factory(engine)() as session:
            source = await frozen_input(session, BASELINE)
            save("baseline", source)
            save("rules", await explain(session, run_id=BASELINE))
            save("annual", await interval_report(session, run_id=BASELINE, start="2015-01-01", end="2026-09-04"))
            for label, lo, hi in [("exclude-2015", "2016-01-01", "2026-09-04"),
                                  ("development", "2015-01-01", "2023-12-31"),
                                  ("subsequent-used", "2024-01-01", "2026-09-04"),
                                  ("2024-risk", "2024-01-01", "2024-03-31")]:
                save(label, await interval_report(session, run_id=BASELINE, start=lo, end=hi, yearly=False))
            save("comparison-history", await compare_runs(session, run_ids=[BASELINE, OLD, OLD_COST],
                                                            allowed_differences=["fee"]))
            save("active-memories", [dict(r) for r in (await session.execute(text("""
                SELECT memory_id,memory_type,left(content,8000) AS content,source_refs,tags,supersedes_id
                FROM research_memories WHERE status='active' ORDER BY created_at DESC LIMIT 30
            """))).mappings()])
            save("history", [dict(r) for r in (await session.execute(text("""
                SELECT run_id,status,manifest_checksum,result_checksum,manifest::jsonb->'strategy_version' AS version,
                manifest::jsonb->'parameters' AS parameters,manifest::jsonb->'fee_config' AS fee_config,
                manifest::jsonb->'risk_config' AS risk_config,manifest::jsonb->'portfolio_config' AS portfolio_config,
                manifest::jsonb->'dataset_releases' AS releases,manifest::jsonb->'factor_series' AS factors,
                left(manifest::jsonb->'strategy_spec'->>'description',400) AS description,
                result::jsonb->'annualized_return' AS annualized_return,result::jsonb->'max_drawdown' AS drawdown,
                result::jsonb->'commission_paid' AS commission,result::jsonb->'tax_paid' AS tax,
                result::jsonb->'slippage_paid' AS slippage,result::jsonb->'fill_count' AS fills,
                error_code,left(error_summary,1000) AS error_summary
                FROM research_runs WHERE manifest::jsonb->'strategy_spec'->>'strategy_id'='sc-mr-macd-composite-v1'
                ORDER BY created_at DESC LIMIT 100
            """))).mappings()])
            save("experiments", [dict(r) for r in (await session.execute(text("""
                SELECT experiment_id,status,final_test_unsealed,version_stamp,plan,
                rejection_reason,left(notes,4000) AS notes
                FROM research_experiments ORDER BY created_at DESC LIMIT 30
            """))).mappings()])
            refs = [r["artifact_id"] for r in source["manifest"]["factor_series"]]
            save("factor-coverage", [dict(r) for r in (await session.execute(text("""
                SELECT series_id,code_artifact,code_commit,release_id,dataset_release_ids,params,
                window_start,window_end,dates,content_checksum,quality,artifact_relpath
                FROM research_factor_series WHERE series_id=ANY(:ids)
            """), {"ids": refs})).mappings()])
            save("warning-counts", [dict(r) for r in (await session.execute(text("""
                SELECT a.stage,left(w::text,500) AS warning,count(*) AS count
                FROM research_run_artifacts a CROSS JOIN LATERAL jsonb_array_elements(
                  COALESCE(a.payload::jsonb->'warnings','[]'::jsonb)) w
                WHERE a.run_id=:id GROUP BY 1,2 ORDER BY 3 DESC LIMIT 40
            """), {"id": BASELINE})).mappings()])
        print(f"snapshot saved: {OUT}", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(snapshot())
