"""预置因子进程内 factor_series 构建通道(issue #398)。

覆盖:

* **引擎逐值** —— stub provider → 真实 ``build_window_data_mount`` →
  ``run_predefined_factor_series``:产出与**独立 pandas PIT 参考**逐值
  对照(决策日采样 = available_at 门控),基准标的(index)保留在时序
  因子值域、被截面因子构建期剔除(#380);
* **审计同构** —— ``default_predefined_prefix_audit`` 复用真实
  ``run_prefix_invariance_audit`` 引擎:因果样板因子通过;故意读未来的
  临时目录因子被检出(首个分歧日期,不落库语义由执行器承担);
* **执行器同构** —— ``kind=predefined_factor``:免沙箱开关(无 Docker
  前置)、目录锚解析、缓存命中、审计检出 → failed=lookahead_detected、
  落库 record kind=p_ 语义;``kind=factor`` 路径(容器 + 沙箱门)零变化
  由既有 #360/#371/#375 测试守护。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pandas as pd
import pytest

from finboard_backtest.background_jobs.executors.factor_series_build import (
    LOOKAHEAD_DETECTED,
    FactorSeriesBuildExecutor,
    FactorSeriesBuildPayload,
)
from finboard_backtest.factors.predefined import (
    FactorSeriesFrame,
    PredefinedFactorDefinition,
    PredefinedFactorInput,
    get_predefined_factor,
    predefined_factor_commit,
)
from finboard_backtest.research_sandbox.predefined_runner import (
    UNKNOWN_PREDEFINED_FACTOR,
    default_predefined_prefix_audit,
    run_predefined_factor_series,
)
from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec
from finboard_data.releases import ReleaseDatasetKind
from finboard_persistence import FactorSeriesRecord

# --------------------------------------------------------------------- #
# stub provider(对齐 FrozenReleaseProvider 最小面;与 test_data_mount 同风格)
# --------------------------------------------------------------------- #

SYMBOLS = ("600000.SH", "000001.SZ", "600519.SH")
INDEX_SYMBOL = "000300.SH"
ALL_SYMBOLS = (*SYMBOLS, INDEX_SYMBOL)


@dataclass
class _Inst:
    code: str
    market: str = "SH"
    instrument_type: str = "stock"
    industry: str | None = None


@dataclass
class _Sym:
    code: str


@dataclass
class _Bar:
    symbol: _Sym
    timestamp: datetime
    open: float = 9.0
    high: float = 11.0
    low: float = 8.0
    close: float = 10.0
    volume: float = 100.0
    amount: float = 1000.0


@dataclass
class _PITBar:
    code: str
    day: date
    close: float
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            self.available_at = datetime(
                self.day.year, self.day.month, self.day.day, 15, 30, tzinfo=UTC
            )

    @property
    def bar(self) -> _Bar:
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=datetime(
                self.day.year, self.day.month, self.day.day
            ),
            close=self.close,
        )


@dataclass
class _Kind:
    value: str


@dataclass
class _Release:
    release_id: str
    dataset_kind: Any
    instruments: list[_Inst] = field(default_factory=list)
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2023, 1, 2)
    end_date: date = date(2024, 12, 31)


@dataclass
class _StubProvider:
    release: _Release
    bars: list[_PITBar] = field(default_factory=list)

    async def fetch_point_in_time_bars(
        self, symbol: Any, period: Any, start: Any, end: Any, *, decision_at: Any, adjust: str = "qfq"
    ) -> list[_PITBar]:
        del period, start, end, adjust
        gate = decision_at.date()
        return [b for b in self.bars if b.code == symbol.code and b.day <= gate]

    async def fetch_daily_metrics(
        self, symbol: Any, *, start: Any, end: Any, decision_at: Any
    ) -> list[Any]:
        del start, end, decision_at
        return []


def _price_path(seed: int, days: int) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(seed)
    return list(
        100.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.015, size=days))
    )


def _bars() -> list[_PITBar]:
    """2023-01-03 起连续 260 个自然日(全标的;指数为独立路径)。"""
    start = date(2023, 1, 3)
    days = [start + timedelta(days=i) for i in range(260)]
    bars: list[_PITBar] = []
    paths = {symbol: _price_path(7 + i, 260) for i, symbol in enumerate(SYMBOLS)}
    index_path = _price_path(42, 260)
    for i, day in enumerate(days):
        for symbol in SYMBOLS:
            bars.append(_PITBar(code=symbol, day=day, close=paths[symbol][i]))
        bars.append(_PITBar(code=INDEX_SYMBOL, day=day, close=index_path[i]))
    return bars


BARS_RELEASE = "DR-bars-398"
DAILY_RELEASE = "DR-daily-398"


def _instruments() -> list[_Inst]:
    return [
        _Inst("600000.SH", industry="银行"),
        _Inst("000001.SZ", industry="银行"),
        _Inst("600519.SH", industry="食品饮料"),
        _Inst(INDEX_SYMBOL, instrument_type="index"),
    ]


def _provider(release_id: str = BARS_RELEASE) -> _StubProvider:
    if release_id == DAILY_RELEASE:
        return _StubProvider(
            release=_Release(DAILY_RELEASE, _Kind("daily_metrics"), _instruments()),
            bars=[],
        )
    return _StubProvider(
        release=_Release(BARS_RELEASE, _Kind("bars"), _instruments()),
        bars=_bars(),
    )


def _provider_factory(rid: str) -> _StubProvider:
    return _provider(rid)


def _spec(name: str = "return_21d") -> FactorSeriesRunSpec:
    # 窗口落在 bar 区间内、且 cut 之后仍有 bar(前视审计可对照分歧)。
    dates = tuple(date(2023, 8, 30) + timedelta(days=k) for k in range(6))
    return FactorSeriesRunSpec(
        code_artifact=name,
        code_commit=predefined_factor_commit(name),
        release_id=BARS_RELEASE,
        dataset_release_ids=(DAILY_RELEASE,),
        params={},
        window_start=dates[0],
        window_end=dates[-1],
        dates=dates,
    )


def _settings() -> Any:
    class _S:
        research_sandbox_workspace_root = "data_cache/research_sandbox"
        research_sandbox_max_nan_ratio = 0.5
        research_sandbox_min_coverage = 0.5

    return _S()


# --------------------------------------------------------------------- #
# 引擎逐值
# --------------------------------------------------------------------- #


class TestEngineValues:
    async def test_return_21d_matches_independent_pandas_pit(self) -> None:
        result = await run_predefined_factor_series(
            _spec(), settings=_settings(), release_provider_factory=_provider_factory
        )
        assert result.dates == _spec().dates
        assert result.quality is not None
        assert result.metrics["mode"] == "predefined_inprocess"
        # 时序因子值域 = 全挂载标的(含基准 index;消费端统一剔除)
        assert set(result.values[result.dates[0]]) == set(ALL_SYMBOLS)
        # 独立 pandas 参考:逐标的 close/close.shift(21)-1,按决策日 asof
        bars = _bars()
        for symbol in ALL_SYMBOLS:
            closes = pd.Series(
                [b.close for b in bars if b.code == symbol],
                index=[b.day for b in bars if b.code == symbol],
            )
            reference = closes / closes.shift(21) - 1.0
            for day in result.dates:
                expected = reference.loc[:day].iloc[-1]
                got = result.values[day][symbol]
                assert got is not None, (symbol, day)
                assert got == pytest.approx(float(expected), rel=1e-12)

    async def test_quality_and_mount_archive(self) -> None:
        result = await run_predefined_factor_series(
            _spec(), settings=_settings(), release_provider_factory=_provider_factory
        )
        assert result.quality.passed
        assert result.mount is not None
        assert result.mount_manifest_checksum == result.mount.manifest_checksum
        assert result.run_id is None
        # 挂载窗口上界 fail-closed 由 build_window_data_mount 承担;窗口
        # 内最后决策日次日的 bar 不可见于采样(前缀不变性审计兜底)。

    async def test_cross_section_factor_excludes_benchmark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cross_section=True 的临时目录条目:采样面收窄到可交易域(#380)。"""

        def rank_compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
            closes = inp.bars("close")
            per_symbol = {
                symbol: series.values.copy() for symbol, series in closes.items()
            }
            return inp.sample(closes, per_symbol)

        entry = PredefinedFactorDefinition(
            name="zz_rank_test",
            title="临时截面测试因子(raw 值,采样面即截面域)",
            family="test",
            direction=get_predefined_factor("return_21d").direction,
            signal_eligible=True,
            data_dependencies=("bars.close",),
            window=1,
            implementation_version="1",
            compute=rank_compute,
            cross_section=True,
        )
        monkeypatch.setitem(
            __import__(
                "finboard_backtest.factors.predefined.registry",
                fromlist=["PREDEFINED_FACTORS"],
            ).PREDEFINED_FACTORS,
            entry.name,
            entry,
        )
        result = await run_predefined_factor_series(
            _spec("zz_rank_test"),
            settings=_settings(),
            release_provider_factory=_provider_factory,
        )
        for day in result.dates:
            assert set(result.values[day]) == set(SYMBOLS)
            assert INDEX_SYMBOL not in result.values[day]
        # raw close 逐值对照
        bars = _bars()
        for symbol in SYMBOLS:
            closes = {
                b.day: b.close for b in bars if b.code == symbol
            }
            for day in result.dates:
                expected = closes[max(d for d in closes if d <= day)]
                assert result.values[day][symbol] == pytest.approx(expected)

    async def test_version_mismatch_rejected(self) -> None:
        spec = replace(
            _spec(), code_commit="predefined-000000000000"
        )
        with pytest.raises(Exception, match="实现版本锚不一致") as exc_info:
            await run_predefined_factor_series(
                spec, settings=_settings(), release_provider_factory=_provider_factory
            )
        assert getattr(exc_info.value, "code", "") == "predefined_version_mismatch"

    async def test_unknown_factor_rejected(self) -> None:
        spec = replace(_spec(), code_artifact="no_such_factor")
        with pytest.raises(Exception, match="未注册的平台预置因子") as exc_info:
            await run_predefined_factor_series(
                spec, settings=_settings(), release_provider_factory=_provider_factory
            )
        assert getattr(exc_info.value, "code", "") == UNKNOWN_PREDEFINED_FACTOR


