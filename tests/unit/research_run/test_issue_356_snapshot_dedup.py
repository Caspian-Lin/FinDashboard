"""快照决策点按业务日期去重 —— 同日期同决策只执行一次(issue #356)。

现象:多快照 run 的决策日推导此前按 ``(decision_at, snapshot_id)`` 元组
去重,同一业务日期冻结多个快照(不同 snapshot_id,agent 实测 x4)时逐个
展开成重复决策 —— 同一日期的同一决策被重复加载 / 执行 / 落库。不影响
正确性(重复决策输入完全一致,组合管线对同一目标的再次再平衡是零订单
no-op),纯浪费算力。

本文件锁定:

* 推导层去重(``_snapshot_decision_days``):按 ``decision_at.date()``(与
  ``LoadedDecisionContext.business_date`` 同口径)收敛,每日期保留最晚
  ``decision_at`` 的代表项(并列取 snapshot_id 最大,顺序确定);
* 决策执行次数:多快照同日期 run 的 ``_build_decision`` 调用数 == 业务
  日期数(仿 #314 零重算的调用计数断言风格);
* 等值:重复决策对组合状态是纯 no-op(零订单零成交、账本不变),去重
  前后组合承载的报告字段逐值一致(仅 decision_count 按设计缩小);
* #314 口径一致:``_resume_schedule_total`` 与执行期走同一去重函数,
  快速路径「已落库数 == 推导总数」判定不受影响;
* 单快照与 multi_period 路径行为不变(multi_period 决策日来自发布日历,
  冻结多少快照都不影响期数)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import finboard_backtest.research_run.signal_engine as signal_engine_module
from finboard_backtest.research_run import (
    DECISION_STAGE_COUNT,
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    _derive_rebalance_decision_days,
    _snapshot_decision_days,
    build_decision_load_contexts,
)

from .test_issue_304_partial_evidence import (
    DECISION_AT,
    DECISION_AT_2,
    _closes_provider,
)
from .test_issue_304_partial_evidence import (
    _manifest as _signal_manifest,
)
from .test_issue_304_partial_evidence import (
    _spec as _signal_spec,
)
from .test_issue_306_load_probe import (
    _month_end_decisions,
    _noop_snapshot_provider,
    _price_only_spec,
)
from .test_issue_314_checkpoint_resume import _CountingSignalAdapter
from .test_signal_engine import _obs, _StubSnapshot

pytestmark = pytest.mark.asyncio

#: 与测试观测的 available_at 同一冻结时点(全部同日决策时点可见同一批观测)。
SNAPSHOT_AT = datetime(2023, 12, 29, tzinfo=UTC)

_DUP_IDS = ("factor-a", "factor-b", "factor-c", "factor-d")


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _snapshot(
    snapshot_id: str,
    decision_at: datetime,
    *,
    momentum_shift: float = 0.0,
) -> _StubSnapshot:
    """给定 id / 决策时点的多因子快照(观测 available_at 恒为 SNAPSHOT_AT)。"""

    symbols = ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")
    observations = []
    for index, symbol in enumerate(symbols):
        observations.append(_obs(symbol, "pb", 1.0 + index * 0.1, available_at=SNAPSHOT_AT))
        observations.append(
            _obs(symbol, "momentum", 0.05 * index + momentum_shift, available_at=SNAPSHOT_AT)
        )
        observations.append(
            _obs(symbol, "volatility_20d", 0.3 - 0.03 * index, available_at=SNAPSHOT_AT)
        )
    return _StubSnapshot(
        snapshot_id=snapshot_id, decision_at=decision_at, observations=tuple(observations)
    )


def _dup_snapshots(decision_at: datetime) -> dict[str, _StubSnapshot]:
    """同一决策日的 4 个不同快照(issue #356 实测 x4 的输入形态)。"""

    return {
        snapshot_id: _snapshot(snapshot_id, decision_at, momentum_shift=float(index))
        for index, snapshot_id in enumerate(_DUP_IDS)
    }


def _snapshot_provider(snapshots: dict[str, _StubSnapshot]) -> Any:
    async def provider(snapshot_id: str) -> _StubSnapshot | None:
        return snapshots.get(snapshot_id)

    return provider


def _dup_manifest(
    snapshot_ids: tuple[str, ...],
    *,
    run_id: str,
    idempotency_key: str,
    zero_fee: bool = False,
) -> ResearchRunManifest:
    """multi_factor 规格(#304 rank 规则)+ 指定快照清单的 manifest。

    ``zero_fee=True`` 时成交假设全部费用 / 滑点归零 —— 等值对照测试用:
    零费用下首决策成交后权益不变,重复决策对同一目标的再定容恰好为零
    (构造「重复决策本就幂等」的精确前提)。
    """

    spec = _signal_spec(rank_threshold=0.5)
    if zero_fee:
        from finboard_backtest.strategy_spec.contracts import (
            ExecutionModel,
            ExecutionTiming,
        )

        spec = spec.model_copy(
            update={
                "execution_model": ExecutionModel(
                    timing=ExecutionTiming.NEXT_OPEN,
                    commission_rate=0.0,
                    minimum_commission=0.0,
                    sell_tax_rate=0.0,
                    slippage_bps=0.0,
                )
            }
        )
    return _signal_manifest(
        spec,
        run_id=run_id,
        idempotency_key=idempotency_key,
        snapshot_ids=snapshot_ids,
    )


async def _legacy_snapshot_decision_days(
    manifest: ResearchRunManifest,
    snapshot_provider: Any,
) -> list[tuple[datetime, str | None]]:
    """issue #356 修复前的推导实现(``sorted(set(...))`` 按元组去重)。

    仅在等值对照测试中经 monkeypatch 注入,用于构造「去重前」的真实 run。
    """

    decision_days: list[tuple[datetime, str | None]] = []
    for ref in manifest.factor_snapshots:
        snapshot = await snapshot_provider(ref.artifact_id)
        if snapshot is None:
            raise ValueError(f"因子快照缺失: {ref.artifact_id}")
        decision_days.append((snapshot.decision_at, snapshot.snapshot_id))
    return sorted(set(decision_days))


def _flat_tail_provider() -> Any:
    """构造「重复决策精确幂等」的行情形态:决策日之前随机游走(协方差 /
    动量可估),决策日及其后各标的按末日价持平 —— 决策日收盘 == 执行日
    (次日)开盘,零费用下首次成交后权益不变,重复决策对同一目标的再定容
    恰好为零。"""

    import numpy as np

    from tests.unit.research_run.test_issue_304_partial_evidence import (
        SYMBOLS as _SIGNAL_SYMBOLS,
    )
    from tests.unit.research_run.test_signal_engine import (
        _StubExecution,
        _StubInstrument,
        _StubProvider,
        _StubRelease,
    )

    walk_days = [date(2023, 12, 15) + timedelta(days=i) for i in range(14)]
    flat_start = date(2024, 1, 2)
    flat_days = [flat_start + timedelta(days=i) for i in range(60)]
    rng = np.random.default_rng(20240102)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in _SIGNAL_SYMBOLS:
        price = 10.0
        series: dict[date, Decimal] = {}
        for day in walk_days:
            price *= 1.0 + float(rng.normal(0.001, 0.01))
            series[day] = Decimal(str(round(price, 4)))
        last = series[walk_days[-1]]
        for day in flat_days:
            series[day] = last
        closes[symbol] = series
    # 零费用执行元数据:lot_info 的费率来自发布 instruments(优先于规格的
    # execution_model),必须一并归零才能构造「重复决策精确幂等」。
    zero_fee_instruments = tuple(
        _StubInstrument(
            code=symbol,
            execution=_StubExecution(
                commission_rate=Decimal("0"),
                commission_min=Decimal("0"),
                stamp_tax_rate=Decimal("0"),
            ),
        )
        for symbol in _SIGNAL_SYMBOLS
    )
    return _StubProvider(
        release=_StubRelease("release-v1", zero_fee_instruments),
        closes_by_symbol=closes,
    )


async def _load_dup_contexts(
    manifest: ResearchRunManifest,
    provider: Any,
    snapshots: dict[str, _StubSnapshot],
) -> Any:
    def release_factory(release_id: str) -> Any:
        assert release_id == "release-v1"
        return provider

    return await build_decision_load_contexts(
        manifest,
        release_provider_factory=release_factory,
        snapshot_provider=_snapshot_provider(snapshots),
    )


# ---------------------------------------------------------------------------
# Part A:推导层去重(_snapshot_decision_days)
# ---------------------------------------------------------------------------


class TestSnapshotDecisionDayDedup:
    async def test_same_date_snapshots_dedup_to_one_entry(self) -> None:
        """4 个快照同一 decision_at(不同 snapshot_id)→ 收敛为 1 条,
        代表项取 snapshot_id 最大者(与排序末位一致,确定)。"""
        manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-dedup-0001",
            idempotency_key="issue356-dedup-1",
        )

        days = await _snapshot_decision_days(manifest, _snapshot_provider(_dup_snapshots(DECISION_AT)))

        assert days == [(DECISION_AT, "factor-d")]

    async def test_same_date_different_times_keep_latest(self) -> None:
        """同日不同时点的快照同样收敛为 1 条,保留最晚 decision_at ——
        晚时点 PIT 可见性是同日早时点的超集,与去重前「当日最后一个决策
        收敛出最终组合状态」语义一致。"""
        early = datetime(2024, 1, 2, 7, 0, tzinfo=UTC)
        mid = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)
        manifest = _dup_manifest(
            ("snap-early", "snap-mid", "snap-late"),
            run_id="RR-issue356-dedup-0002",
            idempotency_key="issue356-dedup-2",
        )
        snapshots = {
            "snap-early": _snapshot("snap-early", early),
            "snap-mid": _snapshot("snap-mid", mid),
            "snap-late": _snapshot("snap-late", DECISION_AT),  # 2024-01-02 15:00
        }

        days = await _snapshot_decision_days(manifest, _snapshot_provider(snapshots))

        assert days == [(DECISION_AT, "snap-late")]

    async def test_distinct_dates_all_preserved_sorted(self) -> None:
        """不同业务日期的决策时点全部保留、升序;同日期的重复只在日期内
        收敛(D1 两个快照 → 1 条,D0 / D2 各 1 条)。"""
        day0 = datetime(2023, 12, 29, 15, 0, tzinfo=UTC)
        manifest = _dup_manifest(
            ("snap-d1a", "snap-d1b", "snap-d0", "snap-d2"),
            run_id="RR-issue356-dedup-0003",
            idempotency_key="issue356-dedup-3",
        )
        snapshots = {
            "snap-d1a": _snapshot("snap-d1a", DECISION_AT),
            "snap-d1b": _snapshot("snap-d1b", DECISION_AT, momentum_shift=0.2),
            "snap-d0": _snapshot("snap-d0", day0),
            "snap-d2": _snapshot("snap-d2", DECISION_AT_2),
        }

        days = await _snapshot_decision_days(manifest, _snapshot_provider(snapshots))

        assert days == [
            (day0, "snap-d0"),
            (DECISION_AT, "snap-d1b"),
            (DECISION_AT_2, "snap-d2"),
        ]

    async def test_representative_independent_of_manifest_order(self) -> None:
        """代表项选择只取决于 (decision_at, snapshot_id) 排序,与 manifest
        中快照的冻结顺序无关(确定性口径)。"""
        snapshots = {
            "factor-b": _snapshot("factor-b", DECISION_AT),
            "factor-a": _snapshot("factor-a", DECISION_AT, momentum_shift=0.3),
        }
        order_ab = _dup_manifest(
            ("factor-a", "factor-b"),
            run_id="RR-issue356-dedup-0004",
            idempotency_key="issue356-dedup-4",
        )
        order_ba = _dup_manifest(
            ("factor-b", "factor-a"),
            run_id="RR-issue356-dedup-0005",
            idempotency_key="issue356-dedup-5",
        )

        days_ab = await _snapshot_decision_days(order_ab, _snapshot_provider(snapshots))
        days_ba = await _snapshot_decision_days(order_ba, _snapshot_provider(snapshots))

        assert days_ab == days_ba == [(DECISION_AT, "factor-b")]


