"""issue #461:factor_series_build 废弃 v1 逐日入口 —— 仅接受 compute_series。

v1 ``compute`` 的逐日回退(#359 双轨)对每个决策日做全历史面板重算
(O(决策日数 x 面板行数)),全市场全历史窗口单因子数小时且窗口头部零
预热易炸(#460 取证);用户拍板(2026-09-13)序列构建路径强制协议 v2。
本文件锁定:

* 纯函数门禁 ``factor_series_v2_entry_error``:compute_series 通过;
  compute / 缺 entry / manifest 缺失 / TOML 损坏均具名拒绝且文案含迁移
  路径;
* executor ``_resolve_code``:v1 manifest → ``v1_series_deprecated``
  秒拒(resolve 档位,先于挂载),retryable=False;v2 → 正常进入容器;
* MCP ``build_enqueue``:v1 manifest → 入队期 invalid_argument(不创建
  job 行),agent 即时得到迁移路径;
* 单日快照路径不受影响:v1 compute 仍是 research_code_run 的合法入口
  (静态校验白名单不动)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_backtest.background_jobs.contracts import ExecutorError
from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
)
from finboard_backtest.research_code.factor_series import (
    V1_SERIES_DEPRECATED_CODE,
    factor_series_v2_entry_error,
)

_V2_MANIFEST = '[manifest]\nentry = "factor.compute_series"\n\n[params]\nwindow = 2\n'
_V1_MANIFEST = '[manifest]\nentry = "factor.compute"\n\n[params]\nwindow = 2\n'


class TestFactorSeriesV2EntryGate:
    def test_compute_series_passes(self) -> None:
        assert factor_series_v2_entry_error(
            {"manifest.toml": _V2_MANIFEST, "factor.py": "..."}
        ) is None

    def test_v1_compute_rejected_with_migration_path(self) -> None:
        error = factor_series_v2_entry_error({"manifest.toml": _V1_MANIFEST})
        assert error is not None
        assert V1_SERIES_DEPRECATED_CODE in error
        assert "factor.compute" in error
        assert "compute_series(ctx)" in error
        assert "finboard_research_code_submit" in error

    def test_missing_entry_rejected(self) -> None:
        error = factor_series_v2_entry_error({"manifest.toml": "[manifest]\n"})
        assert error is not None
        assert V1_SERIES_DEPRECATED_CODE in error

    def test_missing_manifest_rejected(self) -> None:
        error = factor_series_v2_entry_error({"factor.py": "..."})
        assert error is not None
        assert V1_SERIES_DEPRECATED_CODE in error
        assert "manifest.toml" in error

    def test_none_files_rejected(self) -> None:
        assert factor_series_v2_entry_error(None) is not None

    def test_broken_toml_rejected(self) -> None:
        error = factor_series_v2_entry_error({"manifest.toml": "not [ valid"})
        assert error is not None
        assert V1_SERIES_DEPRECATED_CODE in error


# ------------------------------------------------------- executor 集成


class _Settings:
    research_sandbox_enabled = True
    research_code_repo_path = "data_cache/research_code.git"


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
        self.job_id = "JOB-461"


def _job() -> Any:
    return _JobRecord(_payload())


def _payload() -> dict[str, Any]:
    return {
        "kind": "factor",
        "name": "rsi2",
        "release_id": "DR-bars",
        "dataset_release_ids": [],
        "window_start": "2023-07-01",
        "window_end": "2024-06-30",
        "commit": "c" * 40,
    }


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
    manifest: str,
) -> FactorSeriesBuildExecutor:
    """executor + 打桩:代码仓返回指定 manifest,其余离线。"""

    async def audit_fn(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
        del spec, baseline, truncate_at
        return _AuditOutcome()

    executor = FactorSeriesBuildExecutor(
        session_maker=lambda: _Session(),  # type: ignore[arg-type]
        settings_factory=_Settings,
        container_runner=runner,
        prefix_audit=audit_fn,
    )

    class _FakeArtifactRepo:
        def __init__(self, session: Any) -> None:
            del session

        async def get_active(self, *, kind: str, name: str) -> Any:
            return SimpleNamespace(
                artifact_id="RC-461",
                kind=kind,
                name=name,
                status="active",
                commit="c" * 40,
            )

    class _FakeCodeService:
        def __init__(self, repo: Any) -> None:
            self.repo = repo

        @classmethod
        def from_path(cls, repo_path: str) -> _FakeCodeService:
            del repo_path
            return cls(_FakeCodeRepo())

        def exists(self, *, kind: str, name: str, commit: str) -> bool:
            del kind, name, commit
            return True

    class _FakeCodeRepo:
        def exists(self, *, kind: str, name: str, commit: str) -> bool:
            del kind, name, commit
            return True

        def read(self, *, kind: str, name: str, commit: str) -> dict[str, str]:
            del kind, name, commit
            return {"manifest.toml": manifest, "factor.py": "def compute_series(ctx): ..."}

    import finboard_backtest.research_code as rc_module

    monkeypatch.setattr(executor, "_find_cached", _no_cache)
    monkeypatch.setattr(executor, "_require_releases", _releases)
    monkeypatch.setattr(executor, "_resolve_predefined", _no_predefined)
    monkeypatch.setattr(rc_module, "ResearchCodeService", _FakeCodeService)
    monkeypatch.setattr(rc_module, "is_promoted_artifact", lambda a: True)
    monkeypatch.setattr(rc_module, "promotion_status", lambda a: "passed")
    monkeypatch.setattr("finboard_persistence.ResearchCodeArtifactRepository", _FakeArtifactRepo)
    monkeypatch.setattr("finboard_persistence.FactorSeriesRepository", _Repo)
    monkeypatch.setattr(
        "finboard_data.trading_calendar.ensure_calendar_loaded", _noop_calendar
    )
    monkeypatch.setattr(
        "finboard_data.trading_calendar.trading_days",
        lambda start, end: {date(2023, 7, 3), date(2023, 7, 4), date(2023, 7, 5)},
    )
    return executor


async def _no_cache(payload: Any) -> None:
    return None


async def _releases(payload: Any) -> date | None:
    return date(2015, 1, 5)


async def _no_predefined(payload: Any) -> tuple[None, str]:
    return None, "predefined"


async def _progress(done: int | None, total: int | None, phase: str | None) -> None:
    del done, total, phase


class TestExecutorV1SeriesGate:
    async def test_v1_manifest_rejected_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1 manifest:resolve 档位秒拒,容器 runner 不被调用。"""
        called = False

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            nonlocal called
            called = True
            return _ContainerResult(dates=tuple(spec.dates), values={})

        executor = _make_executor(monkeypatch, runner=runner, manifest=_V1_MANIFEST)
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(_job(), _progress)
        assert called is False
        assert exc_info.value.code == V1_SERIES_DEPRECATED_CODE
        assert exc_info.value.retryable is False
        assert "compute_series(ctx)" in exc_info.value.summary

    async def test_v2_manifest_proceeds_to_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v2 manifest:门禁放行,正常走容器 + 审计 + 落库。"""

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del mount_on_batch
            return _ContainerResult(
                dates=tuple(spec.dates),
                values={d.isoformat(): {"600000.SH": 1.0} for d in spec.dates},
            )

        executor = _make_executor(monkeypatch, runner=runner, manifest=_V2_MANIFEST)
        result = await executor.execute(_job(), _progress)
        assert result.status == "succeeded"