# --------------------------------------------------------------------- #
# 审计同构(真实引擎 + 进程内 build_fn)
# --------------------------------------------------------------------- #


class TestPrefixAudit:
    async def test_causal_sample_factor_passes(self) -> None:
        spec = _spec()
        baseline = await run_predefined_factor_series(
            spec, settings=_settings(), release_provider_factory=_provider_factory
        )
        report = await default_predefined_prefix_audit(
            spec, baseline, truncate_at=spec.dates[3]
        )
        assert report.passed

    async def test_lookahead_factor_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """故意读未来的临时因子(每日截面 = 全窗口最后收盘)被审计检出。"""

        def lookahead_compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
            closes = inp.bars("close")
            per_symbol = {
                symbol: series.values
                for symbol, series in closes.items()
            }
            # 非法实现:每个决策日都取「整窗口最后一行」的值(未来数据)
            frame: FactorSeriesFrame = {}
            for day in inp.decision_dates:
                frame[day] = {
                    symbol: (
                        float(values[-1])
                        if math.isfinite(float(values[-1]))
                        else None
                    )
                    for symbol, values in per_symbol.items()
                }
            return frame

        entry = PredefinedFactorDefinition(
            name="zz_lookahead_test",
            title="临时前视测试因子",
            family="test",
            direction=get_predefined_factor("return_21d").direction,
            signal_eligible=True,
            data_dependencies=("bars.close",),
            window=1,
            implementation_version="1",
            compute=lookahead_compute,
        )
        registry = __import__(
            "finboard_backtest.factors.predefined.registry",
            fromlist=["PREDEFINED_FACTORS"],
        )
        monkeypatch.setitem(registry.PREDEFINED_FACTORS, entry.name, entry)
        # commit 锚按目录现值重算(临时条目在目录中,锚函数可解析)
        spec = FactorSeriesRunSpec(
            code_artifact=entry.name,
            code_commit=predefined_factor_commit(entry.name),
            release_id=BARS_RELEASE,
            dataset_release_ids=(DAILY_RELEASE,),
            params={},
            window_start=date(2023, 8, 30),
            window_end=date(2023, 9, 4),
            dates=tuple(date(2023, 8, 30) + timedelta(days=k) for k in range(6)),
        )
        baseline = await run_predefined_factor_series(
            spec, settings=_settings(), release_provider_factory=_provider_factory
        )
        report = await default_predefined_prefix_audit(
            spec, baseline, truncate_at=spec.dates[3]
        )
        assert not report.passed
        assert report.first_divergence_date is not None
        assert report.first_divergence_date <= spec.dates[3]


