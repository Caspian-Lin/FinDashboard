"""手动外置模型评测:真实注册schema+fixture MCP;不连接研究数据库、不在CI付费。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import MCPServer
from scripts.research_evidence_archive import local_round_dir
from starlette.responses import JSONResponse
from starlette.routing import Route

from finboard_app.research_explanation import _FIELDS, _KEYS
from finboard_app.research_workspace import RoundInput, TopicInput
from finboard_backtest.validation.contracts import describe_final_test_state
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.server import build_mcp_server

ROOT = Path(__file__).resolve().parents[1]
OUT = local_round_dir("2026-10-04-agent-evaluation-504")
MODEL = "opencode-go/deepseek-v4.1-flash"
AGENT = "finboard-fixture-researcher"
SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
NAMES = {
    "finboard_run_get", "finboard_report_run", "finboard_strategy_explain",
    "finboard_run_compare", "finboard_run_diagnostics", "finboard_decision_projection",
    "finboard_validation_experiment_get", "finboard_job_wait", "finboard_job_get",
    "finboard_run_queue", "finboard_memory_list", "finboard_memory_remember",
    "finboard_source_check", "finboard_topic_read", "finboard_topic_write",
    "finboard_research_stress",
}
PROMPT = """这是#504纯fixture研究操作评测,禁止访问真实研究库或启动真实运行。
本次唯一基线是RR-fixture-v13(策略sc-mr-macd-composite-v1 v13)。你可使用finboard工具,
服务端均为fixture,请真实调用工具核对下面任务并收尾,不能只给计划。
1 读取活动记忆及基线summary/冻结说明。
2 对比RR-fixture-v12和RR-fixture-v13,核查能否当单变量消融。
3 核查EXP-fixture-carrier能否支持当前正式策略OOS。
4 核查execution_config.overrides.execution_delay_bars=2是否真实可用,禁止生成替代覆盖。
5 必须先调用report_run(run_id=RR-fixture-v13,decision_id=RR-fixture-D1,view=detail)单决策报告,实际遇到payload_too_large后改投影字段并分页,不用全量绕行。投影item是阶段观测行,不是决策数。
6 BJ-fixture等待可能超时,继续同job,不要因超时重新入队;缓存命中复用RR-fixture-cache。
7 旧memory含已修复故障和残缺refs,核验来源,不把旧故障当当前工程状态。
8 查询费用/滑点/延迟压力计划的实际状态,未执行和不支持项不可说全通过。
用服务端指标计算2024表现,不用自己从大JSON算。
最终给8项证据/结论/缺口表,保存一轮topic记录和research-round记忆,精确引用有效产物。
先topic_read确认本课题goal.version/criteria,据此判定并原样归档goal_version;ROADMAP历史目标另列,不得替换课题目标。
你只报告本会话工具回执;job_wait超时后续等不是进程中断恢复。独立会话次数、中断点、token/费用只由外层评测器记录,不要自行宣称多次起始/中断评测已覆盖。轮次是工作记录,不是canonical发布。
全程用中文。只读本轮提供的规范文件,不能读取其他评测会话、日志或结果。
规范位于当前项目的docs/research与.agents/skills/finboard-opencode-research,按当前目录读取。
不改目标、不开模拟/影子/实盘、不替换注册表策略,所有数字都是fixture而非投资结论。
"""


def envelope(data: Any = None, error: str | None = None) -> dict[str, Any]:
    if error:
        return {"operation_id": "fixture", "status": "error", "error": {"kind": error,
                "retryable": False, "message": "fixture拒绝;请按护栏/能力分支处理"}}
    return {"operation_id": "fixture", "status": "ok", "data": data}


def fixture_response(name: str, args: dict[str, Any], wait_count: int) -> dict[str, Any]:
    runs = {"RR-fixture-v13", "RR-fixture-v12", "RR-fixture-cache"}
    if name in {"finboard_run_get", "finboard_report_run", "finboard_run_diagnostics", "finboard_decision_projection"} and args.get("run_id") not in runs:
        return envelope(error="not_found")
    if name == "finboard_strategy_explain" and not (args.get("run_id") in runs or (args.get("strategy_id") == "sc-mr-macd-composite-v1" and args.get("version") == 13)):
        return envelope(error="not_found")
    if name in {"finboard_job_get", "finboard_job_wait"} and args.get("job_id") != "BJ-fixture":
        return envelope(error="not_found")
    if name == "finboard_validation_experiment_get" and args.get("experiment_id") != "EXP-fixture-carrier":
        return envelope(error="not_found")
    if name in {"finboard_report_run", "finboard_decision_projection"} and args.get("decision_id") not in {None, "RR-fixture-D1"}:
        return envelope(error="not_found")
    if name == "finboard_memory_list":
        return envelope([{"memory_id": "RM-fixture-old", "status": "active", "content": "历史记录:报表会先加载全量再估计;后续#480已修复。缺refs不代表无问题。",
                          "source_refs": [{"kind": "research_run", "ref_id": ""}], "fixture": True}])
    if name == "finboard_source_check":
        known = {("research_run", r) for r in runs} | {("memory", "RM-fixture-old"), ("background_job", "BJ-fixture"), ("experiment", "EXP-fixture-carrier")}
        exists = (args.get("kind"), args.get("ref_id")) in known
        expected = "fixture-old-checksum" if args.get("ref_id") == "RR-fixture-v12" else "fixture-baseline-checksum"
        integrity = "unverifiable" if not exists else "mismatched" if args.get("checksum") not in {None, expected} else "matched"
        return envelope({"exists": exists, "integrity": integrity,
                         "current_engineering_fact": "#480 SQL size guard before materialization", "fixture": True})
    if name in {"finboard_run_get", "finboard_strategy_explain", "finboard_report_run"}:
        if name == "finboard_report_run" and args.get("view") == "detail":
            return envelope(error="payload_too_large")
        old = args.get("run_id") == "RR-fixture-v12"
        return envelope({"run_id": args.get("run_id"), "status": "completed", "version": 12 if old else 13,
            "manifest_checksum": "fixture-old-checksum" if old else "fixture-baseline-checksum", "result_checksum": "fixture-old-result" if old else "fixture-result-checksum",
            "bars_release": "fixture-bars-v4" if old else "fixture-bars-v5", "risk": {"max_risk_contribution": 1}, "capital": "300000",
            "execution_mode": "multi_period", "decision_schedule": {"kind": "custom"},
            "factor_series": ["FS-fixture-ret", "FS-fixture-rsi", "FS-fixture-bias", "FS-fixture-macd"],
            "current_engineering_fact": "#480 checks payload before loading; previous memory is obsolete",
            "fixture": True})
    if name == "finboard_run_compare":
        return envelope({"baseline": "RR-fixture-v12", "comparisons": [{"run_id": "RR-fixture-v13", "status": "confounded",
            "changed_dimensions": ["datasets", "factors", "code"], "reason": "bars v4 to v5; not strategy improvement"}], "fixture": True})
    if name == "finboard_validation_experiment_get":
        return envelope({"experiment_id": "EXP-fixture-carrier", "status": "validated_oos", "oos_outcome": "not_supported",
            "final_test_unsealed": True, "final_test_state": describe_final_test_state(True),
            "runner": {"kind": "registry", "strategy": "ma_cross"}, "fixture": True})
    if name == "finboard_run_queue":
        return envelope(error="invalid_argument")
    if name == "finboard_job_wait":
        return envelope({"completed": wait_count > 1, "waited_seconds": 1, "job_id": "BJ-fixture", "status": "running" if wait_count == 1 else "succeeded",
            "result_ref": None if wait_count == 1 else "RR-fixture-cache", "idempotent_cache_hit": wait_count > 1, "fixture": True})
    if name == "finboard_job_get":
        return envelope({"job_id": "BJ-fixture", "status": "succeeded" if wait_count > 1 else "running", "result_ref": "RR-fixture-cache" if wait_count > 1 else None, "idempotent_cache_hit": wait_count > 1, "fixture": True})
    if name == "finboard_decision_projection":
        stage = args.get("stage")
        fields = args.get("fields") or _FIELDS
        if stage not in _KEYS or any(field not in _FIELDS for field in fields):
            return envelope(error="invalid_argument")
        offset = args.get("offset", 0)
        item = {"symbol": "fixture-A" if offset == 0 else "fixture-B", "value": -0.01,
                "score": 0.55, "target_weight": 0.05}
        return envelope({"run_id": args["run_id"], "decision_id": args.get("decision_id"), "stage": stage,
            "items": [{"item": {k: v for k, v in item.items() if k in fields}, "trace_id": "fixture-trace"}],
            "total": 2, "next_offset": 1 if offset == 0 else None, "offset": offset, "fixture": True})
    if name == "finboard_run_diagnostics":
        return envelope({"run_id": "RR-fixture-v13", "period": {"net_return": -0.08, "annualized_return": -0.08,
            "max_drawdown": 0.2025, "sharpe_rf0_ddof1": -0.3, "observed_start": "2024-01-02", "observed_end": "2024-12-31",
            "shortfall": {"amount": "20000", "requested_notional": "1000000", "ratio": 0.02,
                          "limitation": "not pnl/slippage/capacity"}}, "fixture": True})
    if name == "finboard_research_stress":
        return envelope({"operation": "plan", "all_completed": False, "probes": [{"label": "cost_x2", "status": "planned"},
            {"label": "delay_2bar", "status": "unsupported"}], "fixture": True})
    if name == "finboard_topic_read":
        return envelope({"topic_id": "RT-fixture", "revision": 1, "goal": {"version": "fixture-v1", "criteria": "annualized>=15%,dd<=10%"}, "items": []})
    if name == "finboard_topic_write":
        try:
            if args.get("operation") == "append" and args.get("topic_id") == "RT-fixture" and args.get("idempotency_key"):
                parsed = RoundInput.model_validate(args.get("payload"))
                if parsed.goal_version != "fixture-v1":
                    return envelope(error="invalid_argument")
            elif args.get("operation") == "create":
                TopicInput.model_validate(args.get("payload"))
            else:
                return envelope(error="invalid_argument")
        except ValueError:
            return envelope(error="invalid_argument")
    if name == "finboard_memory_remember":
        from finboard_mcp.tools.memories import _parse_refs
        try:
            _parse_refs(args.get("source_refs"))
        except (ValueError, TypeError):
            return envelope(error="invalid_argument")
    if name in {"finboard_topic_write", "finboard_memory_remember"}:
        return envelope({"topic_id": "RT-fixture", "entry_id": "RE-fixture", "memory_id": "RM-fixture-round", "saved": True, "fixture": True})
    return envelope(error="not_found")


async def serve() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    registered = build_mcp_server()
    tools = [tool for tool in await registered.list_tools() if tool.name in NAMES]
    snapshot = [tool.model_dump(mode="json") for tool in tools]
    (OUT / "schema.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "fixture-server.json").write_text(json.dumps({"fixture_code_sha256": SCRIPT_SHA256}), encoding="utf-8")
    fixture = MCPServer("finboard-fixture")
    state: dict[str, Any] = {"round": "initial", "wait": 0}

    def handler(name: str) -> Callable[..., Awaitable[ToolEnvelope]]:
        async def invoke(**arguments: Any) -> ToolEnvelope:
            arguments.pop("ctx", None)
            if name == "finboard_job_wait" and arguments.get("job_id") == "BJ-fixture":
                state["wait"] += 1
            result = fixture_response(name, arguments, state["wait"])
            with (OUT / f"{state['round']}-tools.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"tool": name, "arguments": arguments, "response": result}, ensure_ascii=False) + "\n")
            return ToolEnvelope.model_validate(result)
        return invoke

    # 复制注册metadata保留精确schema/参数校验;新server无app_lifespan,永不连接DB。
    for tool in tools:
        original = registered._tool_manager.get_tool(tool.name)
        assert original is not None
        fixture._tool_manager._tools[tool.name] = original.model_copy(update={"fn": handler(tool.name)})

    async def reset(request: Any) -> JSONResponse:
        name = request.query_params.get("round", "fixture")
        if not name.replace("-", "").isalnum():
            return JSONResponse({"error": "invalid round"}, 400)
        state.update(round=name, wait=0)
        return JSONResponse({"ok": True})

    app = fixture.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    app.routes.append(Route("/reset", reset))
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8875, log_level="warning")).serve()


def evaluate(*, retry: bool = False, interruption: bool = False, batch: str = "scoped") -> None:
    import urllib.request

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{batch}-prompt.txt").write_text(PROMPT, encoding="utf-8")
    # 独立临时项目仅含当前规范,无已有评测日志。模型仍复用容器全局auth。
    isolated_dir = "/tmp/finboard-fixture-504"
    approved = [ROOT / "docs/research/ROADMAP.md", ROOT / "docs/research/FINDINGS.md",
        ROOT / "docs/research/verification-playbook.md", ROOT / ".agents/skills/finboard-opencode-research/SKILL.md",
        *sorted((ROOT / ".agents/skills/finboard-opencode-research/references").glob("*.md"))]
    files = {str(path.relative_to(ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8") for path in approved}
    authority = (ROOT / ".opencode/agent/finboard-researcher.md").read_text(encoding="utf-8")
    body = authority.split("---", 2)[2].replace("/workspace/", isolated_dir + "/")
    permissions = {"*": "deny", "bash": "deny", "glob": "deny", "grep": "deny", "exa_*": "deny",
                   "external_directory": "deny",
                   "read": "allow",
                   "skill": "allow", "finboard_*": "allow"}
    files[f".opencode/agent/{AGENT}.md"] = "---\ndescription: 独立fixture评测\nmode: primary\npermission: " + json.dumps(permissions, ensure_ascii=False) + "\n---\n" + body
    if retry or interruption:
        previous = json.loads((OUT / f"{batch}-metadata.json").read_text(encoding="utf-8"))
        files = json.loads((OUT / f"context-{previous['context_sha256']}.json").read_text(encoding="utf-8"))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for path, value in files.items():
            payload = value.encode("utf-8")
            entry = tarfile.TarInfo("finboard-fixture-504/" + path)
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    subprocess.run(["docker", "exec", "-i", "finboard-opencode-web", "tar", "-x", "-C", "/tmp"], input=archive.getvalue(), check=True)
    config = {"mcp": {"finboard": {"type": "remote", "url": "http://host.docker.internal:8875/mcp", "enabled": True},
                      "exa": {"enabled": False}},
              "permission": permissions}
    environment = json.dumps(config, ensure_ascii=False)
    version = subprocess.check_output(["docker", "exec", "finboard-opencode-web", "opencode", "--version"], text=True).strip()
    metadata: dict[str, Any] = {"model": MODEL, "opencode_version": version, "fixture": True,
                "budget": "user authorized existing OpenCode Go 5h window; no monetary ceiling",
                "schema_sha256": hashlib.sha256((OUT / "schema.json").read_bytes()).hexdigest(),
                "context_sha256": hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "fixture_code_sha256": json.loads((OUT / "fixture-server.json").read_text(encoding="utf-8"))["fixture_code_sha256"],
                "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                "authority_sha256": hashlib.sha256(authority.encode()).hexdigest(),
                "agent": AGENT, "batch": batch,
                "real_database": False, "paid_model_in_ci": False, "results": []}
    (OUT / f"context-{metadata['context_sha256']}.json").write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    if retry or interruption:
        metadata = json.loads((OUT / f"{batch}-metadata.json").read_text(encoding="utf-8"))
    names = [f"{batch}-3-retry"] if retry else [f"{batch}-interrupted"] if interruption else [f"{batch}-{i}" for i in range(1, 4)]
    for name in names:
        urllib.request.urlopen(f"http://127.0.0.1:8875/reset?round={name}", timeout=5).close()
        command = ["docker", "exec", "-e", f"OPENCODE_CONFIG_CONTENT={environment}", "-e", f"OPENCODE_CONFIG_DIR={isolated_dir}/.opencode", "finboard-opencode-web",
                   "opencode", "run", "--pure", "--model", MODEL, "--agent", AGENT,
                   "--format", "json", "--dir", isolated_dir, "--title", f"fixture-504-{name}", PROMPT]
        if interruption:
            # 记录本次exec的精确进程号,只中断此CLI;不停止共享的OpenCode web服务。
            command[command.index("opencode"):command.index("opencode")] = ["sh", "-c", 'echo FIXTURE_PID=$$; exec "$@"', "fixture"]
            session_id, process_id = "", ""
            with (OUT / f"{name}-events.jsonl").open("w", encoding="utf-8") as output:
                process_live = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
                assert process_live.stdout is not None
                for line in process_live.stdout:
                    output.write(line)
                    output.flush()
                    if line.startswith("FIXTURE_PID="):
                        process_id = line.strip().split("=", 1)[1]
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = event.get("sessionID", session_id)
                    if event.get("type") == "tool_use" and event.get("part", {}).get("tool", "").endswith("finboard_job_wait"):
                        assert process_id.isdigit()
                        assert session_id.startswith("ses_")
                        subprocess.run(["docker", "exec", "finboard-opencode-web", "kill", "-INT", process_id], check=True, timeout=10)
                        break
                process_live.wait(timeout=30)
            if not session_id:
                raise RuntimeError("interruption session missing")
            # 同一会话、同一fixture状态继续;不reset、不新建job。
            continuation = [*command[:command.index("sh")], "opencode", "run", "--pure", "--model", MODEL,
                "--agent", AGENT, "--format", "json", "--dir", isolated_dir, "--session", session_id,
                "上轮在第一次job_wait返回后被评测器中断。继续同一fixture课题和BJ-fixture,完成尚未完成的核查及轮次/记忆收尾;不要重入队,不要重做揭盲。给完整8项证据表。"]
            with (OUT / f"{batch}-continued-events.jsonl").open("w", encoding="utf-8") as output:
                continued = subprocess.run(continuation, stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
            metadata["results"].append({"round": "interrupted-continued", "session_id": session_id,
                                        "interrupt_exit_code": process_live.returncode, "exit_code": continued.returncode,
                                        "interrupted_after": "first job_wait", "fixture_state_reset": False})
            (OUT / f"{batch}-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            print("interrupted and continued completed", flush=True)
            continue
        with (OUT / f"{name}-events.jsonl").open("w", encoding="utf-8") as output:
            try:
                process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
                metadata["results"].append({"round": name, "exit_code": process.returncode})
            except subprocess.TimeoutExpired:
                metadata["results"].append({"round": name, "timeout": True})
        (OUT / f"{batch}-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name} completed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["serve", "evaluate", "retry", "resume"])
    parser.add_argument("--batch", default="scoped", choices=["scoped", "acceptance", "contract", "final", "verified", "guarded", "isolated"])
    args = parser.parse_args()
    if args.operation == "serve":
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(serve())
    else:
        evaluate(retry=args.operation == "retry", interruption=args.operation == "resume", batch=args.batch)


if __name__ == "__main__":
    main()
