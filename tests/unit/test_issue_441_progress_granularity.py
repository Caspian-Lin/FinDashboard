"""issue #441:factor_series_build 阶段内进度上报 —— execute / audit 长阶段逐批可见。

research_run(#188/#308)/ bulk_download(``make_sync_progress``)均有阶段内
细粒度进度,factor_series_build 此前只有 ~7 档阶段级 —— execute(挂载流式
+ 容器计算)与 audit(串行截断点)占墙钟大头却整段黑箱。本文件锁定:

* **进度序列** —— start → resolve → cache_check → execute →
  container:start → mount k/n(单飞合并)→ container:done → audit →
  audit 1/2 → audit 2/2 → succeeded;数值列停在阶段档位(done 单调
  不减、total 只增,worker ``update_progress`` 夹紧规则),phase 逐帧
  变化使 #306 僵尸指纹 (progress_done, phase) 保持活跃;
* **旧式 runner 静默退化** —— 注入 ``(spec)`` 契约的 runner(测试 seam)
  经签名探测不透传 mount_on_batch,无 mount 帧,其余帧与终态不变;
* **缓存命中不进长阶段** —— cache_hit 终态帧不变,无 container/mount 帧;
* **data_mount on_batch** —— 窗口/单日挂载可选逐批回调,缺省 None 行为
  零变化(清单与落盘逐字节一致)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_backtest.background_jobs.executors._progress import (
    make_stage_phase_progress,
)
from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
)
from finboard_backtest.research_sandbox.data_mount import (
    build_data_mount,
    build_window_data_mount,
)
from tests.unit.research_sandbox.test_data_mount import (
    _DailyRecord,
    _FinRecord,
    _Inst,
    _Kind,
    _PITBar,
    _Release,
    _StubProvider,
)

_PROGRESS_MODULE = "finboard_backtest.background_jobs.executors._progress"


# --------------------------------------------------------------- 公共脚手架


@dataclass
class _ContainerResult:
    """容器产出契约的最小投影(``_record_from_result`` 消费)。"""

    dates: tuple[date, ...]
    values: dict[str, dict[str, float]]
    quality: dict[str, Any] | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class _AuditOutcome:
    passed: bool = True
    first_divergence_date: date | None = None


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


async def _noop_calendar() -> set[date]:
    return set()


def _recorder() -> tuple[
    Callable[[int, int | None, str | None], Awaitable[None]],
    list[tuple[int, int, str]],
]:
    frames: list[tuple[int, int, str]] = []

    async def progress(
        done: int | None, total: int | None, phase: str | None
    ) -> None:
        # 执行器恒传非 None 数值(worker 侧契约允许 None,防御性收窄)
        assert done is not None
        assert total is not None
        assert phase is not None
        frames.append((done, total, phase))

    return progress, frames


async def _drain_pending_progress() -> None:
    """等干单飞合并的 fire-and-forget 进度任务(断言前收口)。"""
    import finboard_backtest.background_jobs.executors._progress as progress_mod

    for _ in range(100):
        pending = [t for t in progress_mod._pending_progress_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def _make_executor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: Any,
    days: tuple[date, ...] = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
) -> tuple[FactorSeriesBuildExecutor, dict[str, Any]]:
    """执行器 + 离线桩:解析/缓存/发布存在性打桩,容器与审计经构造注入。"""
    observed: dict[str, Any] = {"specs": [], "cuts": []}

    async def audit_fn(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
        del spec, baseline
        observed["cuts"].append(truncate_at)
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

    async def _releases(payload: Any) -> None:
        return None

    monkeypatch.setattr(executor, "_find_cached", _no_cache)
    monkeypatch.setattr(executor, "_resolve_code", _code)
    monkeypatch.setattr(executor, "_require_releases", _releases)
    monkeypatch.setattr(
        "finboard_persistence.FactorSeriesRepository", _Repo
    )
    # 交易日历与预热离线化(执行器在函数体内 import,须在源头替换)
    monkeypatch.setattr(
        "finboard_data.trading_calendar.ensure_calendar_loaded", _noop_calendar
    )
    monkeypatch.setattr(
        "finboard_data.trading_calendar.trading_days",
        lambda start, end: set(days),
    )
    return executor, observed


def _job(payload: dict[str, Any]) -> Any:
    return _JobRecord(payload)


class _JobRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.job_id = "JOB-441"


def _payload(days: tuple[date, ...]) -> dict[str, Any]:
    return {
        "kind": "factor",
        "name": "mom20",
        "release_id": "DR-bars",
        "dataset_release_ids": [],
        "window_start": days[0].isoformat(),
        "window_end": days[-1].isoformat(),
        "commit": "c" * 40,
    }


def _assert_progress_contract(frames: list[tuple[int, int, str]]) -> None:
    """worker update_progress 既有规则:done 单调不减、total 只增不减。"""
    prev_done = 0
    prev_total = 0
    for done, total, _phase in frames:
        assert done >= prev_done
        assert total >= prev_total
        assert done <= total
        prev_done, prev_total = done, total


# ------------------------------------------------------- 执行器进度序列


class TestExecutorProgressSequence:
    async def test_happy_path_frame_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全链路帧序:0/N → mount k/M → container → audit 1/2 → 2/2 → 终态。"""
        mount_batches = 6
        observed_specs: list[Any] = []

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            observed_specs.append(spec)
            if mount_on_batch is not None:
                for k in range(1, mount_batches + 1):
                    mount_on_batch(k, mount_batches)
                    await asyncio.sleep(0)  # 让单飞任务落一帧
            return _ContainerResult(
                dates=tuple(spec.dates),
                values={d.isoformat(): {"600000.SH": 1.0} for d in spec.dates},
            )

        executor, _observed = _make_executor(monkeypatch, runner=runner)
        progress, frames = _recorder()
        result = await executor.execute(
            _job(_payload((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))),
            progress,
        )
        await _drain_pending_progress()
        assert result.status == "succeeded"

        phases = [phase for _, _, phase in frames]
        assert phases[:5] == [
            "factor_series_build:start",
            "factor_series_build:resolve",
            "factor_series_build:cache_check",
            "factor_series_build:execute",
            "factor_series_build:container:start",
        ]
        # mount 子序列:介于 container:start 与 container:done,k 升序且末帧 M/M
        end = phases.index("factor_series_build:container:done")
        mount_phases = phases[5:end]
        assert mount_phases, "execute 长阶段必须有 mount 细粒度帧"
        ks = [int(p.rsplit(" ", 1)[1].split("/")[0]) for p in mount_phases]
        assert ks == sorted(ks)
        assert len(set(ks)) == len(ks)
        assert mount_phases[-1] == f"factor_series_build:mount {mount_batches}/{mount_batches}"
        # 子序列之后帧序固定:container:done → audit → audit 1/2 → 2/2 → 终态
        assert phases[end:] == [
            "factor_series_build:container:done",
            "factor_series_build:audit",
            "factor_series_build:audit 1/2",
            "factor_series_build:audit 2/2",
            "factor_series_build:succeeded",
        ]
        # 数值列停在阶段档位:mount/容器帧 (3,5)、audit 帧 (4,5)、终态 (5,5)
        for done, total, phase in frames:
            if phase.startswith("factor_series_build:mount"):
                assert (done, total) == (3, 5)
            elif phase.startswith("factor_series_build:audit "):
                assert (done, total) == (4, 5)
        assert frames[-1] == (5, 5, "factor_series_build:succeeded")
        _assert_progress_contract(frames)

    async def test_phase_changes_keep_zombie_fingerprint_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """done 静止的阶段内,phase 逐帧变化 —— #306 指纹 (done, phase) 活跃。"""

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del spec
            if mount_on_batch is not None:
                for k in range(1, 4):
                    mount_on_batch(k, 3)
                    await asyncio.sleep(0)
            return _ContainerResult(
                dates=tuple(date(2024, 1, 2 + i) for i in range(3)),
                values={
                    date(2024, 1, 2 + i).isoformat(): {"600000.SH": 1.0}
                    for i in range(3)
                },
            )

        executor, _observed = _make_executor(monkeypatch, runner=runner)
        progress, frames = _recorder()
        await executor.execute(
            _job(_payload((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))),
            progress,
        )
        await _drain_pending_progress()
        stationary = [
            (f1, f2)
            for f1, f2 in pairwise(frames)
            if f1[0] == f2[0] and f1[2] != f2[2]
        ]
        assert stationary, "阶段内必须存在 done 不变而 phase 变化的帧"

    async def test_old_style_runner_degrades_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧式 (spec) runner:不透传回调(无 mount 帧),其余帧与终态不变。"""

        async def runner(spec: Any) -> Any:
            del spec
            return _ContainerResult(
                dates=tuple(date(2024, 1, 2 + i) for i in range(3)),
                values={
                    date(2024, 1, 2 + i).isoformat(): {"600000.SH": 1.0}
                    for i in range(3)
                },
            )

        executor, _observed = _make_executor(monkeypatch, runner=runner)
        progress, frames = _recorder()
        result = await executor.execute(
            _job(_payload((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))),
            progress,
        )
        await _drain_pending_progress()
        assert result.status == "succeeded"
        assert [phase for _, _, phase in frames] == [
            "factor_series_build:start",
            "factor_series_build:resolve",
            "factor_series_build:cache_check",
            "factor_series_build:execute",
            "factor_series_build:container:start",
            "factor_series_build:container:done",
            "factor_series_build:audit",
            "factor_series_build:audit 1/2",
            "factor_series_build:audit 2/2",
            "factor_series_build:succeeded",
        ]

    async def test_single_cut_reports_one_of_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """短窗口(2 决策日)只有 1 个截断点:audit 1/1,帧序仍完整。"""
        days = (date(2024, 1, 2), date(2024, 1, 3))

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:
            del mount_on_batch
            return _ContainerResult(
                dates=tuple(spec.dates),
                values={d.isoformat(): {"600000.SH": 1.0} for d in spec.dates},
            )

        executor, observed = _make_executor(monkeypatch, runner=runner, days=days)
        progress, frames = _recorder()
        result = await executor.execute(_job(_payload(days)), progress)
        await _drain_pending_progress()
        assert result.status == "succeeded"
        assert len(observed["cuts"]) == 1
        assert "factor_series_build:audit 1/1" in [p for _, _, p in frames]

    async def test_cache_hit_skips_long_stages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存命中:阶段总数与终态 phase 不变,不进 container/mount 长阶段。"""

        async def runner(spec: Any, *, mount_on_batch: Any = None) -> Any:  # pragma: no cover
            raise AssertionError("cache_hit 不得启动构建")

        executor, observed = _make_executor(monkeypatch, runner=runner)

        async def _cached(payload: Any) -> Any:
            return SimpleNamespace(series_id="FS-1", series_key="key-1")

        monkeypatch.setattr(executor, "_find_cached", _cached)
        progress, frames = _recorder()
        result = await executor.execute(
            _job(_payload((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))),
            progress,
        )
        await _drain_pending_progress()
        assert result.status == "succeeded"
        assert result.result_ref == "FS-1"
        assert observed["specs"] == []
        assert frames == [
            (0, 5, "factor_series_build:start"),
            (1, 5, "factor_series_build:resolve"),
            (2, 5, "factor_series_build:cache_check"),
            (5, 5, "factor_series_build:cache_hit"),
        ]


