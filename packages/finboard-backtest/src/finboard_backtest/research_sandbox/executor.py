"""``research_code_run`` 执行器 —— 沙箱执行接入统一队列(issue #216;
#359 增 mode=factor_series 区间执行)。

worker 领取 ``kind=research_code_run`` 任务后,按 payload 重建执行输入::

    {
      "kind": "factor",                # v1 仅 factor
      "mode": "factor_series",         # 可省 = factor(v1 单日,issue #359)
      "name": "mom20",
      "commit": "<sha>",               # 可省 = active+passed 引用
      "artifact_id": "RC-...",         # 可选,与 (kind,name) 二选一定位
      "dataset_release_ids": ["DR-..."],   # 至少一个 bars 类发布
      "decision_at": "2024-06-03T07:00:00+00:00",  # tz-aware ISO;
                                        # factor_series 模式可省(= window_end 日终)
      "window_start": "2024-01-01",    # factor_series 模式必填(issue #359)
      "window_end": "2024-06-30",      # factor_series 模式必填
      "dates": ["2024-01-31", ...],    # factor_series 模式必填(窗口决策日,升序)
      "symbols": ["600000.SH"],        # 可省 = bars 发布全部标的
      "params": {...}                  # 可省,覆盖 manifest.params
    }

流程(6 个进度阶段):解析 → 沙箱开关/镜像 → 解析代码(执行端重放 #215
静态校验)→ 数据挂载(v1 PIT 物理隔离 / factor_series 窗口挂载 v3)→
容器执行 → 归档登记。run 记录 ``research_code_runs`` 持有三向引用:
code commit x 数据 release x 输出 checksum;stdout/stderr/退出码/资源
用量归档 workspace,失败信息含峰值内存与日志路径。

issue #217:成功输出先过质量门(NaN 比例 / 覆盖率,阈值见
``research_sandbox_max_nan_ratio`` / ``research_sandbox_min_coverage``),
不合格拒绝入库(run failed,错误指明阈值);通过则把有限值观测化为
``FeatureSnapshot`` 落库(``source_run_id`` 锚定 run,manifest checksum
做数据面锚点),``research_code_runs.output_snapshot_id`` 回填引用。
issue #359:mode=factor_series 走同一质量门(逐日截面口径,
``check_series_quality``),不合格同样 run failed;canonical
``factor_series.json`` 归档 workspace,序列级快照落库随存储 issue(不
在本执行器 publish 单日 snapshot)。

失败分类见 :mod:`errors`(``timeout`` / ``oom_killed`` /
``output_contract_violation`` / ``runtime_error`` /
``static_validation_failed`` / ``sandbox_unavailable``)。

边界:纯离线研究域;容器无网络无凭证,不触实盘任何组件。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.research_code import (
    ResearchCodeError,
    ResearchCodeService,
    compute_checksum,
    is_promoted_artifact,
    promotion_status,
    validate_submission,
)
from finboard_backtest.research_sandbox.data_mount import (
    DataMount,
    SandboxMountError,
    WindowDataMount,
    build_data_mount,
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
    QUALITY_GATE_FAILED,
    QualityGateError,
    build_factor_snapshot,
    check_output_quality,
    check_series_quality,
)
from finboard_backtest.research_sandbox.runner import (
    FactorSeriesRunSpec,
    ResearchSandboxRunner,
    SandboxRunResult,
    SandboxRunSpec,
    SubprocessDockerDriver,
    _parse_series_payload,
)
from finboard_persistence import ResearchCodeRunRepository
from finboard_persistence.research_code_run_repo import generate_run_id

SettingsFactory = Callable[[], Any]
ServiceFactory = Callable[[Any], ResearchCodeService]
RunnerFactory = Callable[[Any], ResearchSandboxRunner]

_TOTAL_STAGES = 6

#: 执行协议维度(issue #359):factor = v1 单日截面,
#: factor_series = v2 区间执行(compute_series + 窗口挂载 v3)。
MODE_FACTOR = "factor"
MODE_FACTOR_SERIES = "factor_series"
_VALID_MODES = frozenset({MODE_FACTOR, MODE_FACTOR_SERIES})

#: docker 自身失败的退出码(daemon/可执行问题)→ sandbox_unavailable(可重试)
_DOCKER_EXIT_CODES = frozenset({125, 126, 127})

SANDBOX_DISABLED = "sandbox_disabled"
KIND_NOT_IMPLEMENTED = "research_code_run_kind_not_implemented"
DATASET_RELEASE_UNAVAILABLE = "dataset_release_unavailable"


@dataclass(frozen=True)
class ResearchCodeRunPayload:
    """``kind=research_code_run`` 的冻结 payload 投影。"""

    kind: str
    name: str
    dataset_release_ids: tuple[str, ...]
    #: v1 单日模式必填;factor_series 模式可省(= window_end 日终)
    decision_at: datetime | None
    commit: str | None = None
    artifact_id: str | None = None
    symbols: tuple[str, ...] | None = None
    params: dict[str, Any] | None = None
    mode: str = MODE_FACTOR
    window_start: date | None = None
    window_end: date | None = None
    dates: tuple[date, ...] | None = None

    def digest(self) -> str:
        """幂等键摘要(name/commit/releases/decision_at/symbols/params)。"""
        h = hashlib.sha256()
        h.update(self.kind.encode())
        h.update(self.name.encode())
        h.update((self.commit or "-").encode())
        for rid in self.dataset_release_ids:
            h.update(rid.encode())
        h.update((self.decision_at or self._series_anchor()).isoformat().encode())
        if self.symbols:
            for s in sorted(self.symbols):
                h.update(s.encode())
        if self.params:
            h.update(json.dumps(self.params, sort_keys=True, ensure_ascii=False).encode())
        if self.mode == MODE_FACTOR_SERIES:
            h.update(self.mode.encode())
            assert self.window_start is not None
            assert self.window_end is not None
            h.update(self.window_start.isoformat().encode())
            h.update(self.window_end.isoformat().encode())
            for d in self.dates or ():
                h.update(d.isoformat().encode())
        return h.hexdigest()[:16]

    def _series_anchor(self) -> datetime:
        """factor_series 无 decision_at 时的锚点(window_end 日终 UTC)。"""
        end = self.window_end
        if end is None:
            # 解析层保证 factor_series 必带 window_end;防御性兜底
            raise ValueError("factor_series payload 缺少 window_end")
        return datetime.combine(end, time(23, 59, 59, 999999), tzinfo=UTC)


@dataclass
class _Outputs:
    """容器输出目录解析结果。"""

    scores_path: Path | None = None
    scores_checksum: str | None = None
    metrics: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)


class ResearchCodeRunExecutor:
    """``kind=research_code_run`` 执行器(单并发,由 worker kind_concurrency 保证)。"""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: SettingsFactory,
        service_factory: ServiceFactory | None = None,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory
        self._service_factory = service_factory or _default_service_factory
        self._runner_factory = runner_factory or _default_runner_factory

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        payload = _parse_payload(job)
        await progress(0, _TOTAL_STAGES, "research_code_run:start")

        settings = self._settings_factory()
        if settings is None:
            raise ExecutorError(
                code="settings_unavailable",
                summary="无法加载 settings,research_code_run 无法执行",
                retryable=True,
            )
        if not getattr(settings, "research_sandbox_enabled", False):
            raise ExecutorError(
                code=SANDBOX_DISABLED,
                summary=(
                    "研究沙箱未启用(research_sandbox_enabled=false);"
                    "启用需本机 Docker Desktop 与已构建镜像 "
                    "docker/research-sandbox"
                ),
                retryable=False,
                context={"job_id": job.job_id},
            )
        if payload.kind != "factor":
            raise ExecutorError(
                code=KIND_NOT_IMPLEMENTED,
                summary=(f"research_code_run v1 仅支持 kind=factor,收到 {payload.kind!r}"),
                retryable=False,
            )
        if payload.mode not in _VALID_MODES:
            raise ExecutorError(
                code=KIND_NOT_IMPLEMENTED,
                summary=(
                    f"research_code_run mode 须为 factor|factor_series,"
                    f"收到 {payload.mode!r}"
                ),
                retryable=False,
            )

        runner = self._runner_factory(settings)
        image = settings.research_sandbox_image
        try:
            driver = SubprocessDockerDriver(
                getattr(settings, "research_sandbox_docker_bin", "docker")
            )
            digest = await driver.image_digest(image)
        except SandboxError as exc:
            raise ExecutorError(
                code=SANDBOX_UNAVAILABLE,
                summary=str(exc),
                retryable=True,
            ) from exc
        await progress(1, _TOTAL_STAGES, "research_code_run:resolve_code")

        service = self._service_factory(settings)
        workspace_root = Path(settings.research_sandbox_workspace_root)

        async with self._session_maker() as session:
            code, artifact_id, commit = await _resolve_code(session, service, payload, settings)
            issues = validate_submission(
                kind=payload.kind,
                name=payload.name,
                files=code,
                max_files=settings.research_code_max_files,
                max_file_bytes=settings.research_code_max_file_bytes,
            )
            run_repo = ResearchCodeRunRepository(session)
            code_checksum = compute_checksum(code)
            run_id = generate_run_id()
            run_dir = workspace_root / run_id
            run = await run_repo.create(
                kind=payload.kind,
                mode=payload.mode,
                name=payload.name,
                commit=commit,
                code_checksum=code_checksum,
                dataset_release_ids=list(payload.dataset_release_ids),
                dataset_release_checksums={},
                decision_at=payload.decision_at or payload._series_anchor(),
                image=image,
                image_digest=digest,
                artifact_dir=str(run_dir),
                job_id=job.job_id,
                artifact_id=artifact_id,
                params=payload.params,
                run_id=run_id,
            )
            if issues:
                summary = (
                    "静态校验失败("
                    + str(len(issues))
                    + " 个问题):\n"
                    + "\n".join(i.render() for i in issues[:20])
                )
                run = await run_repo.mark_terminal(
                    run.run_id,
                    status="failed",
                    error_code=STATIC_VALIDATION_FAILED,
                    error_summary=summary,
                )
                await session.commit()
                await progress(_TOTAL_STAGES, _TOTAL_STAGES, "research_code_run:failed")
                return _failed_result(run.run_id, STATIC_VALIDATION_FAILED, summary)
            await session.commit()
        await progress(2, _TOTAL_STAGES, "research_code_run:mount")

        if payload.mode == MODE_FACTOR_SERIES:
            return await self._execute_factor_series(
                job=job,
                payload=payload,
                settings=settings,
                runner=runner,
                image=image,
                run_id=run_id,
                run_dir=run_dir,
                code=code,
                commit=commit,
                progress=progress,
            )

        try:
            mount, release_checksums = await self._build_mount(payload, run_dir)
        except (SandboxMountError, SandboxError) as exc:
            code_err = getattr(exc, "code", None) or DATASET_RELEASE_UNAVAILABLE
            return await self._fail(
                run_id,
                code_err,
                str(exc),
                progress=progress,
                mount_checksum=None,
            )
        _stage_write_code(run_dir, code, payload.params)
        await progress(3, _TOTAL_STAGES, "research_code_run:execute")

        result = await runner.run(
            SandboxRunSpec(
                image=image,
                code_dir=run_dir / "code",
                data_dir=run_dir / "data",
                out_dir=run_dir / "out",
                timeout_seconds=settings.research_sandbox_timeout_seconds,
                memory_mb=settings.research_sandbox_memory_mb,
                cpus=settings.research_sandbox_cpus,
                pids_limit=settings.research_sandbox_pids_limit,
                user=settings.research_sandbox_user,
            )
        )
        await progress(4, _TOTAL_STAGES, "research_code_run:archive")

        outputs = _read_outputs(run_dir / "out")
        _archive_logs(run_dir, result, image)
        ok, error_code, error_summary = _classify(result, outputs)
        usage = {**result.usage, "duration_seconds": result.duration_seconds}
        _write_json(run_dir / "usage.json", usage)

        # issue #217:成功执行后的输出质量门 + 快照落库。质量门不过 →
        # run 置 failed(quality_gate_failed),错误信息指明阈值与实际值。
        output_snapshot_id: str | None = None
        metrics = outputs.metrics
        if ok and outputs.scores_path is not None:
            scores = _load_scores(outputs.scores_path)
            quality = check_output_quality(
                scores,
                universe_size=len(mount.symbols),
                max_nan_ratio=settings.research_sandbox_max_nan_ratio,
                min_coverage=settings.research_sandbox_min_coverage,
                universe_symbols=mount.symbols,
            )
            metrics = {**(metrics or {}), "quality_gate": quality.as_dict()}
            if not quality.passed:
                ok = False
                error_code = QUALITY_GATE_FAILED
                error_summary = "输出质量门未通过,拒绝入库: " + ";".join(quality.failures)
            else:
                try:
                    assert payload.decision_at is not None
                    snapshot = build_factor_snapshot(
                        factor_artifact_name=payload.name,
                        run_id=run_id,
                        decision_at=payload.decision_at,
                        commit=commit,
                        mount_manifest_checksum=mount.manifest_checksum,
                        scores=scores,
                        quality=quality,
                    )
                    from finboard_persistence import FeatureSnapshotRepository

                    async with self._session_maker() as session:
                        await FeatureSnapshotRepository(session).publish(snapshot)
                        await session.commit()
                    output_snapshot_id = snapshot.snapshot_id
                except QualityGateError as exc:
                    ok = False
                    error_code = QUALITY_GATE_FAILED
                    error_summary = f"输出质量门未通过,拒绝入库: {exc}"

        async with self._session_maker() as session:
            run_repo = ResearchCodeRunRepository(session)
            run = await run_repo.mark_terminal(
                run_id,
                status="succeeded" if ok else "failed",
                error_code=error_code,
                error_summary=error_summary,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                oom_killed=result.oom_killed,
                usage=usage,
                metrics=metrics,
                scores_checksum=outputs.scores_checksum,
                mount_manifest_checksum=mount.manifest_checksum,
                dataset_release_checksums=release_checksums,
                output_snapshot_id=output_snapshot_id,
            )
            await session.commit()
        await progress(_TOTAL_STAGES, _TOTAL_STAGES, f"research_code_run:{run.status}")
        if ok:
            return JobResult(status="succeeded", result_ref=run_id)
        return _failed_result(run_id, error_code or RUNTIME_ERROR, error_summary or "")

    # ---- 内部 -------------------------------------------------------------

    async def _build_mount(
        self, payload: ResearchCodeRunPayload, run_dir: Path
    ) -> tuple[DataMount, dict[str, str]]:
        """物化挂载;返回 (mount, {release_id: release_checksum} 数据侧锚定)。"""
        from finboard_data.releases import FrozenReleaseProvider
        from finboard_persistence import ResearchDatasetReleaseRepository

        root = Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"))
        providers: list[Any] = []
        checksums: dict[str, str] = {}
        async with self._session_maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            for release_id in payload.dataset_release_ids:
                release = await repo.get(release_id)
                if release is None:
                    raise SandboxError(
                        DATASET_RELEASE_UNAVAILABLE,
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
        assert payload.decision_at is not None
        mount = await build_data_mount(
            providers=providers,
            decision_at=payload.decision_at,
            out_root=run_dir / "data",
            symbols=payload.symbols,
        )
        return mount, checksums

    async def _execute_factor_series(
        self,
        *,
        job: JobRecord,
        payload: ResearchCodeRunPayload,
        settings: Any,
        runner: ResearchSandboxRunner,
        image: str,
        run_id: str,
        run_dir: Path,
        code: dict[str, str],
        commit: str,
        progress: ProgressCallback,
    ) -> JobResult:
        """mode=factor_series:窗口挂载 v3 + 区间容器 + 逐日质量门(issue #359)。

        canonical ``factor_series.json`` 归档 workspace 并以文件 SHA256 进
        ``scores_checksum``;序列级快照落库随存储 issue(本执行器不 publish
        单日 snapshot,``output_snapshot_id`` 恒 None)。质量门沿用 #217 语义:
        不合格 run failed(quality_gate_failed),错误指明阈值与实际值。
        """
        _: str = job.job_id  # 归属已在 run 记录 create 时锚定
        try:
            mount, release_checksums = await self._build_window_mount(
                payload, run_dir, commit=commit
            )
        except (SandboxMountError, SandboxError) as exc:
            code_err = getattr(exc, "code", None) or DATASET_RELEASE_UNAVAILABLE
            return await self._fail(
                run_id,
                code_err,
                str(exc),
                progress=progress,
                mount_checksum=None,
            )
        _stage_write_code(run_dir, code, payload.params)
        await progress(3, _TOTAL_STAGES, "research_code_run:execute")

        result = await runner.run(
            SandboxRunSpec(
                image=image,
                code_dir=run_dir / "code",
                data_dir=run_dir / "data",
                out_dir=run_dir / "out",
                timeout_seconds=settings.research_sandbox_timeout_seconds,
                memory_mb=settings.research_sandbox_memory_mb,
                cpus=settings.research_sandbox_cpus,
                pids_limit=settings.research_sandbox_pids_limit,
                user=settings.research_sandbox_user,
                mode=MODE_FACTOR_SERIES,
            )
        )
        await progress(4, _TOTAL_STAGES, "research_code_run:archive")

        series_path = run_dir / "out" / "factor_series.json"
        metrics = _read_series_metrics(run_dir / "out")
        _archive_logs(run_dir, result, image)
        usage = {**result.usage, "duration_seconds": result.duration_seconds}
        _write_json(run_dir / "usage.json", usage)
        error = _classify_series_result(result, series_path)
        ok = error is None
        error_code: str | None = None
        error_summary: str | None = None
        if error is not None:
            error_code, error_summary = error

        series_checksum: str | None = None
        if ok:
            parsed = _parse_series_payload(
                series_path,
                FactorSeriesRunSpec(
                    code_artifact=payload.name,
                    code_commit=commit,
                    release_id=mount.release_id,
                    dataset_release_ids=payload.dataset_release_ids,
                    params=dict(payload.params or {}),
                    window_start=mount.window_start,
                    window_end=mount.window_end,
                    dates=mount.dates,
                ),
            )
            series_checksum = _sha256_file(series_path)
            quality = check_series_quality(
                parsed["values"],
                dates=mount.dates,
                universe=mount.symbols,
                max_nan_ratio=settings.research_sandbox_max_nan_ratio,
                min_coverage=settings.research_sandbox_min_coverage,
            )
            metrics = {**(metrics or {}), "quality_gate": quality.as_dict()}
            if not quality.passed:
                ok = False
                error_code = QUALITY_GATE_FAILED
                error_summary = (
                    "输出质量门未通过,拒绝入库: " + ";".join(quality.failures)
                )

        async with self._session_maker() as session:
            run_repo = ResearchCodeRunRepository(session)
            run = await run_repo.mark_terminal(
                run_id,
                status="succeeded" if ok else "failed",
                error_code=error_code,
                error_summary=error_summary,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                oom_killed=result.oom_killed,
                usage=usage,
                metrics=metrics,
                scores_checksum=series_checksum,
                mount_manifest_checksum=mount.manifest_checksum,
                dataset_release_checksums=release_checksums,
            )
            await session.commit()
        await progress(_TOTAL_STAGES, _TOTAL_STAGES, f"research_code_run:{run.status}")
        if ok:
            return JobResult(status="succeeded", result_ref=run_id)
        return _failed_result(run_id, error_code or RUNTIME_ERROR, error_summary or "")

    async def _build_window_mount(
        self,
        payload: ResearchCodeRunPayload,
        run_dir: Path,
        *,
        commit: str,
    ) -> tuple[WindowDataMount, dict[str, str]]:
        """物化 factor_series 窗口挂载(v3);返回 (mount, release checksums)。"""
        from finboard_data.releases import FrozenReleaseProvider
        from finboard_persistence import ResearchDatasetReleaseRepository

        assert payload.window_start is not None
        assert payload.window_end is not None
        assert payload.dates is not None
        root = Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"))
        providers: list[Any] = []
        checksums: dict[str, str] = {}
        async with self._session_maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            for release_id in payload.dataset_release_ids:
                release = await repo.get(release_id)
                if release is None:
                    raise SandboxError(
                        DATASET_RELEASE_UNAVAILABLE,
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
        mount = await build_window_data_mount(
            providers=providers,
            window_start=payload.window_start,
            window_end=payload.window_end,
            dates=payload.dates,
            out_root=run_dir / "data",
            code_artifact=payload.name,
            code_commit=commit,
            release_id=_anchor_bars_release_id(providers, payload.dataset_release_ids),
            dataset_release_ids=payload.dataset_release_ids,
            symbols=payload.symbols,
        )
        return mount, checksums

    async def _fail(
        self,
        run_id: str,
        error_code: str,
        summary: str,
        *,
        progress: ProgressCallback,
        mount_checksum: str | None,
    ) -> JobResult:
        async with self._session_maker() as session:
            await ResearchCodeRunRepository(session).mark_terminal(
                run_id,
                status="failed",
                error_code=error_code,
                error_summary=summary,
                mount_manifest_checksum=mount_checksum,
            )
            await session.commit()
        await progress(_TOTAL_STAGES, _TOTAL_STAGES, "research_code_run:failed")
        return _failed_result(run_id, error_code, summary)


# --------------------------------------------------------------------------- #
# payload / 代码解析
# --------------------------------------------------------------------------- #


def _parse_payload(job: JobRecord) -> ResearchCodeRunPayload:
    payload = job.payload
    try:
        kind = _require_str(payload, "kind")
        name = _require_str(payload, "name")
        raw_releases = payload.get("dataset_release_ids")
        if (
            not isinstance(raw_releases, list)
            or not raw_releases
            or not all(isinstance(r, str) and r for r in raw_releases)
        ):
            raise ValueError("dataset_release_ids 须为非空字符串数组")
        mode = payload.get("mode", MODE_FACTOR)
        if mode is None:
            mode = MODE_FACTOR
        if not isinstance(mode, str) or mode not in _VALID_MODES:
            raise ValueError(
                f"mode 须为 {'|'.join(sorted(_VALID_MODES))},收到 {mode!r}"
            )
        raw_decision = payload.get("decision_at")
        decision_at: datetime | None = None
        if raw_decision is not None:
            if not isinstance(raw_decision, str):
                raise ValueError("decision_at 须为 ISO 字符串")
            decision_at = datetime.fromisoformat(raw_decision)
            if decision_at.tzinfo is None:
                raise ValueError("decision_at 必须带时区")
        window_start = _parse_payload_date(payload, "window_start")
        window_end = _parse_payload_date(payload, "window_end")
        raw_dates = payload.get("dates")
        if raw_dates is not None and (
            not isinstance(raw_dates, list)
            or not all(
                isinstance(d, str) and date.fromisoformat(d) for d in raw_dates
            )
        ):
            raise ValueError("dates 须为 YYYY-MM-DD 字符串数组")
        dates = (
            tuple(date.fromisoformat(str(d)) for d in raw_dates)
            if raw_dates is not None
            else None
        )
        if mode == MODE_FACTOR_SERIES:
            missing = [
                key
                for key, value in (
                    ("window_start", window_start),
                    ("window_end", window_end),
                    ("dates", dates),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"mode=factor_series 须携带 {missing}(窗口语义,issue #359)"
                )
            assert window_start is not None
            assert window_end is not None
            assert dates is not None
            if list(dates) != sorted(dates):
                raise ValueError("dates 须按升序排列")
            outside = [
                d for d in dates if d < window_start or d > window_end
            ]
            if outside:
                raise ValueError(
                    f"dates 含窗口外决策日: {outside[:3]}"
                    f"(window [{window_start}, {window_end}])"
                )
            if decision_at is None:
                # 锚点 = window_end 日终(run 记录 decision_at 列非空)
                decision_at = datetime.combine(
                    window_end, time(23, 59, 59, 999999), tzinfo=UTC
                )
        elif decision_at is None:
            raise ValueError("decision_at 须为 ISO 字符串")
        symbols = payload.get("symbols")
        if symbols is not None and (
            not isinstance(symbols, list) or not all(isinstance(s, str) and s for s in symbols)
        ):
            raise ValueError("symbols 须为字符串数组")
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params 须为对象")
        commit = payload.get("commit")
        if commit is not None and not isinstance(commit, str):
            raise ValueError("commit 须为字符串")
        artifact_id = payload.get("artifact_id")
        if artifact_id is not None and not isinstance(artifact_id, str):
            raise ValueError("artifact_id 须为字符串")
    except ValueError as exc:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"research_code_run payload 非法: {exc}",
            retryable=False,
            context={"job_id": job.job_id},
        ) from exc
    return ResearchCodeRunPayload(
        kind=kind,
        name=name,
        dataset_release_ids=tuple(raw_releases),
        decision_at=decision_at,
        commit=commit,
        artifact_id=artifact_id,
        symbols=tuple(symbols) if symbols is not None else None,
        params=params,
        mode=mode,
        window_start=window_start,
        window_end=window_end,
        dates=dates,
    )


def _parse_payload_date(payload: dict[str, object], key: str) -> date | None:
    raw = payload.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} 须为 YYYY-MM-DD 字符串")
    return date.fromisoformat(raw)


def _anchor_bars_release_id(
    providers: list[Any], dataset_release_ids: tuple[str, ...]
) -> str:
    """窗口挂载锚定的 bars 主发布(canonical factor_series.json 回填)。"""
    for provider in providers:
        kind = getattr(provider.release.dataset_kind, "value", None)
        if kind == "bars":
            return str(provider.release.release_id)
    return dataset_release_ids[0]


def _read_series_metrics(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / "metrics.json"
    if not path.exists():
        return None
    try:
        parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return parsed
    except (OSError, json.JSONDecodeError):
        return None


def _classify_series_result(
    result: SandboxRunResult, series_path: Path
) -> tuple[str, str] | None:
    """factor_series 容器失败分类;(error_code, summary),成功 None。"""
    if result.timed_out:
        return (
            TIMEOUT,
            f"容器超过墙钟超时({result.duration_seconds}s)被 kill;"
            f"峰值内存 {result.usage.get('max_mem_mb')}MB / "
            f"CPU {result.usage.get('max_cpu_percent')}%",
        )
    if result.oom_killed or result.exit_code == 137:
        return (
            OOM_KILLED,
            f"容器内存超限被 OOM kill(exit={result.exit_code})",
        )
    if result.exit_code == 0:
        if series_path.exists():
            return None
        return (
            OUTPUT_CONTRACT_VIOLATION,
            "exit 0 但缺少 factor_series.json(canonical 输出缺失)",
        )
    if result.exit_code == 3:
        return (
            OUTPUT_CONTRACT_VIOLATION,
            _series_output_error(series_path.parent) or "输出契约不符",
        )
    if result.exit_code == 4:
        return (
            RUNTIME_ERROR,
            _series_output_error(series_path.parent)
            or (result.stderr.strip()[:500] or "容器内运行时异常"),
        )
    if result.exit_code in _DOCKER_EXIT_CODES:
        return (
            SANDBOX_UNAVAILABLE,
            f"docker 运行失败(exit={result.exit_code}): "
            f"{result.stderr.strip()[:400]}",
        )
    return (
        RUNTIME_ERROR,
        f"容器非预期退出码 {result.exit_code}: "
        f"{_series_output_error(series_path.parent) or result.stderr.strip()[:400]}",
    )


def _series_output_error(out_dir: Path) -> str | None:
    path = out_dir / "error.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    message = payload.get("message")
    return str(message) if message else None


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 须为非空字符串")
    return value


async def _resolve_code(
    session: AsyncSession,
    service: ResearchCodeService,
    payload: ResearchCodeRunPayload,
    settings: Any,
) -> tuple[dict[str, str], str | None, str]:
    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    if payload.artifact_id is not None:
        artifact = await repo.get(payload.artifact_id)
        if artifact is None:
            raise ExecutorError(
                code="missing_research_code",
                summary=f"研究代码产物不存在: {payload.artifact_id}",
                retryable=False,
            )
        if artifact.kind != payload.kind or artifact.name != payload.name:
            raise ExecutorError(
                code="invalid_payload",
                summary=(
                    f"artifact_id 与 (kind,name) 不一致: {payload.artifact_id} "
                    f"vs ({payload.kind}, {payload.name})"
                ),
                retryable=False,
            )
        if artifact.status == "retired":
            raise ExecutorError(
                code="invalid_payload",
                summary=f"artifact 已 retired,不能启动新沙箱执行: {payload.artifact_id}",
                retryable=False,
            )
        if artifact.status == "active" and not is_promoted_artifact(artifact):
            raise ExecutorError(
                code="invalid_payload",
                summary=(
                    f"artifact active 但未通过 screen+OOS 晋级门: {payload.artifact_id} "
                    f"promotion_status={promotion_status(artifact)}"
                ),
                retryable=False,
            )
    else:
        artifact = await repo.get_active(kind=payload.kind, name=payload.name)
        if artifact is None:
            raise ExecutorError(
                code="missing_research_code",
                summary=(
                    f"没有 active+passed 的研究代码: kind={payload.kind} "
                    f"name={payload.name}(先提交并完成晋级)"
                ),
                retryable=False,
            )
    commit = payload.commit or artifact.commit
    if commit != artifact.commit:
        raise ExecutorError(
            code="invalid_payload",
            summary=(
                f"指定 commit {commit[:12]} 不是该 artifact 的 active 引用"
                f"(artifact={artifact.commit[:12]});历史版本先 "
                "finboard_research_code_rollback"
            ),
            retryable=False,
        )
    if not service.exists(kind=payload.kind, name=payload.name, commit=commit):
        raise ExecutorError(
            code="missing_research_code",
            summary=f"git 仓库中不存在该版本: {payload.kind}/{payload.name}@{commit[:12]}",
            retryable=False,
        )
    try:
        code = service.read(kind=payload.kind, name=payload.name, commit=commit)
    except ResearchCodeError as exc:
        raise ExecutorError(
            code="missing_research_code",
            summary=str(exc),
            retryable=False,
        ) from exc
    return code, artifact.artifact_id, commit


# --------------------------------------------------------------------------- #
# 输出解析 / 失败分类 / 归档
# --------------------------------------------------------------------------- #


def _load_scores(path: Path) -> dict[str, float]:
    """读取 scores.parquet 为 ``{symbol: score}``(null → NaN,#217)。"""
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["symbol", "score"])
    symbols = table.column("symbol").to_pylist()
    values = table.column("score").to_pylist()
    return {
        str(symbol): (float(value) if value is not None else float("nan"))
        for symbol, value in zip(symbols, values, strict=True)
    }


def _read_outputs(out_dir: Path) -> _Outputs:
    outputs = _Outputs()
    if not out_dir.exists():
        outputs.problems.append(f"输出目录不存在: {out_dir}")
        return outputs
    scores = out_dir / "scores.parquet"
    if scores.exists():
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(scores)
            columns = set(table.column_names)
            if not {"symbol", "score"} <= columns:
                outputs.problems.append(f"scores.parquet 缺列: {sorted(columns)}")
            else:
                outputs.scores_path = scores
                outputs.scores_checksum = _sha256_file(scores)
        except Exception as exc:
            outputs.problems.append(f"scores.parquet 不可解析: {exc}")
    else:
        outputs.problems.append("缺少 scores.parquet")
    metrics_path = out_dir / "metrics.json"
    if metrics_path.exists():
        try:
            outputs.metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            outputs.problems.append(f"metrics.json 不可解析: {exc}")
    else:
        outputs.problems.append("缺少 metrics.json")
    error_path = out_dir / "error.json"
    if error_path.exists():
        try:
            outputs.error = json.loads(error_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            outputs.error = None
    return outputs


def _classify(result: SandboxRunResult, outputs: _Outputs) -> tuple[bool, str | None, str | None]:
    """(是否成功, error_code, error_summary)。"""
    if result.timed_out:
        return (
            False,
            TIMEOUT,
            (f"容器超过墙钟超时({result.duration_seconds}s)被 kill;{_usage_note(result, outputs)}"),
        )
    if result.oom_killed or result.exit_code == 137:
        return (
            False,
            OOM_KILLED,
            (f"容器内存超限被 OOM kill(exit={result.exit_code});{_usage_note(result, outputs)}"),
        )
    if result.exit_code == 0:
        if outputs.scores_checksum and outputs.metrics is not None:
            return True, None, None
        return (
            False,
            OUTPUT_CONTRACT_VIOLATION,
            (
                "exit 0 但输出不完整: "
                + "; ".join(outputs.problems)
                + f";{_usage_note(result, outputs)}"
            ),
        )
    if result.exit_code == 3:
        message = (outputs.error or {}).get("message", "输出契约不符")
        return False, OUTPUT_CONTRACT_VIOLATION, str(message)
    if result.exit_code == 4:
        message = (outputs.error or {}).get(
            "message", result.stderr.strip()[:500] or "容器内运行时异常"
        )
        return False, RUNTIME_ERROR, str(message)
    if result.exit_code in _DOCKER_EXIT_CODES:
        return (
            False,
            SANDBOX_UNAVAILABLE,
            (f"docker 运行失败(exit={result.exit_code}): {result.stderr.strip()[:400]}"),
        )
    return (
        False,
        RUNTIME_ERROR,
        (
            f"容器非预期退出码 {result.exit_code}: "
            f"{(outputs.error or {}).get('message') or result.stderr.strip()[:400]}"
        ),
    )


def _usage_note(result: SandboxRunResult, outputs: _Outputs) -> str:
    return (
        f"峰值内存 {result.usage.get('max_mem_mb')}MB / CPU {result.usage.get('max_cpu_percent')}%"
    )


def _archive_logs(
    run_dir: Path,
    result: SandboxRunResult,
    image: str,
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
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _stage_write_code(run_dir: Path, code: dict[str, str], params: dict[str, Any] | None) -> None:
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _failed_result(run_id: str, error_code: str, summary: str) -> JobResult:
    return JobResult(
        status="failed",
        result_ref=run_id,
        error_code=error_code,
        error_summary=summary[:4000],
    )


def _default_service_factory(settings: Any) -> ResearchCodeService:
    return ResearchCodeService.from_path(settings.research_code_repo_path)


def _default_runner_factory(settings: Any) -> ResearchSandboxRunner:
    return ResearchSandboxRunner(
        SubprocessDockerDriver(getattr(settings, "research_sandbox_docker_bin", "docker"))
    )


_: type[JobExecutor] = ResearchCodeRunExecutor

__all__ = [
    "DATASET_RELEASE_UNAVAILABLE",
    "KIND_NOT_IMPLEMENTED",
    "SANDBOX_DISABLED",
    "ResearchCodeRunExecutor",
    "ResearchCodeRunPayload",
]
