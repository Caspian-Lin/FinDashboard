"""research_run 分阶段进度上报单元测试(issue #188)。

不依赖 PostgreSQL:用 InMemoryResearchRunStore + 固定样本 DecisionSequenceAdapter
验证 Coordinator 在 ``stage x decision`` 粒度回调 ``progress(done, total, phase)``:

* 每次 decision 回调 13 次(每个 stage 一次),再加 REPORT 1 次;
* phase 命名 ``research_run:<stage>``,终态 phase 仍由执行器/协调器按 status 收口;
* done/total 按「stage x decision」计数:单决策 total=13;双决策第 1 段 total=13、
  第 2 段 total=26,REPORT 段 total=27;
* 进度回调抛错不影响运行状态机(尽力而为可观测性)。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

import pytest

from finboard_backtest.research_run import (
    DECISION_STAGE_COUNT,
    DecisionBundle,
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRunCoordinator,
    ResearchRunStatus,
)

from .conftest import fixed_report

_SINGLE_TOTAL = DECISION_STAGE_COUNT
_TWO_TOTAL = DECISION_STAGE_COUNT * 2 + 1  # 2 decisions + report


class _ProgressRecorder:
    """记录 (done, total, phase) 回调序列。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, str | None]] = []

    async def __call__(
        self, done: int, total: int | None, phase: str | None
    ) -> None:
        self.calls.append((done, total, phase))


async def _run_with_progress(manifest, adapter, reporter) -> None:
    record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
        manifest,
        adapter,
        progress=reporter,
    )
    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary


def _single_decision_adapter(manifest, decision) -> DecisionSequenceAdapter:
    return DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )


def _two_decision_adapter(manifest, first, second) -> DecisionSequenceAdapter:
    # 第二段决策必须反映前一决策累积持仓(200 股/80k 现金),否则
    # Coordinator 的「持仓必须由成交驱动」校验会失败。
    report = replace(
        fixed_report("ma_cross", second),
        decision_count=2,
        order_count=2,
        fill_count=2,
    )
    return DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(first, second),
        report=report,
    )


def _second_decision(decision_factory) -> DecisionBundle:
    return cast(
        DecisionBundle,
        decision_factory(
            index=1,
            positions=(
                ResearchPosition(
                    symbol="510300.SH",
                    position_side=ResearchPositionSide.LONG,
                    quantity=Decimal("200"),
                    average_price=Decimal("100"),
                    market_price=Decimal("100"),
                    market_value=Decimal("20000"),
                    realized_pnl=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                ),
            ),
            cash=Decimal("80000"),
            market_value=Decimal("20000"),
        ),
    )


@pytest.mark.asyncio
async def test_single_decision_reports_each_stage_once(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = _single_decision_adapter(manifest, decision)
    reporter = _ProgressRecorder()

    await _run_with_progress(manifest, adapter, reporter)

    # 13 stages + 1 report。
    assert len(reporter.calls) == DECISION_STAGE_COUNT + 1
    phases = [phase for _, _, phase in reporter.calls]
    assert phases == [
        "research_run:universe",
        "research_run:features",
        "research_run:signals",
        "research_run:targets_before_constraints",
        "research_run:constraints",
        "research_run:targets_after_constraints",
        "research_run:risk_exits",
        "research_run:targets_after_risk",
        "research_run:capital_feasibility",
        "research_run:rebalance_plan",
        "research_run:orders",
        "research_run:fills",
        "research_run:ledger",
        "research_run:report",
    ]


@pytest.mark.asyncio
async def test_single_decision_done_total_counts(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = _single_decision_adapter(manifest, decision)
    reporter = _ProgressRecorder()

    await _run_with_progress(manifest, adapter, reporter)

    for index, (done, total, phase) in enumerate(reporter.calls[:-1]):
        assert done == index + 1
        assert total == _SINGLE_TOTAL
        assert phase is not None
        assert phase.startswith("research_run:")
    done, total, phase = reporter.calls[-1]
    assert done == _SINGLE_TOTAL + 1
    assert total == _SINGLE_TOTAL + 1
    assert phase == "research_run:report"


@pytest.mark.asyncio
async def test_two_decisions_grow_total_and_done(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    first = decision_factory()
    second = _second_decision(decision_factory)
    adapter = _two_decision_adapter(manifest, first, second)
    reporter = _ProgressRecorder()

    await _run_with_progress(manifest, adapter, reporter)

    assert len(reporter.calls) == _TWO_TOTAL
    first_segment = reporter.calls[: DECISION_STAGE_COUNT]
    second_segment = reporter.calls[
        DECISION_STAGE_COUNT : DECISION_STAGE_COUNT * 2
    ]
    for index, (done, total, _) in enumerate(first_segment):
        assert done == index + 1
        assert total == DECISION_STAGE_COUNT
    for index, (done, total, _) in enumerate(second_segment):
        assert done == DECISION_STAGE_COUNT + index + 1
        assert total == DECISION_STAGE_COUNT * 2
    done, total, _ = reporter.calls[-1]
    assert (done, total) == (_TWO_TOTAL, _TWO_TOTAL)


@pytest.mark.asyncio
async def test_progress_failure_does_not_break_run(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = _single_decision_adapter(manifest, decision)

    async def boomy_progress(done, total, phase) -> None:
        del done, total, phase
        raise RuntimeError("observability channel down")

    record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
        manifest,
        adapter,
        progress=boomy_progress,
    )

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
