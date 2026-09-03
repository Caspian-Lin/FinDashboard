"""issue #305:interrupted run 恢复通道 —— replay 放开 INTERRUPTED 并自动继承冻结输入。

RR-7a74 事故:run 被标 interrupted(checkpoint 保留)后 ``replay`` 仅接受
completed,只能 cancel 后手工带全部快照 ID 重新入队。本文件锁定:

* Coordinator(权威判定):{COMPLETED, INTERRUPTED} 可重放,CANCELLED 拒绝;
* interrupted 重放自动继承原 manifest 全部冻结输入(input_checksum 相等、
  factor_snapshots 全集零手工),血缘标注 ``replay_of_run_id`` +
  ``replay_source_status``;
* completed 重放行为零变化(确定性对照仍生效)+ 血缘标注;
* interrupted 源无 result_checksum,重放即全新执行(不做确定性对照)。
"""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from finboard_backtest.research_run import (
    REPLAYABLE_SOURCE_STATUSES,
    DecisionSequenceAdapter,
    FrozenArtifactRef,
    InMemoryResearchRunStore,
    ResearchRunConflictError,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunStatus,
    manifest_from_json,
    stable_checksum,
    to_json_value,
)

from .conftest import fixed_report

#: 模拟 RR-7a74:多快照冻结输入(>1 个 snapshot ID,验证「全集零手工」)。
_MULTI_SNAPSHOTS = (
    FrozenArtifactRef(
        artifact_id="factor-v1",
        version="v1",
        checksum="b" * 64,
        capabilities=("factor:close",),
    ),
    FrozenArtifactRef(
        artifact_id="factor-v2",
        version="v1",
        checksum="c" * 64,
        capabilities=("factor:momentum",),
    ),
    FrozenArtifactRef(
        artifact_id="factor-v3",
        version="v2",
        checksum="d" * 64,
        capabilities=("factor:volatility",),
    ),
)


def _multi_snapshot_manifest(manifest: ResearchRunManifest) -> ResearchRunManifest:
    return replace(manifest, factor_snapshots=_MULTI_SNAPSHOTS)


class _InterruptAfterFirstDecisionAdapter:
    """产出一条决策后模拟超时/进程崩溃(runner 收口为 INTERRUPTED)。"""

    strategy_kind = "ma_cross"

    def __init__(self, decision: object) -> None:
        self._decision = decision

    def validate_manifest(self, manifest: ResearchRunManifest) -> None:
        del manifest

    async def decisions(self, manifest: ResearchRunManifest):
        del manifest
        yield self._decision
        raise ResearchRunInterruptedError("strategy timeout")

    def build_report(self, manifest: ResearchRunManifest, decisions):
        del manifest, decisions
        raise AssertionError("report must not be called")


def _frozen_inputs(manifest: ResearchRunManifest) -> dict[str, object]:
    """manifest 中与运行身份无关的冻结输入投影(与 input_checksum 同口径)。"""
    return {
        "strategy_spec": manifest.strategy_spec,
        "strategy_spec_checksum": manifest.strategy_spec_checksum,
        "dataset_releases": manifest.dataset_releases,
        "factor_snapshots": manifest.factor_snapshots,
        "parameters": manifest.parameters,
        "code_version": manifest.code_version,
        "initial_capital": manifest.initial_capital,
    }


@pytest.mark.asyncio
async def test_interrupted_run_replay_inherits_frozen_inputs(
    manifest_factory, decision_factory
) -> None:
    """interrupted run 一条 replay 命令恢复:冻结输入全集自动继承,零手工。"""
    manifest = _multi_snapshot_manifest(manifest_factory())
    decision = decision_factory(manifest=manifest)
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)

    interrupted = await coordinator.execute(
        manifest, _InterruptAfterFirstDecisionAdapter(decision)
    )
    assert interrupted.status is ResearchRunStatus.INTERRUPTED
    assert interrupted.result_checksum is None

    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-replay-305-interrupted",
        idempotency_key="replay-305-interrupted",
        requested_by="unit-test",
        adapter=DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
    )

    # 恢复成功:新 run 从冻结输入完整跑到 COMPLETED。
    assert replay.status is ResearchRunStatus.COMPLETED, replay.error_summary
    assert replay.result_checksum is not None
    # 血缘标注:replay-of-interrupted。
    assert replay.manifest.replay_of_run_id == manifest.run_id
    assert replay.manifest.replay_source_status == "interrupted"
    # 冻结输入零手工继承:input_checksum 与逐项冻结输入和源 run 一致
    # (含 factor_snapshots 全部 ID)。
    assert replay.manifest.input_checksum == manifest.input_checksum
    assert _frozen_inputs(replay.manifest) == _frozen_inputs(manifest)
    assert replay.manifest.factor_snapshots == _MULTI_SNAPSHOTS
    # 源 run 不被复活:仍 INTERRUPTED,原 job 语义不受影响。
    source_after = await store.get(manifest.run_id)
    assert source_after is not None
    assert source_after.status is ResearchRunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_completed_replay_unchanged_and_annotated(
    manifest_factory, decision_factory
) -> None:
    """completed 重放行为零变化(确定性对照),血缘标注 completed。"""
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)
    source = await coordinator.execute(manifest, adapter)

    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-replay-305-completed",
        idempotency_key="replay-305-completed",
        requested_by="unit-test",
        adapter=adapter,
    )

    assert replay.status is ResearchRunStatus.COMPLETED
    assert replay.result_checksum == source.result_checksum
    assert replay.manifest.replay_of_run_id == manifest.run_id
    assert replay.manifest.replay_source_status == "completed"
    assert replay.manifest.input_checksum == manifest.input_checksum


