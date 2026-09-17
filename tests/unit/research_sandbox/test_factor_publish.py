"""沙箱因子输出质量门与快照构造(issue #217)。"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from finboard_backtest.research_sandbox.factor_publish import (
    QUALITY_GATE_FAILED,
    QualityGateError,
    build_factor_snapshot,
    check_output_quality,
)

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


def _gate(
    scores: dict[str, float],
    *,
    universe: int = 10,
    max_nan: float = 0.5,
    min_cov: float = 0.5,
):
    return check_output_quality(
        scores, universe_size=universe, max_nan_ratio=max_nan, min_coverage=min_cov
    )


class TestCheckOutputQuality:
    def test_all_finite_passes(self) -> None:
        scores = {f"S{i}": float(i) for i in range(10)}
        report = _gate(scores)
        assert report.passed
        assert report.failures == ()
        assert report.nan_ratio == 0.0
        assert report.coverage == 1.0
        # 阈值随报告输出(可操作错误的数据基础)
        assert report.as_dict()["thresholds"] == {
            "max_nan_ratio": 0.5,
            "min_coverage": 0.5,
        }

    def test_nan_ratio_exceeds_threshold(self) -> None:
        # coverage 阈值放宽到 0.2,单独验证 NaN 比例超上限(0.75 > 0.5)
        scores = {f"S{i}": float(i) for i in range(2)}
        scores.update({f"S{i}": math.nan for i in range(2, 8)})
        report = _gate(scores, universe=8, min_cov=0.2)
        assert not report.passed
        assert len(report.failures) == 1
        assert "NaN 比例" in report.failures[0]
        assert "max_nan_ratio=0.5" in report.failures[0]
        assert report.nan_ratio == pytest.approx(0.75)

    def test_coverage_below_threshold(self) -> None:
        scores = {f"S{i}": float(i) for i in range(4)}
        report = _gate(scores, universe=10)
        assert not report.passed
        assert any("覆盖率" in f and "min_coverage=0.5" in f for f in report.failures)
        assert report.coverage == pytest.approx(0.4)

    def test_empty_output_rejected(self) -> None:
        report = _gate({})
        assert not report.passed
        assert any("输出为空" in f for f in report.failures)

    def test_scores_exceeding_universe_rejected(self) -> None:
        scores = {f"S{i}": 1.0 for i in range(12)}
        report = _gate(scores, universe=10)
        assert not report.passed
        assert any("超出挂载 universe" in f for f in report.failures)

    def test_custom_thresholds_respected(self) -> None:
        scores = {f"S{i}": float(i) for i in range(4)} | {"S9": math.nan}
        # 默认阈值下通过(nan 0.2 / coverage 0.4<0.5 不过)——单独验证宽松阈值
        report = _gate(scores, universe=10, min_cov=0.3, max_nan=0.5)
        assert report.passed


class TestMissingSymbols:
    """缺失标的点名(issue #237)。"""

    def test_no_missing_when_all_finite(self) -> None:
        report = check_output_quality(
            {"A": 1.0, "B": 2.0},
            universe_size=2,
            max_nan_ratio=0.5,
            min_coverage=0.5,
            universe_symbols=["A", "B"],
        )
        assert report.missing_symbols == ()
        assert report.missing_symbols_total == 0
        assert report.as_dict()["missing_symbols"] == []

    def test_partial_missing_names_symbols(self) -> None:
        report = check_output_quality(
            {"A": 1.0, "B": math.nan},
            universe_size=4,
            max_nan_ratio=0.5,
            min_coverage=0.2,
            universe_symbols=["D", "A", "C", "B"],
        )
        assert report.passed
        # NaN 输出与未产出同属缺失;排序输出
        assert report.missing_symbols == ("B", "C", "D")
        assert report.missing_symbols_total == 3

    def test_all_missing_lists_total(self) -> None:
        report = check_output_quality(
            {},
            universe_size=3,
            max_nan_ratio=0.5,
            min_coverage=0.5,
            universe_symbols=["A", "B", "C"],
        )
        assert not report.passed
        assert report.missing_symbols == ("A", "B", "C")
        assert report.missing_symbols_total == 3

    def test_truncation_bounded_with_total(self) -> None:
        universe = [f"S{i:03d}" for i in range(60)]
        report = check_output_quality(
            {},
            universe_size=60,
            max_nan_ratio=0.5,
            min_coverage=0.5,
            universe_symbols=universe,
        )
        assert len(report.missing_symbols) == 50
        assert report.missing_symbols_total == 60
        assert report.missing_symbols[0] == "S000"

    def test_without_universe_symbols_total_only(self) -> None:
        # 未传清单时保持旧行为:不点名,总数按 universe 差额记
        report = _gate({"A": 1.0}, universe=4)
        assert report.missing_symbols == ()
        assert report.missing_symbols_total == 3


class TestBuildFactorSnapshot:
    def _quality(self, scores: dict[str, float], universe: int = 4):
        return check_output_quality(
            scores, universe_size=universe, max_nan_ratio=0.5, min_coverage=0.5
        )

    def test_finite_only_observations_with_prefix(self) -> None:
        scores = {"A": 1.0, "B": math.nan, "C": 3.0, "D": 4.0}
        quality = self._quality(scores)
        assert quality.passed
        snapshot = build_factor_snapshot(
            factor_artifact_name="mom20",
            run_id="RCR-x",
            decision_at=_TS,
            commit="a" * 40,
            mount_manifest_checksum="m" * 64,
            scores=scores,
            quality=quality,
        )
        assert snapshot.source_run_id == "RCR-x"
        assert snapshot.dataset_release_id is None
        assert snapshot.dataset_release_checksum == "m" * 64
        assert snapshot.code_version == "a" * 40
        # NaN 标的不进观测;feature_name 加 u_ 前缀
        assert [o.symbol for o in snapshot.observations] == ["A", "C", "D"]
        assert {o.feature_name for o in snapshot.observations} == {"u_mom20"}
        assert all(o.source == "research_code_run" for o in snapshot.observations)
        assert snapshot.issues  # 质量统计入 issues

    def test_failed_quality_rejects_snapshot(self) -> None:
        scores = {"A": math.nan, "B": math.nan}
        quality = self._quality(scores)
        assert not quality.passed
        with pytest.raises(QualityGateError):
            build_factor_snapshot(
                factor_artifact_name="mom20",
                run_id="RCR-x",
                decision_at=_TS,
                commit="a" * 40,
                mount_manifest_checksum="m" * 64,
                scores=scores,
                quality=quality,
            )

    def test_error_code_constant(self) -> None:
        # 与 errors.py 分类清单一致的稳定错误码
        assert QUALITY_GATE_FAILED == "quality_gate_failed"
