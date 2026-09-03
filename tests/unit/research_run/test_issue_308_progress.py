"""issue #308:research_run 实时进度透出 —— 加载期 k/N + 决策级 phase 编码。

锁定四组不变量:

* 决策执行期 phase 编码 ``research_run:<stage>#<序号>@<YYYY-MM-DD>``
  (序号 1-based,日期 = decision business_date),REPORT 段保持
  ``research_run:report`` 原样;done/total 数值与 #188 完全一致
  (每 stage 一帧、total 随已发现决策递增);
* 加载期**首帧**:决策日推导完成后、close 矩阵预建**之前**上报 ``(0, N)``
  —— 用「首帧时 provider 尚无一次 PIT bar 读取」证明帧先于预建,覆盖
  预建这段此前零进度的空白窗(RR-7a74 的「数小时 0/0」);
* 分块边界探针序列保持 #306 形态 ``[(0, N), (4, N)]``(首帧顶替原首块
  边界的 k=0 帧,总调用次数与取值序列不变,#306 既有断言零回归);
* single_shot 与 multi_period 同机制:single_shot 加载段同样透出
  ``decision_load`` 帧池(k/N 按快照决策日数)。

真实发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建(复用 #306
的固定样本),不依赖 PostgreSQL(probe 的 DB 写入路径由集成测试覆盖)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

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
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    build_decision_load_contexts,
)
from finboard_data.releases import FrozenReleaseProvider, PointInTimeBar

from .conftest import fixed_report
from .test_issue_306_load_probe import (
    _RELEASE_ID,
    _build_release,
    _month_end_decisions,
    _noop_snapshot_provider,
    _price_only_spec,
)

# ---- 决策执行期 phase 编码 ---------------------------------------------------

#: 固定样本 decision_factory 的 business_date(index=0 → 2024-01-02)。
_DECISION_DATE = "2024-01-02"


def _expected_stage_phases(index: int, date_text: str) -> list[str]:
    """决策执行期 phase 期望序列(issue #308 编码,序号 1-based)。"""

    return [
        f"research_run:{stage}#{index}@{date_text}"
        for stage in (
            "universe",
            "features",
            "signals",
            "targets_before_constraints",
            "constraints",
            "targets_after_constraints",
            "risk_exits",
            "targets_after_risk",
            "capital_feasibility",
            "rebalance_plan",
            "orders",
            "fills",
            "ledger",
        )
    ]


class _ProgressRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, str | None]] = []

    async def __call__(
        self, done: int, total: int | None, phase: str | None
    ) -> None:
        self.calls.append((done, total, phase))


@pytest.mark.asyncio
async def test_decision_phase_encodes_index_and_date(
    manifest_factory, decision_factory
) -> None:
    """单决策:13 个 stage 帧全部携带 #1@<日期>,REPORT 段无后缀。"""

    manifest = manifest_factory()
    decision = decision_factory()
    reporter = _ProgressRecorder()
    record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
        manifest,
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
        progress=reporter,
    )
    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

    assert [phase for _, _, phase in reporter.calls] == [
        *_expected_stage_phases(1, _DECISION_DATE),
        "research_run:report",
    ]
    # done/total 数值口径与 #188 完全一致(编码只动 phase,不动计数)。
    for offset, (done, total, _) in enumerate(reporter.calls[:-1]):
        assert done == offset + 1
        assert total == DECISION_STAGE_COUNT
    assert reporter.calls[-1] == (
        DECISION_STAGE_COUNT + 1,
        DECISION_STAGE_COUNT + 1,
        "research_run:report",
    )


@pytest.mark.asyncio
async def test_second_decision_phase_index_advances(
    manifest_factory, decision_factory
) -> None:
    """双决策:第二段 phase 序号推进为 #2,日期随 business_date 变化。"""

    first = decision_factory()
    second = cast(
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
    from dataclasses import replace as _replace

    report = _replace(
        fixed_report("ma_cross", second),
        decision_count=2,
        order_count=2,
        fill_count=2,
    )
    reporter = _ProgressRecorder()
    record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
        manifest_factory(),
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(first, second),
            report=report,
        ),
        progress=reporter,
    )
    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

    first_segment = reporter.calls[:DECISION_STAGE_COUNT]
    second_segment = reporter.calls[DECISION_STAGE_COUNT : DECISION_STAGE_COUNT * 2]
    assert [phase for _, _, phase in first_segment] == _expected_stage_phases(
        1, "2024-01-02"
    )
    assert [phase for _, _, phase in second_segment] == _expected_stage_phases(
        2, "2024-01-03"
    )
    # REPORT 段 phase 保持 #188 原样(终态兼容不受编码影响)。
    assert reporter.calls[-1][2] == "research_run:report"
    # total 自修正口径不变:第 2 段 total=26,REPORT 段 done=total=27。
    assert second_segment[-1][1] == DECISION_STAGE_COUNT * 2
    assert reporter.calls[-1][:2] == (
        DECISION_STAGE_COUNT * 2 + 1,
        DECISION_STAGE_COUNT * 2 + 1,
    )


