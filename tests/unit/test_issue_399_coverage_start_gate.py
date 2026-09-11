"""#361 覆盖检查消费 ``min_history_bars`` 覆盖起点声明(issue #399)。

* 声明覆盖起点的因子(beta_1320d 等 7 个):序列在**首个决策日**全缺测
  → 入队具名拒绝 ``predefined_factor_series_coverage_start_missing``,
  错误附覆盖起点 / 修复路径(窗口前移 / 短窗口变体);
* 未声明因子(批次 0 语义):值全缺测不检查、行为零变化;
* 首个决策日有值(即便后续日期缺测)→ 放行;
* 既有门控(未找到序列 / 锚定失配 / 日期未覆盖)回归不受影响。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from finboard_backtest.research_code.predefined_factors import (
    PREDEFINED_ANCHOR_MISMATCH_CODE,
    PREDEFINED_COVERAGE_MISSING_CODE,
    PREDEFINED_COVERAGE_START_MISSING_CODE,
    predefined_factor_series_coverage_gate_error,
)
from finboard_persistence.factor_series_repo import FactorSeriesRecord

RELEASE_ID = "multi-asset-core-20260906-v6"
WINDOW_START = date(2024, 1, 2)
WINDOW_END = date(2024, 12, 31)
DECISION_DATES = (date(2024, 3, 1), date(2024, 6, 3), date(2024, 9, 2))


def _record(
    name: str,
    *,
    dates: tuple[date, ...] = DECISION_DATES,
    values: dict[str, dict[str, float | None]] | None = None,
    release_id: str = RELEASE_ID,
) -> FactorSeriesRecord:
    if values is None:
        values = {str(day): {"600000.SH": 1.0} for day in dates}
    return FactorSeriesRecord.build(
        code_artifact=name,
        code_commit="predefined-test0001",
        kind="predefined_factor",
        release_id=release_id,
        dataset_release_ids=(),
        params={},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        dates=dates,
        values=values,
    )


class _FakeLookup:
    """按裸名返回预置记录的 series 反查桩。"""

    def __init__(self, records: dict[str, FactorSeriesRecord | None]) -> None:
        self._records = records

    async def find_matching(self, **_kwargs: object) -> FactorSeriesRecord | None:
        name = str(_kwargs.get("code_artifact"))
        return self._records.get(name)


def _probe(series: Any, *, decision_dates: Sequence[date]) -> list[date]:
    covered = set(series.dates)
    return sorted(day for day in decision_dates if day not in covered)


def _gate(
    records: dict[str, FactorSeriesRecord | None],
    referenced: tuple[str, ...],
    *,
    decision_dates: tuple[date, ...] = DECISION_DATES,
    bars_release_id: str = RELEASE_ID,
) -> str | None:
    import asyncio

    return asyncio.run(
        predefined_factor_series_coverage_gate_error(
            referenced_predefined=referenced,
            series_lookup=_FakeLookup(records),
            bars_release_id=bars_release_id,
            dataset_release_ids=(),
            decision_dates=decision_dates,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            coverage_missing=_probe,
        )
    )


def _all_none_record(
    name: str, dates: tuple[date, ...] = DECISION_DATES
) -> FactorSeriesRecord:
    return _record(
        name,
        dates=dates,
        values={str(day): {"600000.SH": None} for day in dates},
    )


class TestCoverageStartGate:
    def test_long_window_factor_first_day_all_none_rejected(self) -> None:
        """beta_1320d 首个决策日全缺测 → 具名拒绝,附覆盖起点与修复路径。"""
        record = _all_none_record("beta_1320d")
        error = _gate({"beta_1320d": record}, ("p_beta_1320d",))
        assert error is not None
        assert PREDEFINED_COVERAGE_START_MISSING_CODE in error
        assert "1320" in error
        assert "2024-03-01" in error
        assert "finboard_factor_series_build" in error
        assert "短窗口变体" in error

    def test_partially_warmed_series_rejected_with_realized_start(self) -> None:
        """前段缺测、后段有值 → 拒绝并给出首个非缺测决策日。"""
        values: dict[str, dict[str, float | None]] = {
            str(date(2024, 3, 1)): {"600000.SH": None},
            str(date(2024, 6, 3)): {"600000.SH": None},
            str(date(2024, 9, 2)): {"600000.SH": 0.5},
        }
        record = _record("beta_1320d", values=values)
        error = _gate({"beta_1320d": record}, ("p_beta_1320d",))
        assert error is not None
        assert PREDEFINED_COVERAGE_START_MISSING_CODE in error
        assert "2024-09-02" in error

    def test_first_decision_day_with_value_passes(self) -> None:
        """首个决策日有值(后续缺测不在本检查范围)→ 放行。"""
        values: dict[str, dict[str, float | None]] = {
            str(date(2024, 3, 1)): {"600000.SH": 1.0},
            str(date(2024, 6, 3)): {"600000.SH": None},
            str(date(2024, 9, 2)): {"600000.SH": None},
        }
        record = _record("beta_1320d", values=values)
        assert _gate({"beta_1320d": record}, ("p_beta_1320d",)) is None

    def test_undeclared_factor_values_not_checked(self) -> None:
        """未声明 min_history_bars 的因子值全缺测 → 不做值检查(批次 0 语义)。"""
        record = _all_none_record("return_21d")
        assert _gate({"return_21d": record}, ("p_return_21d",)) is None

    def test_existing_gates_unaffected(self) -> None:
        """既有具名拒绝(未找到 / 锚定失配 / 日期未覆盖)回归。"""
        # 未找到序列
        error = _gate({}, ("p_beta_1320d",))
        assert error is not None
        assert PREDEFINED_COVERAGE_MISSING_CODE in error
        # 锚定失配
        record = _record("beta_1320d", release_id="other-release")
        error = _gate({"beta_1320d": record}, ("p_beta_1320d",))
        assert error is not None
        assert PREDEFINED_ANCHOR_MISMATCH_CODE in error
        # 日期未覆盖(日期检查先于覆盖起点检查)
        values: dict[str, dict[str, float | None]] = {
            str(date(2024, 6, 3)): {"600000.SH": 1.0},
            str(date(2024, 9, 2)): {"600000.SH": 1.0},
        }
        record = _record(
            "beta_1320d",
            dates=(date(2024, 6, 3), date(2024, 9, 2)),
            values=values,
        )
        error = _gate({"beta_1320d": record}, ("p_beta_1320d",))
        assert error is not None
        assert PREDEFINED_COVERAGE_MISSING_CODE in error

    def test_empty_decision_dates_passes(self) -> None:
        record = _all_none_record("beta_1320d")
        assert _gate({"beta_1320d": record}, ("p_beta_1320d",), decision_dates=()) is None

    def test_unregistered_reference_reports_first_gate(self) -> None:
        """未注册名走既有 unregistered 分支(在覆盖检查之前)。"""
        from finboard_backtest.research_code.predefined_factors import (
            PREDEFINED_UNREGISTERED_CODE,
            predefined_factor_reference_gate_error,
        )

        error = predefined_factor_reference_gate_error(
            required_factor_sources=("p_no_such",),
            multi_period=True,
        )
        assert error is not None
        assert PREDEFINED_UNREGISTERED_CODE in error

    def test_registry_declaration_consistency(self) -> None:
        """目录声明不变量:每个声明覆盖起点的因子 warmup >= window。

        #399 基线 7 个精确到值;后续批次(#429 等)按同一不变量追加自己的
        声明集合,各批次在其测试文件内做批次 scope 精确断言 —— 此处不再
        断言全局总数(每加一批都会破)。
        """
        from finboard_backtest.factors.predefined import PREDEFINED_FACTORS

        declared = {
            name: item.min_history_bars
            for name, item in PREDEFINED_FACTORS.items()
            if item.min_history_bars is not None
        }
        assert {
            name: declared.get(name)
            for name in (
                "resid_momentum_120d",
                "resid_momentum_250d",
                "rsrs_beta_600d",
                "rsrs_r2_600d",
                "beta_1320d",
                "corr_market_1320d",
                "sharpe_1320d",
            )
        } == {
            "resid_momentum_120d": 239,
            "resid_momentum_250d": 499,
            "rsrs_beta_600d": 600,
            "rsrs_r2_600d": 600,
            "beta_1320d": 1320,
            "corr_market_1320d": 1320,
            "sharpe_1320d": 1320,
        }
        for name, warmup in declared.items():
            item = PREDEFINED_FACTORS[name]
            assert item.min_history_bars == warmup
            assert item.window is not None
            assert warmup >= item.window
