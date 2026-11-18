"""从宿主机验证研究版的首次研究、重启与权限边界。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_RELEASE = "synthetic-starter-v1"
_STRATEGY = "synthetic-starter-momentum"
_COMPOSE = ["docker", "compose", "--file", "docker/research/compose.yaml"]
_OPERATOR = ["exec", "-T", "gateway", "python", "/opt/finboard/entrypoint.py", "operator"]
_TERMINAL = {"succeeded", "failed", "cancelled"}
_OFFLINE = os.getenv("FINBOARD_SMOKE_OFFLINE") == "1"
if _OFFLINE:
    _COMPOSE.extend(["--file", "docker/research/compose.offline.yaml"])

_LOCAL_REQUEST = """
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

path, headers, data = json.load(sys.stdin)
body = data.encode() if data is not None else None
try:
    with urlopen(Request("http://127.0.0.1:8000" + path, data=body, headers=headers), timeout=30) as response:
        result = [response.status, response.read().decode()]
except HTTPError as exc:
    result = [exc.code, exc.read().decode()]
print(json.dumps(result))
"""


def compose(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*_COMPOSE, *args], input=data, check=True, capture_output=True, timeout=120,
    )


def request(
    base: str, path: str, payload: dict[str, Any] | None = None, *,
    token: str = "", expected: int = 200,
) -> dict[str, Any]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["MCP-Protocol-Version"] = "2025-06-18"
    body = json.dumps(payload).encode() if payload is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    if _OFFLINE:
        # 内部 Docker 网络也隔离了发布端口。从网关的 loopback 发同样的 HTTP 请求。
        result = compose(
            "exec", "-T", "gateway", "python", "-c", _LOCAL_REQUEST,
            data=json.dumps([path, headers, body.decode() if body else None]).encode(),
        )
        status_code, output = json.loads(result.stdout)
        if status_code != expected:
            raise RuntimeError(f"{path}: status {status_code}, {output[:2000]!r}")
        return cast(dict[str, Any], json.loads(output))
    req = Request(base + path, data=body, headers=headers)
    try:
        with urlopen(req, timeout=30) as response:
            if response.status != expected:
                raise RuntimeError(f"{path}: status {response.status}, expected {expected}")
            output = response.read()
    except HTTPError as exc:
        output = exc.read()
        if exc.code != expected:
            raise RuntimeError(f"{path}: status {exc.code}, {output[:2000]!r}") from None
    return cast(dict[str, Any], json.loads(output))


def credential(preset: str) -> str:
    client_id = f"MC-{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    token = f"fbmc_{client_id}_{secret}"
    compose(*_OPERATOR, "client-register", data=json.dumps({
        "client_id": client_id, "client": "smoke", "preset": preset,
        "token_prefix": secret[:16], "token_hash": hashlib.sha256(token.encode()).hexdigest(),
    }).encode())
    return token


def rpc(base: str, token: str, method: str, args: dict[str, Any]) -> dict[str, Any]:
    reply = request(base, "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": args,
    }, token=token)
    if "error" in reply:
        raise RuntimeError(str(reply["error"]))
    return cast(dict[str, Any], reply["result"])


def call(
    base: str, token: str, tool: str, args: dict[str, Any], *, status: str = "ok",
) -> dict[str, Any]:
    result = rpc(base, token, "tools/call", {"name": tool, "arguments": args})
    envelope = result.get("structuredContent")
    if envelope is None:
        envelope = json.loads(result["content"][0]["text"])
    if envelope["status"] != status:
        raise RuntimeError(f"{tool}: {envelope}")
    return cast(dict[str, Any], envelope["error"] if status == "denied" else envelope["data"])


def strategy(base: str) -> None:
    path = "/api/research/strategy-specs"
    try:
        request(base, f"{path}/{_STRATEGY}/versions/1")
    except RuntimeError as exc:
        if "status 404," not in str(exc):
            raise
    else:
        return
    query = urlencode({"strategy_id": _STRATEGY, "dataset_release_ids": _RELEASE})
    spec = request(base, f"{path}/templates/multi_factor?{query}")
    spec.update(name="Synthetic starter momentum", description="Synthetic starter data only")
    spec["validation_plan"]["benchmark_symbol"] = "FBDEMOIDX.SH"
    graph = spec["feature_graph"]
    graph["nodes"] = [node for node in graph["nodes"] if node["node_id"] in {
        "momentum", "volatility", "low_risk_score", "composite",
    }]
    composite = next(node for node in graph["nodes"] if node["node_id"] == "composite")
    composite.update(inputs=["momentum", "low_risk_score"], weights=[0.8, 0.2])
    spec["signal_rules"]["rules"][0]["threshold"] = 0.5
    draft = request(base, f"{path}/drafts", {"spec": spec}, expected=201)
    request(base, f"{path}/{_STRATEGY}/publish", {
        "version": draft["version"], "expected_version": draft["version"],
    })


def smoke(base: str, bundle: Path, *, restart: bool) -> dict[str, Any]:
    started = time.perf_counter()
    compose("up", "-d", "--wait", "--wait-timeout", "120")
    assert request(base, "/api/research-edition/health")["edition"] == "research"
    assert request(base, "/mcp", expected=401)["error"] == "unauthorized"
    ready = time.perf_counter() - started
    request(base, "/api/orders", expected=404)
    request(base, "/api/positions", expected=404)
    request(base, "/api/account", expected=404)

    read_only = credential("readonly")
    writer = credential("topic-writer")
    init = {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "research-edition-smoke", "version": "1"},
    }
    assert rpc(base, read_only, "initialize", init)["serverInfo"]["name"] == "finboard"
    started = time.perf_counter()
    raw_bundle = bundle.read_bytes()
    result = compose(*_OPERATOR, "import", data=raw_bundle)
    imported = time.perf_counter() - started
    release = json.loads(result.stdout.splitlines()[-1])
    assert release["release_id"] == _RELEASE
    assert release == json.loads(compose(*_OPERATOR, "import", data=raw_bundle).stdout.splitlines()[-1])
    rejected = subprocess.run(
        [*_COMPOSE, *_OPERATOR, "import"], input=raw_bundle[:100],
        check=False, capture_output=True, timeout=30,
    )
    assert rejected.returncode != 0
    strategy(base)

    topic = call(base, writer, "finboard_topic_write", {
        "operation": "create",
        "payload": {
            "title": "Synthetic starter research", "question": "Can the fixed sample run offline?",
            "goal": {
                "version": "v1", "criteria": "complete the synthetic multi-period run",
                "source": {"kind": "document", "ref_id": "ROADMAP.md"},
            },
        },
    })
    topic_id = topic["topic_id"]
    call(base, read_only, "finboard_topic_read", {"topic_id": topic_id, "view": "context"})
    scope = {
        "universe": {"explicit_symbols": [f"FBDEMO{i:02d}.SH" for i in range(1, 9)]},
        "decision": {"start": "2024-01-02", "end": "2024-02-23"},
        "warmup_sessions": 100, "tail_sessions": 1,
        "datasets": {"required": ["bars"]}, "bars_adjustment": "qfq",
        "release_selection": "locked", "locked_release_ids": [_RELEASE],
        "benchmarks": ["FBDEMOIDX.SH"],
    }
    resolved = call(base, read_only, "finboard_data_scope_preview", {
        "scope": scope, "strategy_id": _STRATEGY, "strategy_version": 1, "mode": "existing_spec",
    })
    assert not resolved["blockers"], resolved
    args = {
        "topic_id": topic_id, "goal_version": "v1",
        "payload": {
            "strategy_id": _STRATEGY, "strategy_version": 1, "dataset_release_ids": [_RELEASE],
            "idempotency_key": f"synthetic-starter-v1-run-{secrets.token_hex(6)}",
            "code_version": _RELEASE,
            "initial_capital": "200000", "requested_by": "starter-smoke",
            "data_scope": scope, "data_scope_resolution_id": resolved["resolution_id"],
            "parameters": {"decision_schedule": {"kind": "weekly"}},
        },
    }
    denied = call(base, read_only, "finboard_topic_run_start", args, status="denied")
    assert denied["kind"] == "permission_denied"
    started = time.perf_counter()
    receipt = call(base, writer, "finboard_topic_run_start", args)
    queued = time.perf_counter() - started
    deadline = started + 120
    job_id, run_id = receipt["job_id"], receipt["run_id"]
    while True:
        job = request(base, f"/api/jobs/{job_id}")
        if job["status"] in _TERMINAL:
            break
        if time.perf_counter() > deadline:
            raise TimeoutError(f"starter run still pending: {job}")
        time.sleep(0.2)
    assert job["status"] == "succeeded", job
    run = call(base, read_only, "finboard_run_get", {"run_id": run_id, "view": "detail"})
    readable = time.perf_counter() - started
    assert run["status"] == "completed", run
    assert _RELEASE in json.dumps(run)
    assert run["manifest"]["strategy_spec"]["validation_plan"]["benchmark_symbol"] == "FBDEMOIDX.SH"
    metrics = json.dumps(
        {
            name: run["result"][name]
            for name in (
                "strategy_return", "benchmark_return", "max_drawdown", "final_equity",
                "decision_count", "fill_count", "accounting_invariants_passed",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if restart:
        compose("stop")
        started = time.perf_counter()
        compose("up", "-d", "--wait", "--wait-timeout", "120")
        restarted = time.perf_counter() - started
        restored = call(base, read_only, "finboard_topic_run_get", {"topic_id": topic_id, "run_id": run_id})
        assert run_id in json.dumps(restored)
        restored_run = call(base, read_only, "finboard_run_get", {"run_id": run_id, "view": "detail"})
        assert restored_run["status"] == "completed"
        assert release["checksum"] in json.dumps(restored_run)
    else:
        restarted = None
    trading_rows = compose(
        "exec", "-T", "postgres", "psql", "-U", "findashboard", "-d", "findashboard",
        "-At", "-c",
        "SELECT (SELECT count(*) FROM orders) + (SELECT count(*) FROM fills) "
        "+ (SELECT count(*) FROM positions) + (SELECT count(*) FROM audit_logs)",
    )
    assert trading_rows.stdout.strip() == b"0"
    created_at, started_at, finished_at = (
        datetime.fromisoformat(job[key].replace("Z", "+00:00"))
        for key in ("created_at", "started_at", "finished_at")
    )
    worker_duration = (finished_at - started_at).total_seconds()
    budget = 30 if os.getenv("FINBOARD_RESEARCH_MIN_POOL_SYMBOLS") == "0" else 10
    if worker_duration > budget:
        raise RuntimeError(f"fixed starter execution exceeded the {budget}s budget: {worker_duration}")
    return {
        "environment": platform.platform(),
        "image": os.environ.get("FINBOARD_IMAGE"), "bundle_bytes": len(raw_bundle),
        "release_checksum": release["checksum"], "topic_id": topic_id, "run_id": run_id,
        "metrics_sha256": hashlib.sha256(metrics.encode()).hexdigest(),
        "ready_s": ready, "import_s": imported, "submit_s": queued, "readable_s": readable,
        "queue_s": (started_at - created_at).total_seconds(), "worker_s": worker_duration,
        "restarted_s": restarted,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--no-restart", action="store_true")
    opts = parser.parse_args()
    endpoint = f"http://127.0.0.1:{os.getenv('FINBOARD_PORT', '8176')}"
    print(json.dumps(smoke(endpoint, opts.bundle, restart=not opts.no_restart), indent=2))
