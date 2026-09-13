"""research_run 断点续算 —— 跳过已落库决策的重算(issue #314)。

配套 #294 的自动重试:决策级 checkpoint 此前只保证写入幂等去重、不省计算
(重试从零重算全部决策)。本文件锁定:

* artifact 读回:13 stage 全齐 + 逐 artifact 校验和复验 + 无损重建为
  ``DecisionBundle``;任何缺失 / 不一致 / 重建失败在前缀处截断(fail-closed
  兜底,宁重算不漂移);
* 协议:适配器 ``resume_from``(getattr 探测,#304 先例)接受前缀后对已
  落库决策零重算;组合管线状态由最后一个已完成决策精确重建(全新执行
  逐值等价,含账本 / 风险状态 / 决策 ID 内嵌全局序号);
* 冲突语义零变化:存储校验和篡改仍走既有「checkpoint 内容冲突」
  fail-closed(重算端幂等去重抛 ResearchRunConflictError);载荷损坏
  (checksum 未动)在读回端截断 → 全量重算,不误报冲突;
* 快速路径:全部决策已落库且报告不依赖冻结输入时跳过整段加载
  (``build_decision_load_contexts``),报告构建只消费读回的决策;
* 进度语义:恢复时 progress 从已落库前缀重新逐段上报(done 单调、
  final 覆盖全部决策 + report;#188/#308 兼容)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DECISION_ARTIFACT_STAGES,
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchArtifact,
    ResearchFillAction,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
    ResearchRunStage,
    ResearchRunStatus,
    completed_decision_prefix,
    decision_artifact_index,
    to_json_value,
)
from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    FeatureValue,
    NormalizedSignal,
    UniverseCandidate,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.runner import (
    _DECISION_STAGES,
    DECISION_STAGE_COUNT,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
)

from .conftest import fixed_report
from .test_issue_304_partial_evidence import (
    DECISION_AT,
    DECISION_AT_2,
    _closes_provider,
)
from .test_issue_304_partial_evidence import (
    SYMBOLS as _SIGNAL_SYMBOLS,
)
from .test_issue_304_partial_evidence import (
    _manifest as _signal_manifest,
)
from .test_issue_304_partial_evidence import (
    _spec as _signal_spec,
)
from .test_signal_engine import _obs, _StubSnapshot

pytestmark = pytest.mark.asyncio

SYMBOLS = ("A.SH", "B.SH", "C.SH")


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _artifact_fingerprint(
    artifacts: Sequence[ResearchArtifact],
) -> list[tuple[str, str, str]]:
    """跨 run_id 可比的 artifact 指纹(剥掉 run 前缀的 suffix + stage + checksum)。"""
    return [
        (item.artifact_id.split(":A:", 1)[1], item.stage.value, item.checksum)
        for item in artifacts
    ]


def _multi_input(
    day_index: int,
    scores: tuple[float, ...] = (1.0, 1.0, 1.0),
    *,
    price: float = 10.0,
) -> PortfolioDecisionInput:
    """三标的、逐日决策的固定组合输入(与集成测试同构)。

    ``price`` 同时作为决策价与执行价(逐日可变,用于制造平仓价 != 建仓价
    的 realized_pnl != 0 场景,见 ``test_resume_accepts_prefix_ending_with_closed_book``)。
    """

    decision_at = datetime(2024, 1, 2 + day_index, 15, tzinfo=UTC)
    return PortfolioDecisionInput(
        business_date=date(2024, 1, 2 + day_index),
        decision_at=decision_at,
        execution_at=datetime(2024, 1, 3 + day_index, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("单元测试候选池通过",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=10.0,
                source_artifact_ids=("release-v1",),
                available_at=decision_at,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=score,
                action="buy" if score > 0 else "sell",
                rule_id="issue314-signal",
                rationale="断点续算固定信号",
            )
            for symbol, score in zip(SYMBOLS, scores, strict=True)
        ),
        prices=dict.fromkeys(SYMBOLS, price),
        execution_prices=dict.fromkeys(SYMBOLS, price),
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in SYMBOLS},
        input_artifact_ids=("release-v1",),
        covariance=CovarianceEstimate(
            matrix=np.diag([0.01, 0.01, 0.01]),
            tickers=list(SYMBOLS),
            shrinkage=0.0,
            n_observations=252,
        ),
    )


def _evolving_decisions(
    build: Any, manifest: Any, count: int
) -> tuple[DecisionBundle, ...]:
    """跨决策一致演化的固定样本(open/open[/close]),过 coordinator 校验。

    ``build`` 为 conftest 的 ``decision_factory`` fixture 注入的构造闭包。
    """

    def _position(quantity: int, market_value: str) -> tuple[ResearchPosition, ...]:
        return (
            ResearchPosition(
                symbol="510300.SH",
                position_side=ResearchPositionSide.LONG,
                quantity=Decimal(quantity),
                average_price=Decimal("100"),
                market_price=Decimal("100"),
                market_value=Decimal(market_value),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            ),
        )

    built: list[DecisionBundle] = [
        build(manifest=manifest, index=0, action=ResearchFillAction.OPEN_LONG)
    ]
    if count >= 2:
        built.append(
            build(
                manifest=manifest,
                index=1,
                action=ResearchFillAction.OPEN_LONG,
                positions=_position(200, "20000"),
                cash=Decimal("80000"),
                market_value=Decimal("20000"),
            )
        )
    if count >= 3:
        built.append(
            build(
                manifest=manifest,
                index=2,
                action=ResearchFillAction.CLOSE_LONG,
                positions=_position(100, "10000"),
                cash=Decimal("90000"),
                market_value=Decimal("10000"),
            )
        )
    return tuple(built)


def _multi_report(kind: str, decisions: tuple[Any, ...]) -> Any:
    return replace(
        fixed_report(kind, decisions[-1]),
        decision_count=len(decisions),
        order_count=sum(len(item.orders) for item in decisions),
        fill_count=sum(len(item.fills) for item in decisions),
    )


class _InterruptAfterKAdapter:
    """产出 k 条决策后模拟进程崩溃(hook-less 适配器,report 段不可达)。"""

    strategy_kind = "ma_cross"

    def __init__(self, decisions: tuple[Any, ...], k: int, report: Any) -> None:
        self._decisions = decisions
        self._k = k
        self._report = report

    def validate_manifest(self, manifest: Any) -> None:
        del manifest

    async def decisions(self, manifest: Any) -> AsyncIterator[Any]:
        del manifest
        for produced, decision in enumerate(self._decisions, start=1):
            yield decision
            if produced >= self._k:
                raise ResearchRunInterruptedError("strategy timeout")

    def build_report(self, manifest: Any, decisions: Any) -> Any:
        del manifest, decisions
        raise AssertionError("report must not be reached after interrupt")


class _CountingPipeline(PortfolioPipelineAdapter):
    """统计 _build_decision 调用次数的管线(零重算断言用)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.build_calls = 0

    def _build_decision(self, **kwargs: Any) -> Any:
        self.build_calls += 1
        return super()._build_decision(**kwargs)