# ------------------------------------------------------- 挂载逐批进度


_WINDOW_DAYS = (date(2024, 6, 3), date(2024, 6, 4))
_SYMBOLS = ["600000.SH", "600001.SH"]


def _avail(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)


def _window_providers() -> tuple[_StubProvider, _StubProvider, _StubProvider]:
    bars = _StubProvider(
        release=_Release(
            "DR-bars",
            _Kind("bars"),
            [_Inst(code) for code in _SYMBOLS],
            start_date=_WINDOW_DAYS[0],
            end_date=_WINDOW_DAYS[-1],
        ),
        bars=[
            _PITBar(code=code, day=day, close=10.0)
            for code in _SYMBOLS
            for day in _WINDOW_DAYS
        ],
    )
    daily = _StubProvider(
        release=_Release(
            "DR-daily",
            _Kind("daily_metrics"),
            [_Inst(code) for code in _SYMBOLS],
            start_date=_WINDOW_DAYS[0],
            end_date=_WINDOW_DAYS[-1],
        ),
        daily=[
            _DailyRecord(
                symbol=code,
                trade_date=day,
                pb=Decimal("1.0"),
                available_at=_avail(day),
            )
            for code in _SYMBOLS
            for day in _WINDOW_DAYS
        ],
    )
    fin = _StubProvider(
        release=_Release(
            "DR-fin",
            _Kind("financial_indicators"),
            [_Inst(code) for code in _SYMBOLS],
            start_date=_WINDOW_DAYS[0],
            end_date=_WINDOW_DAYS[-1],
        ),
        financial=[
            _FinRecord(
                symbol=code,
                announcement_date=_WINDOW_DAYS[0],
                report_period=date(2024, 3, 31),
                eps=Decimal("1.0"),
                available_at=_avail(_WINDOW_DAYS[0]),
            )
            for code in _SYMBOLS
        ],
    )
    return bars, daily, fin


