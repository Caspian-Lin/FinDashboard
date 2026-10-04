"""追加研究课题/轮次与记忆纠正链;不改原run、不执行研究、不发布canonical结论。"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from scripts.execute_smallcap_review_505 import OUT, data, save

from finboard_mcp.context import app_lifespan
from finboard_mcp.server import build_mcp_server
from finboard_mcp.tools import memories, workspace


async def main() -> None:
    if (OUT / "workspace-archive.json").exists():
        print("archive already saved; no duplicate topic/memory")
        return
    if not (OUT / "window-repair-results.json").exists():
        raise RuntimeError("engineering replay has not finished; do not archive a completed round")
    baseline = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    sources = json.loads((OUT / "executed-sources.json").read_text(encoding="utf-8"))
    refs = [{"kind": "research_run", "ref_id": baseline["manifest"]["run_id"], "checksum": baseline["manifest_checksum"]},
            {"kind": "experiment", "ref_id": "bc9fc5af85e84173"},
            {"kind": "memory", "ref_id": "RM-a14a4b147ccd4afb9501980a"},
            {"kind": "document", "ref_id": "rounds/2026-10-04-smallcap-v13/report.md"}]
    refs += [{"kind": "research_run", "ref_id": s["manifest"]["run_id"], "checksum": s["manifest_checksum"]} for s in sources]
    summary = "#505固定v13复核:原年化18.88%/DD20.25%,剔2015年化7.46%;2024 reset基线净-16.03%/DD28.59%。12项有限回顾完成,2项工程复核另归档;不支持当前15%/<10%门,原ROADMAP15%/20%亦未过。独立alpha/正式OOS/延迟冲击缺证据,已用窗口不重揭盲,不晋级。报告与FINDINGS/ROADMAP为待PR评审草稿,并非已合并canonical结论。"
    async with app_lifespan(build_mcp_server()) as app:
        # 查询现有课题目录留证据;本issue另建固定复核锚,不改已有用户课题目标。
        save("workspace-before", data(await workspace.call(app, "read", {"limit": 50, "offset": 0})))
        payload: dict[str, Any] = {"title": "#505 小盘复合v13固定可信度复核", "question": "原v13能否满足当次目标并提供可信独立收益证据?",
            "goal": {"version": "2026-09-22-v13-15pct-ddlt10pct", "criteria": "年化>=15%,最大回撤<10%;来源记忆/冻结v13,另列ROADMAP2026-09-01的15%/20%目标,均不自动修改",
                     "source": {"kind": "memory", "ref_id": "RM-a14a4b147ccd4afb9501980a"}},
            "status": "closed", "conclusion": "not_supported", "summary": summary,
            "open_questions": ["合格未用窗口及因子覆盖", "历史退市/ST元数据PIT缺口", "微盘风格匹配与独立alpha", "2Bar延迟/冲击/实盘残差"],
            "next_step": "停止本轮;后续重开须另登记新差异/假设/预算,canonical待PR评审"}
        topic_path = OUT / "workspace-topic-created.json"
        topic = json.loads(topic_path.read_text(encoding="utf-8")) if topic_path.exists() else data(await workspace.call(app, "write", {"operation": "create", "payload": payload}))
        save("workspace-topic-created", topic)
        entry = data(await workspace.call(app, "write", {"operation": "append", "topic_id": topic["topic_id"],
            "idempotency_key": "review505-v1-closed", "payload": {"entry_type": "round", "goal_version": payload["goal"]["version"],
                "objective": "固定v13可信度复核,不是无限调优", "action": "有界年/区间诊断+同源检查+预注册12项研究+同参数窗口统计工程复核",
                "rationale": "旧v4消融混源;只补缺失v5证据,负结果与不足均可收束", "outcome": "completed", "conclusion": summary,
                "confidence": "high", "next_step": payload["next_step"], "source_refs": refs, "branch": "feat/research-verification-501-505"}}))
        save("workspace-round-created", entry)
        old = data(await memories.get_memory(app, "RM-3023e7c4e40548c89877efb9"))
        correction_path = OUT / "memory-correction.json"
        corrected = json.loads(correction_path.read_text(encoding="utf-8")) if correction_path.exists() else data(await memories.correct(app, memory_id=old["memory_id"],
            content="2026-10-04工程事实纠正:旧记忆的fee_config静默忽略是当时故障,#482已接线;#503新增显式费用版本供一致控制,不追改历史资产优先口径。#480已在SQL加载前护栏,单决策不豁免。本轮research_window把预热/尾段与绩效明确分开,最早两次窗口统计问题保留并工程复核;旧短日历全发布benchmark不是2024基准。旧运行数字与当时标签仍只作历史,不移植为当前v13成本/独立alpha证据。原文与原引用保留在supersedes_id链;不改原产物。",
            source_refs=[{"kind": "memory", "ref_id": old["memory_id"]}, {"kind": "research_run", "ref_id": baseline["manifest"]["run_id"]},
                         {"kind": "document", "ref_id": "rounds/2026-10-04-smallcap-v13/report.md"}], tags=["research-round", "engineering-correction", "review505"]))
        save("memory-correction", corrected)
        note = data(await memories.remember(app, memory_type="insight", content=summary,
            source_refs=[{"kind": r["kind"], "ref_id": r["ref_id"]} for r in refs],
            tags=["research-round", "research-plan", "finding-confirmed", "review505"]))
        save("workspace-archive", {"topic": topic, "round": entry, "memory": note, "correction": corrected,
            "canonical_status": "PR draft pending review; working summary and agent interpretation are not automatic facts"})


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
