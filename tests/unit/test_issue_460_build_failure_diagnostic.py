"""issue #460:factor_series_build 容器失败的窗口头部零预热诊断。

窗口挂载行情行来自 bars 主发布的全发布区间;``window_start`` 不晚于发布
起点时,窗口头部决策日只有 0-1 根可见 bar,带最小历史守卫的 v1 因子
(RSI/MACD 类)会在窗口头部 raise 炸掉整个构建(2026-09-12 全历史窗口
批次 4 连败:ValueError: bars 历史不足: 1 行)。本文件锁定:

* ``_enrich_container_failure`` 纯函数:命中条件(window_start <= 发布
  起点)时错误摘要附具名修复路径,code/retryable 语义零变化;未命中
  原样返回;
* 执行器集成:runner 抛 SandboxError(runtime_error)时 failed 结果
  携带诊断提示;ExecutorError 与零预热未命中的异常原样透传。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from finboard_backtest.background_jobs.contracts import ExecutorError
from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
    FactorSeriesBuildPayload,
    _enrich_container_failure,
)
from finboard_backtest.research_sandbox.errors import RUNTIME_ERROR, SandboxError

_RELEASE_START = date(2015, 1, 5)


def _typed_payload(window_start: date) -> FactorSeriesBuildPayload:
    return FactorSeriesBuildPayload(
        kind="factor",
        name="rsi2",
        release_id="DR-bars",
        dataset_release_ids=(),
        window_start=window_start,
        window_end=date(2026, 8, 31),
    )


def _payload(window_start: date) -> dict[str, Any]:
    return {
        "kind": "factor",
        "name": "rsi2",
        "release_id": "DR-bars",
        "dataset_release_ids": [],
        "window_start": window_start.isoformat(),
        "window_end": "2026-08-31",
        "commit": "c" * 40,
    }


class TestEnrichContainerFailure:
    def test_window_at_release_start_appends_hint(self) -> None:
        exc = SandboxError(RUNTIME_ERROR, "ValueError: bars 历史不足: 1 行")
        enriched = _enrich_container_failure(
            exc,
            _typed_payload(_RELEASE_START),
            bars_release_start=_RELEASE_START,
        )
        assert isinstance(enriched, ExecutorError)
        assert enriched.code == RUNTIME_ERROR
        assert enriched.retryable is False
        assert "bars 历史不足: 1 行" in enriched.summary
        assert "诊断提示" in enriched.summary
        assert "window_start(2015-01-05)" in enriched.summary
        assert "compute_series" in enriched.summary

    def test_window_after_release_start_returns_original(self) -> None:
        exc = SandboxError(RUNTIME_ERROR, "容器内其它失败")
        enriched = _enrich_container_failure(
            exc,
            _typed_payload(date(2015, 3, 3)),
            bars_release_start=_RELEASE_START,
        )
        assert enriched is exc

    def test_missing_release_start_returns_original(self) -> None:
        exc = SandboxError(RUNTIME_ERROR, "容器内其它失败")
        enriched = _enrich_container_failure(
            exc,
            _typed_payload(_RELEASE_START),
            bars_release_start=None,
        )
        assert enriched is exc

    def test_plain_exception_hint_preserves_type_name(self) -> None:
        exc = RuntimeError("容器内其它失败")
        enriched = _enrich_container_failure(
            exc,
            _typed_payload(_RELEASE_START),
            bars_release_start=_RELEASE_START,
        )
        assert isinstance(enriched, ExecutorError)
        # 普通异常无 .summary 属性:摘要 = str(exc),类型名只进 error_code
        # (与 worker 兜底 `getattr(exc, "summary", None) or str(exc)` 同口径)。
        assert enriched.code == "RuntimeError"
        assert enriched.summary.startswith("容器内其它失败")
        assert "诊断提示" in enriched.summary

    def test_retryable_flag_preserved(self) -> None:
        @dataclass
        class _FakeSandboxError(Exception):
            code: str = "docker_unavailable"
            summary: str = "docker 不可用"
            retryable: bool = True

        exc = _FakeSandboxError()
        enriched = _enrich_container_failure(
            exc,
            _typed_payload(_RELEASE_START),
            bars_release_start=_RELEASE_START,
        )
        assert isinstance(enriched, ExecutorError)
        assert enriched.retryable is True


class _Settings:
    research_sandbox_enabled = True


class _Session:
    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Repo:
    def __init__(self, session: Any) -> None:
        del session

    async def upsert(self, record: Any) -> Any:
        return record


class _JobRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.job_id = "JOB-460"


def _job(payload: dict[str, Any]) -> Any:
    return _JobRecord(payload)


@dataclass
class _ContainerResult:
    dates: tuple[date, ...]
    values: dict[str, dict[str, float]]
    quality: dict[str, Any] | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class _AuditOutcome:
    passed: bool = True
    first_divergence_date: date | None = None


async def _noop_calendar() -> set[date]:
    return set()


def _make_executor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: Any,
    release_start: date | None,
) -> FactorSeriesBuildExecutor:
    async def audit_fn(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
        del spec, baseline, truncate_at
        return _AuditOutcome()

    executor = FactorSeriesBuildExecutor(
        session_maker=lambda: _Session(),  # type: ignore[arg-type]
        settings_factory=_Settings,
        container_runner=runner,
        prefix_audit=audit_fn,
    )

    async def _no_cache(payload: Any) -> None:
        return None

    async def _code(payload: Any) -> tuple[str | None, str]:
        return "RC-1", "c" * 40

    async def _releases(payload: Any) -> date | None:
        return release_start

    monkeypatch.setattr(executor, "_find_cached", _no_cache)
    monkeypatch.setattr(executor, "_resolve_code", _code)
    monkeypatch.setattr(executor, "_require_releases", _releases)
    monkeypatch.setattr("finboard_persistence.FactorSeriesRepository", _Repo)
    monkeypatch.setattr(
        "finboard_data.trading_calendar.ensure_calendar_loaded", _noop_calendar
    )
    monkeypatch.setattr(
        "finboard_data.trading_calendar.trading_days",
        lambda start, end: {date(2026, 8, 31)},
    )
    return executor


class TestExecutorFailureDiagnostic:
    async def test_sandbox_error_at_zero_warmup_carries_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del mount_on_batch
            raise SandboxError(
                RUNTIME_ERROR, "ValueError: bars 历史不足: 1 行"
            )

        executor = _make_executor(
            monkeypatch, runner=runner, release_start=date(2026, 8, 31)
        )

        async def progress(done: int | None, total: int | None, phase: str | None) -> None:
            del done, total, phase

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(
                _job(_payload(date(2026, 8, 31))), progress
            )
        assert exc_info.value.code == RUNTIME_ERROR
        assert "诊断提示" in exc_info.value.summary
        assert "window_start(2026-08-31)" in exc_info.value.summary

    async def test_sandbox_error_with_warmup_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del mount_on_batch
            raise SandboxError(RUNTIME_ERROR, "容器内其它失败")

        executor = _make_executor(
            monkeypatch, runner=runner, release_start=date(2015, 1, 5)
        )

        async def progress(done: int | None, total: int | None, phase: str | None) -> None:
            del done, total, phase

        with pytest.raises(SandboxError) as exc_info:
            await executor.execute(
                _job(_payload(date(2026, 8, 31))), progress
            )
        assert exc_info.value.summary == "容器内其它失败"
        assert "诊断提示" not in exc_info.value.summary

    async def test_executor_error_passthrough_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del mount_on_batch
            raise ExecutorError(
                code="output_contract_violation",
                summary="容器输出缺少升序决策日 dates(空序列)",
                retryable=False,
            )

        executor = _make_executor(
            monkeypatch, runner=runner, release_start=date(2026, 8, 31)
        )

        async def progress(done: int | None, total: int | None, phase: str | None) -> None:
            del done, total, phase

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(
                _job(_payload(date(2026, 8, 31))), progress
            )
        assert "诊断提示" not in exc_info.value.summary
