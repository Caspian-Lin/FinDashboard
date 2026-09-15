"""组合阶段拒绝保留 factor_screen 部分证据集成测试(issue #304,需 PostgreSQL)。

真实链路:screen RR(rank_top → 小买池使 ``max_risk_contribution`` 0.35
数学不可行,静态候选池 6 只入队预检放行)→ worker 执行 → 组合硬约束
REJECTED:

* run 终态保持 REJECTED / ``hard_constraint_rejected``(fail-closed 零改动);
* result JSON 顶层 ``partial=true`` + ``constraint_failure`` 失败决策定位;
* report artifact 存在且携带 ``factor_screen``(#463 前缀口径:拒绝路径
  不再全量重拉输入,screen 覆盖已拉取前缀期次,marker.warnings 具名标注
  ``factor_screen_prefix_scope``);
* 中期拒绝(前缀 >= 2 期)的 screen 照旧可作晋级证据,``source_run_status``
  显式标注;首期拒绝(前缀 1 期)低于晋级门最低期数,具名拒绝;
* rejected 无 partial 标记的 run 仍拒绝作为晋级证据。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
)
from finboard_backtest.strategy_spec import (
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    compile_registered_strategy_spec,
)
from finboard_backtest.strategy_spec.contracts import (
    FeatureGraph,
    ScreenArtifactBinding,
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
)
from finboard_mcp.tools import research_code as research_code_tools
from finboard_mcp.tools import runs as run_tools
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeArtifactRepository,
    ResearchExperimentRepository,
    ResearchRunRepository,
    session_factory,
)
from tests.integration.test_promotion_chain_closure import (
    D1,
    D2,
    FACTOR_COMMIT,
    FACTOR_NAME,
    RELEASE_ID,
    U_FACTOR,
    _make_app,
    _promotion_experiment,
    _publish_spec,
    _register_draft_artifact,
    _register_rcr_and_snapshot,
    _register_release,
    _run_validation_experiment,
    _trend_closes,
)
from tests.integration.test_research_run_signal_engine_worker import (
    SYMBOLS,
    _build_worker,
    _drain_worker,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in (
            "research_run_artifacts",
            "research_runs",
            "background_jobs",
            "research_trials",
            "research_experiments",
            "research_code_runs",
            "research_code_artifacts",
            "factor_feature_snapshots",
            "research_strategy_specs",
            "research_dataset_releases",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


def _infeasible_screen_spec(
    strategy_id: str,
    artifact_id: str,
    *,
    rank_threshold: float = 0.2,
) -> ResearchStrategySpec:
    """引用 u_ 用户因子的 single_shot screen 规格(静态候选池 6 只,
    #303 入队预检按候选池规模放行:6 >= ceil(1/0.35))。

    ``rank_threshold=0.2``:信号买头部 20% → 每期买池 2 只,风险贡献
    1/2=0.5 > 0.35 首期即执行期拒绝 —— #304 要保留证据的原始形态。
    ``rank_threshold=0.5``:期 1 买池 3 只(1/3 <= 0.35 可行);配合中期退市
    收窄候选池(included 只剩头部 2 只)期 2 买池 2 只不可行 —— #463 前缀
    口径下「中期拒绝」仍覆盖完整期次 screen 的形态。
    """
    nodes = (
        FeatureNode(
            node_id="alpha",
            label="用户 alpha",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source=U_FACTOR,
        ),
        FeatureNode(
            node_id="alpha_rank",
            label="alpha 排名",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.CROSS_SECTION_RANK,
            inputs=("alpha",),
        ),
        FeatureNode(
            node_id="alpha_score",
            label="alpha 得分",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.NEGATE,
            inputs=("alpha_rank",),
        ),
        FeatureNode(
            node_id="close",
            label="收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="ret_5",
            label="5 日收益",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=5,
        ),
        FeatureNode(
            node_id="composite",
            label="复合得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("alpha_score", "ret_5"),
            weights=(0.7, 0.3),
        ),
    )
    spec = _template(strategy_id)
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("composite",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=rank_threshold,
                        action=SignalAction.BUY,
                        rationale=(
                            f"复合得分头部 {rank_threshold:.0%} 纳入目标仓位"
                            "(小池风险贡献不可行)。"
                        ),
                    ),
                ),
                conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
            ),
            "screen_artifact_bindings": (
                ScreenArtifactBinding(
                    kind="factor",
                    name=FACTOR_NAME,
                    artifact_id=artifact_id,
                    commit=FACTOR_COMMIT,
                ),
            ),
        }
    )


def _template(strategy_id: str) -> ResearchStrategySpec:
    from finboard_backtest.strategy_spec import build_strategy_template

    return build_strategy_template(
        "multi_factor", strategy_id=strategy_id, dataset_release_ids=(RELEASE_ID,)
    )


def _worker_factory(
    engine: AsyncEngine, *, delist_from: date | None = None
) -> Any:
    closes = _trend_closes()

    def factory(manifest: Any) -> SignalEnginePipelineAdapter:
        from tests.integration.test_research_run_signal_engine_worker import (
            _StubInstrument,
            _StubProvider,
            _StubRelease,
        )

        provider = _StubProvider(
            release=_StubRelease(
                RELEASE_ID,
                tuple(
                    # 中期退市只打头部两只(SYMBOLS 得分序为 F>E>D>C>B>A,
                    # 见 _trend_closes/快照构造):期 2 剩 {A..D},rank_top 0.5
                    # 命中 D → 买池 1 只 → 风险贡献 1.0 数学不可行。
                    _StubInstrument(code=symbol, delist_date=delist_from)
                    if delist_from is not None and symbol in SYMBOLS[4:]
                    else _StubInstrument(code=symbol)
                    for symbol in SYMBOLS
                ),
                start_date=date(2023, 12, 1),
                end_date=date(2024, 1, 31),
            ),
            closes_by_symbol=closes,
        )

        def release_factory(release_id: str) -> Any:
            assert release_id == RELEASE_ID
            return provider

        async def snapshot_provider(snapshot_id: str) -> Any:
            async with session_factory(engine)() as session:
                snapshot = await FeatureSnapshotRepository(session).get(snapshot_id)
            assert snapshot is not None
            return snapshot

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,
            snapshot_provider=snapshot_provider,
        )

    return factory


async def _queue_issue304_run(
    app: Any,
    *,
    snapshot_ids: list[str],
    artifact_id: str,
    strategy_id: str,
    rank_threshold: float = 0.2,
) -> dict[str, Any]:
    spec = _infeasible_screen_spec(
        strategy_id, artifact_id, rank_threshold=rank_threshold
    )
    body = run_tools.parse_queue_payload(
        {
            "idempotency_key": f"issue304-{strategy_id}",
            "strategy_id": spec.strategy_id,
            "strategy_version": 1,
            "dataset_release_ids": [RELEASE_ID],
            "factor_snapshot_ids": snapshot_ids,
            "parameters": {},
            "code_version": "abcdef0123456789",
            "initial_capital": "200000",
            "requested_by": "integration-test",
        }
    )
    return await run_tools.enqueue_research_run(app, body)


class TestIssue304PartialScreenEvidence:
    async def test_rejected_run_keeps_factor_screen_and_promotes(
        self, engine: AsyncEngine
    ) -> None:
        """验收全链:#463 前缀口径下**中期拒绝**的 screen run 仍可作晋级证据。

        期 1 组合构建成功(买池 3 只可行),期 1/2 之间头部标的退市使期 2
        买池收窄到 2 只 → 期 2 硬约束 REJECTED;前缀捕获 = 已拉取的 2 期,
        screen 覆盖完整期次(与旧全量重拉口径同值),promote 兼容并显式
        标注来源。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            await _register_release(session, RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            snapshot_ids = [
                await _register_rcr_and_snapshot(
                    session, artifact=artifact, decision_at=D1, release_id=RELEASE_ID
                ),
                await _register_rcr_and_snapshot(
                    session, artifact=artifact, decision_at=D2, release_id=RELEASE_ID
                ),
            ]
            spec = _infeasible_screen_spec(
                "issue304_screen", artifact.artifact_id, rank_threshold=0.5
            )
            compile_registered_strategy_spec(spec)
            await _publish_spec(session, spec)
            artifact_id = artifact.artifact_id

        ack = await _queue_issue304_run(
            app,
            snapshot_ids=snapshot_ids,
            artifact_id=artifact_id,
            strategy_id="issue304_screen",
            rank_threshold=0.5,
        )
        run_id = cast(str, ack["run_id"])
        assert ack["status"] == "queued"

        # 头部标的中期退市(期 1 在市、期 2 被 exclude_delisted 剔出)
        worker = _build_worker(
            engine, _worker_factory(engine, delist_from=date(2024, 1, 10))
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            # 终态 / error_code 零漂移(#91 fail-closed 不动)
            assert row.status == "rejected", (
                f"status={row.status} error={row.error_code}/{row.error_summary}"
            )
            assert row.error_code == "hard_constraint_rejected"
            # result JSON:partial 标记 + 失败决策定位(顶层具名键)——
            # 期 1 完整构建后于期 2 拒绝
            result = row.result
            assert isinstance(result, dict)
            assert result["partial"] is True
            failure = result["constraint_failure"]
            assert isinstance(failure, dict)
            assert failure["completed_decisions"] == 1
            assert failure["failed_decision_index"] == 2
            assert failure["decision_date"] == D2.date().isoformat()
            # report artifact 存在;期 1 的 13 个决策 stage 完整落库
            artifacts = await ResearchRunRepository(session).list_artifacts(run_id)
            stages = [item.stage for item in artifacts]
            assert stages.count("report") == 1
            assert len(stages) == 13 + 1
            assert "universe" in stages
            assert "orders" in stages
            report_payload = next(
                item.payload["report"] for item in artifacts if item.stage == "report"
            )
            assert isinstance(report_payload, dict)
            assert report_payload["partial"] is True
            assert report_payload["constraint_failure"] == failure
            # #463 前缀口径:拒绝路径不再全量重拉输入 —— screen 覆盖已拉取
            # 前缀(本用例 = 2 期完整期次),marker.warnings 具名标注口径。
            prefix_warnings = [
                item
                for item in failure.get("warnings", [])
                if isinstance(item, dict) and item.get("factor_screen_prefix_scope")
            ]
            assert len(prefix_warnings) == 1
            assert prefix_warnings[0]["screen_periods"] == 2
            screen = report_payload["factor_screen"]
            assert isinstance(screen, dict)
            assert screen["n_periods"] == 2
            metrics = screen["factors"][U_FACTOR]
            assert metrics["rank_ic"] is not None
            # result.factor_screen 与 report 同源
            assert result["factor_screen"] == screen

        # OOS 实验 → validated_oos
        experiment = _promotion_experiment(
            artifact_id=artifact_id,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        job_result = await _run_validation_experiment(
            engine, experiment_id=experiment.experiment_id
        )
        assert job_result.status == "succeeded", (
            f"{job_result.error_code}: {job_result.error_summary}"
        )

        # promote:rejected+partial 的 screen run 可作证据(#304)
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id=run_id,
        )
        assert env.status == "ok", env.error
        assert env.data["status"] == "active"
        assert env.data["promotion_status"] == "passed"

        # 证据显式标注来源 run 状态
        async with session_factory(engine)() as session:
            saved = await ResearchCodeArtifactRepository(session).get(artifact_id)
            assert saved is not None
            evidence = saved.promotion_evidence
            assert isinstance(evidence, dict)
            execution = evidence["execution"]
            assert isinstance(execution, dict)
            assert execution["source_run_status"] == "rejected"
            assert execution["source_run_partial"] is True
            assert execution["run_id"] == run_id

    async def test_first_period_rejection_screen_below_promotion_minimum(
        self, engine: AsyncEngine
    ) -> None:
        """#463 前缀口径的诚实降级:首期即拒绝的 run 只覆盖 1 期 screen
        (旧全量重拉口径会补到 2 期),partial 报告照常保留,但晋级门按
        最低期数(2 期)具名拒绝 —— 不再用重拉伪造证据期数。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            await _register_release(session, RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            snapshot_ids = [
                await _register_rcr_and_snapshot(
                    session, artifact=artifact, decision_at=D1, release_id=RELEASE_ID
                ),
                await _register_rcr_and_snapshot(
                    session, artifact=artifact, decision_at=D2, release_id=RELEASE_ID
                ),
            ]
            spec = _infeasible_screen_spec("issue304_first", artifact.artifact_id)
            compile_registered_strategy_spec(spec)
            await _publish_spec(session, spec)
            artifact_id = artifact.artifact_id

        ack = await _queue_issue304_run(
            app,
            snapshot_ids=snapshot_ids,
            artifact_id=artifact_id,
            strategy_id="issue304_first",
        )
        run_id = cast(str, ack["run_id"])
        assert ack["status"] == "queued"

        worker = _build_worker(engine, _worker_factory(engine))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            assert row.status == "rejected", (
                f"status={row.status} error={row.error_code}/{row.error_summary}"
            )
            assert row.error_code == "hard_constraint_rejected"
            result = row.result
            assert isinstance(result, dict)
            assert result["partial"] is True
            failure = result["constraint_failure"]
            assert isinstance(failure, dict)
            assert failure["completed_decisions"] == 0
            assert failure["failed_decision_index"] == 1
            assert failure["decision_date"] == D1.date().isoformat()
            # 首期即失败:decision artifact 为零,仅 report
            artifacts = await ResearchRunRepository(session).list_artifacts(run_id)
            assert [item.stage for item in artifacts] == ["report"]
            report_payload = artifacts[0].payload["report"]
            assert isinstance(report_payload, dict)
            assert report_payload["partial"] is True
            prefix_warnings = [
                item
                for item in failure.get("warnings", [])
                if isinstance(item, dict) and item.get("factor_screen_prefix_scope")
            ]
            assert len(prefix_warnings) == 1
            assert prefix_warnings[0]["screen_periods"] == 1
            screen = report_payload["factor_screen"]
            assert isinstance(screen, dict)
            assert screen["n_periods"] == 1

        # OOS 实验 → validated_oos(与主用例同流程)
        experiment = _promotion_experiment(
            artifact_id=artifact_id,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        job_result = await _run_validation_experiment(
            engine, experiment_id=experiment.experiment_id
        )
        assert job_result.status == "succeeded", (
            f"{job_result.error_code}: {job_result.error_summary}"
        )

        # promote:1 期 screen 低于晋级门最低期数 → 具名拒绝(诚实降级)
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id=run_id,
        )
        assert env.status == "error", env
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "n_periods_below_minimum" in env.error.message

    async def test_rejected_without_partial_marker_refused_as_evidence(
        self, engine: AsyncEngine
    ) -> None:
        """rejected 但无 partial 标记的 run 仍拒绝作为晋级证据(无证据不放宽)。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            artifact = await _register_draft_artifact(
                session, kind="factor", name="negative_factor", commit="e" * 40
            )
            await session.commit()
            artifact_id = artifact.artifact_id
            experiment = _promotion_experiment(
                artifact_id=artifact_id,
                artifact_name="negative_factor",
                kind="factor",
                commit="e" * 40,
            )
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()

            # 直接落一条 rejected 且无 partial 标记的 run(无 screen 证据)
            repo = ResearchRunRepository(session)
            await repo.create_or_get(
                run_id="RR-issue304negative01",
                idempotency_key="issue304-negative",
                replay_of_run_id=None,
                strategy_id="negative_spec",
                strategy_kind="multi_factor",
                status="rejected",
                schema_version="v2",
                manifest_checksum="a" * 64,
                manifest={"strategy_spec": {"feature_graph": {"nodes": []}}},
                requested_by="integration-test",
            )
            row = await repo.get("RR-issue304negative01")
            assert row is not None
            row.result = {"factor_screen": {"n_periods": 2}}
            row.result_checksum = "b" * 64
            row.error_code = "hard_constraint_rejected"
            await session.commit()

        # run_tool 把 McpToolError 映射为 error envelope(与链路测试同风格断言)
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id="RR-issue304negative01",
        )
        assert env.status == "error", env
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "未完成" in env.error.message

    async def test_enqueue_gate_still_passes_for_runtime_only_infeasibility(
        self, engine: AsyncEngine
    ) -> None:
        """对照:#303 入队预检按静态候选池放行(6 只 >= ceil(1/0.35)),
        执行期拒绝合法存在 —— 这是 #304 的存在前提。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            await _register_release(session, RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            snapshot_ids = [
                await _register_rcr_and_snapshot(
                    session, artifact=artifact, decision_at=D1, release_id=RELEASE_ID
                ),
            ]
            spec = _infeasible_screen_spec("issue304_gate", artifact.artifact_id)
            compile_registered_strategy_spec(spec)
            await _publish_spec(session, spec)

        ack = await _queue_issue304_run(
            app,
            snapshot_ids=snapshot_ids,
            artifact_id=artifact.artifact_id,
            strategy_id="issue304_gate",
        )
        assert ack["status"] == "queued", ack

        # 清理:入队即止,不执行
        async with session_factory(engine)() as session:
            await session.execute(
                text("DELETE FROM background_jobs WHERE queue = 'research'")
            )
            await session.commit()


__all__ = ["TestIssue304PartialScreenEvidence"]