# --------------------------------------------------------------------- #
# 执行器(kind 分派 / 沙箱门 / 缓存 / 落库语义)
# --------------------------------------------------------------------- #


def _job(kind: str = "predefined_factor", name: str = "return_21d") -> Any:
    return {
        "kind": kind,
        "name": name,
        "release_id": BARS_RELEASE,
        "dataset_release_ids": [DAILY_RELEASE],
        "window_start": "2023-08-30",
        "window_end": "2023-09-04",
    }


class _Settings:
    research_sandbox_enabled = False  # 故意关闭:predefined 不得被沙箱门拦截
    research_code_repo_path = "data_cache/research_code"
    research_code_max_files = 8
    research_code_max_file_bytes = 65536




class _FakeReleaseRow:
    def __init__(self) -> None:
        self.dataset_kind = ReleaseDatasetKind.BARS
        self.release_checksum = "c" * 64


class _FakeReleaseRepo:
    def __init__(self, session: Any) -> None:
        pass

    async def get(self, release_id: str) -> _FakeReleaseRow | None:
        return (
            _FakeReleaseRow()
            if release_id in (BARS_RELEASE, DAILY_RELEASE)
            else None
        )


class _FakeSeriesRepo:
    upserted: FactorSeriesRecord | None = None

    def __init__(self, session: Any) -> None:
        pass

    async def find_matching(self, **kwargs: Any) -> FactorSeriesRecord | None:
        del kwargs
        return None

    async def upsert(self, record: FactorSeriesRecord) -> FactorSeriesRecord:
        _FakeSeriesRepo.upserted = record
        return record