# ---------------------------------------------------------------------------
# Part B:决策执行次数(coordinator 全链路,调用计数仿 #314)
# ---------------------------------------------------------------------------


class TestDecisionExecutionDedup:
    async def test_duplicate_same_date_snapshots_execute_once(self) -> None:
        """验收主路径:4 个同日快照的 run 只产出 1 条决策(修复前 x4)——
        artifact = 13 决策段 + 1 report,``_build_decision`` 恰好 1 次。"""
        manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-once-0001",
            idempotency_key="issue356-once-1",
        )
        snapshots = _dup_snapshots(DECISION_AT)
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        record = await coordinator.execute(
            manifest,
            _CountingSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )

        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == DECISION_STAGE_COUNT + 1  # 1 决策 + report
        assert artifacts[0].payload["business_date"] == date(2024, 1, 2).isoformat()

        # _build_decision 调用计数(仿 #314 零重算断言):决策输入只有 1 条。
        counting = _CountingSignalAdapter(
            manifest=manifest, provider=provider, snapshots=snapshots
        )
        decisions = [item async for item in counting.decisions(manifest)]
        assert len(decisions) == 1
        assert counting.last_pipeline is not None
        assert counting.last_pipeline.build_calls == 1

    async def test_mixed_dates_dedup_only_within_date(self) -> None:
        """D1 冻结 2 个快照 + D2 冻结 1 个 → 恰好 2 次决策(修复前 3 次),
        去重只发生在同日期内、不吞并不同日期。"""
        manifest = _dup_manifest(
            ("snap-d1a", "snap-d1b", "snap-d2"),
            run_id="RR-issue356-mixed-0001",
            idempotency_key="issue356-mixed-1",
        )
        snapshots = {
            "snap-d1a": _snapshot("snap-d1a", DECISION_AT),
            "snap-d1b": _snapshot("snap-d1b", DECISION_AT, momentum_shift=0.2),
            "snap-d2": _snapshot("snap-d2", DECISION_AT_2, momentum_shift=-0.1),
        }
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()

        record = await ResearchRunCoordinator(store).execute(
            manifest,
            _CountingSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )

        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == 2 * DECISION_STAGE_COUNT + 1
        business_dates = {
            artifacts[index * DECISION_STAGE_COUNT].payload["business_date"]
            for index in range(2)
        }
        assert business_dates == {
            date(2024, 1, 2).isoformat(),
            date(2024, 2, 1).isoformat(),
        }

    async def test_progress_totals_follow_deduped_decisions(self) -> None:
        """#188/#308 进度:total 随去重后的决策数递增,单调 / 夹紧规则无
        冲突 —— 4 个同日快照的 run 终态 done = 1 决策 + report,决策级
        phase 序号最大只到 #1。"""
        manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-prog-0001",
            idempotency_key="issue356-prog-1",
        )
        snapshots = _dup_snapshots(DECISION_AT)
        provider = _closes_provider(n_days=100)
        calls: list[tuple[int, int | None, str | None]] = []

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            calls.append((done, total, phase))

        record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
            manifest,
            _CountingSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
            progress=progress,
        )

        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        assert calls, "进度回调不应为空"
        done_values = [item[0] for item in calls]
        assert done_values == sorted(done_values)  # done 单调不减
        assert done_values[-1] == DECISION_STAGE_COUNT + 1  # 1 决策 + report
        totals = [item[1] for item in calls if item[1] is not None]
        assert totals == sorted(totals)  # total 只增不减
        decision_phases = [item[2] for item in calls if item[2] and "#2@" in item[2]]
        assert decision_phases == []


