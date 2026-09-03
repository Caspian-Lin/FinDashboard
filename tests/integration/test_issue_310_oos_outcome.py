"""issue #310 端到端集成测试(需 PostgreSQL)。

全 trial OOS 被拒时:

* 执行器(#233)照常完成揭盲(不硬阻断,状态机与一次性语义零变化),
  ``validated_oos`` 终态 + 具名警示 ``unseal_with_rejected_trials`` 持久化在
  ``research_experiments.notes``;
* MCP ``finboard_validation_experiment_get/list`` 返回派生
  ``oos_outcome=not_supported``(status=validated_oos 但结论不是「假设获
  支持」,OOS 流程完成 ≠ 假设获支持);
* 晋级(promote)失败证据 ``gates.validation.oos_outcome`` 同步可见。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.executors.validation_experiment import (
    ValidationExperimentExecutor,
)
from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    new_experiment,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import research_code as research_code_tools
from finboard_mcp.tools import validation_experiments as ve_tools
from finboard_persistence import (
    ResearchCodeArtifactRepository,
    ResearchExperimentRepository,
    ResearchTrialRepository,
    session_factory,
)

pytestmark = pytest.mark.asyncio

FACTOR_NAME = "issue310_factor"
COMMIT = "c" * 40

#: 只把 DSR 门调到不可达 → OOS walk-forward 门必挂(best trial REJECTED),
#: 揭盲门(sharpe/mdd/bootstrap CI)不受影响 → 揭盲仍通过 → validated_oos。
_OOS_FAIL_THRESHOLDS = AcceptanceThresholds(
    min_in_sample_sharpe=-10.0,
    min_oos_sharpe=-10.0,
    max_oos_drawdown=1.0,
    min_oos_calmar=-10.0,
    min_oos_information_ratio=-10.0,
    min_pbo_pass=False,
    min_deflated_sharpe=999.0,
    min_probabilistic_sharpe=0.0,
)


@pytest.fixture(autouse=True)
async def _clean(_engine: AsyncEngine) -> None:
    async with _engine.begin() as conn:
        for table in (
            "research_trials",
            "research_experiments",
            "research_code_artifacts",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


def _make_app(_engine: AsyncEngine) -> McpAppContext:
    from finboard_app.config import Settings
    from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager

    return McpAppContext(
        settings=Settings(),
        session_maker=session_factory(_engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=_engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _experiment() -> Any:
    return new_experiment(
        hypothesis="全 trial OOS 被拒仍揭盲的 oos_outcome 语义验证假设(#310)",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version="v1",
            dataset_versions={},
            selection_config={},
            strategy_kind="ma_cross",
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
        thresholds=_OOS_FAIL_THRESHOLDS,
        robustness=RobustnessPlan(stress_phases=()),
        strategy_params_space={"window": [5, 10]},
    )


def _rising_runner_factory() -> Any:
    """合成 trial runner:稳定上涨 + 单日 -3% 回撤(与既有执行器测试同形)。"""

    async def factory(session: AsyncSession, experiment: Any) -> Any:
        del session, experiment

        async def runner(
            *,
            start: date,
            end: date,
            params: dict[str, object],
            config_overrides: dict[str, object] | None = None,
        ) -> BacktestResult:
            del params, config_overrides, end
            equity: list[tuple[date, Decimal]] = []
            level = 100.0
            for index in range(20):
                level *= 0.97 if index == 10 else 1.01
                equity.append(
                    (start + timedelta(days=index), Decimal(str(round(level, 4))))
                )
            return BacktestResult(equity_curve=equity)

        return runner

    return factory


async def _noop_progress(done: int, total: int | None, phase: str | None) -> None:
    del done, total, phase


async def _run_executor(_engine: AsyncEngine, experiment_id: str) -> Any:
    executor = ValidationExperimentExecutor(
        session_maker=session_factory(_engine),
        runner_factory=_rising_runner_factory(),
    )
    return await executor.execute(
        JobRecord(
            job_id="BJ-issue310",
            kind="validation_experiment",
            queue="research",
            payload={"experiment_id": experiment_id},
            attempt=1,
            max_attempts=1,
            requested_by="integration-test",
        ),
        _noop_progress,
    )


async def _run_and_validate(_engine: AsyncEngine) -> Any:
    """跑通执行器并断言「validated_oos + best trial REJECTED」共存。"""
    experiment = _experiment()
    async with session_factory(_engine)() as session:
        await ResearchExperimentRepository(session).save(experiment)
        await session.commit()
    result = await _run_executor(_engine, experiment.experiment_id)
    assert result.status == "succeeded", (
        f"{result.error_code}: {result.error_summary}"
    )
    async with session_factory(_engine)() as session:
        saved = await ResearchExperimentRepository(session).get(
            experiment.experiment_id
        )
        assert saved is not None
        # 不硬阻断:揭盲照常完成,状态机零变化
        assert saved.status is ExperimentStatus.VALIDATED_OOS
        assert saved.final_test_unsealed is True
        trials = await ResearchTrialRepository(session).list_by_experiment(
            experiment.experiment_id
        )
        oos_trials = [t for t in trials if t.oos_metrics is not None]
        assert len(oos_trials) == 1
        assert oos_trials[0].status is TrialStatus.REJECTED
    return experiment


class TestUnsealWithRejectedTrialsE2E:
    async def test_note_persisted_and_oos_outcome_visible_in_mcp(
        self, _engine: AsyncEngine  # noqa: PT019
    ) -> None:
        """揭盲警示入 notes(持久化);MCP get/list 展示 not_supported。"""
        experiment = await _run_and_validate(_engine)

        app = _make_app(_engine)
        env = await ve_tools.validation_experiment_get(app, experiment.experiment_id)
        assert env.status == "ok", env.error
        assert env.data["status"] == "validated_oos"
        # 警示随实验记录持久化(notes)
        assert "unseal_with_rejected_trials" in env.data["notes"]
        # 验收标准:validated_oos + best trial rejected → oos_outcome=not_supported
        assert env.data["oos_outcome"] == "not_supported"

        env_list = await ve_tools.validation_experiment_list(app)
        assert env_list.status == "ok"
        item = next(
            e for e in env_list.data if e["experiment_id"] == experiment.experiment_id
        )
        assert item["oos_outcome"] == "not_supported"

    async def test_promotion_failure_evidence_carries_oos_outcome(
        self, _engine: AsyncEngine  # noqa: PT019
    ) -> None:
        """晋级失败证据 gates.validation.oos_outcome 同步可见(#304 风格)。"""
        experiment = await _run_and_validate(_engine)

        async with session_factory(_engine)() as session:
            artifact = await ResearchCodeArtifactRepository(session).register(
                kind="factor",
                name=FACTOR_NAME,
                commit=COMMIT,
                path=f"factors/{FACTOR_NAME}/factor.py",
                checksum="a" * 32,
                created_by="integration-test",
                status="draft",
            )
            await session.commit()

        app = _make_app(_engine)
        # screen 证据故意缺失 → 走 _record_promotion_failure(证据归档路径)
        env = await research_code_tools.promote(
            app,
            artifact_id=artifact.artifact_id,
            validation_experiment_id=experiment.experiment_id,
            screen_run_id="RR-missing-screen",
        )
        assert env.status == "error"
        assert env.error is not None

        async with session_factory(_engine)() as session:
            row = await ResearchCodeArtifactRepository(session).get(
                artifact.artifact_id
            )
            assert row is not None
            assert row.promotion_status == "failed"
            evidence = row.promotion_evidence
            assert isinstance(evidence, dict)
            assert evidence["gates"]["validation"]["oos_outcome"] == ("not_supported")