@pytest.mark.asyncio
async def test_completed_replay_still_detects_result_drift(
    manifest_factory, decision_factory
) -> None:
    """completed 源的确定性重放对照保持:结果漂移仍判 non_deterministic_replay。"""
    manifest = manifest_factory()
    decision = decision_factory()
    coordinator = ResearchRunCoordinator(InMemoryResearchRunStore())
    await coordinator.execute(
        manifest,
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
    )

    from dataclasses import replace as dc_replace

    drifted_report = dc_replace(
        fixed_report("ma_cross", decision), sharpe_ratio=9.9
    )
    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-replay-305-drift",
        idempotency_key="replay-305-drift",
        requested_by="unit-test",
        adapter=DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=drifted_report,
        ),
    )

    assert replay.status is ResearchRunStatus.FAILED
    assert replay.error_code == "non_deterministic_replay"


@pytest.mark.asyncio
async def test_cancelled_run_replay_is_rejected(
    manifest_factory, decision_factory
) -> None:
    """CANCELLED 是显式用户意图:拒绝重放,错误给出去向说明。"""
    manifest = manifest_factory()
    decision = decision_factory()
    store = InMemoryResearchRunStore()
    await store.create_or_get(manifest)
    coordinator = ResearchRunCoordinator(store)
    await coordinator.cancel(manifest.run_id)

    unused_adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    with pytest.raises(ResearchRunConflictError) as exc_info:
        await coordinator.replay(
            source_run_id=manifest.run_id,
            new_run_id="RR-replay-305-cancelled",
            idempotency_key="replay-305-cancelled",
            requested_by="unit-test",
            adapter=unused_adapter,
        )

    message = str(exc_info.value)
    assert "interrupted" in message
    assert "finboard_run_replay" in message


def test_replayable_source_statuses_exactly_completed_and_interrupted() -> None:
    """三处守卫共用的权威判定:恰好 {COMPLETED, INTERRUPTED}。"""
    assert frozenset(
        {ResearchRunStatus.COMPLETED, ResearchRunStatus.INTERRUPTED}
    ) == REPLAYABLE_SOURCE_STATUSES


def test_manifest_round_trips_replay_lineage(manifest_factory) -> None:
    """血缘标注经 JSON round-trip 无损,且不影响 checksum 复现。"""
    replayed = replace(
        manifest_factory(),
        run_id="RR-replay-305-rt",
        idempotency_key="replay-305-rt-0001",
        replay_of_run_id="RR-test-run-00000001",
        replay_source_status="interrupted",
    )
    payload = to_json_value(replayed)
    assert isinstance(payload, dict)
    restored = manifest_from_json(payload)
    assert restored.replay_of_run_id == "RR-test-run-00000001"
    assert restored.replay_source_status == "interrupted"
    assert restored.checksum == replayed.checksum

    plain = manifest_factory()
    plain_payload = to_json_value(plain)
    assert isinstance(plain_payload, dict)
    # None 血缘随 asdict 进 JSON(与 strategy_version 同例),读回仍为 None。
    assert plain_payload["replay_source_status"] is None
    plain_restored = manifest_from_json(plain_payload)
    assert plain_restored.replay_source_status is None
    assert plain_restored.checksum == plain.checksum


def test_replay_lineage_validation(manifest_factory) -> None:
    """血缘标注 fail-closed:必须伴随 replay_of_run_id 且只接受合法状态。"""
    with pytest.raises(ValueError, match="replay_of_run_id"):
        replace(
            manifest_factory(),
            replay_source_status="interrupted",
        )
    with pytest.raises(ValueError, match="completed/interrupted"):
        replace(
            manifest_factory(),
            replay_of_run_id="RR-test-run-00000001",
            replay_source_status="cancelled",
        )


def test_plain_manifest_checksum_stable_without_lineage(manifest_factory) -> None:
    """历史兼容:非重放 manifest 的 checksum 不因新增血缘字段漂移。

    既有 #234 语义:``checksum`` 在 strategy_version 为 None 时弹出该键;
    issue #305 对 ``replay_source_status`` 采取同一先例 —— None 时弹出,
    改版前创建的 manifest 按同一输入可复现同一 manifest_checksum
    (幂等重提交不因字段新增而误判「相同身份对应不同 manifest」)。
    """
    manifest = manifest_factory()
    payload = asdict(manifest)
    # 模拟改版前(字段不存在)的 payload:弹出两个条件键后 checksum 必须一致。
    payload.pop("strategy_version", None)
    payload.pop("replay_source_status", None)
    assert stable_checksum(payload) == manifest.checksum

    # 血缘标注出现时则纳入 checksum(replay run 身份可区分)。
    replayed = replace(
        manifest,
        replay_of_run_id=manifest.run_id,
        replay_source_status="interrupted",
    )
    replayed_payload = asdict(replayed)
    replayed_payload.pop("strategy_version", None)
    assert stable_checksum(replayed_payload) == replayed.checksum
    assert replayed.checksum != manifest.checksum
