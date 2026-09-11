"""ResearchSandboxRunner —— 一次性 Docker 容器执行(issue #216;#359 增
factor_series 区间执行入口)。

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

区间执行入口(issue #359):``run_factor_series_container`` 把
:class:`FactorSeriesRunSpec` 走完整链路 —— 代码解析(静态校验重放)→
窗口挂载 v3(PIT 上界 = window_end 日终)→ 一次性容器(``--mode
factor_series``)→ canonical ``factor_series.json`` 解析 + 质量门指标
(阈值复用 ``research_sandbox_*``)。**签名被 #360(编排接入)依赖,
不可偏离**。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from finboard_backtest.research_sandbox.data_mount import (
    WindowDataMount,
    build_window_data_mount,
)
from finboard_backtest.research_sandbox.errors import (
    OOM_KILLED,
    OUTPUT_CONTRACT_VIOLATION,
    RUNTIME_ERROR,
    SANDBOX_UNAVAILABLE,
    STATIC_VALIDATION_FAILED,
    TIMEOUT,
    SandboxError,
)
from finboard_backtest.research_sandbox.factor_publish import (
    SeriesQualityReport,
    check_series_quality,
)

_STATS_INTERVAL_SECONDS = 1.0
_MEM_USAGE_RE = re.compile(r"^([\d.]+)\s*([KMGT]i?B)", re.IGNORECASE)
_MEM_UNITS = {"b": 1.0, "kb": 1024.0, "kib": 1024.0, "mb": 1024.0**2,
              "mib": 1024.0**2, "gb": 1024.0**3, "gib": 1024.0**3,
              "tb": 1024.0**4, "tib": 1024.0**4}

#: docker 自身失败的退出码(daemon/可执行问题)→ sandbox_unavailable
_DOCKER_EXIT_CODES = frozenset({125, 126, 127})


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
    #: 执行协议(issue #218 / #359):factor = factor.compute→scores,
    #: strategy = strategy.decide→targets(挂载清单 v2 含权重回显),
    #: factor_series = factor.compute_series→factor_series.json(挂载 v3)
    mode: str = "factor"


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
            "--mode",
            spec.mode,
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


# --------------------------------------------------------------------------- #
# 区间因子执行(issue #359;编排接入见 #360,签名不可偏离)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FactorSeriesRunSpec:
    """一次 ``factor.compute_series`` 区间沙箱执行的全部输入。

    ``release_id``:挂载锚定的 bars 主发布(canonical factor_series.json
    的 ``release_id``);``dataset_release_ids``:参与挂载的全部发布
    (含研究数据发布);``dates``:平台推导的窗口内决策日(升序)。
    """

    code_artifact: str
    code_commit: str
    release_id: str
    dataset_release_ids: tuple[str, ...]
    params: dict[str, Any]
    window_start: date
    window_end: date
    dates: tuple[date, ...]


@dataclass(frozen=True)
class FactorSeriesOutput:
    """区间沙箱执行的产出(canonical JSON 解析形式 + 质量门指标)。

    ``values``:``{date: {symbol: float | None}}``(canonical JSON 的解析
    形式,非有限值已归一为 None);``quality``:逐日截面口径的质量门指标
    (阈值来自 ``research_sandbox_max_nan_ratio`` /
    ``research_sandbox_min_coverage``;``passed=False`` 由调用方决定处置,
    本函数不因质量门抛错)。``series_checksum``:canonical JSON 文件
    SHA256(存储 issue 的产物锚点)。
    """

    code_artifact: str
    code_commit: str
    release_id: str
    dataset_release_ids: tuple[str, ...]
    params: dict[str, Any]
    window_start: date
    window_end: date
    dates: tuple[date, ...]
    values: dict[date, dict[str, float | None]]
    quality: SeriesQualityReport
    metrics: dict[str, Any]
    usage: dict[str, Any]
    mount_manifest_checksum: str
    series_checksum: str
    workspace_dir: Path
    image: str
    image_digest: str
    #: 本次执行实际使用的窗口挂载(issue #371,仅内存传递不落库):编排方
    #: (#360 执行器)据此为审计变体派生更紧上界的过滤挂载,免全量重物化。
    mount: WindowDataMount | None = None


def _validate_series_spec(spec: FactorSeriesRunSpec) -> None:
    if not spec.code_artifact or not spec.code_commit:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            "FactorSeriesRunSpec 须携带 code_artifact 与 code_commit",
        )
    if not spec.release_id or not spec.dataset_release_ids:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            "FactorSeriesRunSpec 须携带 release_id 与非空 dataset_release_ids",
        )
    if not spec.dates:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            "FactorSeriesRunSpec.dates 不能为空(窗口内决策日序列)",
        )
    if list(spec.dates) != sorted(spec.dates):
        raise SandboxError(OUTPUT_CONTRACT_VIOLATION, "dates 须按升序排列")
    outside = [
        d for d in spec.dates if d < spec.window_start or d > spec.window_end
    ]
    if outside:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            f"dates 含窗口外决策日: {outside[:3]}"
            f"(window [{spec.window_start}, {spec.window_end}])",
        )


async def run_factor_series_container(
    spec: FactorSeriesRunSpec,
    *,
    settings: Any | None = None,
    driver: DockerDriver | None = None,
    release_provider_factory: Callable[[str], Any] | None = None,
    workspace_root: Path | None = None,
    mount_override: WindowDataMount | None = None,
    mount_on_batch: Callable[[int, int], None] | None = None,
) -> FactorSeriesOutput:
    """执行一次区间因子沙箱容器(issue #359;#360 编排调用)。

    链路:spec 校验 → 沙箱开关/镜像 digest → 代码解析(静态校验重放)→
    窗口挂载 v3(PIT 上界 = window_end 日终,窗口外数据 fail-closed)→
    一次性容器(``--mode factor_series``,canonical ``factor_series.json``)
    → 失败分类(:mod:`errors` 码,抛 :class:`SandboxError`)→ 质量门指标
    (不抛错,由调用方按 ``quality.passed`` 处置)→ 归档 stdout/stderr/
    container.json/usage.json 到 workspace。

    ``mount_override``(issue #371,审计变体路径):传入已构建的窗口挂载
    时跳过 provider 物化,直接以该挂载运行容器——挂载与 spec 的一致性经
    :func:`_validate_mount_override` fail-closed 校验。缺省 None 走原路径。

    ``mount_on_batch``(issue #441,可选):主构建挂载逐标的批次进度
    ``(done, total)``,透传给 :func:`build_window_data_mount`;缺省 None
    零行为变化。``mount_override`` 路径无物化,不产生批次事件。

    依赖注入(测试与 #360 编排用,均可缺省走默认):``settings`` 缺省经
    ``finboard_app.config.load_settings`` 延迟加载;``driver`` 缺省走
    ``docker`` CLI;``release_provider_factory`` 缺省从研究数据发布表解析
    checksum 后构造 ``FrozenReleaseProvider``(需要 DB);``workspace_root``
    缺省 ``settings.research_sandbox_workspace_root``。
    """
    _validate_series_spec(spec)
    if settings is None:
        settings = _default_series_settings()
    if settings is None:
        raise SandboxError(
            SANDBOX_UNAVAILABLE, "无法加载 settings,区间因子执行不可用"
        )
    if not getattr(settings, "research_sandbox_enabled", False):
        raise SandboxError(
            "sandbox_disabled",
            "研究沙箱未启用(research_sandbox_enabled=false);区间因子执行"
            "需要本机 Docker Desktop 与已构建镜像 docker/research-sandbox",
        )
    driver = driver or SubprocessDockerDriver(
        getattr(settings, "research_sandbox_docker_bin", "docker")
    )
    image = settings.research_sandbox_image
    digest = await driver.image_digest(image)

    code = await _resolve_series_code(settings, spec)
    workspace = workspace_root or Path(settings.research_sandbox_workspace_root)
    run_dir = workspace / f"FSC-{uuid.uuid4().hex[:12]}"
    _stage_series_code(run_dir, code, spec.params)

    if mount_override is not None:
        _validate_mount_override(spec, mount_override)
        mount = mount_override
        release_checksums: dict[str, str] = {}
    else:
        providers, release_checksums = await _series_providers(
            spec, release_provider_factory, settings
        )
        mount = await build_window_data_mount(
            providers=providers,
            window_start=spec.window_start,
            window_end=spec.window_end,
            dates=spec.dates,
            out_root=run_dir / "data",
            code_artifact=spec.code_artifact,
            code_commit=spec.code_commit,
            release_id=spec.release_id,
            dataset_release_ids=spec.dataset_release_ids,
            on_batch=mount_on_batch,
        )

    result = await ResearchSandboxRunner(driver).run(
        SandboxRunSpec(
            image=image,
            code_dir=run_dir / "code",
            data_dir=mount.root,
            out_dir=run_dir / "out",
            timeout_seconds=settings.research_sandbox_timeout_seconds,
            memory_mb=settings.research_sandbox_memory_mb,
            cpus=settings.research_sandbox_cpus,
            pids_limit=settings.research_sandbox_pids_limit,
            user=settings.research_sandbox_user,
            mode="factor_series",
        )
    )
    _archive_series_run(run_dir, result, image, mount)
    series_path = run_dir / "out" / "factor_series.json"
    metrics = _read_series_metrics(run_dir / "out")
    error = _classify_series(result, run_dir / "out")
    if error is not None:
        code_err, summary = error
        raise SandboxError(code_err, summary)
    payload = _parse_series_payload(series_path, spec)

    quality = check_series_quality(
        payload["values"],
        dates=spec.dates,
        universe=mount.symbols,
        max_nan_ratio=settings.research_sandbox_max_nan_ratio,
        min_coverage=settings.research_sandbox_min_coverage,
    )
    return FactorSeriesOutput(
        code_artifact=payload["code_artifact"],
        code_commit=payload["code_commit"],
        release_id=payload["release_id"],
        dataset_release_ids=payload["dataset_release_ids"],
        params=payload["params"],
        window_start=payload["window_start"],
        window_end=payload["window_end"],
        dates=payload["dates"],
        values=payload["values"],
        quality=quality,
        metrics=metrics or {},
        usage={
            **result.usage,
            "duration_seconds": result.duration_seconds,
            "release_checksums": release_checksums,
        },
        mount_manifest_checksum=mount.manifest_checksum,
        series_checksum=_sha256_file(series_path),
        workspace_dir=run_dir,
        image=image,
        image_digest=digest,
        mount=mount,
    )


def _default_series_settings() -> Any | None:
    from finboard_backtest.background_jobs.executors._providers import (
        default_settings_factory,
    )

    return default_settings_factory()


async def _resolve_series_code(
    settings: Any, spec: FactorSeriesRunSpec
) -> dict[str, str]:
    """按 (kind=factor, name, commit) 解析代码并重放 #215 静态校验。"""
    from finboard_backtest.research_code import (
        ResearchCodeError,
        ResearchCodeService,
        compute_checksum,
        validate_submission,
    )

    service = ResearchCodeService.from_path(settings.research_code_repo_path)
    if not service.exists(
        kind="factor", name=spec.code_artifact, commit=spec.code_commit
    ):
        raise SandboxError(
            "missing_research_code",
            f"git 仓库中不存在该因子代码版本: factor/"
            f"{spec.code_artifact}@{spec.code_commit[:12]}",
        )
    try:
        code = service.read(
            kind="factor", name=spec.code_artifact, commit=spec.code_commit
        )
    except ResearchCodeError as exc:
        raise SandboxError("missing_research_code", str(exc)) from exc
    issues = validate_submission(
        kind="factor",
        name=spec.code_artifact,
        files=code,
        max_files=settings.research_code_max_files,
        max_file_bytes=settings.research_code_max_file_bytes,
    )
    if issues:
        summary = (
            "因子代码静态校验失败("
            + str(len(issues))
            + " 个问题):\n"
            + "\n".join(issue.render() for issue in issues[:20])
        )
        raise SandboxError(STATIC_VALIDATION_FAILED, summary)
    _: str = compute_checksum(code)
    return code


async def _series_providers(
    spec: FactorSeriesRunSpec,
    release_provider_factory: Callable[[str], Any] | None,
    settings: Any,
) -> tuple[list[Any], dict[str, str]]:
    """构造挂载 providers;返回 (providers, {release_id: checksum})。

    ``release_provider_factory`` 注入时直接使用(测试/#360 编排免 DB);
    否则从研究数据发布表解析 checksum 后构造 ``FrozenReleaseProvider``
    (与 ResearchCodeRunExecutor._build_mount 同口径)。

    provider 迭代序见 :func:`_series_provider_ids`(bars 主发布在前,#371)。
    """
    if release_provider_factory is not None:
        return (
            [
                release_provider_factory(rid)
                for rid in _series_provider_ids(spec)
            ],
            {},
        )
    import os

    from finboard_data.releases import FrozenReleaseProvider
    from finboard_persistence import (
        ResearchDatasetReleaseRepository,
        create_async_engine,
        session_factory,
    )

    engine = create_async_engine(settings.db_url)
    try:
        maker = session_factory(engine)
        root = Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"))
        providers: list[Any] = []
        checksums: dict[str, str] = {}
        async with maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            for release_id in _series_provider_ids(spec):
                release = await repo.get(release_id)
                if release is None:
                    raise SandboxError(
                        "dataset_release_unavailable",
                        f"研究数据发布不存在: {release_id}",
                    )
                checksums[release_id] = release.release_checksum
                providers.append(
                    FrozenReleaseProvider(
                        release_root=root,
                        release_id=release_id,
                        expected_checksum=release.release_checksum,
                    )
                )
        return providers, checksums
    finally:
        await engine.dispose()


def _series_provider_ids(spec: FactorSeriesRunSpec) -> list[str]:
    """挂载 provider 的 release_id 迭代序(issue #371)。

    bars 主发布(``spec.release_id``)**必须**进挂载 —— 挂载的行情行全部
    来自它;研究发布联合集去重后跟随。此前只挂 dataset_release_ids,而
    MCP 入队层又禁止把 bars 主发布放进联合集,任何真实 build 必然在走完
    全量物化后报「窗口挂载不含任何行情行」(BJ-7DA144 事故根因)。
    """
    ids = [spec.release_id]
    for rid in spec.dataset_release_ids:
        if rid != spec.release_id:
            ids.append(rid)
    return ids


def _validate_mount_override(
    spec: FactorSeriesRunSpec, mount: WindowDataMount
) -> None:
    """mount_override 与 spec 的一致性 fail-closed 校验(issue #371)。

    审计变体容器只应看到「截断后的窗口」:挂载的窗口字段、代码与发布溯源
    必须与 spec 逐项一致,任何错位都拒绝运行(防错误复用基线挂载把窗口后
    数据泄进变体容器)。
    """
    checks: tuple[tuple[str, object, object], ...] = (
        ("code_artifact", mount.code_artifact, spec.code_artifact),
        ("code_commit", mount.code_commit, spec.code_commit),
        ("release_id", mount.release_id, spec.release_id),
        (
            "dataset_release_ids",
            list(mount.dataset_release_ids),
            list(spec.dataset_release_ids),
        ),
        ("window_start", mount.window_start, spec.window_start),
        ("window_end", mount.window_end, spec.window_end),
        ("dates", list(mount.dates), list(spec.dates)),
    )
    mismatches = [
        f"{name}: mount={actual!r} vs spec={expected!r}"
        for name, actual, expected in checks
        if actual != expected
    ]
    if mismatches:
        raise SandboxError(
            "mount_spec_mismatch",
            "mount_override 与 spec 不一致: " + "; ".join(mismatches),
        )
    if not mount.manifest_path.exists():
        raise SandboxError(
            "mount_spec_mismatch",
            f"mount_override 缺少挂载清单: {mount.manifest_path}",
        )


def _stage_series_code(
    run_dir: Path, code: dict[str, str], params: dict[str, Any]
) -> None:
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in code.items():
        target = code_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    if params:
        (code_dir / "params.json").write_text(
            json.dumps(params, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _archive_series_run(
    run_dir: Path, result: SandboxRunResult, image: str, mount: WindowDataMount
) -> None:
    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    (run_dir / "container.json").write_text(
        json.dumps(
            {
                "container_id": result.container_id,
                "image": image,
                "image_digest": result.image_digest,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "oom_killed": result.oom_killed,
                "duration_seconds": result.duration_seconds,
                "command": result.command,
                "mount_manifest_checksum": mount.manifest_checksum,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "usage.json").write_text(
        json.dumps(
            {**result.usage, "duration_seconds": result.duration_seconds},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_series_metrics(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / "metrics.json"
    if not path.exists():
        return None
    try:
        parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return parsed
    except (OSError, json.JSONDecodeError):
        return None


def _classify_series(
    result: SandboxRunResult, out_dir: Path
) -> tuple[str, str] | None:
    """(error_code, summary);成功返回 None。口径对齐 executor/strategy_exec。"""
    if result.timed_out:
        return TIMEOUT, f"容器超过墙钟超时({result.duration_seconds}s)被 kill"
    if result.oom_killed or result.exit_code == 137:
        return OOM_KILLED, f"容器内存超限被 OOM kill(exit={result.exit_code})"
    if result.exit_code == 0:
        if (out_dir / "factor_series.json").exists():
            return None
        return OUTPUT_CONTRACT_VIOLATION, "exit 0 但缺少 factor_series.json"
    if result.exit_code == 3:
        return OUTPUT_CONTRACT_VIOLATION, _series_error_message(out_dir) or (
            "输出契约不符"
        )
    if result.exit_code == 4:
        return RUNTIME_ERROR, _series_error_message(out_dir) or (
            result.stderr.strip()[:500] or "容器内运行时异常"
        )
    if result.exit_code in _DOCKER_EXIT_CODES:
        return SANDBOX_UNAVAILABLE, (
            f"docker 运行失败(exit={result.exit_code}): "
            f"{result.stderr.strip()[:400]}"
        )
    return RUNTIME_ERROR, (
        f"容器非预期退出码 {result.exit_code}: "
        f"{_series_error_message(out_dir) or result.stderr.strip()[:400]}"
    )


def _series_error_message(out_dir: Path) -> str | None:
    path = out_dir / "error.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    message = payload.get("message")
    return str(message) if message else None


def _parse_series_payload(
    path: Path, spec: FactorSeriesRunSpec
) -> dict[str, Any]:
    """解析 canonical ``factor_series.json`` 并对照 spec 校验一致性。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            f"factor_series.json 不可解析: {exc}",
        ) from exc
    try:
        parsed: dict[str, Any] = {
            "code_artifact": str(raw["code_artifact"]),
            "code_commit": str(raw["code_commit"]),
            "release_id": str(raw["release_id"]),
            "dataset_release_ids": tuple(
                str(rid) for rid in raw["dataset_release_ids"]
            ),
            "params": dict(raw.get("params") or {}),
            "window_start": date.fromisoformat(str(raw["window_start"])),
            "window_end": date.fromisoformat(str(raw["window_end"])),
            "dates": tuple(
                date.fromisoformat(str(d)) for d in raw.get("dates", ())
            ),
            "values": {
                date.fromisoformat(str(day)): {
                    str(symbol): (
                        float(value) if value is not None else None
                    )
                    for symbol, value in cross.items()
                }
                for day, cross in (raw.get("values") or {}).items()
            },
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            f"factor_series.json 结构不符 canonical 契约: {exc}",
        ) from exc
    if int(raw.get("protocol_version", 0)) != 2:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            f"factor_series.json protocol_version 须为 2,收到 "
            f"{raw.get('protocol_version')!r}",
        )
    if parsed["dates"] != spec.dates:
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            f"factor_series.json dates 与 spec 不一致: 文件 "
            f"{len(parsed['dates'])} 日 vs spec {len(spec.dates)} 日",
        )
    if parsed["code_commit"] != spec.code_commit or (
        parsed["release_id"] != spec.release_id
    ):
        raise SandboxError(
            OUTPUT_CONTRACT_VIOLATION,
            "factor_series.json 溯源字段与 spec 不一致"
            f"(code_commit/release_id: {parsed['code_commit'][:12]}/"
            f"{parsed['release_id']})",
        )
    return parsed


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "DockerDriver",
    "FactorSeriesOutput",
    "FactorSeriesRunSpec",
    "ResearchSandboxRunner",
    "SandboxRunResult",
    "SandboxRunSpec",
    "SubprocessDockerDriver",
    "run_factor_series_container",
]