# ---------------------------------------------------------------------------
# Part C:等值 —— 重复决策是 no-op,去重不改变组合产物
# ---------------------------------------------------------------------------


class TestDedupEquivalence:
    async def test_legacy_expansion_vs_dedup_report_equivalence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """构造性等值对照:同一数据 / 快照,分别用修复前(逐快照展开)与
        修复后(按日期去重)的推导各跑一条完整 run:

        * 组合结果字段(收益 / 权益 / 现金 / 费用 / 回撤 / 夏普)逐值一致;
        * 订单 / 成交条数恰为 4 倍(重复决策各自重做同一组合构建),正是
          本 issue 要消除的纯浪费;
        * 代表决策(修复前排序末位)的输入段(universe / features /
          signals,与账本状态无关)与去重后唯一决策逐位一致 —— 去重不改变
          任何决策的输入,只移除冗余执行。

        零费用 + 决策日持平行情(见 :func:`_flat_tail_provider`)使重复执行
        不残留净敞口,组合结果可严格对照;一般形态下重复执行的矫正单会带来
        有界费用噪声 —— 去重把它连同算力一起消除,同样是本 issue 的意图。"""
        snapshots = _dup_snapshots(DECISION_AT)
        provider = _flat_tail_provider()
        post_manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-post-0001",
            idempotency_key="issue356-post-1",
            zero_fee=True,
        )
        post_store = InMemoryResearchRunStore()
        post_record = await ResearchRunCoordinator(post_store).execute(
            post_manifest,
            _CountingSignalAdapter(
                manifest=post_manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert post_record.status is ResearchRunStatus.COMPLETED, post_record.error_summary
        assert post_record.result is not None

        # 修复前(monkeypatch 旧推导):4 次重复决策。
        monkeypatch.setattr(
            signal_engine_module, "_snapshot_decision_days", _legacy_snapshot_decision_days
        )
        pre_manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-pre-0001",
            idempotency_key="issue356-pre-1",
            zero_fee=True,
        )
        pre_store = InMemoryResearchRunStore()
        pre_record = await ResearchRunCoordinator(pre_store).execute(
            pre_manifest,
            _CountingSignalAdapter(
                manifest=pre_manifest, provider=provider, snapshots=snapshots
            ),
        )
        monkeypatch.undo()
        assert pre_record.status is ResearchRunStatus.COMPLETED, pre_record.error_summary
        assert pre_record.result is not None

        pre_report = pre_record.result
        post_report = post_record.result
        # 决策数按设计缩小(4 → 1),组合结果字段逐值一致。
        assert pre_report.decision_count == 4
        assert post_report.decision_count == 1
        for field in (
            "strategy_return",
            "benchmark_symbol",
            "benchmark_return",
            "excess_return",
            "sharpe_ratio",
            "max_drawdown",
            "final_equity",
            "final_cash",
            "commission_paid",
            "tax_paid",
            "slippage_paid",
            "fill_shortfall",
            "accounting_invariants_passed",
            "execution_mode",
        ):
            assert getattr(pre_report, field) == getattr(post_report, field), field

        # 执行次数诊断量随重复次数放大:修复前 4 条决策各自做同一组合构建,
        # 订单 / 成交条数恰为修复后的 4 倍(浪费的直观形态)。
        assert pre_report.order_count == 4 * post_report.order_count
        assert pre_report.fill_count == 4 * post_report.fill_count

        # constraint_impact 是「逐决策 |before-after| 求和」的诊断量,不是组合
        # 结果:重复决策重复做同一约束投影,修复前把同一诊断量求和多次。
        # 数学上:修复后(仅代表决策)的每键值是修复前总和的一个加项,
        # 不超过总和,且键集是修复前的子集。
        assert set(post_report.constraint_impact) <= set(pre_report.constraint_impact)
        for key, post_value in post_report.constraint_impact.items():
            assert post_value <= pre_report.constraint_impact[key], key

        # 修复前 4 条决策全部落在同一业务日期(被修复的 x4 形态本身)。
        pre_artifacts = await pre_store.list_artifacts(pre_manifest.run_id)
        assert len(pre_artifacts) == 4 * DECISION_STAGE_COUNT + 1
        for index in range(4):
            payload = pre_artifacts[index * DECISION_STAGE_COUNT].payload
            assert payload["business_date"] == date(2024, 1, 2).isoformat()

        # 代表决策(factor-d,修复前排序末位 = 决策 3)的输入段(universe /
        # features / signals,纯函数于冻结输入与决策时点、与账本状态无关)
        # 与去重后唯一决策逐位一致 —— 去重不改变任何决策的输入,只移除
        # 冗余执行。
        post_artifacts = await post_store.list_artifacts(post_manifest.run_id)
        assert len(post_artifacts) == DECISION_STAGE_COUNT + 1
        for stage_offset in range(3):
            pre_checksum = pre_artifacts[3 * DECISION_STAGE_COUNT + stage_offset].checksum
            post_checksum = post_artifacts[stage_offset].checksum
            assert pre_checksum == post_checksum, f"stage offset {stage_offset}"

    async def test_feature_merge_still_covers_all_snapshots(self) -> None:
        """去重只收敛决策时点,不改变特征装配口径:决策特征仍按**全部**
        冻结快照 PIT 合并(某特征只存在于非代表快照时照常可见)。"""
        manifest = _dup_manifest(
            ("factor-base", "factor-extra"),
            run_id="RR-issue356-feat-0001",
            idempotency_key="issue356-feat-1",
        )
        extra = _StubSnapshot(
            snapshot_id="factor-extra",
            decision_at=DECISION_AT,
            observations=tuple(
                _obs(symbol, "momentum", 9.9, available_at=SNAPSHOT_AT)
                for symbol in ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")
            ),
        )
        snapshots = {"factor-base": _snapshot("factor-base", DECISION_AT), "factor-extra": extra}
        provider = _closes_provider(n_days=100)

        contexts = await _load_dup_contexts(manifest, provider, snapshots)

        assert len(contexts) == 1
        momentum = [
            item for item in contexts[0].features if item.feature_id == "momentum"
        ]
        # 代表项是 factor-extra(排序末位);非代表的 factor-base 观测同样并入
        # (_load_features 按全部快照合并,与去重无关)。
        assert any(item.value == 9.9 for item in momentum)
        assert any(item.value != 9.9 for item in momentum)


# ---------------------------------------------------------------------------
# Part D:#314 断点续算口径一致
# ---------------------------------------------------------------------------


class TestResumeScheduleConsistency:
    async def test_resume_schedule_total_matches_deduped_schedule(self) -> None:
        """``_resume_schedule_total``(#314 快速路径的总数推导)与执行期
        走同一去重函数:4 个同日快照的 run 推导总数 = 1,不因快照数膨胀。"""
        manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-resu-0001",
            idempotency_key="issue356-resu-1",
        )
        adapter = _CountingSignalAdapter(
            manifest=manifest,
            provider=_closes_provider(n_days=100),
            snapshots=_dup_snapshots(DECISION_AT),
        )

        assert await adapter._resume_schedule_total() == 1

    async def test_all_completed_resume_takes_fast_path_after_dedup(self) -> None:
        """report 段中断的 4 同日快照 run:已落库 1 决策 == 去重后推导总数
        1 → 快速路径跳过整段加载(#314 语义在去重后依然成立)。"""
        manifest = _dup_manifest(
            _DUP_IDS,
            run_id="RR-issue356-fast-0001",
            idempotency_key="issue356-fast-1",
        )
        snapshots = _dup_snapshots(DECISION_AT)
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        class _InterruptAtReport(_CountingSignalAdapter):
            def build_report(self, manifest: Any, decisions: Any) -> Any:
                from finboard_backtest.research_run import ResearchRunInterruptedError

                del manifest, decisions
                raise ResearchRunInterruptedError("report-stage crash")

        interrupted = await coordinator.execute(
            manifest,
            _InterruptAtReport(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == DECISION_STAGE_COUNT  # 恰好 1 个决策落库

        resumed_adapter = _CountingSignalAdapter(
            manifest=manifest, provider=provider, snapshots=snapshots
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 快速路径:整段加载与组合构建全部跳过(推导总数与已落库数同口径)。
        assert resumed_adapter.load_calls == 0
        assert resumed_adapter.last_pipeline is None


# ---------------------------------------------------------------------------
# Part E:单快照与 multi_period 回归(本无重复,行为不变)
# ---------------------------------------------------------------------------


class TestUnchangedPaths:
    async def test_single_snapshot_single_decision(self) -> None:
        """单快照 run:去重前后都是 1 条决策,快照归属不变。"""
        manifest = _dup_manifest(
            ("factor-only",),
            run_id="RR-issue356-single-01",
            idempotency_key="issue356-single-1",
        )
        snapshots = {"factor-only": _snapshot("factor-only", DECISION_AT)}

        contexts = await _load_dup_contexts(manifest, _closes_provider(n_days=100), snapshots)

        assert len(contexts) == 1
        assert contexts[0].context.business_date == date(2024, 1, 2)
        assert contexts[0].snapshot_id == "factor-only"

    async def test_multi_period_ignores_frozen_snapshot_duplicates(
        self, tmp_path: Any
    ) -> None:
        """multi_period:决策期来自发布交易日历,冻结多少(重复)快照都不
        影响期数 —— 快照去重不泄漏进多期推导。"""
        from tests.unit.research_run.test_issue_306_load_probe import _build_release

        provider = await _build_release(tmp_path)
        spec = _price_only_spec()
        expected_periods = len(_month_end_decisions())
        manifest = ResearchRunManifest(
            run_id="RR-issue356-multi-001",
            idempotency_key="issue356-multi-1",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id=provider.release.release_id,
                    version="v1",
                    checksum="a" * 64,
                    capabilities=("stock",),
                ),
            ),
            # 冻结 3 个同日快照引用:多期推导不读快照决策日。
            factor_snapshots=tuple(
                FrozenArtifactRef(
                    artifact_id=snapshot_id,
                    version="v1",
                    checksum="b" * 64,
                    capabilities=("factor:momentum",),
                )
                for snapshot_id in ("snap-dup-a", "snap-dup-b", "snap-dup-c")
            ),
            parameters={"rebalance_frequency": "monthly"},
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )

        decision_days = await _derive_rebalance_decision_days(provider, "monthly")
        assert len(decision_days) == expected_periods

        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=lambda release_id: provider,
            snapshot_provider=_noop_snapshot_provider,
        )
        assert len(contexts) == expected_periods
        # 每期恰一条:业务日期互不相同(日历推导天然去重)。
        dates = [item.context.business_date for item in contexts]
        assert len(set(dates)) == len(dates)

    async def test_snapshot_missing_still_fails_closed(self) -> None:
        """快照缺失 fail-closed 语义保留(错误文案不变)。"""
        manifest = _dup_manifest(
            ("ghost-snapshot",),
            run_id="RR-issue356-miss-0001",
            idempotency_key="issue356-miss-1",
        )
        with pytest.raises(ValueError, match="因子快照缺失"):
            await _snapshot_decision_days(manifest, _noop_snapshot_provider)
