"""研究代码晋级闭环补口集成测试(issue #233 + #234,需 PostgreSQL)。

#233:``kind=validation_experiment`` 执行器编排 —— IS → walk-forward →
一次性揭盲 → validated_oos;失败映射 / 预算 / 重复揭盲拒绝;MCP
``finboard_validation_experiment_run`` 入队预检与幂等。
#234:screen 用途 draft 产物显式绑定通道 —— 编译期放行、入队实绑校验、
manifest 冻结、fake 全链路(draft artifact → RCR 快照 → screen RR →
实验 → promote → active+passed → 消费门放行);非 screen 引用门不变。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace as dc_replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
    JobResult,
)
from finboard_backtest.background_jobs.executors.validation_experiment import (
    ValidationExperimentExecutor,
)
from finboard_backtest.research_code import (
    active_user_factor_names,
    resolve_screen_bindings,
)
from finboard_backtest.research_code.promotion import is_promoted_artifact
from finboard_backtest.research_run import ResearchRunStatus
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
)
from finboard_backtest.research_sandbox.factor_publish import (
    build_factor_snapshot,
    check_output_quality,
)
from finboard_backtest.result import BacktestResult
from finboard_backtest.strategy_spec import (
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    build_strategy_template,
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
    StrategyCodeArtifactRef,
)
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    increment_trials_used,
    new_experiment,
    transition_status,
)
from finboard_backtest.validation.runner import ValidationRunner
from finboard_data.releases import (
    RELEASE_FIELDS,
    AssetCapability,
    CapabilityStatus,
    ReleasedInstrument,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import research_code as research_code_tools
from finboard_mcp.tools import runs as run_tools
from finboard_mcp.tools import validation_experiments as ve_tools
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeArtifactRepository,
    ResearchCodeRunRepository,
    ResearchDatasetReleaseRepository,
    ResearchExperimentRepository,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
    ResearchTrialRepository,
    session_factory,
)
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)
from tests.integration.test_research_run_signal_engine_worker import (
    SYMBOLS,
    _build_worker,
    _drain_worker,
)

pytestmark = pytest.mark.asyncio

FACTOR_NAME = "agent_alpha"
U_FACTOR = f"u_{FACTOR_NAME}"
STRATEGY_NAME = "mean_reversion_zscore"
FACTOR_COMMIT = "a" * 40
STRATEGY_COMMIT = "b" * 40
RELEASE_ID = "screen-chain-release-v1"
MULTI_RELEASE_ID = "frozen-release-multi"
D1 = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
D2 = datetime(2024, 1, 16, 15, 0, tzinfo=UTC)


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


def _make_app(engine: AsyncEngine, *, sandbox_enabled: bool = False) -> McpAppContext:
    from finboard_app.config import Settings
    from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager

    return McpAppContext(
        settings=Settings(research_sandbox_enabled=sandbox_enabled),
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


# ---------------------------------------------------------------------------
# 夹具:数据发布 / draft 产物 / RCR 快照 / screen 规格
# ---------------------------------------------------------------------------


def _instrument(code: str) -> ReleasedInstrument:
    from finboard_data.releases import default_execution_metadata

    return ReleasedInstrument(
        code=code,
        name=code,
        name_history=(),
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        artifact_path=f"bars/{code}_D1_qfq.parquet",
        artifact_checksum="a" * 64,
        artifact_size=1024,
        row_count=40,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        expected_sessions=40,
        missing_sessions=0,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("1.0"),
        category="stock",
        ready=True,
        list_date=date(2020, 1, 1),
        delist_date=None,
        industry=None,
    )


def _bars_release(release_id: str) -> Any:
    from finboard_data.releases import ResearchDatasetRelease

    return ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-bars",
        source="screen-chain",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=tuple(_instrument(code) for code in SYMBOLS),
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(SYMBOLS),
                ready_count=len(SYMBOLS),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=release_id,
        release_checksum="c" * 64,
    )


async def _register_release(session: AsyncSession, release_id: str) -> None:
    await ResearchDatasetReleaseRepository(session).publish(_bars_release(release_id))
    await session.commit()


async def _register_draft_artifact(
    session: AsyncSession, *, kind: str, name: str, commit: str
) -> Any:
    return await ResearchCodeArtifactRepository(session).register(
        kind=kind,
        name=name,
        commit=commit,
        path=f"{kind}s/{name}",
        checksum="d" * 16,
        created_by="integration-test",
        status="draft",
    )


def _trend_closes() -> dict[str, dict[date, Decimal]]:
    """漂移随标的序号递减 + 阻尼震荡的确定性价格轨迹。

    漂移保证 forward return 排序稳定(IC 门可控);震荡让日收益率方差
    非退化(组合协方差 fail-closed 门不误伤)。
    """
    import math

    trading_days: list[date] = []
    day = date(2023, 12, 1)
    while day <= date(2024, 1, 31):
        if day.weekday() < 5:
            trading_days.append(day)
        day += timedelta(days=1)
    closes: dict[str, dict[date, Decimal]] = {}
    for index, symbol in enumerate(SYMBOLS):
        drift = 0.011 - index * 0.002  # A 最强 → F 最弱
        price = 10.0
        closes[symbol] = {}
        for day_index, day in enumerate(trading_days):
            price *= 1.0 + drift
            wiggle = 1.0 + 0.005 * math.sin(0.7 * day_index + index * 0.9)
            closes[symbol][day] = Decimal(str(round(price * wiggle, 4)))
    return closes


def _baseline_values() -> dict[str, float]:
    """对照特征(momentum)观测:与 u_ 因子 spearman 相关约 0.71,低于 0.8 门。

    #311:晋级相关性门 fail-closed,screen 证据必须含非空 correlation,
    横截面需至少一个非用户因子特征作为 baseline;这里给 fake 沙箱快照
    附带一组排序部分一致的对照观测(6 标的,秩 d² 合计 10)。
    """
    return {
        "A.SH": 3.0,
        "B.SH": 4.0,
        "C.SH": 5.0,
        "D.SH": 1.0,
        "E.SH": 2.0,
        "F.SH": 0.5,
    }


async def _register_rcr_and_snapshot(
    session: AsyncSession,
    *,
    artifact: Any,
    decision_at: datetime,
    release_id: str,
) -> str:
    """登记成功 RCR + 产出 u_ 快照(fake 沙箱执行产物,#217 形状)。

    #311:快照额外附带一组 momentum 对照观测,使 screen run 横截面
    存在非用户因子 baseline、correlation 真实可算 —— 修复前空
    correlation 凭门洞即可晋级,修复后链路须产出真实相关性证据。
    """
    from finboard_data.factor_lab import FeatureObservation, build_feature_snapshot

    run_repo = ResearchCodeRunRepository(session)
    rcr = await run_repo.create(
        kind="factor",
        name=artifact.name,
        commit=artifact.commit,
        code_checksum="deadbeef",
        dataset_release_ids=[release_id],
        dataset_release_checksums={release_id: "c" * 64},
        decision_at=decision_at,
        image="finboard-research-sandbox:test",
        image_digest="sha256:" + "9" * 64,
        artifact_dir="data_cache/research_sandbox/test",
        artifact_id=artifact.artifact_id,
    )
    scores = {
        symbol: float(len(SYMBOLS) - index) for index, symbol in enumerate(SYMBOLS)
    }
    quality = check_output_quality(
        scores, universe_size=len(SYMBOLS), max_nan_ratio=0.5, min_coverage=0.5
    )
    assert quality.passed
    snapshot = build_factor_snapshot(
        factor_artifact_name=artifact.name,
        run_id=rcr.run_id,
        decision_at=decision_at,
        commit=artifact.commit,
        mount_manifest_checksum="m" * 64,
        scores=scores,
        quality=quality,
    )
    observations = (
        *snapshot.observations,
        *(
            FeatureObservation(
                symbol=symbol,
                feature_name="momentum",
                value=value,
                observed_at=decision_at,
                available_at=decision_at,
                source="research_code_run",
                source_version=artifact.commit,
            )
            for symbol, value in sorted(_baseline_values().items())
        ),
    )
    snapshot = build_feature_snapshot(
        dataset_release_id=None,
        dataset_release_checksum="m" * 64,
        decision_at=decision_at,
        code_version=artifact.commit,
        observations=observations,
        source_run_id=rcr.run_id,
        issues=snapshot.issues,
    )
    await FeatureSnapshotRepository(session).publish(snapshot)
    await run_repo.mark_terminal(
        rcr.run_id,
        status="succeeded",
        exit_code=0,
        scores_checksum="s" * 64,
        output_snapshot_id=snapshot.snapshot_id,
    )
    return snapshot.snapshot_id


def _screen_factor_spec(strategy_id: str, artifact_id: str) -> ResearchStrategySpec:
    """引用 u_ 用户因子 + close 派生收益的 single_shot screen 规格(#234 绑定)。"""
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
    spec = build_strategy_template(
        "multi_factor",
        strategy_id=strategy_id,
        dataset_release_ids=(RELEASE_ID,),
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("composite",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="复合得分前 50% 纳入目标仓位。",
                    ),
                    SignalRule(
                        rule_id="bottom_score_sell",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_BOTTOM,
                        threshold=0.5,
                        action=SignalAction.SELL,
                        priority=10,
                        rationale="复合得分落入后 50% 时退出。",
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


async def _publish_spec(session: AsyncSession, spec: ResearchStrategySpec) -> None:
    """编译(绑定放行)+ 登记 draft + publish(与 REST 语义一致)。"""
    plan = compile_registered_strategy_spec(spec)
    repo = ResearchStrategySpecRepository(session)
    await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=plan.checksum,
        payload=spec.canonical_payload(),
    )
    await repo.publish(spec.strategy_id, 1, expected_version=1)
    await session.commit()


async def _queue_factor_screen_run(
    app: McpAppContext, *, snapshot_ids: list[str], suffix: str, artifact_id: str
) -> dict[str, Any]:
    spec = _screen_factor_spec(f"screen_factor_{suffix}", artifact_id)
    body = run_tools.parse_queue_payload(
        {
            "idempotency_key": f"screen-chain-{suffix}",
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


def _promotion_experiment(
    *,
    artifact_id: str,
    artifact_name: str,
    kind: str,
    commit: str,
) -> Any:
    """绑定 artifact 的 #57 实验(宽松阈值,聚焦编排而非统计数学)。"""
    return new_experiment(
        hypothesis="研究代码晋级闭环集成验证假设(占位,长度足够)",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version="v2",
            dataset_versions={"research_dataset_release": RELEASE_ID},
            selection_config={},
            strategy_kind=kind,
            code_artifact_id=artifact_id,
            code_artifact_name=artifact_name,
            code_kind=kind,
            code_commit=commit,
        ),
        plan=ValidationPlan(
            mode=ValidationMode.ROLLING,
            train_start=date(2022, 1, 1),
            train_end=date(2022, 12, 31),
            validation_start=date(2023, 1, 1),
            validation_end=date(2023, 6, 30),
            test_start=date(2023, 7, 1),
            test_end=date(2023, 12, 31),
            train_window_days=126,
            test_window_days=21,
            step_days=21,
            trial_budget=4,
        ),
        thresholds=AcceptanceThresholds(
            min_in_sample_sharpe=0.1,
            min_oos_sharpe=-10.0,
            max_oos_drawdown=1.0,
            min_oos_calmar=-10.0,
            min_oos_information_ratio=-10.0,
            min_pbo_pass=False,
            min_deflated_sharpe=-10.0,
            min_probabilistic_sharpe=0.0,
        ),
        robustness=RobustnessPlan(stress_phases=()),
        strategy_params_space={"window": [5, 10]},
    )


def _fake_runner_factory(calls: list[tuple[date, date]]) -> Any:
    """合成 trial runner:上涨权益曲线(IS/OOS/揭盲门全过)。"""

    async def factory(session: AsyncSession, experiment: Any) -> Any:
        del session, experiment

        async def runner(
            *,
            start: date,
            end: date,
            params: dict[str, object],
            config_overrides: dict[str, object] | None = None,
        ) -> BacktestResult:
            del params, config_overrides
            calls.append((start, end))
            # 稳定上涨 + 单日 -3% 回撤:夏普为正且 calmar 有限(Infinity 无法
            # 落 JSON 列),聚焦编排而非统计数学。
            equity: list[tuple[date, Decimal]] = []
            level = 100.0
            for index in range(20):
                if index == 10:
                    level *= 0.97
                else:
                    level *= 1.01
                equity.append(
                    (start + timedelta(days=index), Decimal(str(round(level, 4))))
                )
            return BacktestResult(equity_curve=equity)

        return runner

    return factory


def _ragged_runner_factory() -> Any:
    """不等长权益曲线 runner(#244 回归):模拟真实交易日与近似切窗的点数差。

    旧实现的 PBO 矩阵以顺序窗口序列为行,各窗口点数不等直接触发 CSCV
    严格等长断言;修复后矩阵取自 IS 竞争 trial,窗口不等长不再致命。
    """

    async def factory(session: AsyncSession, experiment: Any) -> Any:
        del session, experiment
        state = {"calls": 0}

        async def runner(
            *,
            start: date,
            end: date,
            params: dict[str, object],
            config_overrides: dict[str, object] | None = None,
        ) -> BacktestResult:
            del params, config_overrides, end
            state["calls"] += 1
            n_points = 20 - ((state["calls"] - 1) % 8)
            equity: list[tuple[date, Decimal]] = []
            level = 100.0
            for index in range(n_points):
                # 单日 -3% 回撤:避免 mdd=0 → calmar=Infinity(无法落 JSON 列)
                level *= 0.97 if index == 10 else 1.01
                equity.append(
                    (start + timedelta(days=index), Decimal(str(round(level, 4))))
                )
            return BacktestResult(equity_curve=equity)

        return runner

    return factory


async def _run_validation_experiment(
    engine: AsyncEngine,
    *,
    experiment_id: str,
    calls: list[tuple[date, date]] | None = None,
) -> JobResult:
    tracked: list[tuple[date, date]] = []
    executor = ValidationExperimentExecutor(
        session_maker=session_factory(engine),
        runner_factory=_fake_runner_factory(tracked),
    )
    result = await executor.execute(
        JobRecord(
            job_id="BJ-test",
            kind="validation_experiment",
            queue="research",
            payload={"experiment_id": experiment_id},
            attempt=1,
            max_attempts=1,
            requested_by="integration-test",
        ),
        _noop_progress,
    )
    if calls is not None:
        calls.extend(tracked)
    return result


async def _noop_progress(done: int, total: int | None, phase: str | None) -> None:
    del done, total, phase


# ---------------------------------------------------------------------------
# #234:实绑门控(REST+MCP 共用函数)
# ---------------------------------------------------------------------------


class TestScreenBindingGates:
    async def test_resolve_errors_and_happy_path(self, engine: AsyncEngine) -> None:
        async with session_factory(engine)() as session:
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            spec = _screen_factor_spec("screen_factor_gate", artifact.artifact_id)

            # 正对照:实绑通过,u_ 名单解析
            resolution, error = await resolve_screen_bindings(session, spec=spec)
            assert error is None
            assert resolution is not None
            assert U_FACTOR in resolution.user_factor_names
            assert resolution.bindings[0]["artifact_id"] == artifact.artifact_id

            # 退化:无绑定规格 → (None, None),行为与既有门一致
            plain = spec.model_copy(update={"screen_artifact_bindings": ()})
            resolution2, error2 = await resolve_screen_bindings(session, spec=plain)
            assert resolution2 is None
            assert error2 is None

            # retired 拒绝
            retired = await ResearchCodeArtifactRepository(session).register(
                kind="factor",
                name="retired_factor",
                commit="e" * 40,
                path="factors/retired_factor",
                checksum="e" * 16,
                created_by="test",
                status="active",
            )
            await session.commit()
            await ResearchCodeArtifactRepository(session).retire(retired.artifact_id)
            await session.commit()
            bound_retired = plain.model_copy(
                update={
                    "screen_artifact_bindings": (
                        ScreenArtifactBinding(
                            kind="factor",
                            name="retired_factor",
                            artifact_id=retired.artifact_id,
                        ),
                    )
                }
            )
            _, error3 = await resolve_screen_bindings(session, spec=bound_retired)
            assert error3 is not None
            assert "retired" in error3

            # 不存在
            bad_id = plain.model_copy(
                update={
                    "screen_artifact_bindings": (
                        ScreenArtifactBinding(
                            kind="factor",
                            name=FACTOR_NAME,
                            artifact_id="RC-" + "0" * 24,
                        ),
                    )
                }
            )
            _, error4 = await resolve_screen_bindings(session, spec=bad_id)
            assert error4 is not None
            assert "不存在" in error4

            # name 不一致
            bad_name = plain.model_copy(
                update={
                    "screen_artifact_bindings": (
                        ScreenArtifactBinding(
                            kind="factor",
                            name="other_name",
                            artifact_id=artifact.artifact_id,
                        ),
                    )
                }
            )
            _, error5 = await resolve_screen_bindings(session, spec=bad_name)
            assert error5 is not None
            assert "不一致" in error5

            # commit 不一致
            bad_commit = plain.model_copy(
                update={
                    "screen_artifact_bindings": (
                        ScreenArtifactBinding(
                            kind="factor",
                            name=FACTOR_NAME,
                            artifact_id=artifact.artifact_id,
                            commit="f" * 40,
                        ),
                    )
                }
            )
            _, error6 = await resolve_screen_bindings(session, spec=bad_commit)
            assert error6 is not None
            assert "commit" in error6

    async def test_non_screen_reference_still_rejected(self, engine: AsyncEngine) -> None:
        """验收:非 screen 引用门行为不变 —— 普通运行引用 draft 因子入队秒级拒绝。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            await _register_release(session, RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            snapshot_id = await _register_rcr_and_snapshot(
                session, artifact=artifact, decision_at=D1, release_id=RELEASE_ID
            )
            # 普通规格(无绑定)也能登记 —— 名单外因子在编译期会被拒,
            # 这里直接绕过编译登记(仅测试语义),验证入队门仍拒绝。
            spec = _screen_factor_spec("screen_factor_plain", artifact.artifact_id)
            spec = spec.model_copy(update={"screen_artifact_bindings": ()})
            plan = compile_registered_strategy_spec(
                spec, user_factor_sources=frozenset({U_FACTOR})
            )
            repo = ResearchStrategySpecRepository(session)
            await repo.create_draft(
                strategy_id=spec.strategy_id,
                schema_version=spec.schema_version,
                name=spec.name,
                strategy_kind=spec.strategy_kind,
                checksum=plan.checksum,
                payload=spec.canonical_payload(),
            )
            await repo.publish(spec.strategy_id, 1, expected_version=1)
            await session.commit()

        body = run_tools.parse_queue_payload(
            {
                "idempotency_key": "screen-chain-plain",
                "strategy_id": "screen_factor_plain",
                "strategy_version": 1,
                "dataset_release_ids": [RELEASE_ID],
                "factor_snapshot_ids": [snapshot_id],
                "parameters": {},
                "code_version": "abcdef0123456789",
                "initial_capital": "200000",
                "requested_by": "integration-test",
            }
        )
        from finboard_mcp.execution import McpToolError

        with pytest.raises(McpToolError) as exc_info:
            await run_tools.enqueue_research_run(app, body)
        assert exc_info.value.kind == "invalid_argument"
        assert "u_agent_alpha" in exc_info.value.message


# ---------------------------------------------------------------------------
# #234:factor 通道首次晋级全链路
# ---------------------------------------------------------------------------


class TestFactorScreenPromotionChain:
    async def test_full_chain_submit_to_promotion(self, engine: AsyncEngine) -> None:
        """draft artifact → RCR 快照 → screen RR(factor_screen)→ 实验 → promote。"""
        app = _make_app(engine)
        async with session_factory(engine)() as session:
            await _register_release(session, RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="factor", name=FACTOR_NAME, commit=FACTOR_COMMIT
            )
            await session.commit()
            snapshot_ids = [
                await _register_rcr_and_snapshot(
                    session,
                    artifact=artifact,
                    decision_at=D1,
                    release_id=RELEASE_ID,
                ),
                await _register_rcr_and_snapshot(
                    session,
                    artifact=artifact,
                    decision_at=D2,
                    release_id=RELEASE_ID,
                ),
            ]
            spec = _screen_factor_spec("screen_factor_full", artifact.artifact_id)
            # 编译期绑定放行(active 名单为空)
            compile_registered_strategy_spec(spec)
            await _publish_spec(session, spec)
            artifact_id = artifact.artifact_id

        ack = await _queue_factor_screen_run(
            app, snapshot_ids=snapshot_ids, suffix="full", artifact_id=artifact_id
        )
        run_id = cast(str, ack["run_id"])
        assert ack["status"] == "queued"

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
                    tuple(_StubInstrument(code=symbol) for symbol in SYMBOLS),
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
                    snapshot = await FeatureSnapshotRepository(session).get(
                        snapshot_id
                    )
                assert snapshot is not None
                return snapshot

            return SignalEnginePipelineAdapter(
                manifest=manifest,
                release_provider_factory=release_factory,
                snapshot_provider=snapshot_provider,
            )

        worker = _build_worker(engine, factory)
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            assert row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={row.status} error={row.error_code}/{row.error_summary}"
            )
            result = row.result or {}
            factor_screen = result.get("factor_screen")
            assert isinstance(factor_screen, dict)
            assert factor_screen["n_periods"] == 2
            metrics = factor_screen["factors"][U_FACTOR]
            assert metrics["rank_ic"] is not None
            assert abs(metrics["rank_ic"]) >= 0.02
            assert metrics["average_turnover"] is not None
            assert metrics["average_turnover"] <= 0.8
            # #311:横截面含 momentum baseline,correlation 真实可算,
            # 不再凭空证据静默过门。
            assert set(factor_screen["correlation_baselines"]) == {"momentum"}
            assert metrics["correlation"]
            assert all(abs(value) <= 0.8 for value in metrics["correlation"].values())

        # OOS 门(#233 执行器)→ validated_oos
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
        async with session_factory(engine)() as session:
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.status is ExperimentStatus.VALIDATED_OOS
            assert saved.final_test_unsealed is True

        # promote → active+passed(四向绑定校验 + 快照 source_run_id 追溯)
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id=run_id,
        )
        assert env.status == "ok", env.error
        assert env.data["status"] == "active"
        assert env.data["promotion_status"] == "passed"
        # #311:gate 逐项结果(含相关性 pass)随 promote 响应可见
        evidence = cast(dict[str, Any], env.data["promotion_evidence"])
        assert cast(dict[str, Any], evidence["gates"])["screen_checks"] == {
            "n_periods": "pass",
            "rank_ic": "pass",
            "average_turnover": "pass",
            "correlation": "pass",
        }

        # 消费门放行:普通(无绑定)规格引用 u_ 因子可编译
        async with session_factory(engine)() as session:
            names = await active_user_factor_names(session)
        assert U_FACTOR in names
        plain_payload = _screen_factor_spec(
            "screen_factor_consume", artifact_id
        ).canonical_payload()
        plain_payload.pop("screen_artifact_bindings")
        plain_spec = ResearchStrategySpec.model_validate(plain_payload)
        plan = compile_registered_strategy_spec(
            plain_spec, user_factor_sources=names
        )
        assert U_FACTOR in plan.required_factor_sources


# ---------------------------------------------------------------------------
# #233:验证实验执行器编排
# ---------------------------------------------------------------------------


class TestValidationExperimentExecutor:
    async def test_in_sample_gate_failure_maps_to_rejected(
        self, engine: AsyncEngine
    ) -> None:
        """全部 trial 被 IS 门拒绝 → REJECTED,job failed 带可读原因。"""
        experiment = dc_replace(
            _promotion_experiment(
                artifact_id="RC-" + "1" * 24,
                artifact_name=FACTOR_NAME,
                kind="factor",
                commit=FACTOR_COMMIT,
            ),
            thresholds=AcceptanceThresholds(min_in_sample_sharpe=999.0),
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        result = await _run_validation_experiment(
            engine, experiment_id=experiment.experiment_id
        )
        assert result.status == "failed"
        assert result.error_code == "experiment_rejected"
        assert result.error_summary is not None
        async with session_factory(engine)() as session:
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.status is ExperimentStatus.REJECTED
            # 失败也算试验:全部候选落库
            trials = await ResearchTrialRepository(session).list_by_experiment(
                experiment.experiment_id
            )
            assert len(trials) == 2
            assert all(t.status is TrialStatus.REJECTED for t in trials)

    async def test_reexecute_after_terminal_refused(self, engine: AsyncEngine) -> None:
        """揭盲不可重做:终态后重复执行秒级拒绝(ExecutorError → worker 收口 failed)。"""
        experiment = _promotion_experiment(
            artifact_id="RC-" + "2" * 24,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        first = await _run_validation_experiment(
            engine, experiment_id=experiment.experiment_id
        )
        assert first.status == "succeeded"
        with pytest.raises(ExecutorError) as exc_info:
            await _run_validation_experiment(
                engine, experiment_id=experiment.experiment_id
            )
        assert exc_info.value.code == "experiment_not_runnable"
        assert not exc_info.value.retryable

    async def test_resume_skips_persisted_trials(self, engine: AsyncEngine) -> None:
        """断点续跑:已持久化 trial 不重复执行,trials_used 不重复递增。"""
        experiment = _promotion_experiment(
            artifact_id="RC-" + "3" * 24,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        calls: list[tuple[date, date]] = []
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            # 模拟中断前已落库的第 1 个 trial(HYPOTHESIS → IN_SAMPLE + 1 次计数)
            experiment = transition_status(experiment, ExperimentStatus.IN_SAMPLE)
            experiment = increment_trials_used(experiment)
            await ResearchExperimentRepository(session).save(experiment)
            await ResearchTrialRepository(session).save(
                TrialRecord(
                    trial_id=f"{experiment.experiment_id}-t0",
                    experiment_id=experiment.experiment_id,
                    trial_index=0,
                    parameters={"window": 5},
                    status=TrialStatus.SELECTED,
                )
            )
            await session.commit()
        result = await _run_validation_experiment(
            engine, experiment_id=experiment.experiment_id, calls=calls
        )
        assert result.status == "succeeded", (
            f"{result.error_code}: {result.error_summary}"
        )
        async with session_factory(engine)() as session:
            trials = await ResearchTrialRepository(session).list_by_experiment(
                experiment.experiment_id
            )
            # 2 个候选只有 t1 待跑;t0 幂等 upsert,不产生第二个 IS trial
            assert [t.trial_id for t in trials] == [
                f"{experiment.experiment_id}-t0",
                f"{experiment.experiment_id}-t1",
            ]
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.trials_used == 2
            assert saved.status is ExperimentStatus.VALIDATED_OOS

    async def test_missing_experiment_named_error(self, engine: AsyncEngine) -> None:
        with pytest.raises(ExecutorError) as exc_info:
            await _run_validation_experiment(engine, experiment_id="does-not-exist")
        assert exc_info.value.code == "missing_experiment"

    async def test_mcp_run_tool_enqueue_and_prechecks(
        self, engine: AsyncEngine
    ) -> None:
        """MCP 入队:正对照建 job;幂等;终态 conflict;预算耗尽 invalid_argument。"""
        app = _make_app(engine)
        experiment = _promotion_experiment(
            artifact_id="RC-" + "4" * 24,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()

        env = await ve_tools.validation_experiment_run(
            app, experiment_id=experiment.experiment_id
        )
        assert env.status == "ok", env.error
        assert env.data["kind"] == "validation_experiment"
        first_job = env.data["job_id"]
        env_again = await ve_tools.validation_experiment_run(
            app, experiment_id=experiment.experiment_id
        )
        assert env_again.data["job_id"] == first_job
        assert env_again.data["created"] is False

        env_missing = await ve_tools.validation_experiment_run(
            app, experiment_id="missing-experiment"
        )
        assert env_missing.status == "error"
        assert env_missing.error is not None
        assert env_missing.error.kind == "not_found"

        exhausted = dc_replace(
            experiment, trials_used=experiment.plan.trial_budget
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(exhausted)
            await session.commit()
        env_budget = await ve_tools.validation_experiment_run(
            app, experiment_id=experiment.experiment_id
        )
        assert env_budget.status == "error"
        assert env_budget.error is not None
        assert env_budget.error.kind == "invalid_argument"
        assert "预算" in env_budget.error.message


    async def test_full_chain_survives_ragged_window_lengths(
        self, engine: AsyncEngine
    ) -> None:
        """#244 回归:窗口收益不等长(真实节假日 vs 近似切窗)不再崩溃。

        修复前:PBO 矩阵以顺序窗口序列为行,不等长触发 CSCV 严格等长
        断言,实验对一切配置必然崩溃且停留 in_sample;修复后矩阵取自
        IS 竞争 trial,全链路照常走到 validated_oos。
        """
        experiment = _promotion_experiment(
            artifact_id="RC-" + "6" * 24,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        executor = ValidationExperimentExecutor(
            session_maker=session_factory(engine),
            runner_factory=_ragged_runner_factory(),
        )
        result = await executor.execute(
            JobRecord(
                job_id="BJ-ragged",
                kind="validation_experiment",
                queue="research",
                payload={"experiment_id": experiment.experiment_id},
                attempt=1,
                max_attempts=1,
                requested_by="integration-test",
            ),
            _noop_progress,
        )
        assert result.status == "succeeded", (
            f"{result.error_code}: {result.error_summary}"
        )
        async with session_factory(engine)() as session:
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.status is ExperimentStatus.VALIDATED_OOS

    async def test_unexpected_failure_named_and_resumable(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """意外异常兜底(#244):具名 experiment_execution_failed,状态保留可续跑。

        兜底不把实验推进终态 —— 崩溃是平台问题而非实验结论,REJECTED
        会永久烧掉实验(揭盲不可重做);保留 in_sample 让修复后重入队
        断点续跑(已持久化 trial 不重跑)。
        """

        async def _boom(self: ValidationRunner) -> Any:
            raise ValueError("all trials must have the same length")

        monkeypatch.setattr(ValidationRunner, "run_walk_forward", _boom)
        experiment = _promotion_experiment(
            artifact_id="RC-" + "7" * 24,
            artifact_name=FACTOR_NAME,
            kind="factor",
            commit=FACTOR_COMMIT,
        )
        async with session_factory(engine)() as session:
            await ResearchExperimentRepository(session).save(experiment)
            await session.commit()
        executor = ValidationExperimentExecutor(
            session_maker=session_factory(engine),
            runner_factory=_fake_runner_factory([]),
        )
        job = JobRecord(
            job_id="BJ-crash",
            kind="validation_experiment",
            queue="research",
            payload={"experiment_id": experiment.experiment_id},
            attempt=1,
            max_attempts=1,
            requested_by="integration-test",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "experiment_execution_failed"
        assert not exc_info.value.retryable
        assert "in_sample" in exc_info.value.summary
        assert "重新入队" in exc_info.value.summary

        async with session_factory(engine)() as session:
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.status is ExperimentStatus.IN_SAMPLE
            assert not saved.final_test_unsealed
            trials = await ResearchTrialRepository(session).list_by_experiment(
                experiment.experiment_id
            )
            assert len(trials) == 2

        # 修复后(此处以解除注入模拟)重入队断点续跑 → validated_oos
        monkeypatch.undo()
        result = await executor.execute(job, _noop_progress)
        assert result.status == "succeeded", (
            f"{result.error_code}: {result.error_summary}"
        )
        async with session_factory(engine)() as session:
            saved = await ResearchExperimentRepository(session).get(
                experiment.experiment_id
            )
            assert saved is not None
            assert saved.status is ExperimentStatus.VALIDATED_OOS


# ---------------------------------------------------------------------------
# #234:strategy 通道 screen 全链路
# ---------------------------------------------------------------------------


class TestStrategyScreenChannel:
    async def test_strategy_screen_enqueue_freeze_and_promotion(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """draft 策略 artifact:绑定 → 入队冻结 commit/ID → worker 执行 → promote。"""
        from tests.integration.test_research_run_signal_engine_worker import (
            _multi_period_provider,
        )
        from tests.integration.test_user_code_strategy_run import (
            FakeSandboxCaller,
            _user_code_factory,
        )

        app = _make_app(engine, sandbox_enabled=True)
        async with session_factory(engine)() as session:
            await _register_release(session, MULTI_RELEASE_ID)
            artifact = await _register_draft_artifact(
                session, kind="strategy", name=STRATEGY_NAME, commit=STRATEGY_COMMIT
            )
            await session.commit()

            template = build_strategy_template(
                "multi_factor",
                strategy_id="screen_strategy_v1",
                dataset_release_ids=(MULTI_RELEASE_ID,),
            )
            spec = template.model_copy(
                update={
                    "strategy_kind": "user_code",
                    "feature_graph": FeatureGraph(nodes=(), outputs=()),
                    "signal_rules": SignalRules(rules=()),
                    "code_artifact": StrategyCodeArtifactRef(name=STRATEGY_NAME),
                    "screen_artifact_bindings": (
                        ScreenArtifactBinding(
                            kind="strategy",
                            name=STRATEGY_NAME,
                            artifact_id=artifact.artifact_id,
                            commit=STRATEGY_COMMIT,
                        ),
                    ),
                }
            )
            # 编译期绑定放行(active 名单为空)
            compile_registered_strategy_spec(spec)
            await _publish_spec(session, spec)
            artifact_id = artifact.artifact_id

        body = run_tools.parse_queue_payload(
            {
                "idempotency_key": "screen-strategy-v1",
                "strategy_id": "screen_strategy_v1",
                "strategy_version": 1,
                "dataset_release_ids": [MULTI_RELEASE_ID],
                "parameters": {"rebalance_frequency": "monthly"},
                "code_version": "abcdef0123456789",
                "initial_capital": "200000",
                "requested_by": "integration-test",
            }
        )
        ack = await run_tools.enqueue_research_run(app, body)
        run_id = cast(str, ack["run_id"])

        # manifest 冻结:commit + artifact_id 落进 code_artifact
        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            manifest_spec = cast(dict[str, Any], row.manifest)["strategy_spec"]
            ref = manifest_spec["code_artifact"]
            assert ref["commit"] == STRATEGY_COMMIT
            assert ref["artifact_id"] == artifact_id
            assert manifest_spec["screen_artifact_bindings"]

        def weights_fn(
            index: int, symbols: tuple[str, ...], current: dict[str, float]
        ) -> dict[str, float]:
            """每期同序非空权重:strategy_screen 需要 ≥5 标的截面(rank_ic 门)。"""
            del index, current
            weights = {"A.SH": 0.20, "B.SH": 0.17, "C.SH": 0.14,
                       "D.SH": 0.11, "E.SH": 0.08, "F.SH": 0.05}
            return {s: w for s, w in weights.items() if s in symbols}

        caller = FakeSandboxCaller(weights_fn=weights_fn)
        worker = _build_worker(
            engine,
            _user_code_factory(_multi_period_provider(), caller, monkeypatch),
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            assert row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={row.status} error={row.error_code}/{row.error_summary}"
            )
            result = row.result or {}
            assert result.get("strategy_screen") is not None
            provenance = result.get("sandbox_provenance")
            assert provenance is not None

        # OOS 实验 + promote → active+passed
        experiment = _promotion_experiment(
            artifact_id=artifact_id,
            artifact_name=STRATEGY_NAME,
            kind="user_code",
            commit=STRATEGY_COMMIT,
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
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id=run_id,
        )
        assert env.status == "ok", env.error
        assert env.data["status"] == "active"
        assert env.data["promotion_status"] == "passed"
        async with session_factory(engine)() as session:
            final = await ResearchCodeArtifactRepository(session).get(artifact_id)
            assert final is not None
            assert is_promoted_artifact(final)
