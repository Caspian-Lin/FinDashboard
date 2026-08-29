"""ResearchSandboxRunner —— 一次性 Docker 容器执行(issue #216)。

单形态(用户决策 2026-08-28):开发机与生产统一 Docker,不做子进程降级;
本机前置要求 Docker Desktop。加固清单:

* ``--network none`` —— 容器内任何 socket 连接失败(断网验证的依据);
* ``--read-only`` 根 + ``/data`` ``/code`` 只读挂载 —— 写挂载路径即失败;
* 输出目录 ``/out`` 为运行期唯一可写挂载(输出只出现在 /out);
* ``--cap-drop ALL`` + ``--security-opt no-new-privileges`` + 非 root user;
* ``--pids-limit`` / ``--cpus`` / ``--memory``(memory-swap=memory,禁 swap);
* ``/tmp`` tmpfs(noexec/nosuid);
* 整跑墙钟超时 → ``docker kill``(SIGKILL),失败分类 ``timeout``。

资源用量归档:容器运行期间轮询 ``docker stats --no-stream`` 采样
(峰值内存 / 峰值 CPU%),连同退出码 / OOMKilled 一起进 run 记录。

``DockerDriver`` 是薄抽象:生产实现走 asyncio subprocess,测试注入假驱动
即可覆盖命令加固参数与失败分类,无需真实 Docker。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from finboard_backtest.research_sandbox.errors import (
    SANDBOX_UNAVAILABLE,
    SandboxError,
)

_STATS_INTERVAL_SECONDS = 1.0
_MEM_USAGE_RE = re.compile(r"^([\d.]+)\s*([KMGT]i?B)", re.IGNORECASE)
_MEM_UNITS = {"b": 1.0, "kb": 1024.0, "kib": 1024.0, "mb": 1024.0**2,
              "mib": 1024.0**2, "gb": 1024.0**3, "gib": 1024.0**3,
              "tb": 1024.0**4, "tib": 1024.0**4}


@dataclass(frozen=True)
class SandboxRunSpec:
    """一次沙箱容器运行的全部输入。"""

    image: str
    #: 代码目录(裸仓库检出文件 + 服务端注入 params.json),挂 /code:ro
    code_dir: Path
    #: data_mount 产物目录,挂 /data:ro
    data_dir: Path
    #: 宿主机输出目录,挂 /out(容器内唯一可写路径)
    out_dir: Path
    timeout_seconds: float = 300.0
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 256
    user: str = "65532"
    tmpfs_size_mb: int = 64


@dataclass
class SandboxRunResult:
    """容器运行结果(退出码/日志/资源用量/分类原料)。"""

    container_id: str
    image_digest: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool
    duration_seconds: float
    usage: dict[str, Any] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)


class DockerDriver(Protocol):
    """docker CLI 的最小面(生产 SubprocessDockerDriver;测试可替换)。"""

    async def image_digest(self, image: str) -> str: ...

    async def run_detached(self, argv: list[str]) -> str: ...

    async def inspect_state(self, container: str) -> dict[str, Any]: ...

    async def stats(self, container: str) -> dict[str, Any] | None: ...

    async def logs(self, container: str) -> tuple[str, str]: ...

    async def kill(self, container: str) -> None: ...

    async def rm(self, container: str) -> None: ...


class SubprocessDockerDriver:
    """经 ``docker`` CLI 的生产实现。

    子进程走 ``subprocess.run`` + ``asyncio.to_thread``:**不能用**
    ``asyncio.create_subprocess_exec`` —— FinBoard 在 Windows 统一
    SelectorEventLoop(psycopg 异步驱动要求),而 Selector 循环在 Windows
    不支持创建子进程(NotImplementedError)。worker 单并发下线程阻塞
    开销可忽略。
    """

    def __init__(self, docker_bin: str = "docker") -> None:
        self._bin = docker_bin

    async def image_digest(self, image: str) -> str:
        out = await self._capture(
            [self._bin, "image", "inspect", image, "--format", "{{.Id}}"]
        )
        digest = out.strip()
        if not digest:
            raise SandboxError(
                SANDBOX_UNAVAILABLE, f"镜像不存在或不可读: {image}"
            )
        return digest

    async def run_detached(self, argv: list[str]) -> str:
        out = await self._capture(argv)
        container = out.strip()
        if not container:
            raise SandboxError(SANDBOX_UNAVAILABLE, "docker run 未返回容器 ID")
        return container

    async def inspect_state(self, container: str) -> dict[str, Any]:
        out = await self._capture(
            [self._bin, "inspect", container, "--format", "{{json .State}}"]
        )
        state: dict[str, Any] = json.loads(out or "{}")
        return state

    async def stats(self, container: str) -> dict[str, Any] | None:
        proc = await asyncio.to_thread(
            subprocess.run,
            [self._bin, "stats", "--no-stream", "--format", "{{json .}}", container],
            capture_output=True,
            check=False,
        )
        text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            sample: dict[str, Any] = json.loads(text)
            return sample
        except json.JSONDecodeError:
            return None

    async def logs(self, container: str) -> tuple[str, str]:
        proc = await asyncio.to_thread(
            subprocess.run,
            [self._bin, "logs", container],
            capture_output=True,
            check=False,
        )
        return (
            (proc.stdout or b"").decode("utf-8", errors="replace"),
            (proc.stderr or b"").decode("utf-8", errors="replace"),
        )

    async def kill(self, container: str) -> None:
        await self._capture([self._bin, "kill", container])

    async def rm(self, container: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            [self._bin, "rm", "-f", container],
            capture_output=True,
            check=False,
        )

    async def _capture(self, argv: list[str]) -> str:
        proc = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, check=False
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise SandboxError(
                SANDBOX_UNAVAILABLE,
                f"`{argv[1] if len(argv) > 1 else 'docker'}` 失败: "
                f"{err.strip()[:500]}",
            )
        return (proc.stdout or b"").decode("utf-8", errors="replace")


class ResearchSandboxRunner:
    """编排一次加固容器运行(命令构造 / 超时 kill / 资源采样)。"""

    def __init__(self, driver: DockerDriver) -> None:
        self._driver = driver

    async def run(self, spec: SandboxRunSpec) -> SandboxRunResult:
        digest = await self._driver.image_digest(spec.image)
        name = f"finboard-sandbox-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(spec.out_dir.mkdir, parents=True, exist_ok=True)
        # 非 root uid 在 Linux 宿主机上无法写 root 拥有的目录;输出目录放开
        # 写权限(内容只会在 run 归档时读取,无安全面扩大)。
        with contextlib.suppress(OSError):
            await asyncio.to_thread(spec.out_dir.chmod, 0o777)
        command = self.build_command(name=name, spec=spec)
        container = await self._driver.run_detached(command)

        started = time.monotonic()
        timed_out = False
        max_mem_mb = 0.0
        max_cpu = 0.0
        samples = 0
        while True:
            state = await self._driver.inspect_state(container)
            if not state.get("Running", False):
                break
            if time.monotonic() - started > spec.timeout_seconds:
                timed_out = True
                await self._driver.kill(container)
                break
            sample = await self._driver.stats(container)
            if sample is not None:
                samples += 1
                max_mem_mb = max(max_mem_mb, _mem_usage_mb(sample.get("MemUsage")))
                with contextlib.suppress(TypeError, ValueError):
                    max_cpu = max(max_cpu, float(sample.get("CPUPerc", "0%")[:-1]))
            await asyncio.sleep(min(_STATS_INTERVAL_SECONDS, spec.timeout_seconds))

        final = await self._driver.inspect_state(container)
        exit_code = final.get("ExitCode")
        oom_killed = bool(final.get("OOMKilled", False))
        stdout, stderr = await self._driver.logs(container)
        await self._driver.rm(container)
        if timed_out:
            exit_code = exit_code if exit_code is not None else 137
        return SandboxRunResult(
            container_id=container,
            image_digest=digest,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            oom_killed=oom_killed,
            duration_seconds=round(time.monotonic() - started, 3),
            usage={
                "max_mem_mb": round(max_mem_mb, 1),
                "max_cpu_percent": round(max_cpu, 1),
                "stats_samples": samples,
            },
            command=command,
        )

    def build_command(self, *, name: str, spec: SandboxRunSpec) -> list[str]:
        """构造加固 docker run 命令(单测直接断言本清单)。"""
        return [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            spec.user,
            "--pids-limit",
            str(spec.pids_limit),
            "--cpus",
            str(spec.cpus),
            "--memory",
            f"{spec.memory_mb}m",
            "--memory-swap",
            f"{spec.memory_mb}m",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={spec.tmpfs_size_mb}m",
            "--mount",
            f"type=bind,source={spec.data_dir.resolve()},target=/data,readonly",
            "--mount",
            f"type=bind,source={spec.code_dir.resolve()},target=/code,readonly",
            "--mount",
            f"type=bind,source={spec.out_dir.resolve()},target=/out",
            spec.image,
            "python",
            "-m",
            "finboard_research_kit.harness",
            "--code-dir",
            "/code",
            "--data-dir",
            "/data",
            "--out-dir",
            "/out",
        ]


def _mem_usage_mb(raw: Any) -> float:
    if not isinstance(raw, str):
        return 0.0
    usage = raw.split("/")[0].strip()
    match = _MEM_USAGE_RE.match(usage)
    if not match:
        return 0.0
    value, unit = match.groups()
    factor = _MEM_UNITS.get(unit.lower().replace("i", ""), 1.0)
    try:
        return float(value) * factor / (1024.0**2)
    except ValueError:
        return 0.0


__all__ = [
    "DockerDriver",
    "ResearchSandboxRunner",
    "SandboxRunResult",
    "SandboxRunSpec",
    "SubprocessDockerDriver",
]