class _FakeSession:
    def __init__(self, *repos: type[Any]) -> None:
        self._repos = repos

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    def _repo(self, repo_type: Any) -> Any:
        name = repo_type.__name__
        for candidate in self._repos:
            if candidate.__name__ == name:
                return candidate(self)
        raise AssertionError(f"未注册的 repo: {name}")


class _SM:
    def __call__(self) -> _FakeSession:
        return _FakeSession(_FakeReleaseRepo, _FakeSeriesRepo)


def _patch_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行器在函数体内 ``from finboard_persistence import ...``,须在源头替换。"""
    monkeypatch.setattr(
        "finboard_persistence.ResearchDatasetReleaseRepository", _FakeReleaseRepo
    )
    monkeypatch.setattr(
        "finboard_persistence.FactorSeriesRepository", _FakeSeriesRepo
    )
    # 交易日历注入(执行器 _build_spec 推导窗口决策日;单测不联网)
    monkeypatch.setattr(
        "finboard_data.trading_calendar.trading_days", _patched_calendar
    )


def _patched_calendar(start: date, end: date) -> set[date]:
    return {
        start + timedelta(days=k)
        for k in range((end - start).days + 1)
    }


class _JobRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.job_id = "JOB-398"


async def _noop_progress(done: int, total: int, phase: str) -> None:
    del done, total, phase


def _executor(
    runner: Any,
    audit: Any,
) -> FactorSeriesBuildExecutor:
    return FactorSeriesBuildExecutor(
        session_maker=cast(Any, None),
        settings_factory=lambda: _Settings(),
        predefined_runner=runner,
        predefined_prefix_audit=audit,
    )


class TestExecutorPredefinedKind:
    async def test_end_to_end_with_real_engine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """真实引擎 + 真实审计 + Fake 持久层:构建 → 审计 → record。"""
        _FakeSeriesRepo.upserted = None

        async def runner(spec: Any) -> Any:
            return await run_predefined_factor_series(
                spec,
                settings=_settings(),
                release_provider_factory=_provider_factory,
                workspace_root=tmp_path,
            )

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
            return await default_predefined_prefix_audit(
                spec, baseline, truncate_at=truncate_at
            )

        executor = _executor(runner, audit)
        executor._session_maker = cast(Any, _SM())
        _patch_repos(monkeypatch)
        result = await executor.execute(
            cast(Any, _JobRecord(_job())), cast(Any, _noop_progress)
        )
        assert result.status == "succeeded"
        assert _FakeSeriesRepo.upserted is not None
        assert result.result_ref == _FakeSeriesRepo.upserted.series_id
        record = _FakeSeriesRepo.upserted
        assert record is not None
        assert record.kind == "predefined_factor"
        assert record.code_artifact == "return_21d"
        assert record.code_commit == predefined_factor_commit("return_21d")
        # 引擎真实产出进入了内容寻址 record(执行器按 A 股交易日历推导
        # 窗口;单测注入合成日历,不依赖 akshare 联网加载)
        expected_dates = tuple(
            sorted(_patched_calendar(date(2023, 8, 30), date(2023, 9, 4)))
        )
        assert record.dates == expected_dates
        assert record.quality is not None

    async def test_sandbox_gate_skipped_but_unknown_factor_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = _executor(None, None)
        executor._session_maker = cast(Any, _SM())
        _patch_repos(monkeypatch)
        from finboard_backtest.background_jobs.contracts import ExecutorError

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(
                cast(Any, _JobRecord(_job(name="no_such_factor"))),
                cast(Any, _noop_progress),
            )
        assert exc_info.value.code == UNKNOWN_PREDEFINED_FACTOR

    async def test_commit_and_params_rejected_for_predefined(self) -> None:
        from finboard_backtest.background_jobs.contracts import ExecutorError

        executor = _executor(None, None)
        payload = _job()
        payload["commit"] = "abc"
        with pytest.raises(ExecutorError) as commit_error:
            await executor._resolve_predefined(
                FactorSeriesBuildPayload(
                    kind="predefined_factor",
                    name="return_21d",
                    release_id="DR-bars-398",
                    dataset_release_ids=(),
                    window_start=date(2023, 10, 2),
                    window_end=date(2023, 10, 7),
                    commit="abc",
                )
            )
        assert "commit/artifact_id" in str(commit_error.value.summary)
        payload_params = _job()
        payload_params["params"] = {"x": 1}
        with pytest.raises(ExecutorError) as params_error:
            await executor._resolve_predefined(
                FactorSeriesBuildPayload(
                    kind="predefined_factor",
                    name="return_21d",
                    release_id="DR-bars-398",
                    dataset_release_ids=(),
                    window_start=date(2023, 10, 2),
                    window_end=date(2023, 10, 7),
                    params={"x": 1},
                )
            )
        assert "params 须为空" in str(params_error.value.summary)

    async def test_unknown_kind_still_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_backtest.background_jobs.contracts import ExecutorError

        executor = _executor(None, None)
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(
                cast(Any, _JobRecord(_job(kind="strategy", name="x"))),
                cast(Any, _noop_progress),
            )
        assert exc_info.value.code == "factor_series_build_kind_not_implemented"

    async def test_audit_failure_blocks_persist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """审计检出前视 → failed=lookahead_detected,不落库。"""
        _FakeSeriesRepo.upserted = None

        async def runner(spec: Any) -> Any:
            return await run_predefined_factor_series(
                spec,
                settings=_settings(),
                release_provider_factory=_provider_factory,
                workspace_root=tmp_path,
            )

        @dataclass(frozen=True)
        class _Outcome:
            passed: bool = False
            first_divergence_date: date | None = date(2023, 8, 31)

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
            del spec, baseline, truncate_at
            return _Outcome()

        executor = _executor(runner, audit)
        executor._session_maker = cast(Any, _SM())
        _patch_repos(monkeypatch)
        result = await executor.execute(
            cast(Any, _JobRecord(_job())), cast(Any, _noop_progress)
        )
        assert result.status == "failed"
        assert result.error_code == LOOKAHEAD_DETECTED
        assert "首个分歧日期" in (result.error_summary or "")
        assert getattr(_FakeSeriesRepo, "upserted", None) is None


# --------------------------------------------------------------------- #
# 行业分组装配(issue #400:inp.industry_groups() 接线)
# --------------------------------------------------------------------- #


class TestIndustryGroupsAssembly:
    async def test_industry_groups_derived_from_release_excluding_benchmark(self) -> None:
        """分组自 bars 主发布 instruments.industry 装配;基准标的剔除。"""
        result = await run_predefined_factor_series(
            _spec(), settings=_settings(), release_provider_factory=_provider_factory
        )
        assert result.industry_groups == {
            "600000.SH": "银行",
            "000001.SZ": "银行",
            "600519.SH": "食品饮料",
        }
        assert INDEX_SYMBOL not in result.industry_groups
        assert result.metrics["industry_groups_mapped"] == 3
        assert result.metrics["industry_groups_missing"] == 0

    async def test_missing_industry_counted_not_fatal(self) -> None:
        """缺行业标的 → 映射缺失(None)+ metrics 计数,构建不炸。"""
        provider = _StubProvider(
            release=_Release(
                BARS_RELEASE,
                _Kind("bars"),
                [
                    _Inst("600000.SH", industry="银行"),
                    _Inst("000001.SZ", industry=None),
                    _Inst("600519.SH"),
                    _Inst(INDEX_SYMBOL, instrument_type="index"),
                ],
            ),
            bars=_bars(),
        )
        result = await run_predefined_factor_series(
            _spec(),
            settings=_settings(),
            release_provider_factory=lambda _rid: provider,
        )
        assert result.industry_groups["000001.SZ"] is None
        assert result.industry_groups.get("600519.SH") is None
        assert result.metrics["industry_groups_missing"] == 2
        assert result.metrics["industry_groups_mapped"] == 1

    async def test_industry_groups_visible_to_factor_compute(self) -> None:
        """因子 compute 经 inp.industry_groups() 拿到装配后的分组。"""
        from finboard_backtest.factors.predefined.operators import cs_neutralize

        seen: dict[str, dict[str, str | None]] = {}

        def neutral_compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
            closes = inp.bars("close")
            seen["groups"] = inp.industry_groups()
            frame = inp.sample(
                closes,
                {symbol: series.values.copy() for symbol, series in closes.items()},
            )
            groups = seen["groups"]
            return {
                day: cs_neutralize(cross, groups) for day, cross in frame.items()
            }

        entry = PredefinedFactorDefinition(
            name="zz_industry_test",
            title="临时行业中性测试因子(组内去均值 close)",
            family="test",
            direction=get_predefined_factor("return_21d").direction,
            signal_eligible=True,
            data_dependencies=("bars.close",),
            window=1,
            implementation_version="1",
            compute=neutral_compute,
            cross_section=True,
        )
        import finboard_backtest.factors.predefined.registry as registry_module

        registry_module.PREDEFINED_FACTORS[entry.name] = entry
        try:
            result = await run_predefined_factor_series(
                _spec(entry.name),
                settings=_settings(),
                release_provider_factory=_provider_factory,
            )
        finally:
            registry_module.PREDEFINED_FACTORS.pop(entry.name, None)
        # 分组透传到因子(基准不在映射中)
        assert seen["groups"] == {
            "600000.SH": "银行",
            "000001.SZ": "银行",
            "600519.SH": "食品饮料",
        }
        # 组内去均值生效:两银行标的之和 ≈ 0(组内均值),食品饮料单标的 → 0
        day = result.dates[-1]
        bank_sum = (result.values[day]["600000.SH"] or 0.0) + (
            result.values[day]["000001.SZ"] or 0.0
        )
        assert bank_sum == pytest.approx(0.0, abs=1e-9)
        assert result.values[day]["600519.SH"] == pytest.approx(0.0)

    async def test_mount_override_requires_explicit_industry_groups(self) -> None:
        """mount_override 路径必须显式传 industry_groups(审计变体继承语义)。"""
        spec = _spec()
        baseline = await run_predefined_factor_series(
            spec, settings=_settings(), release_provider_factory=_provider_factory
        )
        from pathlib import Path

        from finboard_backtest.research_sandbox.data_mount import (
            filter_window_data_mount,
        )

        assert baseline.mount is not None
        variant_mount = await filter_window_data_mount(
            baseline.mount,
            new_window_end=spec.dates[3],
            dates=spec.dates[:4],
            out_root=Path(baseline.workspace_dir) / "data-cut-industry-test",
        )
        with pytest.raises(Exception, match="industry_groups") as exc_info:
            await run_predefined_factor_series(
                replace(spec, dates=spec.dates[:4], window_end=spec.dates[3]),
                settings=_settings(),
                mount_override=variant_mount,
                benchmark_only_symbols=baseline.benchmark_only_symbols,
                workspace_root=baseline.workspace_dir,
            )
        assert getattr(exc_info.value, "code", "") == "mount_spec_mismatch"

    async def test_audit_inherits_industry_groups(self) -> None:
        """审计变体继承基线行业分组:分组敏感因子的截断重算不产生假阳性。"""

        def neutral_compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
            from finboard_backtest.factors.predefined.operators import cs_neutralize

            closes = inp.bars("close")
            frame = inp.sample(
                closes,
                {symbol: series.values.copy() for symbol, series in closes.items()},
            )
            groups = inp.industry_groups()
            return {
                day: cs_neutralize(cross, groups) for day, cross in frame.items()
            }

        entry = PredefinedFactorDefinition(
            name="zz_industry_audit_test",
            title="临时行业分组敏感因子(审计继承验证)",
            family="test",
            direction=get_predefined_factor("return_21d").direction,
            signal_eligible=True,
            data_dependencies=("bars.close",),
            window=1,
            implementation_version="1",
            compute=neutral_compute,
            cross_section=True,
        )
        import finboard_backtest.factors.predefined.registry as registry_module

        registry_module.PREDEFINED_FACTORS[entry.name] = entry
        try:
            spec = FactorSeriesRunSpec(
                code_artifact=entry.name,
                code_commit=predefined_factor_commit(entry.name),
                release_id=BARS_RELEASE,
                dataset_release_ids=(DAILY_RELEASE,),
                params={},
                window_start=date(2023, 8, 30),
                window_end=date(2023, 9, 4),
                dates=tuple(date(2023, 8, 30) + timedelta(days=k) for k in range(6)),
            )
            baseline = await run_predefined_factor_series(
                spec, settings=_settings(), release_provider_factory=_provider_factory
            )
            # 审计期间临时条目仍在目录中(build_fn 需解析)
            report = await default_predefined_prefix_audit(
                spec, baseline, truncate_at=spec.dates[3]
            )
        finally:
            registry_module.PREDEFINED_FACTORS.pop(entry.name, None)
        assert report.passed
