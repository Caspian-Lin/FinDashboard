"""``factor_series_build`` 执行器 —— 内容寻址因子序列构建(issue #360)。

worker 领取 ``kind=factor_series_build`` 任务(单并发,复用 research_code_run
的 worker 槽位约定)后按 payload 重建构建输入::

    {
      "kind": "factor",                    # v1 仅 factor
      "name": "mom20",                     # 代码产物名
      "commit": "<sha>",                   # 可省 = active+passed 引用
      "artifact_id": "RC-...",             # 可选,与 name 联合定位
      "release_id": "DR-...",              # bars 主发布锚定
      "dataset_release_ids": ["DR-..."],   # 研究发布联合集(排序冻结)
      "window_start": "2022-01-01",
      "window_end": "2023-06-30",
      "params": {...}                      # 可选
    }

流程(5 个进度阶段):解析 → 沙箱开关 → 代码/发布解析 → **缓存检查**
(``series_key`` 已存在且 content_checksum 一致 → 直接 succeeded,
``result`` 标注 ``cache_hit``,不启动容器)→ 容器构建(#359
``research_sandbox.runner.run_factor_series_container(spec)``,窗口内逐决策日
执行 factor.compute)→ **前缀不变性审计抽样**(2 个截断点;检出前视
→ failed=``lookahead_detected``,失败分类与 output_contract_violation 同级,
错误具名首个分歧日期)→ 内容寻址落库(``research_factor_series``,
``series_key`` ON CONFLICT 幂等)。

窗口扩展重建后重叠前缀 checksum 必须一致 —— 前缀不变性审计免费充当一致性
自检;换 bars 发布的托管批量重建 = 一次入队 N 个本 job(内容寻址缓存使
未受影响的输入组合自动 unchanged,不新造编排器)。

容器执行与审计本体由 #359 提供(``runner.FactorSeriesRunSpec`` /
``runner.run_factor_series_container`` / ``research_sandbox.audit`` 前缀不变性
引擎);默认实现走真实容器与审计引擎,测试经构造注入 mock。

边界:纯离线研究域;容器无网络无凭证,不触实盘任何组件。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_persistence import FactorSeriesRecord

SettingsFactory = Callable[[], Any]
#: 容器执行回调:(spec) -> result(dates / values / quality / run_id)。
ContainerRunner = Callable[[Any], Awaitable[Any]]
#: 审计回调:(spec, truncate_at) -> outcome(passed / first_divergence_date)。
PrefixAudit = Callable[..., Awaitable[Any]]

_TOTAL_STAGES = 5

SANDBOX_DISABLED = "sandbox_disabled"
KIND_NOT_IMPLEMENTED = "factor_series_build_kind_not_implemented"
DATASET_RELEASE_UNAVAILABLE = "dataset_release_unavailable"
MISSING_RESEARCH_CODE = "missing_research_code"
#: 审计检出的前视失败分类(与 output_contract_violation 同级,#360)
LOOKAHEAD_DETECTED = "lookahead_detected"
#: 缓存命中(code/checksum 一致)时 result_ref 之外的幂等标记
CACHE_HIT = "cache_hit"

_USER_FACTOR_KIND = "factor"


@dataclass(frozen=True)
class FactorSeriesBuildPayload:
    """``kind=factor_series_build`` 的冻结 payload 投影。"""

    kind: str
    name: str
    release_id: str
    dataset_release_ids: tuple[str, ...]
    window_start: date
    window_end: date
    commit: str | None = None
    artifact_id: str | None = None
    params: dict[str, Any] | None = None

    def digest(self) -> str:
        """幂等键摘要(name/commit/releases/window/params)。"""
        h = hashlib.sha256()
        h.update(self.kind.encode())
        h.update(self.name.encode())
        h.update((self.commit or "-").encode())
        h.update(self.release_id.encode())
        for rid in sorted(self.dataset_release_ids):
            h.update(rid.encode())
        h.update(self.window_start.isoformat().encode())
        h.update(self.window_end.isoformat().encode())
        if self.params:
            import json

            h.update(
                json.dumps(
                    self.params, sort_keys=True, ensure_ascii=False
                ).encode("utf-8")
            )
        return h.hexdigest()[:16]


async def _default_container_runner(spec: Any) -> Any:
    """#359 钉死的容器执行入口。"""
    from finboard_backtest.research_sandbox import runner

    return await runner.run_factor_series_container(spec)


