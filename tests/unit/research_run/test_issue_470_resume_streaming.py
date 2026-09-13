"""#470 前半场:断点续算读回流式化 + 收尾瘦指纹的等值锁定。

- 流式前缀(``iter_completed_decision_prefix``)与列表版
  (``completed_decision_prefix``)在完整 / 缺 stage 截断两种形态下产出
  逐值一致;
- ``iter_artifacts`` / ``list_artifact_digests`` 与 ``list_artifacts``
  投影一致(行序 = sequence 升序);
- result_checksum 的输入从「全量 artifact 行」换成瘦指纹后逐字节不变。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from finboard_backtest.research_run import (
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchRunStage,
    completed_decision_prefix,
    iter_completed_decision_prefix,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.contracts import DecisionBundle
from finboard_backtest.research_run.runner import ResearchRunCoordinator

from .test_issue_314_checkpoint_resume import (
    _evolving_decisions,
    _multi_report,
)

pytestmark = pytest.mark.asyncio


async def _completed_run(
    manifest_factory, decision_factory, count: int = 3
) -> tuple[InMemoryResearchRunStore, str]:
    manifest = manifest_factory()
    decisions = _evolving_decisions(decision_factory, manifest, count)
    store = InMemoryResearchRunStore()
    record = await ResearchRunCoordinator(store).execute(
        manifest,
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=decisions,
            report=_multi_report("ma_cross", decisions),
        ),
    )
    assert record.status.value == "completed"
    return store, manifest.run_id


async def _streamed_prefix(
    store: InMemoryResearchRunStore, run_id: str
) -> list[DecisionBundle]:
    return [
        bundle
        async for bundle in iter_completed_decision_prefix(
            run_id, store.iter_artifacts(run_id)
        )
    ]


async def _result_checksum_input(store, run_id: str) -> list[dict[str, str | None]]:
    digests = await store.list_artifact_digests(run_id)
    return [
        {"stage": item.stage.value, "decision_id": item.decision_id, "checksum": item.checksum}
        for item in digests
    ]


async def test_streaming_prefix_equals_list_version(
    manifest_factory, decision_factory
) -> None:
    store, run_id = await _completed_run(manifest_factory, decision_factory)
    artifacts = await store.list_artifacts(run_id)
    listed = completed_decision_prefix(run_id, artifacts)
    streamed = await _streamed_prefix(store, run_id)
    assert len(listed) == 3
    assert streamed == listed


async def test_streaming_prefix_truncates_with_list_version(
    manifest_factory, decision_factory
) -> None:
    store, run_id = await _completed_run(manifest_factory, decision_factory)
    # 删掉第 2 个决策(index=1)的一个 stage 行 → 两种版本都应在 index=1 截断
    victim = next(
        artifact_id
        for artifact_id, artifact in store._artifacts[run_id].items()
        if artifact_id.endswith(":signals") and ":00000001:" in artifact_id
    )
    del store._artifacts[run_id][victim]

    artifacts = await store.list_artifacts(run_id)
    listed = completed_decision_prefix(run_id, artifacts)
    streamed = await _streamed_prefix(store, run_id)
    assert len(listed) == 1
    assert streamed == listed


async def test_digests_match_full_listing_projection(
    manifest_factory, decision_factory
) -> None:
    store, run_id = await _completed_run(manifest_factory, decision_factory)
    artifacts = await store.list_artifacts(run_id)
    digests = await store.list_artifact_digests(run_id)
    assert digests == [
        type(digests[0])(
            stage=item.stage, decision_id=item.decision_id, checksum=item.checksum
        )
        for item in artifacts
    ]
    # 行序 = sequence 升序(与 list_artifacts 一致)
    assert [d.stage for d in digests][:26] == [a.stage for a in artifacts][:26]


async def test_result_checksum_digest_path_byte_identical(
    manifest_factory, decision_factory
) -> None:
    store, run_id = await _completed_run(manifest_factory, decision_factory)
    artifacts = await store.list_artifacts(run_id)
    legacy = stable_checksum(
        [
            {
                "stage": item.stage.value,
                "decision_id": item.decision_id,
                "checksum": item.checksum,
            }
            for item in artifacts
        ]
    )
    assert stable_checksum(await _result_checksum_input(store, run_id)) == legacy
    # report 行(非决策级)同样入指纹,不因流式/瘦读丢行
    assert any(item.stage is ResearchRunStage.REPORT for item in artifacts)
    digests = await store.list_artifact_digests(run_id)
    assert len(digests) == len(artifacts)


async def test_iter_artifacts_order_matches_list(manifest_factory, decision_factory) -> None:
    store, run_id = await _completed_run(manifest_factory, decision_factory)
    listed = await store.list_artifacts(run_id)

    async def _collect(rows: AsyncIterator) -> list[int]:
        return [item.sequence async for item in rows]

    assert await _collect(store.iter_artifacts(run_id)) == [
        item.sequence for item in listed
    ]
    # to_json_value 往返可用(流式行是完整契约对象)
    streamed_first = await store.iter_artifacts(run_id).__anext__()
    assert isinstance(to_json_value(streamed_first), dict)


async def test_cross_section_capture_gate(manifest_factory) -> None:
    """#470 前半场:无用户因子 run 的投影瘦身;u_ 观测兜底回退全量。"""

    from datetime import UTC, datetime

    from finboard_backtest.portfolio import AssetLotInfo
    from finboard_backtest.research_run.contracts import (
        FeatureValue,
        NormalizedSignal,
        UniverseCandidate,
    )
    from finboard_backtest.research_run.factor_screen import (
        _period_cross_section,
        manifest_declares_user_factors,
    )
    from finboard_backtest.research_run.portfolio_pipeline import PortfolioDecisionInput

    def _input(feature_ids: tuple[str, ...]) -> PortfolioDecisionInput:
        decision_at = datetime(2024, 1, 2, 15, tzinfo=UTC)
        return PortfolioDecisionInput(
            business_date=decision_at.date(),
            decision_at=decision_at,
            execution_at=datetime(2024, 1, 3, 9, 30, tzinfo=UTC),
            candidates=(
                UniverseCandidate(
                    symbol="A.SH",
                    included=True,
                    reasons=("t",),
                    asset_class="equity",
                    market="a_share",
                ),
            ),
            features=tuple(
                FeatureValue(
                    symbol="A.SH",
                    feature_id=name,
                    value=1.0,
                    source_artifact_ids=("release-v1",),
                    available_at=decision_at,
                )
                for name in feature_ids
            ),
            signals=(
                NormalizedSignal(
                    symbol="A.SH",
                    score=1.0,
                    action="buy",
                    rule_id="t",
                    rationale="t",
                ),
            ),
            prices={"A.SH": 10.0},
            execution_prices={"A.SH": 10.0},
            lot_info={"A.SH": AssetLotInfo(code="A.SH", lot_size=100)},
            input_artifact_ids=("release-v1",),
            covariance=None,
        )

    plain = _input(("momentum", "pb"))
    gated = _period_cross_section(plain, include_series=False)
    assert gated["others"] == {}
    assert gated["prices"] == {}
    assert gated["user"] == {}
    assert gated["markets"] == {"A.SH": "a_share"}

    full = _period_cross_section(plain, include_series=True)
    assert set(full["others"]) == {"momentum", "pb"}
    assert full["prices"] == {"A.SH": 10.0}

    # 兜底:开关关闭但当期出现 u_ 观测 → 仍构建全量投影(screen 结果与开关无关)
    with_user = _input(("momentum", "u_sandbox1"))
    fallback = _period_cross_section(with_user, include_series=False)
    assert set(fallback["user"]) == {"u_sandbox1"}
    assert set(fallback["others"]) == {"momentum"}
    assert fallback["prices"] == {"A.SH": 10.0}

    # manifest 判定:典型无 u_ 声明 → False;parameters 带引用 → True
    assert manifest_declares_user_factors(manifest_factory()) is False
    from dataclasses import replace as _replace

    referenced = _replace(
        manifest_factory(),
        parameters={"screen_factor": "u_sandbox_alpha"},
    )
    assert manifest_declares_user_factors(referenced) is True
