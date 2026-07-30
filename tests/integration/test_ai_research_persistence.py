"""AI 研究助手持久化与重启恢复集成测试(issue #84)。

覆盖假设→审批→实验登记→机器 OOS 完成→重启恢复的完整流程,
验证失败/被拒绝记录不丢失,审计事件持久化。

需要 PostgreSQL 运行在 127.0.0.1:5432。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.factor_research import (
    DraftArtifact,
    DraftKind,
    DraftStatus,
    FactorHypothesis,
    HypothesisStatus,
    MachineValidationOutcome,
    ParameterSpec,
    Provenance,
    Reference,
    UncertaintyLevel,
)
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_persistence import (
    AIDraftRepository,
    HypothesisWorkflowService,
)
from finboard_persistence.validation_repo import ResearchExperimentRepository


async def _persist_validation_experiment(session: AsyncSession, target_status: str) -> str:
    """创建并持久化一个终态 #57 机器验证实验,返回 experiment_id。"""
    exp = new_experiment(
        hypothesis="#57 机器验证(测试)",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version="v1",
            dataset_versions={"daily_metrics": "2024-01"},
            selection_config={"enabled": False},
            strategy_kind="ma_cross",
        ),
        plan=ValidationPlan(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2020, 12, 31),
            validation_start=date(2021, 1, 1),
            validation_end=date(2021, 6, 30),
            test_start=date(2021, 7, 1),
            test_end=date(2021, 12, 31),
            train_window_days=252,
            test_window_days=63,
            step_days=21,
            trial_budget=20,
        ),
        thresholds=AcceptanceThresholds(),
        robustness=RobustnessPlan(),
        strategy_params_space={"window": [5, 10]},
        notes="测试",
    )
    exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
    if target_status == "validated_oos":
        exp = mark_final_test_unsealed(exp)
        exp = transition_status(exp, ExperimentStatus.VALIDATED_OOS)
    else:
        exp = transition_status(
            exp, ExperimentStatus.REJECTED, rejection_reason="Sharpe 不足"
        )
    await ResearchExperimentRepository(session).save(exp)
    await session.flush()
    return exp.experiment_id