class _CountingSignalAdapter(SignalEnginePipelineAdapter):
    """统计加载整段(_load)与组合构建的真实信号引擎适配器。"""

    def __init__(
        self,
        *,
        manifest: Any,
        provider: Any,
        snapshots: dict[str, Any],
    ) -> None:
        def release_factory(release_id: str) -> Any:
            assert release_id == "release-v1"
            return provider

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        super().__init__(
            manifest=manifest,
            release_provider_factory=release_factory,
            snapshot_provider=snapshot_provider,
        )
        self.load_calls = 0
        self.last_pipeline: _CountingPipeline | None = None

    async def _load(self) -> PortfolioPipelineAdapter:
        self.load_calls += 1
        # issue #463:与生产 _load 同构 —— 输入迭代器经 _iter_captured_inputs
        # 包装(逐期捕获 screen 投影 / 决策日),仅管线类型换成计数桩。
        if self._input_iterator is None:
            self._input_iterator = self._iter_captured_inputs(None)
        pipeline = _CountingPipeline(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._input_iterator,
        )
        self.last_pipeline = pipeline
        return pipeline


class _InterruptAtReportSignalAdapter(_CountingSignalAdapter):
    """全部决策产出后在 report 段崩溃(等价进程死在报告构建)。"""

    def build_report(self, manifest: Any, decisions: Any) -> Any:
        del manifest, decisions
        raise ResearchRunInterruptedError("report-stage crash")