async def _default_prefix_audit(spec: Any, *, truncate_at: date) -> Any:
    """前缀不变性审计(截断模式;#359 纯引擎 + 容器重放)。

    截断变体 = ``dates`` 截到 cut 且 ``window_end`` 收紧到 cut(v3 挂载的
    PIT 上界随 window_end 收紧,变体容器物理上只可见 cut 日终之前的数据);
    基线与变体各重放一次容器,报告携带 ``passed`` / ``first_divergence_date``
    (执行器 ``_audit_sample`` 的消费形态)。
    """
    from finboard_backtest.research_sandbox.audit import run_prefix_invariance_audit
    from finboard_backtest.research_sandbox.runner import run_factor_series_container

    async def build_fn(
        dates: Sequence[date], perturb_from: date | None = None
    ) -> Any:
        del perturb_from  # 截断模式忽略第二参(#359 引擎语义)
        return await run_factor_series_container(
            replace(spec, dates=tuple(dates), window_end=max(dates))
        )

    return await run_prefix_invariance_audit(
        build_fn,
        mode="truncation",
        cut_points=[truncate_at],
        dates=list(spec.dates),
    )


def audit_truncation_points(dates: list[date]) -> list[date]:
    """抽样 2 个截断点(确定性,issue #360)。

    升序决策日序列的 1/3 与 2/3 位(截断点 = 该日重算时只可见其之前的数据);
    序列过短(不足 3 日)时按可得位置去重降级,空序列返回空。
    """
    if not dates:
        return []
    candidates = []
    if len(dates) >= 3:
        candidates = [dates[len(dates) // 3], dates[2 * len(dates) // 3]]
    else:
        candidates = [dates[len(dates) // 2]]
    seen: set[date] = set()
    unique: list[date] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


class FactorSeriesBuildExecutor:
    """``kind=factor_series_build`` 执行器(单并发,由 worker kind_concurrency 保证)。"""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: SettingsFactory,
        container_runner: ContainerRunner | None = None,
        prefix_audit: PrefixAudit | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory
        self._container_runner = container_runner or _default_container_runner
        self._prefix_audit = prefix_audit or _default_prefix_audit

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        payload = _parse_payload(job)
        await progress(0, _TOTAL_STAGES, "factor_series_build:start")

        settings = self._settings_factory()
        if settings is None:
            raise ExecutorError(
                code="settings_unavailable",
                summary="无法加载 settings,factor_series_build 无法执行",
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
        if payload.kind != _USER_FACTOR_KIND:
            raise ExecutorError(
                code=KIND_NOT_IMPLEMENTED,
                summary=(
                    "factor_series_build v1 仅支持 kind=factor,"
                    f"收到 {payload.kind!r}"
                ),
                retryable=False,
            )
        await progress(1, _TOTAL_STAGES, "factor_series_build:resolve")

        await self._require_releases(payload)
        await progress(2, _TOTAL_STAGES, "factor_series_build:cache_check")

        # 缓存检查先行(内容寻址,不依赖 artifact 当前 active 指向——find_matching
        # 按该产物全部已存序列逐行试算 series_key):命中 → succeeded(cache_hit),
        # 不启动容器。retired 产物的已存序列同样可命中(冻结工件不生命周期化)。
        cached = await self._find_cached(payload)
        if cached is not None:
            await progress(
                _TOTAL_STAGES,
                _TOTAL_STAGES,
                f"factor_series_build:{CACHE_HIT}",
            )
            return JobResult(
                status="succeeded",
                result_ref=cached.series_id,
                error_summary=(
                    f"{CACHE_HIT}: series_key={cached.series_key} 已存在且 "
                    f"content_checksum 一致,未重新构建"
                ),
            )

        _artifact_id, commit = await self._resolve_code(payload)
        await progress(3, _TOTAL_STAGES, "factor_series_build:execute")

        spec = self._build_spec(payload, commit=commit)
        result = await self._container_runner(spec)
        record = _record_from_result(
            result,
            code_artifact=payload.name,
            code_commit=commit,
            kind=payload.kind,
            release_id=payload.release_id,
            dataset_release_ids=payload.dataset_release_ids,
            params=payload.params or {},
            window_start=payload.window_start,
            window_end=payload.window_end,
        )
        await progress(4, _TOTAL_STAGES, "factor_series_build:audit")

        # 前缀不变性审计抽样:检出前视 → failed=lookahead_detected(不落库)。
        audit_failure = await self._audit_sample(spec, record)
        if audit_failure is not None:
            return JobResult(
                status="failed",
                result_ref=None,
                error_code=LOOKAHEAD_DETECTED,
                error_summary=audit_failure,
            )

        async with self._session_maker() as session:
            from finboard_persistence import FactorSeriesRepository

            persisted = await FactorSeriesRepository(session).upsert(record)
            await session.commit()
        await progress(
            _TOTAL_STAGES, _TOTAL_STAGES, "factor_series_build:succeeded"
        )
        return JobResult(status="succeeded", result_ref=persisted.series_id)

    # ---- 内部 -------------------------------------------------------------

    async def _resolve_code(
        self, payload: FactorSeriesBuildPayload
    ) -> tuple[str | None, str]:
        """解析 (artifact_id, commit);规则与 research_code_run 同口径。"""
        from finboard_backtest.research_code import (
            ResearchCodeService,
            is_promoted_artifact,
            promotion_status,
        )
        from finboard_persistence import ResearchCodeArtifactRepository

        async with self._session_maker() as session:
            repo = ResearchCodeArtifactRepository(session)
            if payload.artifact_id is not None:
                artifact = await repo.get(payload.artifact_id)
                if artifact is None:
                    raise ExecutorError(
                        code=MISSING_RESEARCH_CODE,
                        summary=f"研究代码产物不存在: {payload.artifact_id}",
                        retryable=False,
                    )
                if artifact.kind != payload.kind or artifact.name != payload.name:
                    raise ExecutorError(
                        code="invalid_payload",
                        summary=(
                            f"artifact_id 与 (kind,name) 不一致: {payload.artifact_id}"
                            f" vs ({payload.kind}, {payload.name})"
                        ),
                        retryable=False,
                    )
                if artifact.status == "retired":
                    raise ExecutorError(
                        code="invalid_payload",
                        summary=(
                            f"artifact 已 retired,不能构建新序列: {payload.artifact_id}"
                        ),
                        retryable=False,
                    )
                if artifact.status == "active" and not is_promoted_artifact(artifact):
                    raise ExecutorError(
                        code="invalid_payload",
                        summary=(
                            f"artifact active 但未通过 screen+OOS 晋级门: "
                            f"{payload.artifact_id} "
                            f"promotion_status={promotion_status(artifact)}"
                        ),
                        retryable=False,
                    )
            else:
                artifact = await repo.get_active(
                    kind=payload.kind, name=payload.name
                )
                if artifact is None:
                    raise ExecutorError(
                        code=MISSING_RESEARCH_CODE,
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
            service = ResearchCodeService.from_path(
                self._settings_factory().research_code_repo_path
            )
            if not service.exists(kind=payload.kind, name=payload.name, commit=commit):
                raise ExecutorError(
                    code=MISSING_RESEARCH_CODE,
                    summary=(
                        f"git 仓库中不存在该版本: {payload.kind}/"
                        f"{payload.name}@{commit[:12]}"
                    ),
                    retryable=False,
                )
            return artifact.artifact_id, commit

    async def _require_releases(self, payload: FactorSeriesBuildPayload) -> None:
        """bars 主发布与联合集逐个存在性检查(fail-visible)。"""
        from finboard_persistence import ResearchDatasetReleaseRepository

        async with self._session_maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            for release_id in (payload.release_id, *payload.dataset_release_ids):
                if await repo.get(release_id) is None:
                    raise ExecutorError(
                        code=DATASET_RELEASE_UNAVAILABLE,
                        summary=f"研究数据发布不存在: {release_id}",
                        retryable=False,
                    )

    async def _find_cached(
        self, payload: FactorSeriesBuildPayload
    ) -> FactorSeriesRecord | None:
        """series_key 精确命中即缓存(同 key 同 checksum 由内容寻址保证)。"""
        from finboard_persistence import FactorSeriesRepository

        async with self._session_maker() as session:
            return await FactorSeriesRepository(session).find_matching(
                code_artifact=payload.name,
                release_id=payload.release_id,
                dataset_release_ids=payload.dataset_release_ids,
                params=payload.params or {},
                window_start=payload.window_start,
                window_end=payload.window_end,
            )

    def _build_spec(
        self,
        payload: FactorSeriesBuildPayload,
        *,
        commit: str,
    ) -> Any:
        """构造 #359 的 ``FactorSeriesRunSpec``。

        窗口内决策日 = A 股交易日历在 ``[window_start, window_end]`` 的
        全部交易日(序列工件逐交易日产出,消费端按决策日索引取子集)。
        """
        from finboard_backtest.research_sandbox import runner
        from finboard_data.trading_calendar import trading_days

        dates = tuple(sorted(trading_days(payload.window_start, payload.window_end)))
        if not dates:
            raise ExecutorError(
                code="invalid_payload",
                summary=(
                    f"窗口 [{payload.window_start.isoformat()}, "
                    f"{payload.window_end.isoformat()}] 内无 A 股交易日"
                    "(交易日历未加载或窗口非法),无法构建因子序列"
                ),
                retryable=False,
            )
        return runner.FactorSeriesRunSpec(
            code_artifact=payload.name,
            code_commit=commit,
            release_id=payload.release_id,
            dataset_release_ids=tuple(payload.dataset_release_ids),
            params=payload.params or {},
            window_start=payload.window_start,
            window_end=payload.window_end,
            dates=dates,
        )

    async def _audit_sample(
        self, spec: Any, record: FactorSeriesRecord
    ) -> str | None:
        """抽 2 个截断点跑前缀不变性审计;返回失败文案或 None(通过)。

        检出 → ``lookahead_detected``,错误具名**首个分歧日期**(多个截断点
        均检出时取最早);审计本体 #359 提供,经构造注入可 mock。
        """
        divergences: list[date] = []
        for truncate_at in audit_truncation_points(list(record.dates)):
            outcome = await self._prefix_audit(spec, truncate_at=truncate_at)
            if not getattr(outcome, "passed", True):
                first = getattr(outcome, "first_divergence_date", None)
                if first is not None:
                    divergences.append(first)
                else:
                    divergences.append(truncate_at)
        if not divergences:
            return None
        first_divergence = min(divergences)
        return (
            f"前缀不变性审计检出前视({LOOKAHEAD_DETECTED}):截断重算的因子值"
            f"与全窗口产出不一致,首个分歧日期 {first_divergence.isoformat()}"
            f"(抽样截断点 {[item.isoformat() for item in audit_truncation_points(list(record.dates))]});"
            "因子代码在决策日 t 只能消费 t 之前的数据,拒绝入库。"
        )


def _parse_payload(job: JobRecord) -> FactorSeriesBuildPayload:
    payload = job.payload
    try:
        kind = _require_str(payload, "kind")
        name = _require_str(payload, "name")
        release_id = _require_str(payload, "release_id")
        raw_releases = payload.get("dataset_release_ids")
        if (
            not isinstance(raw_releases, list)
            or not all(isinstance(r, str) and r for r in raw_releases)
        ):
            raise ValueError("dataset_release_ids 须为字符串数组")
        window_start = _parse_date(payload, "window_start")
        window_end = _parse_date(payload, "window_end")
        if window_end < window_start:
            raise ValueError("window_end 不得早于 window_start")
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
            summary=f"factor_series_build payload 非法: {exc}",
            retryable=False,
            context={"job_id": job.job_id},
        ) from exc
    return FactorSeriesBuildPayload(
        kind=kind,
        name=name,
        release_id=release_id,
        dataset_release_ids=tuple(raw_releases),
        window_start=window_start,
        window_end=window_end,
        commit=commit,
        artifact_id=artifact_id,
        params=params,
    )


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 须为非空字符串")
    return value


def _parse_date(payload: dict[str, object], key: str) -> date:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} 须为 ISO 日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} 不是合法 ISO 日期: {value}") from exc


def _record_from_result(
    result: Any,
    *,
    code_artifact: str,
    code_commit: str,
    kind: str,
    release_id: str,
    dataset_release_ids: tuple[str, ...],
    params: dict[str, Any],
    window_start: date,
    window_end: date,
) -> FactorSeriesRecord:
    """把容器产出(#359 结果契约)装配为内容寻址 Record。

    结果契约(与 #359 钉死):``dates``(升序决策日)、``values``
    (``{date|ISO: {symbol: float|null}}``,date/ISO 两态均可)、``quality``
    (可选质量门归档)、``run_id``(产出 RCR,可空)。
    """
    raw_dates = getattr(result, "dates", None)
    if not raw_dates:
        raise ExecutorError(
            code="output_contract_violation",
            summary="容器输出缺少升序决策日 dates(空序列)",
            retryable=False,
        )
    dates = tuple(
        item if isinstance(item, date) else date.fromisoformat(str(item))
        for item in raw_dates
    )
    if list(dates) != sorted(dates):
        raise ExecutorError(
            code="output_contract_violation",
            summary="容器输出 dates 必须升序",
            retryable=False,
        )
    raw_values = getattr(result, "values", None)
    if not isinstance(raw_values, dict):
        raise ExecutorError(
            code="output_contract_violation",
            summary="容器输出缺少 values 逐日截面",
            retryable=False,
        )
    values: dict[str, dict[str, float | None]] = {}
    for day, day_values in raw_values.items():
        key = day if isinstance(day, str) else str(day)
        values[key] = {
            str(symbol): (None if value is None else float(value))
            for symbol, value in dict(day_values).items()
        }
    quality = getattr(result, "quality", None)
    if quality is not None and not isinstance(quality, dict):
        # #359 产出 SeriesQualityReport dataclass,归档为普通 dict
        quality = asdict(quality)
    source_run_id = getattr(result, "run_id", None)
    return FactorSeriesRecord.build(
        code_artifact=code_artifact,
        code_commit=code_commit,
        kind=kind,
        release_id=release_id,
        dataset_release_ids=dataset_release_ids,
        params=params,
        window_start=window_start,
        window_end=window_end,
        dates=dates,
        values=values,
        quality=quality if isinstance(quality, dict) else None,
        source_run_id=source_run_id if isinstance(source_run_id, str) else None,
    )


_: type[JobExecutor] = FactorSeriesBuildExecutor

__all__ = [
    "CACHE_HIT",
    "DATASET_RELEASE_UNAVAILABLE",
    "KIND_NOT_IMPLEMENTED",
    "LOOKAHEAD_DETECTED",
    "MISSING_RESEARCH_CODE",
    "SANDBOX_DISABLED",
    "FactorSeriesBuildExecutor",
    "FactorSeriesBuildPayload",
    "audit_truncation_points",
]
