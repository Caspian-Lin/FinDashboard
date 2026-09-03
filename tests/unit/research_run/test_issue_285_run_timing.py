"""ResearchRun 分段耗时打点单测(issue #285)。

断言:
* 完成的 run 的 record 携带 ``timing``(decision_load 总耗时 / 逐决策
  execute 聚合 min-avg-max + 最慢决策日 / report 段耗时 / parquet 聚合);
* timing 只挂 record,不进 report、不参与 result_checksum(同 checksum
  重复 save_result 携带不同 timing 不判冲突)。

纯可观测性:不改变 #188 进度上报与 fail-closed 状态机语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import cast

import pytest

from finboard_backtest.research_run import (
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.runner import _DecisionTiming

from .conftest import fixed_report


def _assert_timing_shape(timing: Mapping[str, object], decision_count: int) -> None:
    assert set(timing) == {
        "total_elapsed_seconds",
        "decision_load_elapsed_seconds",
        "decision_execute",
        "report_elapsed_seconds",
        "parquet_reads",
    }
    assert cast(float, timing["total_elapsed_seconds"]) >= 0
    assert cast(float, timing["decision_load_elapsed_seconds"]) >= 0
    assert cast(float, timing["report_elapsed_seconds"]) >= 0
    execute = cast(dict[str, object], timing["decision_execute"])
    assert set(execute) == {
        "count",
        "min_seconds",
        "avg_seconds",
        "max_seconds",
        "slowest_decision_date",
    }
    assert cast(int, execute["count"]) == decision_count
    if decision_count:
        assert cast(float, execute["max_seconds"]) >= cast(
            float, execute["min_seconds"]
        ) >= 0
        assert cast(float, execute["avg_seconds"]) >= 0
        assert isinstance(execute["slowest_decision_date"], str)


class TestDecisionTimingAggregation:
    @pytest.mark.unit
    def test_aggregates_min_avg_max_and_slowest_date(self) -> None:
        timing = _DecisionTiming()
        timing.record("2024-01-02", 0.5)
        timing.record("2024-01-03", 0.1)
        timing.record("2024-01-06", 0.9)

        payload = timing.as_dict()
        assert payload["count"] == 3
        assert payload["min_seconds"] == pytest.approx(0.1)
        assert payload["avg_seconds"] == pytest.approx(0.5)
        assert payload["max_seconds"] == pytest.approx(0.9)
        assert payload["slowest_decision_date"] == "2024-01-06"

    @pytest.mark.unit
    def test_empty_aggregation_shape(self) -> None:
        payload = _DecisionTiming().as_dict()
        assert payload == {
            "count": 0,
            "min_seconds": 0.0,
            "avg_seconds": 0.0,
            "max_seconds": 0.0,
            "slowest_decision_date": None,
        }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_completed_run_records_segment_timing(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory(manifest=manifest)
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)

    record = await coordinator.execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
    assert record.timing is not None
    _assert_timing_shape(record.timing, decision_count=1)
    execute = cast(dict[str, object], record.timing["decision_execute"])
    assert execute["slowest_decision_date"] == str(decision.business_date)
    # 内存 store 无 parquet 读取,聚合为全零形状。
    reads = cast(dict[str, object], record.timing["parquet_reads"])
    assert reads["read_ops"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_timing_not_in_report_and_not_checksummed(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory(manifest=manifest)
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)

    record = await coordinator.execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED
    # timing 是 record 级字段,report 数据类不携带任何计时字段。
    assert record.result is not None
    assert "timing" not in {item.name for item in fields(record.result)}

    # 同 result_checksum 重复 save_result、携带不同 timing:不判漂移冲突,
    # 新 timing 覆盖旧值 —— 证明耗时观测不参与 checksum 语义。
    assert record.result_checksum is not None
    original_timing = record.timing
    assert original_timing is not None
    bumped_total = float(original_timing["total_elapsed_seconds"]) + 1.0  # type: ignore[arg-type]
    resaved = await store.save_result(
        manifest.run_id,
        report=record.result,
        result_checksum=record.result_checksum,
        timing={
            **original_timing,
            "total_elapsed_seconds": bumped_total,
        },
    )
    assert resaved.result_checksum == record.result_checksum
    assert resaved.timing is not None
    assert resaved.timing["total_elapsed_seconds"] == bumped_total
