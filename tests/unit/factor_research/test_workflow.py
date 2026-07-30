"""审批工作流 + 状态机测试 —— 提案→审批→实验→OOS 通过/拒绝完整流程。"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    AuditEventType,
    FactorHypothesis,
    HypothesisStatus,
    MachineValidationOutcome,
    ParameterSpec,
    Reference,
    ResearchWorkflow,
    WorkflowError,
)


def _make_hypothesis(**overrides: object) -> FactorHypothesis:
    defaults: dict[str, object] = {
        "name": "momentum",
        "economic_mechanism": "trend follows trend",
        "input_fields": ("close",),
        "decision_timing": "close",
        "formula": "rank(ts_mean(close, 20))",
        "direction": "long_high",
        "parameters": (ParameterSpec("lookback", 5.0, 60.0, grid_size=5),),
        "references": (Reference(title="Reference Paper"),),
        "expected_failure_scenarios": ("market crash",),
    }
    defaults.update(overrides)
    return FactorHypothesis(**defaults)  # type: ignore[arg-type]


def _passed_outcome(*, validation_experiment_id: str = "exp-57-ok") -> MachineValidationOutcome:
    """构造一个通过的 #57 机器验证终态(由系统从持久化实验读取)。"""
    return MachineValidationOutcome(
        validation_experiment_id=validation_experiment_id,
        status="validated_oos",
        trials_used=3,
    )


def _failed_outcome(
    *,
    validation_experiment_id: str = "exp-57-fail",
    rejection_reason: str = "Sharpe < threshold",
) -> MachineValidationOutcome:
    return MachineValidationOutcome(
        validation_experiment_id=validation_experiment_id,
        status="rejected",
        trials_used=3,
        rejection_reason=rejection_reason,
    )


