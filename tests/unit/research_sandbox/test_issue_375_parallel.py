"""issue #375:factor_series_build 三层并行编排,检出语义零变化。

* **审计截断点串行**(原 #375 并发,后因 Docker Desktop WSL2 VM 总量
  ~8GB 扛不住双审计容器并发峰值、全市场挂载下实测成对 OOM 而改串行,
  见 #424)—— ``_audit_sample`` 逐截断点执行:各自从基线挂载过滤出
  变体挂载 + 独立容器;失败报告仍取最早分歧日期,异常按截断点顺序
  传播;
* **挂载逐标的读取并行** —— 分块 gather(块内并发、块间保序消费流式
  writer),落盘 parquet 与串行逐表逐行一致。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
    audit_truncation_points,
)
from finboard_backtest.research_sandbox.data_mount import (
    build_window_data_mount,
)
from finboard_persistence import FactorSeriesRecord
from tests.unit.research_sandbox.test_data_mount import (
    _Inst,
    _Kind,
    _PITBar,
    _Release,
)

_SYMBOLS = [f"{600000 + i}.SH" for i in range(10)]
_DAYS = (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5))
_DECISION_DATES = tuple(date(2024, 1, d) for d in range(2, 11))


# ----------------------------------------------------------------- 审计并发


@dataclass(frozen=True)
class _Outcome:
    passed: bool = True
    first_divergence_date: date | None = None


def _record() -> FactorSeriesRecord:
    dates = list(_DECISION_DATES)
    values = {d.isoformat(): {"600000.SH": 1.0} for d in dates}
    return FactorSeriesRecord.build(
        code_artifact="mom20",
        code_commit="c" * 40,
        kind="factor",
        release_id="DR-bars",
        dataset_release_ids=(),
        params={},
        window_start=dates[0],
        window_end=dates[-1],
        dates=dates,
        values=values,
    )


def _executor(
    audit: Any,
) -> FactorSeriesBuildExecutor:
    return FactorSeriesBuildExecutor(
        session_maker=cast(Any, None),
        settings_factory=cast(Any, object),
        prefix_audit=audit,
    )


class TestAuditSerial:
    async def test_cuts_run_serially(self) -> None:
        """截断点串行执行(单容器内存足迹,#424),全部通过返回 None。"""
        inflight = {"n": 0, "max": 0}
        seen_cuts: list[date] = []

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> _Outcome:
            del spec, baseline
            seen_cuts.append(truncate_at)
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            await asyncio.sleep(0.01)
            inflight["n"] -= 1
            return _Outcome()

        executor = _executor(audit)
        result = await executor._audit_sample(object(), _record(), object())
        assert result is None
        assert inflight["max"] == 1
        assert seen_cuts == audit_truncation_points(list(_DECISION_DATES))

    async def test_divergence_summary_takes_earliest(self) -> None:
        """多截断点均检出:失败报告取最早分歧日期,截断点清单不变。"""
        cuts = audit_truncation_points(list(_DECISION_DATES))
        assert len(cuts) == 2
        outcome_by_cut = {
            cuts[0]: _Outcome(passed=False, first_divergence_date=date(2024, 1, 5)),
            cuts[1]: _Outcome(passed=False, first_divergence_date=date(2024, 1, 4)),
        }

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> _Outcome:
            del spec, baseline
            await asyncio.sleep(0)
            return outcome_by_cut[truncate_at]

        executor = _executor(audit)
        result = await executor._audit_sample(object(), _record(), object())
        assert result is not None
        assert "2024-01-04" in result
        assert "2024-01-05" in result  # 抽样截断点清单保持原序
        assert "2024-01-08" in result

    async def test_exception_raises_first_in_cut_order(self) -> None:
        """异常按截断点顺序重抛第一个(与串行传播顺序一致)。"""
        cuts = audit_truncation_points(list(_DECISION_DATES))

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> _Outcome:
            del spec, baseline
            await asyncio.sleep(0.01)
            raise RuntimeError(f"boom-{truncate_at.isoformat()}")

        executor = _executor(audit)
        with pytest.raises(RuntimeError, match=f"boom-{cuts[0].isoformat()}"):
            await executor._audit_sample(object(), _record(), object())

    async def test_one_cut_passes_one_raises(self) -> None:
        """单侧异常同样按截断点顺序传播,不被另一侧成功吞掉。"""
        cuts = audit_truncation_points(list(_DECISION_DATES))

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> _Outcome:
            del spec, baseline
            if truncate_at == cuts[1]:
                raise RuntimeError("boom-second")
            return _Outcome()

        executor = _executor(audit)
        with pytest.raises(RuntimeError, match="boom-second"):
            await executor._audit_sample(object(), _record(), object())


# ----------------------------------------------------------------- kind 并发


def test_factor_series_build_kind_concurrency_is_two() -> None:
    """沙箱内存 4096 档(#374)后,factor_series_build 提升为 2 并发。"""
    from finboard_app.cli import _KIND_CONCURRENCY

    assert _KIND_CONCURRENCY["factor_series_build"] == 2
    assert _KIND_CONCURRENCY["research_code_run"] == 1
    # 2026-09-13 全市场 556 期双 run 并发把 40GB 宿主推到 98.8%:research_run
    # 串行化(#424 同理由)。
    assert _KIND_CONCURRENCY["research_run"] == 1


# ------------------------------------------------------------- 挂载读取并行


@dataclass
class _SlowProvider:
    """逐标的 fetch 带 await 让步的 stub,记录最大在途数。"""

    release: _Release
    bars: list[_PITBar] = field(default_factory=list)
    daily: pa.Table | None = None
    stats: dict[str, int] = field(default_factory=lambda: {"n": 0, "max": 0})

    def _track(self) -> None:
        self.stats["n"] += 1
        self.stats["max"] = max(self.stats["max"], self.stats["n"])

    async def fetch_point_in_time_bars(
        self, symbol: Any, period: Any, start: Any, end: Any, *,
        decision_at: Any, adjust: str = "qfq",
    ) -> list[_PITBar]:
        del period, start, end, adjust
        self._track()
        await asyncio.sleep(0.01)
        self.stats["n"] -= 1
        return [
            b
            for b in self.bars
            if b.code == symbol.code and b.day <= decision_at.date()
        ]

    async def fetch_daily_metrics_columns(
        self, symbol: Any, *, start: Any, end: Any, decision_at: Any
    ) -> pa.Table:
        del start, end, decision_at
        self._track()
        await asyncio.sleep(0.01)
        self.stats["n"] -= 1
        assert self.daily is not None
        return self.daily.filter(
            pc.equal(self.daily.column("symbol"), symbol.code)
        )


def _providers() -> tuple[_SlowProvider, _SlowProvider]:
    bars = _SlowProvider(
        release=_Release(
            "DR-bars",
            _Kind("bars"),
            [_Inst(code) for code in _SYMBOLS],
            start_date=_DAYS[0],
            end_date=_DAYS[-1],
        ),
        bars=[
            _PITBar(code=code, day=day, close=10.0 + i + j * 100)
            for j, code in enumerate(_SYMBOLS)
            for i, day in enumerate(_DAYS)
        ],
    )
    daily = _SlowProvider(
        release=_Release(
            "DR-daily",
            _Kind("daily_metrics"),
            [_Inst(code) for code in _SYMBOLS],
            start_date=_DAYS[0],
            end_date=_DAYS[-1],
        ),
        daily=pa.table(
            {
                "symbol": [code for code in _SYMBOLS for _ in _DAYS],
                "trade_date": [
                    day.isoformat() for _ in _SYMBOLS for day in _DAYS
                ],
                "pb": [1.0 + i for _ in _SYMBOLS for i in range(len(_DAYS))],
                "available_at": [
                    f"{day.isoformat()}T17:00:00+00:00"
                    for _ in _SYMBOLS
                    for day in _DAYS
                ],
            }
        ),
    )
    return bars, daily


async def _build(tmp_path: Path, bars: _SlowProvider, daily: _SlowProvider) -> Path:
    out_root = tmp_path / "data"
    await build_window_data_mount(
        providers=[bars, daily],
        window_start=_DAYS[0],
        window_end=_DAYS[-1],
        dates=list(_DAYS),
        out_root=out_root,
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id="DR-bars",
        dataset_release_ids=["DR-daily"],
    )
    return out_root


class TestMountFetchParallel:
    async def test_parallel_rows_equal_serial_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """分块并行与串行参照:bars/daily 落盘逐表逐行一致,且真并发发生。"""
        import finboard_backtest.research_sandbox.data_mount as dm

        # 先默认并发构建,再 patch 并发度=1 建串行参照(patch 在并行构建
        # 之后才生效)
        bars_par, daily_par = _providers()
        parallel_root = await _build(tmp_path / "parallel", bars_par, daily_par)
        assert bars_par.stats["max"] > 1
        assert daily_par.stats["max"] > 1

        bars_ref, daily_ref = _providers()
        monkeypatch.setattr(dm, "_MOUNT_FETCH_CONCURRENCY", 1)
        serial_root = await _build(tmp_path / "serial", bars_ref, daily_ref)

        for name in ("bars.parquet", "daily_metrics.parquet"):
            serial = pq.read_table(serial_root / name)
            parallel = pq.read_table(parallel_root / name)
            assert parallel.equals(serial), name

    async def test_manifest_counters_match_serial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """清单数据集行数计数器与串行一致(行序不变 => 计数不变)。"""
        import json

        import finboard_backtest.research_sandbox.data_mount as dm

        bars_ref, daily_ref = _providers()
        monkeypatch.setattr(dm, "_MOUNT_FETCH_CONCURRENCY", 1)
        serial_root = await _build(tmp_path / "serial", bars_ref, daily_ref)
        bars_par, daily_par = _providers()
        parallel_root = await _build(tmp_path / "parallel", bars_par, daily_par)

        def datasets(root: Path) -> list[dict[str, Any]]:
            manifest: dict[str, Any] = json.loads(
                (root / "mount_manifest.json").read_text(encoding="utf-8")
            )
            items: list[dict[str, Any]] = manifest["datasets"]
            return items

        def key(item: dict[str, Any]) -> str:
            return str(item["dataset_kind"])

        serial_map = {key(item): item for item in datasets(serial_root)}
        parallel_map = {key(item): item for item in datasets(parallel_root)}
        assert parallel_map["bars"]["row_count"] == serial_map["bars"]["row_count"]
        assert (
            parallel_map["daily_metrics"]["row_count"]
            == serial_map["daily_metrics"]["row_count"]
        )
