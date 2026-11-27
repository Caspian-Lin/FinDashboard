"""在 Windows 独立 Docker 项目中复测离线桌面版与可选 Codex CLI 接入。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, cast

from scripts import smoke_research_distribution as research

_ROOT = Path(__file__).resolve().parents[1]
_DOCKER_ENV = {
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES",
}
_ISOLATED_PREFIXES = ("FINBOARD_", "OPENAI_", "CHATGPT_", "CODEX_")


class DesktopProbe:
    def __init__(self, resources: Path, port: int, *, with_codex: bool) -> None:
        self.resources = resources
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        nonce = secrets.token_hex(6)
        self.project = f"finboard-research-desktop-test-{nonce}"
        self.control_project = f"finboard-research-desktop-control-{nonce}"
        self.control_volume = f"{self.control_project}_control"
        self.probe = (_ROOT / ".tmp" / self.project).resolve()
        if self.probe.parent != _ROOT / ".tmp":
            raise ValueError("验收临时目录须位于仓库内的 .tmp")
        self.probe.mkdir(parents=True)
        self.state = self.probe / "state" / self.project
        self.state_file = self.state / "state.json"
        self.ps = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.docker_cmd = [shutil.which("docker.exe") or "docker.exe",
                           "--host", "npipe:////./pipe/dockerDesktopLinuxEngine"]
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(_ISOLATED_PREFIXES)
            and key.upper() not in _DOCKER_ENV
        }
        self.env.update(FINBOARD_DESKTOP_TEST_ROOT=str(self.state), FINBOARD_DESKTOP_TEST_PROJECT=self.project)
        self.profile = self.probe / "codex-profile"
        self.codex = shutil.which("codex") if with_codex else None
        if with_codex:
            if self.codex is None:
                raise ValueError("启用 --with-codex 时须安装真实 Codex CLI")
            self.profile.mkdir()
            (self.profile / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n\n[analytics]\nenabled = false\n',
                encoding="utf-8",
            )
            # CODEX_HOME 仅传给隔离子进程。后者不加载当前用户的默认配置或登录文件。
            self.env["CODEX_HOME"] = str(self.profile)

    def operator(self, action: str, *args: str, capture: bool = False) -> bytes:
        result = subprocess.run(
            [str(self.ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(self.resources / "finboard.ps1"), action, *args],
            check=True, timeout=1200, capture_output=capture, env=self.env,
        )
        return result.stdout if capture else b""

    def docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*self.docker_cmd, *args], timeout=120, check=check, capture_output=True, env=self.env,
        )

    def configure_research_smoke(self) -> dict[str, Any]:
        state = _json_file(self.state_file)
        # 复用研究版冒烟测试的整套冻结样例及真实 MCP/REST 客户端。
        blocked = _DOCKER_ENV | {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        for key in list(os.environ):
            if key.upper() in blocked or key.upper().startswith(_ISOLATED_PREFIXES):
                del os.environ[key]
        os.environ.update(
            FINBOARD_IMAGE=state["images"][0]["tag"],
            FINBOARD_DESKTOP_POSTGRES_IMAGE=state["images"][1]["tag"],
            FINBOARD_DESKTOP_INSTANCE=state["instance_id"],
            FINBOARD_DB_SECRET_FILE=str(self.state / "postgres-password"),
            FINBOARD_PORT=str(self.port),
        )
        research._COMPOSE = [
            *self.docker_cmd, "compose", "--project-name", self.project,
            "--project-directory", str(self.resources),
            "--file", str(self.resources / "compose.yaml"),
            "--file", str(self.resources / "compose.desktop.yaml"),
        ]
        research._OFFLINE = False
        return state

    def codex_json(self, *args: str) -> dict[str, Any]:
        assert self.codex is not None
        result = subprocess.run(
            [self.codex, *args, "--json"], timeout=45, check=True, capture_output=True, env=self.env,
        )
        return cast(dict[str, Any], json.loads(result.stdout))

    def attach(self, preset: str) -> str:
        self.operator("desktop-attach", "--preset", preset)
        plugins = self.codex_json("plugin", "list", "--marketplace", self.project)
        assert any(
            item["name"] == "finboard-research" and item["marketplaceName"] == self.project
            for item in plugins["installed"]
        ), "Codex 隔离 profile 未安装项目专属插件"
        state = self.status()
        assert state["preset"] == preset
        assert state["credential_active"]
        assert not state["pending_revocation"]
        return cast(str, _json_file(self.state / "codex.json")["client_id"])

    def status(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.operator("desktop-status", capture=True).splitlines()[-1]))

    def bridge_tool(self, name: str, arguments: dict[str, Any], *, status: str = "ok") -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        result = subprocess.run(
            [str(self.ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(self.state / "mcp-bridge.ps1"), "-InstallRoot", str(self.state), "-Client", "codex"],
            check=True, timeout=60, capture_output=True, env=self.env,
            input=(json.dumps(message) + "\n").encode(),
        )
        tool = json.loads(result.stdout)["result"]
        envelope = tool.get("structuredContent") or json.loads(tool["content"][0]["text"])
        assert envelope["status"] == status, (name, envelope.get("error", {}).get("kind"))
        return cast(dict[str, Any], envelope["error"] if status == "denied" else envelope["data"])

    def assert_revoked(self, *client_ids: str) -> None:
        for client_id in client_ids:
            assert re.fullmatch(r"MC-[0-9a-f]{12}", client_id)
            result = research.compose(
                "exec", "-T", "postgres", "psql", "-U", "findashboard", "-d", "findashboard",
                "-At", "-c", f"SELECT revoked_at IS NOT NULL FROM mcp_clients WHERE client_id = '{client_id}'",
            )
            assert result.stdout.strip() == b"t", "旧凭据未撤销"

    def cleanup(self) -> None:
        if self.state_file.exists():
            if _json_file(self.state_file)["lifecycle"] == "preserving":
                self.operator("desktop-uninstall", "--mode", "preserve")
            self.operator("desktop-uninstall", "--mode", "purge", "--confirm", "DELETE-THIS-INSTANCE")


def _json_file(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _probe_run(probe: DesktopProbe, evidence: dict[str, Any]) -> str:
    readonly = probe.attach("readonly")
    topic_id, original_id = evidence["topic_id"], evidence["run_id"]
    probe.bridge_tool("finboard_topic_read", {"topic_id": topic_id, "view": "context"})
    manifest = probe.bridge_tool("finboard_run_get", {"run_id": original_id, "view": "detail"})["manifest"]
    scope = manifest["data_scope"]["scope"]
    strategy = {
        "strategy_id": manifest["strategy_spec"]["strategy_id"],
        "strategy_version": manifest["strategy_version"],
    }
    resolution = probe.bridge_tool("finboard_data_scope_preview", {
        **strategy, "scope": scope, "mode": "existing_spec",
    })
    assert not resolution["blockers"]
    payload = {
        "topic_id": topic_id, "goal_version": "v1",
        "payload": {
            **strategy,
            "dataset_release_ids": [release["artifact_id"] for release in manifest["dataset_releases"]],
            "idempotency_key": f"desktop-codex-{probe.project}",
            "code_version": manifest["code_version"], "initial_capital": manifest["initial_capital"],
            "requested_by": "desktop-codex-probe",
            "data_scope": scope, "data_scope_resolution_id": resolution["resolution_id"],
            "parameters": {"decision_schedule": manifest["parameters"]["decision_schedule"]},
        },
    }
    submit = "finboard_topic_run_start"
    assert probe.bridge_tool(submit, payload, status="denied")["kind"] == "permission_denied"
    writer = probe.attach("topic-writer")
    receipt = probe.bridge_tool(submit, payload)
    job_status = ""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job_status = research.request(probe.base, f"/api/jobs/{receipt['job_id']}")["status"]
        if job_status in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    assert job_status == "succeeded", job_status
    current = probe.attach("readonly")
    assert probe.bridge_tool(submit, payload, status="denied")["kind"] == "permission_denied"
    probe.assert_revoked(readonly, writer)
    evidence["codex_run_id"] = receipt["run_id"]
    evidence["bridge_permissions"] = ["permission_denied", "succeeded", "permission_denied"]
    return current


def run(probe: DesktopProbe) -> dict[str, Any]:
    manifest = _json_file(probe.resources / "offline-manifest.json")
    evidence: dict[str, Any] = {
        "project": probe.project, "image_archive_bytes": (probe.resources / "images.tar").stat().st_size,
        "archive_sha256": manifest["archive_sha256"], "images": manifest["images"],
    }
    probe.docker("volume", "create", "--label", f"com.docker.compose.project={probe.control_project}", probe.control_volume)
    try:
        started = time.perf_counter()
        probe.operator("desktop-install", "--port", str(probe.port))
        assert probe.status()["running"], "首次安装后研究服务未就绪"
        evidence["first_ready_s"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        probe.operator("desktop-import")
        evidence["desktop_import_s"] = round(time.perf_counter() - started, 3)
        original_state = probe.configure_research_smoke()
        evidence.update(research.smoke(probe.base, probe.resources / "starter.zip", restart=True))
        active_client = _probe_run(probe, evidence) if probe.codex else None
        probe.operator("desktop-stop")
        probe.operator("desktop-uninstall", "--mode", "preserve")
        assert probe.status()["phase"] == "retained"
        for suffix in ("postgres", "research"):
            probe.docker("volume", "inspect", f"{probe.project}_{suffix}")
        if probe.codex:
            marketplaces = probe.codex_json("plugin", "marketplace", "list")["marketplaces"]
            assert all(item["name"] != probe.project for item in marketplaces)
            assert not (probe.profile / "auth.json").exists(), "隔离验收不得创建登录凭据"
        probe.operator("desktop-install", "--port", str(probe.port))
        assert probe.configure_research_smoke()["instance_id"] == original_state["instance_id"]
        if active_client:
            probe.assert_revoked(active_client)
            evidence["codex_marketplace_removed"] = evidence["old_credentials_revoked"] = True
        token = research.credential("readonly")
        link = research.call(probe.base, token, "finboard_topic_run_get", {
            "topic_id": evidence["topic_id"], "run_id": evidence["run_id"],
        })
        assert evidence["run_id"] in json.dumps(link)
        restored = research.call(probe.base, token, "finboard_run_get", {"run_id": evidence["run_id"], "view": "detail"})
        assert restored["status"] == "completed"
        assert evidence["release_checksum"] in {
            release["checksum"] for release in restored["manifest"]["dataset_releases"]
        }
        evidence["retained_run_readback"] = True
        probe.cleanup()
        assert not probe.status()["has_instance"]
        for suffix in ("postgres", "research"):
            assert probe.docker("volume", "inspect", f"{probe.project}_{suffix}", check=False).returncode != 0
        probe.docker("volume", "inspect", probe.control_volume)
        evidence["unrelated_project_preserved"] = True
    finally:
        try:
            probe.cleanup()
        finally:
            try:
                probe.docker("volume", "rm", probe.control_volume)
            finally:
                (probe.probe / "evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-isolated", action="store_true", required=True)
    parser.add_argument("--resources", type=Path, required=True, help="离线 MSI 的 edition 资源目录")
    parser.add_argument("--port", type=int, default=18377)
    parser.add_argument("--with-codex", action="store_true", help="追加真实 CLI 注册和 stdio 桥权限测试")
    args = parser.parse_args()
    if platform.system() != "Windows" or not 1024 <= args.port <= 65535:
        parser.error("需要 Windows 和 1024 至 65535 范围内的隔离端口")
    if not __debug__:
        parser.error("验收断言需要在未开启 Python 优化时运行")
    resources = args.resources.resolve(strict=True)
    required = {"images.tar", "offline-manifest.json", "finboard.ps1", "desktop-host.ps1",
                "compose.yaml", "compose.desktop.yaml", "starter.zip"}
    if any(not (resources / path).is_file() for path in required):
        parser.error("请指定已打包的离线 edition 资源目录")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))
    probe = DesktopProbe(resources, args.port, with_codex=args.with_codex)
    print(f"隔离验收证据目录 {probe.probe}", flush=True)
    print(json.dumps(run(probe), ensure_ascii=False))


if __name__ == "__main__":
    main()