class TestSubmit:
    def test_valid_hypothesis_submitted(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis()
        result = wf.submit(h, actor="llm")
        assert result.status == HypothesisStatus.PROPOSED
        assert wf.hypothesis_count == 1

    def test_invalid_hypothesis_auto_rejected(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(input_fields=("unknown_field",))
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED
        assert result.rejection_reason is not None
        assert "unknown_field" in result.rejection_reason

    def test_submit_logs_audit(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis()
        wf.submit(h, actor="llm")
        history = wf.audit.get_history(h.hypothesis_id)
        assert len(history) >= 1
        assert history[0].event_type == AuditEventType.VALIDATION_PASSED

    def test_failed_validation_logs_audit(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(input_fields=("bad_field",))
        wf.submit(h, actor="llm")
        entries = wf.audit.get_by_type(AuditEventType.VALIDATION_FAILED)
        assert len(entries) == 1


class TestApprove:
    def test_approve_proposed(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        approved = wf.approve(h.hypothesis_id, approver="alice")
        assert approved.status == HypothesisStatus.APPROVED
        assert approved.approved_by == "alice"
        assert approved.approved_at is not None

    def test_approve_non_proposed_raises(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        with pytest.raises(WorkflowError, match="PROPOSED"):
            wf.approve(h.hypothesis_id, approver="bob")

    def test_approve_unknown_raises(self) -> None:
        wf = ResearchWorkflow()
        with pytest.raises(WorkflowError, match="未知"):
            wf.approve("fh-nonexistent", approver="alice")

    def test_reject_proposed(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        rejected = wf.reject(h.hypothesis_id, approver="bob", reason="too risky")
        assert rejected.status == HypothesisStatus.REJECTED
        assert "too risky" in (rejected.rejection_reason or "")


class TestRegisterExperiment:
    def test_register_approved(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="gpt-4-0613",
            prompt_version="v1",
            dataset_version="ak-2024-01",
            code_version="abc1234",
            registered_by="alice",
        )
        assert reg.experiment_id.startswith("exp-")
        assert reg.model_version == "gpt-4-0613"
        assert reg.code_version == "abc1234"
        assert reg.reference_count >= 1

    def test_register_moves_to_in_sample(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        updated = wf.get_hypothesis(h.hypothesis_id)
        assert updated is not None
        assert updated.status == HypothesisStatus.IN_SAMPLE
        assert updated.experiment_count == 1

    def test_register_non_approved_raises(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        with pytest.raises(WorkflowError, match="APPROVED"):
            wf.register_experiment(
                h.hypothesis_id,
                model_version="v1",
                prompt_version="v1",
                dataset_version="v1",
                code_version="v1",
                registered_by="alice",
            )

    def test_budget_exhausted_raises(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(
            parameters=(ParameterSpec("x", 0.0, 1.0, grid_size=1),),
        ))
        wf.approve(h.hypothesis_id, approver="alice")
        wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        with pytest.raises(WorkflowError, match="预算上限"):
            wf.register_experiment(
                h.hypothesis_id,
                model_version="v1",
                prompt_version="v1",
                dataset_version="v1",
                code_version="v1",
                registered_by="alice",
            )

    def test_multiple_experiments_increment_count(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(
            parameters=(ParameterSpec("x", 0.0, 1.0, grid_size=3),),
        ))
        wf.approve(h.hypothesis_id, approver="alice")
        for _i in range(3):
            wf.register_experiment(
                h.hypothesis_id,
                model_version="v1",
                prompt_version="v1",
                dataset_version="v1",
                code_version="v1",
                registered_by="alice",
            )
        updated = wf.get_hypothesis(h.hypothesis_id)
        assert updated is not None
        assert updated.experiment_count == 3


class TestCompleteExperiment:
    def test_pass_oos(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        result = wf.complete_experiment(
            reg.experiment_id,
            validation=_passed_outcome(),
            completed_by="alice",
        )
        assert result.status == HypothesisStatus.VALIDATED_OOS
        assert result.is_terminal

    def test_fail_oos(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        result = wf.complete_experiment(
            reg.experiment_id,
            validation=_failed_outcome(),
            completed_by="alice",
        )
        assert result.status == HypothesisStatus.REJECTED
        assert "Sharpe" in (result.rejection_reason or "")

    def test_complete_unknown_experiment_raises(self) -> None:
        wf = ResearchWorkflow()
        with pytest.raises(WorkflowError, match="未知实验"):
            wf.complete_experiment(
                "exp-nonexistent",
                validation=_passed_outcome(),
                completed_by="alice",
            )


class TestSupersede:
    def test_supersede(self) -> None:
        wf = ResearchWorkflow()
        old = wf.submit(_make_hypothesis(name="old"))
        new_raw = _make_hypothesis(name="new")
        new = wf.supersede(old.hypothesis_id, new_raw, actor="alice")
        assert new.supersedes_id == old.hypothesis_id
        old_updated = wf.get_hypothesis(old.hypothesis_id)
        assert old_updated is not None
        assert old_updated.status == HypothesisStatus.SUPERSEDED

    def test_supersede_unknown_raises(self) -> None:
        wf = ResearchWorkflow()
        with pytest.raises(WorkflowError, match="未知"):
            wf.supersede("fh-nonexistent", _make_hypothesis())


class TestQueries:
    def test_list_hypotheses_by_status(self) -> None:
        wf = ResearchWorkflow()
        wf.submit(_make_hypothesis(name="h1"))
        h2 = wf.submit(_make_hypothesis(name="h2"))
        wf.approve(h2.hypothesis_id, approver="alice")
        proposed = wf.list_hypotheses(status=HypothesisStatus.PROPOSED)
        approved = wf.list_hypotheses(status=HypothesisStatus.APPROVED)
        assert len(proposed) == 1
        assert len(approved) == 1

    def test_list_experiments(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        exps = wf.list_experiments(h.hypothesis_id)
        assert len(exps) == 1
        all_exps = wf.list_experiments()
        assert len(all_exps) == 1

    def test_get_validation(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        val = wf.get_validation(h.hypothesis_id)
        assert val is not None
        assert val.is_valid


class TestFullWorkflow:
    """提案 → 审批 → 实验 → OOS 通过/拒绝完整离线流程。"""

    def test_full_pass_flow(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(), actor="llm@gpt-4")
        assert h.status == HypothesisStatus.PROPOSED

        h = wf.approve(h.hypothesis_id, approver="alice")
        assert h.status == HypothesisStatus.APPROVED

        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="gpt-4-0613",
            prompt_version="v3",
            dataset_version="ak-2024-01",
            code_version="abc1234",
            registered_by="alice",
        )
        updated = wf.get_hypothesis(h.hypothesis_id)
        assert updated is not None
        assert updated.status == HypothesisStatus.IN_SAMPLE

        h = wf.complete_experiment(
            reg.experiment_id,
            validation=_passed_outcome(),
            completed_by="alice",
        )
        assert h.status == HypothesisStatus.VALIDATED_OOS
        assert h.is_terminal

        history = wf.audit.get_history(h.hypothesis_id)
        events = [e.event_type for e in history]
        assert AuditEventType.VALIDATION_PASSED in events
        assert AuditEventType.APPROVED in events
        assert AuditEventType.EXPERIMENT_REGISTERED in events
        assert AuditEventType.VALIDATED_OOS in events

    def test_full_reject_flow(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        h = wf.complete_experiment(
            reg.experiment_id,
            validation=_failed_outcome(),
            completed_by="alice",
        )
        assert h.status == HypothesisStatus.REJECTED
        assert h.rejection_reason is not None

    def test_full_validation_reject_flow(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(input_fields=("unknown",)))
        assert h.status == HypothesisStatus.REJECTED
        history = wf.audit.get_history(h.hypothesis_id)
        events = [e.event_type for e in history]
        assert AuditEventType.VALIDATION_FAILED in events

    def test_to_text_integration(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        text = h.to_text()
        assert "momentum" in text
        assert "proposed" in text
        assert h.hypothesis_id in text

    def test_failed_experiment_retained(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(input_fields=("bad",)))
        assert wf.hypothesis_count == 1
        assert h.status == HypothesisStatus.REJECTED
