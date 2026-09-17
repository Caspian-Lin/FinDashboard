"""用户自定义因子(u_ 前缀)的编译期校验与入队门控(issue #217;#361 覆盖检查)。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from finboard_backtest.research_code import (
    SERIES_ANCHOR_MISMATCH_CODE,
    SERIES_COVERAGE_MISSING_CODE,
    user_factor_reference_gate_error,
    user_factor_series_coverage_gate_error,
)
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
)


def _spec_with_user_factor() -> ResearchStrategySpec:
    """multi_factor 模板把第一个因子节点换成 u_ 用户因子。"""

    payload = build_strategy_template(
        "multi_factor",
        strategy_id="test_user_factor",
        dataset_release_ids=("frozen-release-v1",),
    ).canonical_payload()
    for node in payload["feature_graph"]["nodes"]:
        if node.get("source") == "pb":
            node["source"] = "u_agent_alpha"
            break
    else:  # pragma: no cover - 模板变化时防御
        raise AssertionError("multi_factor 模板应含 source=pb 的节点")
    return ResearchStrategySpec.model_validate(payload)


class TestCompileUserFactor:
    def test_active_user_factor_compiles(self) -> None:
        spec = _spec_with_user_factor()
        plan = compile_registered_strategy_spec(
            spec, user_factor_sources=frozenset({"u_agent_alpha"})
        )
        assert "u_agent_alpha" in plan.required_factor_sources

    def test_inactive_user_factor_rejected(self) -> None:
        spec = _spec_with_user_factor()
        with pytest.raises(StrategySpecError, match="用户因子不可引用"):
            compile_registered_strategy_spec(spec)
        with pytest.raises(StrategySpecError, match="status=active"):
            compile_registered_strategy_spec(
                spec, user_factor_sources=frozenset({"u_other"})
            )

    def test_user_factor_kind_must_be_factor(self) -> None:
        payload = _spec_with_user_factor().canonical_payload()
        for node in payload["feature_graph"]["nodes"]:
            if node.get("source") == "u_agent_alpha":
                node["kind"] = "market_input"
                break
        spec = ResearchStrategySpec.model_validate(payload)
        with pytest.raises(StrategySpecError, match="kind 须为 factor"):
            compile_registered_strategy_spec(
                spec, user_factor_sources=frozenset({"u_agent_alpha"})
            )

    def test_builtin_path_unaffected(self) -> None:
        spec = build_strategy_template(
            "multi_factor",
            strategy_id="test_builtin",
            dataset_release_ids=("frozen-release-v1",),
        )
        plan = compile_registered_strategy_spec(spec)
        assert "pb" in plan.required_factor_sources


class TestEnqueueGate:
    def test_no_user_factor_passes(self) -> None:
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"pb", "momentum"},
                active_user_factors=frozenset(),
            )
            is None
        )

    def test_retired_user_factor_rejected(self) -> None:
        error = user_factor_reference_gate_error(
            required_factor_sources={"pb", "u_agent_alpha"},
            active_user_factors=frozenset({"u_other"}),
        )
        assert error is not None
        assert "u_agent_alpha" in error
        assert "非 active" in error

    def test_multi_period_user_factor_not_rejected_by_active_gate(self) -> None:
        """issue #361:multi_period 引用 u_ 不再被 active 名单门一刀切秒拒
        (改由 series 覆盖检查把关,见 TestSeriesCoverageGate)。"""
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_agent_alpha"},
                active_user_factors=frozenset({"u_agent_alpha"}),
            )
            is None
        )

    def test_active_user_factor_single_shot_passes(self) -> None:
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_agent_alpha"},
                active_user_factors=frozenset({"u_agent_alpha"}),
            )
            is None
        )


class _StubSeries:
    """覆盖检查 stub series(#361;release_id 锚定 + 已覆盖日期集合)。"""

    def __init__(self, release_id: str, covered: tuple[date, ...]) -> None:
        self.release_id = release_id
        self.covered = frozenset(covered)


class _StubLookup:
    """``SeriesLookup`` 协议 stub:逐 artifact 返回预置 series(或 None)。"""

    def __init__(self, series_by_artifact: dict[str, _StubSeries | None]) -> None:
        self._series = series_by_artifact
        self.calls: list[tuple[str, str]] = []

    async def find_matching(
        self,
        *,
        code_artifact: str,
        release_id: str,
        dataset_release_ids: object = (),
        params: object = None,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> Any:  # 宽返回:与 SeriesLookup 协议结构化兼容(单测 stub)
        self.calls.append((code_artifact, release_id))
        return self._series.get(code_artifact)


def _stub_coverage_missing(
    series: Any, *, decision_dates: Sequence[date]
) -> Sequence[date]:
    covered: Any = getattr(series, "covered", frozenset())
    return tuple(day for day in decision_dates if day not in covered)


_DECISION_DATES = (date(2024, 1, 2), date(2024, 1, 31), date(2024, 2, 28))
_WINDOW = (date(2024, 1, 1), date(2024, 2, 29))


class TestSeriesCoverageGate:
    """issue #361:multi_period x u_ 因子的 series 覆盖检查(替代一刀切秒拒)。"""

    async def test_multi_period_with_full_coverage_passes(self) -> None:
        """全覆盖 series 放行 —— 不再「multi_period 不许引用 u_」。"""
        assert (
            await user_factor_series_coverage_gate_error(
                referenced_user_factors={"u_agent_alpha"},
                series_lookup=_StubLookup(
                    {"agent_alpha": _StubSeries("release-multi", _DECISION_DATES)}
                ),
                bars_release_id="release-multi",
                dataset_release_ids=("release-multi",),
                parameters={"rebalance_frequency": "monthly"},
                decision_dates=_DECISION_DATES,
                window_start=_WINDOW[0],
                window_end=_WINDOW[1],
                coverage_missing=_stub_coverage_missing,
            )
            is None
        )

    async def test_missing_series_named_rejection_with_build_hint(self) -> None:
        error = await user_factor_series_coverage_gate_error(
            referenced_user_factors={"u_agent_alpha"},
            series_lookup=_StubLookup({"agent_alpha": None}),
            bars_release_id="release-multi",
            dataset_release_ids=("release-multi",),
            parameters={"rebalance_frequency": "monthly"},
            decision_dates=_DECISION_DATES,
            window_start=_WINDOW[0],
            window_end=_WINDOW[1],
            coverage_missing=_stub_coverage_missing,
        )
        assert error is not None
        assert SERIES_COVERAGE_MISSING_CODE in error
        assert "finboard_factor_series_build" in error

    async def test_partial_coverage_lists_missing_dates(self) -> None:
        """覆盖不足:具名拒绝 + 缺失决策日期清单。"""
        error = await user_factor_series_coverage_gate_error(
            referenced_user_factors={"u_agent_alpha"},
            series_lookup=_StubLookup(
                {"agent_alpha": _StubSeries("release-multi", (_DECISION_DATES[0],))}
            ),
            bars_release_id="release-multi",
            dataset_release_ids=("release-multi",),
            parameters={"rebalance_frequency": "monthly"},
            decision_dates=_DECISION_DATES,
            window_start=_WINDOW[0],
            window_end=_WINDOW[1],
            coverage_missing=_stub_coverage_missing,
        )
        assert error is not None
        assert SERIES_COVERAGE_MISSING_CODE in error
        assert "2024-01-31" in error
        assert "2024-02-28" in error
        assert "finboard_factor_series_build" in error

    async def test_missing_dates_preview_bounded_at_10(self) -> None:
        """缺失日期清单有界预览 ≤10(总数可见,不刷屏)。"""
        many_dates = tuple(date(2024, 1, 2 + index) for index in range(15))
        error = await user_factor_series_coverage_gate_error(
            referenced_user_factors={"u_agent_alpha"},
            series_lookup=_StubLookup({"agent_alpha": _StubSeries("release-multi", ())}),
            bars_release_id="release-multi",
            dataset_release_ids=("release-multi",),
            parameters={"rebalance_frequency": "monthly"},
            decision_dates=many_dates,
            window_start=many_dates[0],
            window_end=many_dates[-1],
            coverage_missing=_stub_coverage_missing,
        )
        assert error is not None
        assert "共 15 天" in error
        # 预览列表止于第 10 个缺失日期(2024-01-11),其后日期不进文案
        # (重建命令里的窗口端点除外)。
        assert "2024-01-11。请执行" in error
        assert ": 2024-01-12" not in error

    async def test_anchor_mismatch_named_rejection(self) -> None:
        """series 锚定发布与 run bars 主发布不一致:具名拒绝。"""
        error = await user_factor_series_coverage_gate_error(
            referenced_user_factors={"u_agent_alpha"},
            series_lookup=_StubLookup(
                {"agent_alpha": _StubSeries("release-other", _DECISION_DATES)}
            ),
            bars_release_id="release-multi",
            dataset_release_ids=("release-multi",),
            parameters={"rebalance_frequency": "monthly"},
            decision_dates=_DECISION_DATES,
            window_start=_WINDOW[0],
            window_end=_WINDOW[1],
            coverage_missing=_stub_coverage_missing,
        )
        assert error is not None
        assert SERIES_ANCHOR_MISMATCH_CODE in error
        assert "release-other" in error

    async def test_lookup_receives_stripped_artifact_name_and_release(self) -> None:
        """``find_matching`` 收到去 u_ 前缀的 artifact 名与 bars 主发布 id。"""
        lookup = _StubLookup({"agent_alpha": None})
        await user_factor_series_coverage_gate_error(
            referenced_user_factors={"u_agent_alpha"},
            series_lookup=lookup,
            bars_release_id="release-multi",
            dataset_release_ids=("release-multi", "daily-metrics"),
            parameters={"rebalance_frequency": "monthly"},
            decision_dates=_DECISION_DATES,
            window_start=_WINDOW[0],
            window_end=_WINDOW[1],
            coverage_missing=_stub_coverage_missing,
        )
        assert lookup.calls == [("agent_alpha", "release-multi")]

    async def test_empty_decision_dates_passes(self) -> None:
        """发布区间推导不出决策日:不构成覆盖缺口(执行期根因报错兜底)。"""
        assert (
            await user_factor_series_coverage_gate_error(
                referenced_user_factors={"u_agent_alpha"},
                series_lookup=_StubLookup({"agent_alpha": None}),
                bars_release_id="release-multi",
                dataset_release_ids=("release-multi",),
                parameters={"decision_schedule": {"kind": "daily"}},
                decision_dates=(),
                window_start=_WINDOW[0],
                window_end=_WINDOW[1],
                coverage_missing=_stub_coverage_missing,
            )
            is None
        )

    async def test_single_shot_not_gated_by_coverage(self) -> None:
        """single_shot 不走覆盖检查(仍由 #203 快照门控把关)。"""
        assert (
            await user_factor_series_coverage_gate_error(
                referenced_user_factors={"u_agent_alpha"},
                series_lookup=_StubLookup({"agent_alpha": None}),
                bars_release_id="release-multi",
                dataset_release_ids=("release-multi",),
                parameters=None,
                decision_dates=_DECISION_DATES,
                window_start=_WINDOW[0],
                window_end=_WINDOW[1],
                coverage_missing=_stub_coverage_missing,
            )
            is not None
        )
