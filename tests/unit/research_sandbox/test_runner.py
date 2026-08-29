"""ResearchSandboxRunner —— 命令加固 / 超时 kill / 失败分类原料(issue #216)。

FakeDriver 捕获 docker argv 与调用序列,断言加固清单与 AC 一致;
真实容器行为(断网 / 只读 / OOM / 参照一致)见 E2E 门控测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from finboard_backtest.research_sandbox.runner import (
    ResearchSandboxRunner,
    SandboxRunSpec,
    _mem_usage_mb,
)


@dataclass
class FakeDriver:
    image: str = "finboard-research-sandbox:test"
    # 容器终态:exit_code / oom_killed;"hang" = 永不退出(超时场景)
    script: str = "exit:0"
    stats_samples: int = 2
    # 前 N 次 inspect 返回 Running(模拟容器跑一会儿,让 stats 有采样窗口)
    running_polls: int = 2
    argv_seen: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    async def image_digest(self, image: str) -> str:
        return "sha256:abc123"

    async def run_detached(self, argv: list[str]) -> str:
        self.argv_seen = list(argv)
        return "cid-1"

    async def inspect_state(self, container: str) -> dict[str, Any]:
        if self.script == "hang":
            return {"Running": True}
        if self.running_polls > 0:
            self.running_polls -= 1
            return {"Running": True}
        return {
            "Running": False,
            "ExitCode": int(self.script.split(":", 1)[1]),
            "OOMKilled": self.script.startswith("oom"),
        }

    async def stats(self, container: str) -> dict[str, Any] | None:
        if self.stats_samples <= 0:
            return None
        self.stats_samples -= 1
        return {
            "MemUsage": "512.5MiB / 2GiB",
            "CPUPerc": "42.3%",
        }

    async def logs(self, container: str) -> tuple[str, str]:
        return "stdout-line", "stderr-line"

    async def kill(self, container: str) -> None:
        self.killed.append(container)

    async def rm(self, container: str) -> None:
        self.removed.append(container)


def _spec(tmp_path: Path, **overrides: Any) -> SandboxRunSpec:
    defaults: dict[str, Any] = {
        "image": "finboard-research-sandbox:test",
        "code_dir": tmp_path / "code",
        "data_dir": tmp_path / "data",
        "out_dir": tmp_path / "out",
        "timeout_seconds": 5.0,
        "memory_mb": 2048,
        "cpus": 2.0,
        "pids_limit": 256,
    }
    defaults.update(overrides)
    return SandboxRunSpec(**defaults)


class TestHardenedCommand:
    async def test_security_flags_present(self, tmp_path: Path) -> None:
        driver = FakeDriver()
        await ResearchSandboxRunner(driver).run(_spec(tmp_path))
        argv = driver.argv_seen
        # 断网 / 只读根 / 权限剥除 / 非 root / 资源限额
        for flag, value in [
            ("--network", "none"),
            ("--read-only", None),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--user", "65532"),
            ("--pids-limit", "256"),
            ("--cpus", "2.0"),
            ("--memory", "2048m"),
            ("--memory-swap", "2048m"),
        ]:
            assert flag in argv, f"缺少加固参数 {flag}"
            if value is not None:
                assert argv[argv.index(flag) + 1] == value
        assert "--rm" not in argv  # 一次性 = 结束后显式 rm(需先 inspect/logs)
        assert "--detach" in argv

    async def test_mounts_and_tmpfs(self, tmp_path: Path) -> None:
        driver = FakeDriver()
        await ResearchSandboxRunner(driver).run(_spec(tmp_path))
        argv = driver.argv_seen
        mounts = [
            argv[i + 1] for i, tok in enumerate(argv) if tok == "--mount"
        ]
        assert any(
            m.startswith("type=bind,source=") and m.endswith(",target=/data,readonly")
            for m in mounts
        ), "数据面必须只读挂载到 /data"
        assert any(
            m.startswith("type=bind,source=") and m.endswith(",target=/code,readonly")
            for m in mounts
        ), "代码面必须只读挂载到 /code"
        assert any(m.endswith(",target=/out") and "readonly" not in m for m in mounts), (
            "输出挂载 /out 是容器内唯一可写路径"
        )
        tmpfs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
        assert any(t.startswith("/tmp:") and "noexec" in t and "nosuid" in t for t in tmpfs)
        # harness 命令
        tail = argv[argv.index("python"):]
        assert tail[:3] == ["python", "-m", "finboard_research_kit.harness"]

    async def test_container_removed_after_run(self, tmp_path: Path) -> None:
        driver = FakeDriver()
        await ResearchSandboxRunner(driver).run(_spec(tmp_path))
        assert driver.removed == ["cid-1"]


class TestRunScenarios:
    async def test_success_result(self, tmp_path: Path) -> None:
        driver = FakeDriver(script="exit:0")
        result = await ResearchSandboxRunner(driver).run(_spec(tmp_path))
        assert result.exit_code == 0
        assert not result.timed_out
        assert not result.oom_killed
        assert result.image_digest == "sha256:abc123"
        assert result.usage["max_mem_mb"] == pytest.approx(512.5, rel=0.01)
        assert result.usage["max_cpu_percent"] == pytest.approx(42.3)
        assert result.stdout == "stdout-line"
        assert result.stderr == "stderr-line"

    async def test_timeout_kill(self, tmp_path: Path) -> None:
        driver = FakeDriver(script="hang", stats_samples=3)
        result = await ResearchSandboxRunner(driver).run(
            _spec(tmp_path, timeout_seconds=0.3)
        )
        assert result.timed_out
        assert driver.killed == ["cid-1"]
        assert driver.removed == ["cid-1"]

    async def test_oom_flag_reported(self, tmp_path: Path) -> None:
        driver = FakeDriver(script="oom:137")
        result = await ResearchSandboxRunner(driver).run(_spec(tmp_path))
        assert result.oom_killed
        assert result.exit_code == 137


class TestMemUsageParsing:
    def test_units(self) -> None:
        assert _mem_usage_mb("512.5MiB / 2GiB") == pytest.approx(512.5, rel=0.01)
        assert _mem_usage_mb("1.5GiB / 2GiB") == pytest.approx(1536.0, rel=0.01)
        assert _mem_usage_mb("") == 0.0
        assert _mem_usage_mb(None) == 0.0
        assert _mem_usage_mb("garbage") == 0.0