# ---- 加载期首帧与 k/N 序列 ---------------------------------------------------


class _RecordingProvider(FrozenReleaseProvider):
    """记录 PIT bar 读取次数的 provider(观测 close 矩阵预建时点)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pit_read_calls = 0

    async def fetch_point_in_time_bars(
        self,
        symbol: Any,
        period: Any,
        start: Any,
        end: Any,
        *,
        decision_at: Any,
        adjust: str = "qfq",
    ) -> list[PointInTimeBar]:
        self.pit_read_calls += 1
        return await super().fetch_point_in_time_bars(
            symbol,
            period,
            start,
            end,
            decision_at=decision_at,
            adjust=adjust,
        )


class _OrderingProbe:
    """记录 (done, total, 当时 provider PIT 读取次数) 的探针。"""

    def __init__(self, provider: _RecordingProvider) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self._provider = provider

    async def __call__(self, done: int, total: int) -> None:
        self.calls.append((done, total, self._provider.pit_read_calls))


def _recording_provider(tmp_path: Path) -> _RecordingProvider:
    return _RecordingProvider(
        release_root=tmp_path / "releases",
        release_id=_RELEASE_ID,
    )


@pytest.mark.asyncio
async def test_load_first_frame_precedes_close_matrix_prebuild(
    tmp_path: Path,
) -> None:
    """首帧 (0, N) 在 close 矩阵预建之前:PIT 读取次数尚为 0。"""

    await _build_release(tmp_path)
    provider = _recording_provider(tmp_path)
    probe = _OrderingProbe(provider)
    contexts = await build_decision_load_contexts(
        _multi_period_manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        chunk_probe=probe,
    )
    assert len(_month_end_decisions()) == 6
    # 序列形态与 #306 完全一致(首帧顶替原首块边界 k=0 帧,不增调用次数)。
    assert [(done, total) for done, total, _ in probe.calls] == [(0, 6), (4, 6)]
    # 首帧时 provider 尚无一次 PIT 读取 → 帧先于 close 矩阵预建。
    assert probe.calls[0] == (0, 6, 0)
    assert len(contexts) == 6


@pytest.mark.asyncio
async def test_load_probe_sequence_matches_306_shape(tmp_path: Path) -> None:
    """分块边界序列保持 #306 断言形态(既有测试零回归)。"""

    provider = await _build_release(tmp_path)
    probe_calls: list[tuple[int, int]] = []

    async def probe(done: int, total: int) -> None:
        probe_calls.append((done, total))

    contexts = await build_decision_load_contexts(
        _multi_period_manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        chunk_probe=probe,
    )
    assert probe_calls == [(0, 6), (4, 6)]
    assert len(contexts) == 6


@pytest.mark.asyncio
async def test_single_shot_load_reports_frame(tmp_path: Path) -> None:
    """single_shot:加载段同样透出 decision_load 帧(k/N=快照决策日数)。"""

    await _build_release(tmp_path)
    provider = _recording_provider(tmp_path)
    probe_calls: list[tuple[int, int]] = []

    async def probe(done: int, total: int) -> None:
        probe_calls.append((done, total))

    contexts = await build_decision_load_contexts(
        _single_shot_manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_fake_snapshot_provider,
        chunk_probe=probe,
    )
    # 1 个快照决策日 → 单分块:仅首帧 (0, 1)。
    assert probe_calls == [(0, 1)]
    assert len(contexts) == 1


# ---- single_shot 样本 --------------------------------------------------------

#: single_shot 快照决策日(发布区间内、其后仍有成交日)。
_SINGLE_SHOT_DECISION_DAY = date(2024, 2, 15)


def _multi_period_manifest() -> ResearchRunManifest:
    spec = _price_only_spec()
    return ResearchRunManifest(
        run_id="RR-issue308multi0001",
        idempotency_key="issue-308-multi-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _single_shot_manifest() -> ResearchRunManifest:
    spec = _price_only_spec()
    return ResearchRunManifest(
        run_id="RR-issue308single001",
        idempotency_key="issue-308-single-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(
            FrozenArtifactRef(
                artifact_id="snap-issue308",
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:momentum",),
            ),
        ),
        parameters={},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


async def _fake_snapshot_provider(snapshot_id: str) -> Any:
    """单决策时点的最小快照 stub(无观测;load_context 只读 decision_at)。

    返回类型标注 ``Any``:stub 与 :class:`FeatureSnapshotProvider` 协议的
    ``FeatureSnapshot | None`` 兼容靠运行时鸭子类型(load_context 只读
    ``decision_at`` / ``snapshot_id`` / ``observations``)。
    """

    del snapshot_id

    class _Snapshot:
        snapshot_id = "snap-issue308"
        decision_at = datetime.combine(
            _SINGLE_SHOT_DECISION_DAY, time(15, 0), tzinfo=UTC
        )
        observations: tuple[Any, ...] = ()

    return _Snapshot()
