"""``user_code`` 策略的沙箱逐决策执行(issue #218)。

research run(multi_period / single_shot)在每个决策日调用一次沙箱容器执行
``strategy.decide(ctx) -> targets``。与 #216 的独立 ``research_code_run``
执行器共用 runner / data_mount / 静态校验 / 失败分类原语,差异:

* 代码在 run 生命周期内**解析一次**(manifest 冻结的 commit;静态校验重放,
  防 git 仓库内容漂移),逐决策只重物化挂载(PIT + 当前权重回显);
* 每次调用是一次性容器(``--mode strategy``),输出 ``targets.parquet``:
  权重契约由 kit 的 ``normalize_strategy_result`` 在容器内把关(NaN/inf/
  重复 symbol → exit 3);
* 不落 ``research_code_runs`` 行(那是独立沙箱任务的记账单位);provenance
  (commit / 镜像 digest / 逐决策 targets checksum)由 research run 的
  report ``sandbox_provenance`` 段归档。

失败分类沿用 #216 语义;失败把 research run 置 FAILED(策略代码缺陷不是
可重试事故,唯一可重试的是 docker 自身不可用 —— 该情形抛 retryable
标记交由上层处理)。

边界:纯离线研究域,容器无网络无凭证,不触实盘任何组件;decide 只能输出
目标权重,不触任何订单语义。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from finboard_backtest.research_code import (
    ResearchCodeError,
    ResearchCodeService,
    compute_checksum,
    validate_submission,
)
from finboard_backtest.research_sandbox.data_mount import (
    SandboxMountError,
    build_data_mount,
)
from finboard_backtest.research_sandbox.errors import (
    OOM_KILLED,
    OUTPUT_CONTRACT_VIOLATION,
    RUNTIME_ERROR,
    SANDBOX_UNAVAILABLE,
    TIMEOUT,
    SandboxError,
)
from finboard_backtest.research_sandbox.runner import (
    ResearchSandboxRunner,
    SandboxRunResult,
    SandboxRunSpec,
    SubprocessDockerDriver,
)

USER_CODE_STRATEGY_KIND = "user_code"
#: user_code 策略代码 artifact 的 research_code kind(#215 白名单同值)
STRATEGY_CODE_KIND = "strategy"

_DOCKER_EXIT_CODES = frozenset({125, 126, 127})


class UserCodeExecutionError(Exception):
    """user_code 策略沙箱执行失败(research run 置 FAILED)。"""

    def __init__(self, code: str, summary: str, *, retryable: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable


@dataclass(frozen=True)
class StrategyDecisionOutcome:
    """一次 decide 调用的结果(权重 + 逐决策 provenance 记录)。"""

    weights: dict[str, float]
    record: dict[str, Any]


@dataclass
class StrategySandboxCaller:
    """research run 内的沙箱 decide 调用方(代码解析一次,逐决策执行)。

    ``create`` 完成:沙箱开关检查 → 代码解析(manifest 冻结 commit)→
    静态校验重放 → 镜像 digest 解析 → 代码 staging(workspace 按 run 隔离)。
    ``decide`` 每决策日物化挂载并跑一次性容器。
    """

    code: dict[str, str]
    commit: str
    code_checksum: str
    artifact_name: str
    image: str
    image_digest: str
    runner: ResearchSandboxRunner
    run_dir: Path
    providers: list[Any]
    timeout_seconds: float
    memory_mb: int
    cpus: float
    pids_limit: int
    user: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    async def create(
        cls,
        *,
        settings: Any,
        artifact_name: str,
        commit: str | None,
        release_provider_factory: Callable[[str], Any],
        dataset_release_ids: tuple[str, ...] | list[str],
        run_id: str,
        params: Mapping[str, Any] | None = None,
    ) -> StrategySandboxCaller:
        if not getattr(settings, "research_sandbox_enabled", False):
            raise UserCodeExecutionError(
                "sandbox_disabled",
                "研究沙箱未启用(research_sandbox_enabled=false);user_code 策略"
                "执行需要本机 Docker Desktop 与已构建镜像 docker/research-sandbox",
            )
        service = ResearchCodeService.from_path(settings.research_code_repo_path)
        if commit is None:
            # 入队门控已把 active commit 冻结进 manifest;执行期只接受冻结值,
            # 不在此解析 active(避免 run 生命周期内 artifact 状态漂移)。
            raise UserCodeExecutionError(
                "missing_research_code",
                f"user_code 策略 {artifact_name!r} 的 manifest 未冻结 commit;"
                "入队路径异常,请重新入队",
            )
        if not service.exists(kind=STRATEGY_CODE_KIND, name=artifact_name, commit=commit):
            raise UserCodeExecutionError(
                "missing_research_code",
                f"git 仓库中不存在该策略代码版本: {STRATEGY_CODE_KIND}/"
                f"{artifact_name}@{commit[:12]}",
            )
        try:
            code = service.read(kind=STRATEGY_CODE_KIND, name=artifact_name, commit=commit)
        except ResearchCodeError as exc:
            raise UserCodeExecutionError("missing_research_code", str(exc)) from exc
        issues = validate_submission(
            kind=STRATEGY_CODE_KIND,
            name=artifact_name,
            files=code,
            max_files=settings.research_code_max_files,
            max_file_bytes=settings.research_code_max_file_bytes,
        )
        if issues:
            summary = (
                "策略代码静态校验失败("
                + str(len(issues))
                + " 个问题):\n"
                + "\n".join(issue.render() for issue in issues[:20])
            )
            raise UserCodeExecutionError("static_validation_failed", summary)
        docker_bin = getattr(settings, "research_sandbox_docker_bin", "docker")
        runner = ResearchSandboxRunner(SubprocessDockerDriver(docker_bin))
        image = settings.research_sandbox_image
        try:
            digest = await SubprocessDockerDriver(docker_bin).image_digest(image)
        except SandboxError as exc:
            raise UserCodeExecutionError(SANDBOX_UNAVAILABLE, str(exc), retryable=True) from exc
        run_dir = Path(settings.research_sandbox_workspace_root) / run_id
        _stage_code(run_dir, code, dict(params or {}))
        providers = [release_provider_factory(release_id) for release_id in dataset_release_ids]
        return cls(
            code=code,
            commit=commit,
            code_checksum=compute_checksum(code),
            artifact_name=artifact_name,
            image=image,
            image_digest=digest,
            runner=runner,
            run_dir=run_dir,
            providers=providers,
            timeout_seconds=settings.research_sandbox_timeout_seconds,
            memory_mb=settings.research_sandbox_memory_mb,
            cpus=settings.research_sandbox_cpus,
            pids_limit=settings.research_sandbox_pids_limit,
            user=settings.research_sandbox_user,
            params=dict(params or {}),
        )

    def provenance_header(self, *, mode: str, decision_count: int) -> dict[str, Any]:
        """report ``sandbox_provenance`` 的公共头(issue #218 验收:归档
        code commit 与沙箱镜像 digest)。"""
        return {
            "artifact_name": self.artifact_name,
            "kind": STRATEGY_CODE_KIND,
            "commit": self.commit,
            "code_checksum": self.code_checksum,
            "image": self.image,
            "image_digest": self.image_digest,
            "artifact_dir": str(self.run_dir),
            "resource_limits": {
                "timeout_seconds": self.timeout_seconds,
                "memory_mb": self.memory_mb,
                "cpus": self.cpus,
                "pids_limit": self.pids_limit,
                "user": self.user,
            },
            "execution_mode": mode,
            "decision_count": decision_count,
        }

    async def decide(
        self,
        *,
        decision_index: int,
        decision_at: datetime,
        symbols: tuple[str, ...],
        current_weights: Mapping[str, float],
        strategy_constraints: Mapping[str, Any],
    ) -> StrategyDecisionOutcome:
        """单决策日:物化挂载(PIT + 权重回显 + 约束视图)→ 容器 decide。"""
        decision_dir = self.run_dir / f"D{decision_index:04d}"
        try:
            mount = await build_data_mount(
                providers=self.providers,
                decision_at=decision_at,
                out_root=decision_dir / "data",
                symbols=symbols,
                current_weights=current_weights,
                strategy_constraints=strategy_constraints,
            )
        except SandboxMountError as exc:
            raise UserCodeExecutionError("sandbox_mount_failed", str(exc)) from exc
        result = await self.runner.run(
            SandboxRunSpec(
                image=self.image,
                code_dir=self.run_dir / "code",
                data_dir=decision_dir / "data",
                out_dir=decision_dir / "out",
                timeout_seconds=self.timeout_seconds,
                memory_mb=self.memory_mb,
                cpus=self.cpus,
                pids_limit=self.pids_limit,
                user=self.user,
                mode="strategy",
            )
        )
        _archive_logs(decision_dir, result, self.image)
        weights, metrics = _read_targets(decision_dir / "out")
        error = _classify_strategy(result, decision_dir / "out")
        record = {
            "index": decision_index,
            "decision_at": decision_at.isoformat(),
            "mount_manifest_checksum": mount.manifest_checksum,
            "targets_checksum": (
                _sha256(decision_dir / "out" / "targets.parquet")
                if (decision_dir / "out" / "targets.parquet").exists()
                else None
            ),
            "container": {
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "max_mem_mb": result.usage.get("max_mem_mb"),
            },
            "archive": {
                "directory": str(decision_dir),
                "stdout": str(decision_dir / "stdout.txt"),
                "stderr": str(decision_dir / "stderr.txt"),
                "container": str(decision_dir / "container.json"),
            },
            "metrics": metrics,
        }
        if error is not None:
            code, summary = error
            record["error_code"] = code
            raise UserCodeExecutionError(
                code,
                f"user_code 决策 {decision_index}"
                f"({decision_at.date().isoformat()})沙箱执行失败: {summary}",
                retryable=code == SANDBOX_UNAVAILABLE,
            )
        record["n_targets"] = len(weights)
        record["gross_exposure"] = round(sum(abs(weight) for weight in weights.values()), 6)
        return StrategyDecisionOutcome(weights=weights, record=record)


def _stage_code(run_dir: Path, code: dict[str, str], params: dict[str, Any]) -> None:
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in code.items():
        target = code_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    if params:
        (code_dir / "params.json").write_text(
            json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _archive_logs(
    decision_dir: Path, result: SandboxRunResult, image: str
) -> None:
    decision_dir.mkdir(parents=True, exist_ok=True)
    (decision_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (decision_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    (decision_dir / "container.json").write_text(
        json.dumps(
            {
                "container_id": result.container_id,
                "image": image,
                "image_digest": result.image_digest,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "oom_killed": result.oom_killed,
                "duration_seconds": result.duration_seconds,
                "usage": result.usage,
                "command": result.command,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_targets(out_dir: Path) -> tuple[dict[str, float], dict[str, Any] | None]:
    """读 targets.parquet 为 ``{symbol: weight}``;缺失/坏格式返回 ({}, None)。"""
    path = out_dir / "targets.parquet"
    if not path.exists():
        return {}, None
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["symbol", "weight"])
        symbols = table.column("symbol").to_pylist()
        values = table.column("weight").to_pylist()
        weights = {
            str(symbol): float(value)
            for symbol, value in zip(symbols, values, strict=True)
            if value is not None
        }
    except Exception:
        return {}, None
    metrics: dict[str, Any] | None = None
    metrics_path = out_dir / "metrics.json"
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metrics = None
    return weights, metrics


def _classify_strategy(result: SandboxRunResult, out_dir: Path) -> tuple[str, str] | None:
    """(error_code, summary);成功返回 None。口径对齐 #216 executor。"""
    if result.timed_out:
        return TIMEOUT, (f"容器超过墙钟超时({result.duration_seconds}s)被 kill")
    if result.oom_killed or result.exit_code == 137:
        return OOM_KILLED, f"容器内存超限被 OOM kill(exit={result.exit_code})"
    if result.exit_code == 0:
        if (out_dir / "targets.parquet").exists():
            return None
        return OUTPUT_CONTRACT_VIOLATION, "exit 0 但缺少 targets.parquet"
    if result.exit_code == 3:
        message = _error_message(out_dir) or "输出契约不符"
        return OUTPUT_CONTRACT_VIOLATION, message
    if result.exit_code == 4:
        message = _error_message(out_dir) or (result.stderr.strip()[:500] or "容器内运行时异常")
        return RUNTIME_ERROR, message
    if result.exit_code in _DOCKER_EXIT_CODES:
        return SANDBOX_UNAVAILABLE, (
            f"docker 运行失败(exit={result.exit_code}): {result.stderr.strip()[:400]}"
        )
    return RUNTIME_ERROR, (
        f"容器非预期退出码 {result.exit_code}: "
        f"{_error_message(out_dir) or result.stderr.strip()[:400]}"
    )


def _error_message(out_dir: Path) -> str | None:
    path = out_dir / "error.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    message = payload.get("message")
    return str(message) if message else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "STRATEGY_CODE_KIND",
    "USER_CODE_STRATEGY_KIND",
    "StrategyDecisionOutcome",
    "StrategySandboxCaller",
    "UserCodeExecutionError",
]