class _InterruptAfterFirstSignalAdapter(_CountingSignalAdapter):
    """第一条决策产出后中断(等价进程死在决策 2 执行中)。"""

    async def decisions(self, manifest: Any) -> AsyncIterator[Any]:
        produced = 0
        async for decision in super().decisions(manifest):
            yield decision
            produced += 1
            if produced >= 1:
                raise ResearchRunInterruptedError("mid-stream crash")


# ---------------------------------------------------------------------------
# Part A:artifact 读回(重建 / 截断)
# ---------------------------------------------------------------------------


class TestCheckpointResumeReadback:
    async def test_stage_set_matches_runner_decision_stages(self) -> None:
        """镜像 stage 集合与 runner _DECISION_STAGES 完全一致(防两处漂移)。"""
        assert frozenset(_DECISION_STAGES) == DECISION_ARTIFACT_STAGES
        assert len(DECISION_ARTIFACT_STAGES) == DECISION_STAGE_COUNT

    def test_decision_artifact_index_parses_and_rejects(self) -> None:
        run_id = "RR-test-run-00000001"
        assert decision_artifact_index(run_id, f"{run_id}:A:00000012:ledger") == 12
        assert decision_artifact_index(run_id, f"{run_id}:A:report") is None
        assert decision_artifact_index(run_id, f"{run_id}:A:xyz:universe") is None
        assert decision_artifact_index(run_id, "RR-other-run-0000001:A:00000001:universe") is None

    async def test_round_trip_reconstructs_bundles_losslessly(
        self, manifest_factory, decision_factory
    ) -> None:
        """持久化 → 读回 → 重建:逐值等价(含 Decimal/枚举/时区/持仓状态)。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 3)
        adapter = DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=decisions,
            report=_multi_report("ma_cross", decisions),
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

        artifacts = await store.list_artifacts(manifest.run_id)
        rebuilt = completed_decision_prefix(manifest.run_id, artifacts)
        # coordinator 在持久化时注入 decision_id(run_id:D:<index>);读回的
        # 决策携带同一 ID,与原 bundle 逐字段(除注入的 decision_id)等值。
        annotated = [
            replace(item, decision_id=f"{manifest.run_id}:D:{index:08d}")
            for index, item in enumerate(decisions)
        ]
        assert rebuilt == annotated
        # checksum 级等价:重建决策再序列化与原载荷逐字节一致
        for original, bundle in zip(annotated, rebuilt, strict=True):
            assert to_json_value(bundle) == to_json_value(original)

    async def test_prefix_truncates_on_missing_stage(
        self, manifest_factory, decision_factory
    ) -> None:
        """决策落库半途(缺 stage)→ 前缀为空,该决策照常重算。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, _multi_report("ma_cross", decisions))
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        # 模拟崩溃在决策 0 落库半途:删掉末尾 6 个 stage artifact
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == DECISION_STAGE_COUNT
        for item in artifacts[-6:]:
            store._artifacts[manifest.run_id].pop(item.artifact_id)

        prefix = completed_decision_prefix(
            manifest.run_id, await store.list_artifacts(manifest.run_id)
        )
        assert prefix == []

    async def test_prefix_truncates_on_checksum_mismatch(
        self, manifest_factory, decision_factory
    ) -> None:
        """载荷与校验和不一致(存储损坏)→ 该决策截断,不产出不可信决策。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        record = await coordinator.execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross",
                decisions=decisions,
                report=_multi_report("ma_cross", decisions),
            ),
        )
        assert record.status is ResearchRunStatus.COMPLETED

        artifacts = await store.list_artifacts(manifest.run_id)
        target = next(
            item
            for item in artifacts
            if item.stage is ResearchRunStage.LEDGER
            and item.decision_id is not None
            and item.decision_id.endswith("00000001")
        )
        store._artifacts[manifest.run_id][target.artifact_id] = replace(
            target, payload={**target.payload, "ledger": {}}
        )
        prefix = completed_decision_prefix(
            manifest.run_id, await store.list_artifacts(manifest.run_id)
        )
        assert len(prefix) == 1  # 决策 0 仍可读回,决策 1 截断

    async def test_report_artifact_ignored_by_readback(
        self, manifest_factory, decision_factory
    ) -> None:
        """REPORT artifact(decision_id=None)不参与决策前缀。"""
        manifest = manifest_factory()
        decision = decision_factory(manifest=manifest)
        adapter = DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        )
        store = InMemoryResearchRunStore()
        await ResearchRunCoordinator(store).execute(manifest, adapter)
        artifacts = await store.list_artifacts(manifest.run_id)
        assert artifacts[-1].stage is ResearchRunStage.REPORT
        prefix = completed_decision_prefix(manifest.run_id, artifacts)
        assert len(prefix) == 1


# ---------------------------------------------------------------------------
# Part B:PortfolioPipelineAdapter.resume_from(状态种子)
# ---------------------------------------------------------------------------


class TestPipelineResumeFrom:
    async def test_resume_from_rejections(self, manifest_factory) -> None:
        """契约:空前缀 / 超过冻结输入数 / 时序非严格递增 / 已种子化 → 拒绝。"""
        manifest = manifest_factory()
        inputs = tuple(_multi_input(i) for i in range(2))
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        assert adapter.resume_from(()) is False

        three_inputs = tuple(_multi_input(i) for i in range(3))
        three_bundles = [
            item
            async for item in PortfolioPipelineAdapter(
                strategy_kind="ma_cross", decision_inputs=three_inputs
            ).decisions(manifest)
        ]
        # 数量超过冻结输入:拒绝
        assert adapter.resume_from(three_bundles) is False
        # 时序非严格递增:拒绝
        assert adapter.resume_from([three_bundles[0], three_bundles[0]]) is False
        # 接受后二次种子:拒绝
        assert adapter.resume_from(three_bundles[:1]) is True
        assert adapter.resume_from(three_bundles[:1]) is False

    async def test_resume_yields_prefix_then_continues_global_index(
        self, manifest_factory
    ) -> None:
        """种子前缀原样产出,后续决策全局序号连续、内容与一次跑完等值。"""
        manifest = manifest_factory()
        inputs = (
            _multi_input(0, (1.0, 1.0, 1.0)),
            _multi_input(1, (-1.0, -1.0, -1.0)),  # 清仓
            _multi_input(2, (1.0, 1.0, 1.0)),  # 再建仓:恢复边界后仍有真实交易
        )

        one_shot = _CountingPipeline(strategy_kind="ma_cross", decision_inputs=inputs)
        fresh_bundles = [item async for item in one_shot.decisions(manifest)]
        assert len(fresh_bundles) == 3

        partial = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        generator = cast(
            AsyncGenerator[DecisionBundle, None], partial.decisions(manifest)
        )
        prefix = [await generator.__anext__(), await generator.__anext__()]
        await generator.aclose()

        resumed = _CountingPipeline(strategy_kind="ma_cross", decision_inputs=inputs)
        assert resumed.resume_from(prefix) is True
        resumed_bundles = [item async for item in resumed.decisions(manifest)]

        assert resumed.build_calls == 1  # 前缀零重算
        assert resumed_bundles == fresh_bundles  # 逐值等价(含账本/风险状态)
        for original, rebuilt in zip(fresh_bundles, resumed_bundles, strict=True):
            assert to_json_value(rebuilt) == to_json_value(original)
        # 恢复边界之后的决策(清仓再建仓)有真实指令,且内嵌全局序号 2
        instruction_ids = [
            instruction.instruction_id for instruction in resumed_bundles[2].rebalance_plan
        ]
        assert instruction_ids
        assert all(":I:00000002:" in item for item in instruction_ids)

    async def test_resume_accepts_prefix_ending_with_closed_book(
        self, manifest_factory
    ) -> None:
        """前缀最后决策平仓且平仓价 != 建仓价时种子仍被接受(2026-09-13 修复)。

        生产形态(RR-bff0):重放的 prefix 最后决策清仓 300308.SZ
        (``realized_pnl=1126.47``),记账后 ``quantity=0`` 但
        ``realized_pnl != 0``,故仍出现在 ``DecisionBundle.positions`` 里;
        而 ``_risk_state.high_water_prices`` 只记录 ``quantity > 0`` 标的 →
        旧校验取到 None 必抛「重放价格高水位与风险状态不一致」→ 种子被拒、
        每次重试从零重算。既有用例价格恒定(``realized_pnl=0``,平仓账面不
        入 positions)故未暴露。
        """
        manifest = manifest_factory()
        inputs = (
            _multi_input(0, (1.0, 1.0, 1.0), price=10.0),
            _multi_input(1, (-1.0, -1.0, -1.0), price=12.0),  # 平仓且有盈亏
        )
        fresh = [
            item
            async for item in PortfolioPipelineAdapter(
                strategy_kind="ma_cross", decision_inputs=inputs
            ).decisions(manifest)
        ]
        # 前提:第二期平仓产生非零 realized_pnl(平仓账面进入 positions 记录)。
        closed = [
            position
            for position in fresh[1].positions
            if position.quantity == 0 and position.realized_pnl != 0
        ]
        assert closed, fresh[1].positions

        resumed = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        assert resumed.resume_from(fresh) is True
        rebuilt = [item async for item in resumed.decisions(manifest)]
        assert rebuilt == fresh

    async def test_resume_state_seeding_matches_fresh_run_exactly(
        self, manifest_factory
    ) -> None:
        """种子后的账本状态与全新执行逐值一致(现金/费用/持仓/风险状态)。"""
        manifest = manifest_factory()
        inputs = (
            _multi_input(0, (1.0, 1.0, 1.0)),
            _multi_input(1, (-1.0, -1.0, -1.0)),
            _multi_input(2, (1.0, 1.0, 1.0)),
        )
        one_shot = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        fresh = [item async for item in one_shot.decisions(manifest)]

        partial = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        generator = cast(
            AsyncGenerator[DecisionBundle, None], partial.decisions(manifest)
        )
        prefix = [await generator.__anext__()]
        await generator.aclose()

        resumed = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=inputs
        )
        assert resumed.resume_from(prefix) is True
        rebuilt = [item async for item in resumed.decisions(manifest)]
        assert rebuilt == fresh
        assert rebuilt[-1].ledger == fresh[-1].ledger
        assert rebuilt[-1].risk_state == fresh[-1].risk_state
        assert rebuilt[-1].positions == fresh[-1].positions


# ---------------------------------------------------------------------------
# Part C:Coordinator 接线(旧路径 / 损坏兜底 / 冲突 fail-closed / 进度)
# ---------------------------------------------------------------------------


class TestCoordinatorResume:
    async def test_hookless_resume_recomputes_but_stays_consistent(
        self, manifest_factory, decision_factory
    ) -> None:
        """无 resume_from 的适配器:恢复走全量重算,幂等去重保证 artifact /
        result_checksum 与一次跑完完全一致。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        report = _multi_report("ma_cross", decisions)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, report)
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        assert len(await store.list_artifacts(manifest.run_id)) == DECISION_STAGE_COUNT

        resumed = await coordinator.execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary

        control_manifest = replace(
            manifest,
            run_id="RR-control-314-0000001",
            idempotency_key="control-314",
        )
        control = await coordinator.execute(
            control_manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED
        assert resumed.result_checksum == control.result_checksum
        assert resumed.result == control.result
        assert _artifact_fingerprint(await store.list_artifacts(manifest.run_id)) == (
            _artifact_fingerprint(await store.list_artifacts(control_manifest.run_id))
        )

    async def test_corrupted_payload_falls_back_to_full_recompute(
        self, manifest_factory, decision_factory
    ) -> None:
        """载荷损坏(checksum 未动)→ 读回截断 → 全量重算 → 幂等去重通过,
        run 仍 COMPLETED 且 checksum 等值(宁重算不漂移,不误报冲突)。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        report = _multi_report("ma_cross", decisions)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, report)
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        artifacts = await store.list_artifacts(manifest.run_id)
        universe = next(
            item for item in artifacts if item.stage is ResearchRunStage.UNIVERSE
        )
        store._artifacts[manifest.run_id][universe.artifact_id] = replace(
            universe, payload={**universe.payload, "candidates": []}
        )
        resumed = await coordinator.execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary

        control_manifest = replace(
            manifest, run_id="RR-control-314-corrupt", idempotency_key="control-314-c"
        )
        control = await coordinator.execute(
            control_manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert resumed.result_checksum == control.result_checksum

    async def test_checksum_tampering_conflicts_fail_closed(
        self, manifest_factory, decision_factory
    ) -> None:
        """checksum 篡改 → 重算端幂等去重抛「checkpoint 内容冲突」→ FAILED
        (既有冲突语义零变化,issue #314 验收)。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        report = _multi_report("ma_cross", decisions)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, report)
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        artifacts = await store.list_artifacts(manifest.run_id)
        universe = next(
            item for item in artifacts if item.stage is ResearchRunStage.UNIVERSE
        )
        store._artifacts[manifest.run_id][universe.artifact_id] = replace(
            universe, checksum="f" * 64
        )
        resumed = await coordinator.execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert resumed.status is ResearchRunStatus.FAILED
        assert resumed.error_code == "ResearchRunConflictError"
        # 内存 store 与 PostgreSQL repo 的冲突文案不同源,语义一致:
        # 「断点重放内容不一致」/「checkpoint 内容冲突」。
        summary = resumed.error_summary or ""
        assert "断点重放内容不一致" in summary or "checkpoint 内容冲突" in summary

    async def test_progress_re_reports_from_resume_prefix(
        self, manifest_factory, decision_factory
    ) -> None:
        """恢复执行从已落库前缀重新逐段上报 progress:done 单调、最终覆盖
        全部决策 + report;phase 保持 #308 决策级编码。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        report = _multi_report("ma_cross", decisions)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, report)
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        calls: list[tuple[int, int | None, str | None]] = []

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            calls.append((done, total, phase))

        resumed = await coordinator.execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
            progress=progress,
        )
        assert resumed.status is ResearchRunStatus.COMPLETED
        assert calls, "恢复执行必须重新上报进度"
        # 恢复执行重新上报:首个回调从决策 1 的第一个 stage 开始
        assert calls[0][0] == 1
        assert calls[0][2] is not None
        assert calls[0][2].startswith("research_run:universe#1@")
        # done 单调不减、最终覆盖全部决策 + report(2*13 + 1)
        done_values = [item[0] for item in calls]
        assert done_values == sorted(done_values)
        assert done_values[-1] == 2 * DECISION_STAGE_COUNT + 1
        # total 只增不减
        totals = [item[1] for item in calls if item[1] is not None]
        assert totals == sorted(totals)
        assert calls[-1][2] == "research_run:report"


# ---------------------------------------------------------------------------
# Part D:信号引擎断点续算(部分完成 + 全部完成快速路径)
# ---------------------------------------------------------------------------


def _plain_snapshot(decision_at: datetime, *, momentum_shift: float = 0.0) -> _StubSnapshot:
    """不含用户因子(u_ 前缀)观测的快照 —— 快速路径前置要求报告不依赖
    冻结输入(引用用户因子的 run 不走快速路径,见
    ``SignalEnginePipelineAdapter._resume_all_completed``)。"""

    at = datetime(2023, 12, 29, tzinfo=UTC)
    observations = []
    for index, symbol in enumerate(_SIGNAL_SYMBOLS):
        observations.append(_obs(symbol, "pb", 1.0 + index * 0.1, available_at=at))
        observations.append(
            _obs(symbol, "momentum", 0.05 * index + momentum_shift, available_at=at)
        )
        observations.append(
            _obs(symbol, "volatility_20d", 0.3 - 0.03 * index, available_at=at)
        )
    return _StubSnapshot(
        snapshot_id="factor-v1", decision_at=decision_at, observations=tuple(observations)
    )


_U_FACTOR = "u_agent_alpha"


def _user_factor_snapshot(
    decision_at: datetime, *, alpha_seed: float = 6.0
) -> _StubSnapshot:
    """带用户因子(u_ 前缀)观测的快照(信号引擎 #217 screen 场景)。"""

    at = datetime(2023, 12, 29, tzinfo=UTC)
    observations = []
    for index, symbol in enumerate(_SIGNAL_SYMBOLS):
        observations.append(_obs(symbol, "pb", 1.0 + index * 0.1, available_at=at))
        observations.append(_obs(symbol, "momentum", 0.05 * index, available_at=at))
        observations.append(
            _obs(symbol, "volatility_20d", 0.3 - 0.03 * index, available_at=at)
        )
        observations.append(
            _obs(symbol, _U_FACTOR, alpha_seed - index, available_at=at)
        )
    return _StubSnapshot(
        snapshot_id="factor-v1", decision_at=decision_at, observations=tuple(observations)
    )


def _signal_setup(run_id: str, idempotency_key: str) -> tuple[Any, dict[str, Any]]:
    spec = _signal_spec(rank_threshold=0.5)
    manifest = _signal_manifest(
        spec,
        run_id=run_id,
        idempotency_key=idempotency_key,
        snapshot_ids=("factor-v1", "factor-v2", "factor-v3"),
    )
    snapshots = {
        "factor-v1": _user_factor_snapshot(DECISION_AT),
        "factor-v2": _user_factor_snapshot(DECISION_AT_2, alpha_seed=3.0),
        "factor-v3": _user_factor_snapshot(
            datetime(2024, 3, 1, 15, 0, tzinfo=UTC), alpha_seed=9.0
        ),
    }
    return manifest, snapshots


class TestSignalEngineResume:
    async def test_partial_resume_skips_completed_decision_recompute(self) -> None:
        """验收主路径:3 决策跑 1 条后中断 → 恢复后已完成决策零重算
        (_build_decision 仅 2 次),result_checksum 与一次跑完等值。"""
        manifest, snapshots = _signal_setup("RR-issue314-partial-0001", "issue314-partial")
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        interrupted = await coordinator.execute(
            manifest,
            _InterruptAfterFirstSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == DECISION_STAGE_COUNT  # 仅决策 1 的 13 artifact

        resumed_adapter = _CountingSignalAdapter(
            manifest=manifest, provider=provider, snapshots=snapshots
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 部分完成:加载整段照常(既定取舍),已完成决策零重算
        assert resumed_adapter.load_calls == 1
        assert resumed_adapter.last_pipeline is not None
        assert resumed_adapter.last_pipeline.build_calls == 2
        # #470 前半场:种子消费完毕即释放(signal_engine 侧属性清空),
        # 断言改为「已消费且不再驻留」
        assert resumed_adapter._resume_bundles is None

        # 对照:一次跑完
        control_manifest, _ = _signal_setup(
            "RR-issue314-partial-ctrl", "issue314-partial-ctrl"
        )
        control = await coordinator.execute(
            control_manifest,
            _CountingSignalAdapter(
                manifest=control_manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED
        assert resumed.result_checksum == control.result_checksum
        assert _artifact_fingerprint(await store.list_artifacts(manifest.run_id)) == (
            _artifact_fingerprint(await store.list_artifacts(control_manifest.run_id))
        )

    async def test_all_completed_resume_skips_entire_load(self) -> None:
        """全部决策已落库(report 段崩溃)且报告不依赖冻结输入 → 快速路径:
        跳过整段加载(_load 0 次、_build_decision 0 次),报告只消费读回
        决策,result_checksum 与一次跑完等值。"""
        manifest, _ = _signal_setup("RR-issue314-fast-0001", "issue314-fast")
        snapshots = {
            "factor-v1": _plain_snapshot(DECISION_AT),
            "factor-v2": _plain_snapshot(DECISION_AT_2, momentum_shift=0.2),
            "factor-v3": _plain_snapshot(
                datetime(2024, 3, 1, 15, 0, tzinfo=UTC), momentum_shift=-0.1
            ),
        }
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        interrupted = await coordinator.execute(
            manifest,
            _InterruptAtReportSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        artifacts = await store.list_artifacts(manifest.run_id)
        assert len(artifacts) == 3 * DECISION_STAGE_COUNT  # 3 决策全部落库

        resumed_adapter = _CountingSignalAdapter(
            manifest=manifest, provider=provider, snapshots=snapshots
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 快速路径:加载整段与组合构建全部跳过(输入迭代器从未创建、
        # _fast_path 置位 —— 旧断言「_inputs == ()」的等价新形态,#463)
        assert resumed_adapter.load_calls == 0
        assert resumed_adapter.last_pipeline is None
        assert resumed_adapter._input_iterator is None
        assert resumed_adapter._fast_path is True
        # #470 前半场:快速路径种子消费完毕即释放,不再驻留
        assert resumed_adapter._resume_bundles is None

        control_manifest, _ = _signal_setup(
            "RR-issue314-fast-ctrl", "issue314-fast-ctrl"
        )
        control = await coordinator.execute(
            control_manifest,
            _CountingSignalAdapter(
                manifest=control_manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED
        assert resumed.result_checksum == control.result_checksum

    async def test_user_factor_run_never_takes_fast_path(self) -> None:
        """引用用户因子的 run(u_ 前缀特征)即使全部决策已落库也不走快速
        路径 —— factor_screen 只能从已构建的冻结输入计算,报告等值优先;
        该场景落完整加载 + 全前缀种子(_build_decision 0 次),结果与一次
        跑完等值。"""
        manifest, snapshots = _signal_setup("RR-issue314-ufast-0001", "issue314-ufast")
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest,
            _InterruptAtReportSignalAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        resumed_adapter = _CountingSignalAdapter(
            manifest=manifest, provider=provider, snapshots=snapshots
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 守卫生效:完整加载;全前缀种子 → 零组合构建
        assert resumed_adapter.load_calls == 1
        assert resumed_adapter.last_pipeline is not None
        assert resumed_adapter.last_pipeline.build_calls == 0
        assert resumed.result is not None
        assert resumed.result.factor_screen is not None  # screen 未因恢复而丢失

        control_manifest, _ = _signal_setup(
            "RR-issue314-ufast-ctrl", "issue314-ufast-ctrl"
        )
        control = await coordinator.execute(
            control_manifest,
            _CountingSignalAdapter(
                manifest=control_manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED
        assert resumed.result_checksum == control.result_checksum

    async def test_resume_all_completed_declines_on_schedule_mismatch(self) -> None:
        """已落库数 != 冻结输入可推导的决策总数 → 快速路径拒绝(防漂移)。"""
        manifest, snapshots = _signal_setup("RR-issue314-mis-000001", "issue314-mis")
        adapter = _CountingSignalAdapter(
            manifest=manifest,
            provider=_closes_provider(n_days=100),
            snapshots=snapshots,
        )
        # 用组合输入充当读回前缀(仅数量/时序参与判定;鸭子类型即可,
        # mypy 视角显式 cast)。
        bundles = cast(
            "tuple[DecisionBundle, ...]",
            (_multi_input(0), _multi_input(1), _multi_input(2)),
        )
        assert adapter.resume_from(bundles) is True
        seeded = adapter._resume_bundles
        assert seeded is not None

        async def two() -> int | None:
            return 2

        adapter._resume_schedule_total = two  # type: ignore[method-assign]
        assert await adapter._resume_all_completed(seeded) is False

        async def three() -> int | None:
            return 3

        adapter._resume_schedule_total = three  # type: ignore[method-assign]
        assert await adapter._resume_all_completed(seeded) is True

        async def broken() -> int | None:
            return None

        adapter._resume_schedule_total = broken  # type: ignore[method-assign]
        assert await adapter._resume_all_completed(seeded) is False


class _DecliningResumeAdapter:
    """实现 resume_from 但始终拒绝的适配器(coordinator 回退路径用)。"""

    strategy_kind = "ma_cross"

    def __init__(self, decisions: tuple[Any, ...], report: Any) -> None:
        self._decisions = decisions
        self._report = report
        self.resume_offers = 0

    def validate_manifest(self, manifest: Any) -> None:
        del manifest

    def resume_from(self, completed: Any) -> bool:
        del completed
        self.resume_offers += 1
        return False

    async def decisions(self, manifest: Any) -> AsyncIterator[Any]:
        del manifest
        for decision in self._decisions:
            yield decision

    def build_report(self, manifest: Any, decisions: Any) -> Any:
        del manifest, decisions
        return self._report


class TestCoordinatorDeclinedResume:
    async def test_declined_seed_falls_back_to_full_recompute(
        self, manifest_factory, decision_factory
    ) -> None:
        """适配器实现 resume_from 但拒绝种子 → 全量重算,结果仍与一次跑完
        等值(幂等去重兜底)。"""
        manifest = manifest_factory()
        decisions = _evolving_decisions(decision_factory, manifest, 2)
        report = _multi_report("ma_cross", decisions)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest, _InterruptAfterKAdapter(decisions, 1, report)
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        declining = _DecliningResumeAdapter(decisions=decisions, report=report)
        resumed = await coordinator.execute(manifest, declining)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        assert declining.resume_offers == 1

        control_manifest = replace(
            manifest, run_id="RR-control-314-decline", idempotency_key="control-314-d"
        )
        control = await coordinator.execute(
            control_manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross", decisions=decisions, report=report
            ),
        )
        assert resumed.result_checksum == control.result_checksum
