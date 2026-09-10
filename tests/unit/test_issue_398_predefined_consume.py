"""平台预置因子消费端门控单测(issue #398)。

* 引用门控:未注册名具名拒绝;single_shot 未声明序列拒绝;声明覆盖放行;
* series 覆盖检查(multi_period):无序列 / 锚定发布不一致 / 覆盖不足 /
  全覆盖放行,反查 params={}(与用户因子 #361 通道同构差异点);
* 编译期:未注册 p_ 引用 fail-visible;已注册放行;
* series 覆盖集合 kind-aware:``series_covered_factor_names`` 对
  predefined 记录产出 ``p_`` 名、用户记录产出 ``u_`` 名。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast

import pytest

from finboard_backtest.research_code.factor_series import (
    series_covered_factor_names,
)
from finboard_backtest.research_code.predefined_factors import (
    PREDEFINED_ANCHOR_MISMATCH_CODE,
    PREDEFINED_COVERAGE_MISSING_CODE,
    PREDEFINED_SERIES_UNDECLARED_CODE,
    PREDEFINED_UNREGISTERED_CODE,
    predefined_factor_reference_gate_error,
    predefined_factor_series_coverage_gate_error,
)
from finboard_backtest.strategy_spec import StrategySpecError
from finboard_backtest.strategy_spec.contracts import (
    FeatureKind,
    FeatureNode,
    FeatureOperator,
)
from finboard_backtest.strategy_spec.registry import (
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_persistence import FactorSeriesRecord

DECISION_DATES = tuple(date(2024, 1, 2) + timedelta(days=k) for k in range(5))
WINDOW_START = date(2024, 1, 1)
WINDOW_END = date(2024, 1, 31)
BARS_RELEASE = "DR-bars"


class _LookupStub:
    """SeriesLookup stub:按 (code_artifact, release_id, params) 命中。"""

    def __init__(self, series: FactorSeriesRecord | None) -> None:
        self._series = series
        self.queries: list[dict[str, Any]] = []

    async def find_matching(
        self,
        *,
        code_artifact: str,
        release_id: str,
        dataset_release_ids: Any,
        params: Any,
        window_start: date,
        window_end: date,
    ) -> FactorSeriesRecord | None:
        self.queries.append(
            {
                "code_artifact": code_artifact,
                "release_id": release_id,
                "params": dict(params),
            }
        )
        return self._series


def _series(
    *,
    release_id: str = BARS_RELEASE,
    dates: tuple[date, ...] = DECISION_DATES,
) -> FactorSeriesRecord:
    values = {
        day.isoformat(): {"600000.SH": 0.01} for day in dates
    }
    return FactorSeriesRecord.build(
        code_artifact="return_21d",
        code_commit="predefined-aaaaaaaaaaaa",
        kind="predefined_factor",
        release_id=release_id,
        dataset_release_ids=(),
        params={},
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        dates=list(dates),
        values=values,
    )


class TestReferenceGate:
    def test_unregistered_name_rejected(self) -> None:
        error = predefined_factor_reference_gate_error(
            required_factor_sources={"p_no_such"},
            multi_period=True,
        )
        assert error is not None
        assert PREDEFINED_UNREGISTERED_CODE in error
        assert "p_no_such" in error
        assert "return_21d" in error  # 可用清单

    def test_single_shot_undeclared_rejected(self) -> None:
        error = predefined_factor_reference_gate_error(
            required_factor_sources={"p_return_21d"},
            series_covered_factors=frozenset(),
            multi_period=False,
        )
        assert error is not None
        assert PREDEFINED_SERIES_UNDECLARED_CODE in error
        assert "finboard_factor_series_build" in error

    def test_single_shot_declared_passes(self) -> None:
        assert (
            predefined_factor_reference_gate_error(
                required_factor_sources={"p_return_21d"},
                series_covered_factors={"p_return_21d"},
                multi_period=False,
            )
            is None
        )

    def test_multi_period_uncovered_defers_to_coverage_gate(self) -> None:
        assert (
            predefined_factor_reference_gate_error(
                required_factor_sources={"p_return_21d"},
                series_covered_factors=frozenset(),
                multi_period=True,
            )
            is None
        )

    def test_u_and_catalog_sources_ignored(self) -> None:
        assert (
            predefined_factor_reference_gate_error(
                required_factor_sources={"u_mine", "momentum", "close"},
                multi_period=False,
            )
            is None
        )


class TestCoverageGate:
    async def test_missing_series_rejected_with_rebuild_hint(self) -> None:
        error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(Any, _LookupStub(None)),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=(),
            decision_dates=DECISION_DATES,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert error is not None
        assert PREDEFINED_COVERAGE_MISSING_CODE in error
        assert "kind=predefined_factor" in error

    async def test_lookup_uses_bare_artifact_and_empty_params(self) -> None:
        """反查以目录裸名 + params={}(与用户因子 #361 的差异点)。"""
        lookup = _LookupStub(None)
        await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(Any, lookup),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=("DR-x",),
            decision_dates=DECISION_DATES,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert lookup.queries[0]["code_artifact"] == "return_21d"
        assert lookup.queries[0]["params"] == {}
        assert lookup.queries[0]["release_id"] == BARS_RELEASE

    async def test_anchor_mismatch_rejected(self) -> None:
        error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(
                Any, _LookupStub(_series(release_id="DR-other"))
            ),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=(),
            decision_dates=DECISION_DATES,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert error is not None
        assert PREDEFINED_ANCHOR_MISMATCH_CODE in error
        assert "DR-other" in error

    async def test_insufficient_coverage_lists_missing_dates(self) -> None:
        short = _series(dates=DECISION_DATES[:2])
        error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(Any, _LookupStub(short)),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=(),
            decision_dates=DECISION_DATES,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert error is not None
        assert PREDEFINED_COVERAGE_MISSING_CODE in error
        assert "缺失 3 个决策日" in error
        assert DECISION_DATES[2].isoformat() in error

    async def test_full_coverage_passes(self) -> None:
        error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(Any, _LookupStub(_series())),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=(),
            decision_dates=DECISION_DATES,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert error is None

    async def test_empty_decision_dates_passes(self) -> None:
        error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined={"p_return_21d"},
            series_lookup=cast(Any, _LookupStub(None)),
            bars_release_id=BARS_RELEASE,
            dataset_release_ids=(),
            decision_dates=(),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert error is None


# --------------------------------------------------------------------- #
# 编译期 + 覆盖集合 kind-aware
# --------------------------------------------------------------------- #


def _spec_with_predefined_source(source: str) -> Any:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="test_p_factor",
        dataset_release_ids=("frozen-release-v1",),
    )
    node = FeatureNode(
        node_id="p_alpha",
        label="预置因子",
        kind=FeatureKind.FACTOR,
        operator=FeatureOperator.IDENTITY,
        source=source,
    )
    nodes = (*spec.feature_graph.nodes, node)
    return spec.model_copy(
        update={"feature_graph": spec.feature_graph.model_copy(update={"nodes": nodes})}
    )


class TestCompileTimeGate:
    def test_unregistered_predefined_source_rejected(self) -> None:
        spec = _spec_with_predefined_source("p_no_such_factor")
        with pytest.raises(StrategySpecError, match="未注册的平台预置因子"):
            compile_registered_strategy_spec(spec)

    def test_registered_predefined_source_passes(self) -> None:
        spec = _spec_with_predefined_source("p_return_21d")
        plan = compile_registered_strategy_spec(spec)
        assert "p_return_21d" in plan.required_factor_sources


class TestSeriesCoveredNames:
    def test_kind_aware_prefixes(self) -> None:
        predefined = _series()
        user = FactorSeriesRecord.build(
            code_artifact="mom20",
            code_commit="c" * 40,
            kind="factor",
            release_id=BARS_RELEASE,
            dataset_release_ids=(),
            params={},
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            dates=list(DECISION_DATES),
            values={d.isoformat(): {"600000.SH": 1.0} for d in DECISION_DATES},
        )
        covered = series_covered_factor_names([predefined, user])
        assert covered == frozenset({"p_return_21d", "u_mom20"})