def _hypothesis(name: str = "momentum", **overrides: object) -> FactorHypothesis:
    defaults: dict[str, object] = {
        "name": name,
        "economic_mechanism": "趋势延续",
        "input_fields": ("close",),
        "decision_timing": "close",
        "formula": "rank(ts_mean(close, 20))",
        "direction": "long_high",
        "parameters": (ParameterSpec("w", 5.0, 20.0, grid_size=3),),
        "references": (Reference(title="Paper"),),
        "expected_failure_scenarios": ("反转",),
    }
    defaults.update(overrides)
    return FactorHypothesis(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
class TestHypothesisPersistence:
    async def test_submit_persists_and_reloads(self, db_session: AsyncSession) -> None:
        service = HypothesisWorkflowService(db_session)
        h = await service.submit(_hypothesis(), actor="llm")
        await db_session.commit()

        # 重启恢复:新 service 实例从 DB 重建
        reloaded = HypothesisWorkflowService(db_session)
        restored = await reloaded.get_hypothesis(h.hypothesis_id)
        assert restored is not None
        assert restored.status == HypothesisStatus.PROPOSED
        assert restored.name == "momentum"

    async def test_full_approve_experiment_complete_flow(self, db_session: AsyncSession) -> None:
        # 先创建一个真实持久化的 #57 VALIDATED_OOS 实验(满足外键约束)
        vexp_id = await _persist_validation_experiment(db_session, "validated_oos")
        await db_session.commit()

        service = HypothesisWorkflowService(db_session)
        h = await service.submit(_hypothesis(), actor="llm")
        await db_session.commit()

        approved = await service.approve(h.hypothesis_id, approver="alice")
        await db_session.commit()
        assert approved.status == HypothesisStatus.APPROVED

        reg = await service.register_experiment(
            h.hypothesis_id,
            model_version="gpt-4",
            prompt_version="v1",
            dataset_version="ds-1",
            code_version="abc",
            registered_by="alice",
        )
        await db_session.commit()

        completed = await service.complete_experiment(
            reg.experiment_id,
            validation=MachineValidationOutcome(
                validation_experiment_id=vexp_id,
                status="validated_oos",
                trials_used=3,
            ),
            completed_by="alice",
        )
        await db_session.commit()
        assert completed.status == HypothesisStatus.VALIDATED_OOS

        # 重启恢复:验证终态
        reloaded = HypothesisWorkflowService(db_session)
        restored = await reloaded.get_hypothesis(h.hypothesis_id)
        assert restored is not None
        assert restored.status == HypothesisStatus.VALIDATED_OOS
        experiments = await reloaded.list_experiments(h.hypothesis_id)
        assert len(experiments) == 1
        assert experiments[0].validation_experiment_id == vexp_id

    async def test_rejected_outcome_persists(self, db_session: AsyncSession) -> None:
        vexp_id = await _persist_validation_experiment(db_session, "rejected")
        await db_session.commit()

        service = HypothesisWorkflowService(db_session)
        h = await service.submit(_hypothesis(), actor="llm")
        await service.approve(h.hypothesis_id, approver="alice")
        reg = await service.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        rejected = await service.complete_experiment(
            reg.experiment_id,
            validation=MachineValidationOutcome(
                validation_experiment_id=vexp_id,
                status="rejected",
                trials_used=3,
                rejection_reason="Sharpe 不足",
            ),
            completed_by="alice",
        )
        await db_session.commit()
        assert rejected.status == HypothesisStatus.REJECTED

        # 被拒绝记录不丢失
        reloaded = HypothesisWorkflowService(db_session)
        restored = await reloaded.get_hypothesis(h.hypothesis_id)
        assert restored is not None
        assert restored.status == HypothesisStatus.REJECTED
        assert "Sharpe" in (restored.rejection_reason or "")

    async def test_failed_validation_hypothesis_retained(self, db_session: AsyncSession) -> None:
        """白名单验证失败的假设同样保留(不丢失)。"""
        service = HypothesisWorkflowService(db_session)
        bad = _hypothesis(input_fields=("unknown_field",))
        rejected = await service.submit(bad, actor="llm")
        await db_session.commit()
        assert rejected.status == HypothesisStatus.REJECTED

        reloaded = HypothesisWorkflowService(db_session)
        restored = await reloaded.get_hypothesis(bad.hypothesis_id)
        assert restored is not None
        assert restored.status == HypothesisStatus.REJECTED

    async def test_audit_events_persist(self, db_session: AsyncSession) -> None:
        service = HypothesisWorkflowService(db_session)
        h = await service.submit(_hypothesis(), actor="llm")
        await service.approve(h.hypothesis_id, approver="alice")
        await db_session.commit()

        reloaded = HypothesisWorkflowService(db_session)
        events = await reloaded.list_audit(h.hypothesis_id)
        event_types = [e.event_type.value for e in events]
        assert "validation_passed" in event_types
        assert "approved" in event_types

    async def test_supersede_persists(self, db_session: AsyncSession) -> None:
        service = HypothesisWorkflowService(db_session)
        old = await service.submit(_hypothesis(name="v1"), actor="llm")
        await db_session.commit()
        new = await service.supersede(
            old.hypothesis_id, _hypothesis(name="v2"), actor="alice"
        )
        await db_session.commit()

        reloaded = HypothesisWorkflowService(db_session)
        old_restored = await reloaded.get_hypothesis(old.hypothesis_id)
        new_restored = await reloaded.get_hypothesis(new.hypothesis_id)
        assert old_restored is not None
        assert old_restored.status == HypothesisStatus.SUPERSEDED
        assert new_restored is not None
        assert new_restored.supersedes_id == old.hypothesis_id


@pytest.mark.asyncio
class TestDraftPersistence:
    def _draft(self, draft_id: str = "ai-draft-test") -> DraftArtifact:
        return DraftArtifact(
            draft_id=draft_id,
            kind=DraftKind.ANSWER,
            provenance=Provenance(
                provider="fake", model_version="v0", prompt_version="v1"
            ),
            status=DraftStatus.PROPOSED,
            payload={"answer": "x"},
            uncertainty=UncertaintyLevel.LOW,
        )

    async def test_save_get_list(self, db_session: AsyncSession) -> None:
        repo = AIDraftRepository(db_session)
        await repo.save(self._draft())
        await db_session.commit()

        loaded = await repo.get("ai-draft-test")
        assert loaded is not None
        assert loaded.status == DraftStatus.PROPOSED

        items = await repo.list_by_kind_status(DraftKind.ANSWER, DraftStatus.PROPOSED)
        assert len(items) >= 1

    async def test_approve_flow_persists(self, db_session: AsyncSession) -> None:
        repo = AIDraftRepository(db_session)
        draft = self._draft()
        await repo.save(draft)
        await db_session.commit()

        from datetime import UTC, datetime

        approved = draft.with_status(
            DraftStatus.APPROVED,
            approved_by="alice",
            approved_at=datetime.now(UTC),
        )
        await repo.save(approved)
        await db_session.commit()

        reloaded = await repo.get(draft.draft_id)
        assert reloaded is not None
        assert reloaded.status == DraftStatus.APPROVED
        assert reloaded.approved_by == "alice"