class TestMountOnBatch:
    async def test_window_mount_reports_batches(self, tmp_path: Any) -> None:
        """窗口挂载逐批上报:bars/daily 逐标的、公告类整集计 1,分母跨发布累计。"""
        bars, daily, fin = _window_providers()
        seen: list[tuple[int, int]] = []
        await build_window_data_mount(
            providers=[bars, daily, fin],
            window_start=_WINDOW_DAYS[0],
            window_end=_WINDOW_DAYS[-1],
            dates=list(_WINDOW_DAYS),
            out_root=tmp_path / "data",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="DR-bars",
            dataset_release_ids=["DR-daily", "DR-fin"],
            on_batch=lambda done, total: seen.append((done, total)),
        )
        # 2 bars 标的 + 2 daily 标的 + 1 公告类整集 = 5 批
        assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]

    async def test_window_mount_default_none_zero_change(self, tmp_path: Any) -> None:
        """on_batch 缺省 None 行为零变化:清单与落盘 parquet 逐字节一致。"""
        bars_a, daily_a, fin_a = _window_providers()
        bars_b, daily_b, fin_b = _window_providers()
        common: dict[str, Any] = {
            "window_start": _WINDOW_DAYS[0],
            "window_end": _WINDOW_DAYS[-1],
            "dates": list(_WINDOW_DAYS),
            "code_artifact": "mom20",
            "code_commit": "c" * 40,
            "release_id": "DR-bars",
            "dataset_release_ids": ["DR-daily", "DR-fin"],
        }
        mount_with = await build_window_data_mount(
            providers=[bars_a, daily_a, fin_a],
            out_root=tmp_path / "with",
            on_batch=lambda done, total: None,
            **common,
        )
        mount_without = await build_window_data_mount(
            providers=[bars_b, daily_b, fin_b],
            out_root=tmp_path / "without",
            **common,
        )
        assert mount_with.manifest_dict() == mount_without.manifest_dict()
        for name in ("bars.parquet", "daily_metrics.parquet", "financial_indicators.parquet"):
            assert (tmp_path / "with" / name).read_bytes() == (
                tmp_path / "without" / name
            ).read_bytes(), name

    async def test_data_mount_v2_reports_batches(self, tmp_path: Any) -> None:
        """v2 单日挂载同口径:逐标的批次计数,分母 = 过滤后标的总数。"""
        bars, daily, _fin = _window_providers()
        seen: list[tuple[int, int]] = []
        mount = await build_data_mount(
            providers=[bars, daily],
            decision_at=datetime(2024, 6, 5, 15, 30, tzinfo=UTC),
            out_root=tmp_path / "data",
            on_batch=lambda done, total: seen.append((done, total)),
        )
        assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]
        assert mount.symbols == tuple(sorted(_SYMBOLS))


# ------------------------------------------------------- 单飞 phase 工厂


class TestStagePhaseProgress:
    async def test_fixed_scale_single_flight_merge(self) -> None:
        """数值列恒为阶段档位,k/n 只进 phase 文本;高频触发合并留最新帧。"""
        progress, frames = _recorder()
        on_batch = make_stage_phase_progress(
            progress,
            phase_prefix="factor_series_build:mount",
            done=3,
            total=5,
        )
        on_batch(1, 4)
        on_batch(2, 4)
        on_batch(3, 4)
        on_batch(4, 4)
        await _drain_pending_progress()
        assert frames, "至少上报一帧"
        for done, total, phase in frames:
            assert (done, total) == (3, 5)
            assert re.fullmatch(r"factor_series_build:mount [1-4]/4", phase)
        assert frames[-1][2] == "factor_series_build:mount 4/4"
        ks = [int(f[2].rsplit(" ", 1)[1].split("/")[0]) for f in frames]
        assert ks == sorted(ks)

    async def test_zero_total_renders_bare_prefix(self) -> None:
        """n=0 时 phase 退化为裸前缀(与 make_sync_progress 同防御)。"""
        progress, frames = _recorder()
        on_batch = make_stage_phase_progress(
            progress, phase_prefix="factor_series_build:mount", done=3, total=5
        )
        on_batch(0, 0)
        await _drain_pending_progress()
        assert frames == [(3, 5, "factor_series_build:mount")]
