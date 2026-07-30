"""故障注入测试 —— 模型不可用 / 引用缺失 / 实验中断 / 版本漂移。"""

from __future__ import annotations

import contextlib

import pytest

from finboard_backtest.factor_research import (
    AuditEventType,
    FactorHypothesis,
    FakeLLMProvider,
    HypothesisStatus,
    LLMUnavailableError,
    MachineValidationOutcome,
    ParameterSpec,
    Reference,
    ResearchWorkflow,
    WorkflowError,
    sanitize_prompt,
)


def _make_hypothesis(**overrides: object) -> FactorHypothesis:
    defaults: dict[str, object] = {
        "name": "test",
        "economic_mechanism": "x",
        "input_fields": ("close",),
        "decision_timing": "close",
        "formula": "rank(close)",
        "direction": "long_high",
        "parameters": (ParameterSpec("lookback", 5.0, 60.0, grid_size=3),),
        "references": (Reference(title="Paper"),),
    }
    defaults.update(overrides)
    return FactorHypothesis(**defaults)  # type: ignore[arg-type]


class TestModelUnavailable:
    def test_llm_unavailable_raises(self) -> None:
        provider = FakeLLMProvider(fail_on_call=True)
        with pytest.raises(LLMUnavailableError):
            provider.generate_hypothesis("prompt")

    def test_llm_exhausted_raises(self) -> None:
        provider = FakeLLMProvider(hypotheses=[])
        with pytest.raises(LLMUnavailableError):
            provider.generate_hypothesis("prompt")

    def test_workflow_not_affected_by_llm_failure(self) -> None:
        wf = ResearchWorkflow()
        provider = FakeLLMProvider(fail_on_call=True)
        with contextlib.suppress(LLMUnavailableError):
            provider.generate_hypothesis("prompt")
        assert wf.hypothesis_count == 0
        assert len(wf.audit) == 0

    def test_provider_recovery(self) -> None:
        h = _make_hypothesis()
        provider = FakeLLMProvider(hypotheses=[h])
        provider.fail_on_call = True
        with pytest.raises(LLMUnavailableError):
            provider.generate_hypothesis("prompt")
        provider.fail_on_call = False
        result = provider.generate_hypothesis("prompt")
        assert result.name == "test"


class TestReferenceMissing:
    def test_no_references_warning_not_error(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis(references=()))
        val = wf.get_validation(h.hypothesis_id)
        assert val is not None
        assert val.is_valid
        assert any("文献" in w for w in val.warnings)

    def test_hypothesis_with_references(self) -> None:
        wf = ResearchWorkflow()
        refs = (
            Reference(title="Paper A", year=2020),
            Reference(title="Paper B", year=2022, doi="10.1234/abc"),
        )
        h = wf.submit(_make_hypothesis(references=refs))
        val = wf.get_validation(h.hypothesis_id)
        assert val is not None
        assert val.is_valid
        assert not any("文献" in w for w in val.warnings)


class TestExperimentInterrupted:
    def test_interrupt_registered_experiment(self) -> None:
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
        interrupted = wf.interrupt_experiment(
            reg.experiment_id,
            reason="system crash",
            actor="system",
        )
        assert interrupted.status == "interrupted"

    def test_interrupt_unknown_raises(self) -> None:
        wf = ResearchWorkflow()
        with pytest.raises(WorkflowError, match="未知"):
            wf.interrupt_experiment("exp-xxx", reason="x", actor="x")

    def test_interrupt_logs_audit(self) -> None:
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
        wf.interrupt_experiment(reg.experiment_id, reason="crash", actor="system")
        entries = wf.audit.get_by_type(AuditEventType.EXPERIMENT_INTERRUPTED)
        assert len(entries) == 1

    def test_interrupt_already_completed_raises(self) -> None:
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
        wf.complete_experiment(
            reg.experiment_id,
            validation=MachineValidationOutcome(
                validation_experiment_id="exp-57-ok",
                status="validated_oos",
                trials_used=3,
            ),
            completed_by="alice",
        )
        with pytest.raises(WorkflowError, match="不能中断"):
            wf.interrupt_experiment(reg.experiment_id, reason="x", actor="x")


class TestVersionDrift:
    def test_supersede_creates_version_link(self) -> None:
        wf = ResearchWorkflow()
        old = wf.submit(_make_hypothesis(name="v1"))
        new_h = _make_hypothesis(name="v2")
        linked = wf.supersede(old.hypothesis_id, new_h, actor="alice")
        assert linked.supersedes_id == old.hypothesis_id

    def test_superseded_cannot_register_experiment(self) -> None:
        wf = ResearchWorkflow()
        old = wf.submit(_make_hypothesis())
        wf.supersede(old.hypothesis_id, _make_hypothesis(name="new"), actor="x")
        with pytest.raises(WorkflowError, match="APPROVED"):
            wf.register_experiment(
                old.hypothesis_id,
                model_version="v1",
                prompt_version="v1",
                dataset_version="v1",
                code_version="v1",
                registered_by="alice",
            )

    def test_double_supersede(self) -> None:
        wf = ResearchWorkflow()
        h1 = wf.submit(_make_hypothesis(name="v1"))
        h2 = wf.supersede(h1.hypothesis_id, _make_hypothesis(name="v2"))
        h3 = wf.supersede(h2.hypothesis_id, _make_hypothesis(name="v3"))
        assert h3.supersedes_id == h2.hypothesis_id
        h1_updated = wf.get_hypothesis(h1.hypothesis_id)
        assert h1_updated is not None
        assert h1_updated.status == HypothesisStatus.SUPERSEDED


class TestPromptInjectionResistance:
    def test_prompt_with_code_injection_still_validated(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(
            formula="import os; exec('rm -rf /')",
        )
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED

    def test_prompt_with_sql_injection_rejected(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(
            formula="SELECT * FROM accounts WHERE broker.send_order()",
        )
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED

    def test_sanitized_prompt_does_not_leak(self) -> None:
        secret_prompt = "api_key=sk-abcd1234efgh5678ijkl9012"
        sanitized = sanitize_prompt(secret_prompt)
        assert "sk-abcd1234" not in sanitized


class TestNoBrokerConnection:
    """验证系统不连接 Broker / OrderManager / 实盘配置。"""

    @staticmethod
    def _import_lines(module_path: str) -> list[str]:
        import importlib

        mod = importlib.import_module(module_path)
        assert mod.__file__ is not None
        with open(mod.__file__) as f:
            return [
                line for line in f
                if line.strip().startswith(("import ", "from "))
            ]

    def test_factor_research_hypothesis_no_broker_import(self) -> None:
        for line in self._import_lines("finboard_backtest.factor_research.hypothesis"):
            assert "broker" not in line.lower()
            assert "order_manager" not in line.lower()

    def test_workflow_has_no_broker_import(self) -> None:
        for line in self._import_lines("finboard_backtest.factor_research.workflow"):
            assert "broker" not in line.lower()
            assert "order_manager" not in line.lower()

    def test_provider_has_no_broker_import(self) -> None:
        for line in self._import_lines("finboard_backtest.factor_research.provider"):
            assert "broker" not in line.lower()
            assert "order_manager" not in line.lower()

    def test_whitelist_rejects_broker_keywords(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(formula="broker.place_order()")
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED

    def test_whitelist_rejects_order_manager(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(formula="order_manager.send()")
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED

    def test_whitelist_rejects_kill_switch(self) -> None:
        wf = ResearchWorkflow()
        h = _make_hypothesis(formula="kill_switch.disable()")
        result = wf.submit(h)
        assert result.status == HypothesisStatus.REJECTED


class TestExperimentRegistrationVersions:
    def test_versions_recorded(self) -> None:
        wf = ResearchWorkflow()
        h = wf.submit(_make_hypothesis())
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="gpt-4-0613",
            prompt_version="prompt-v5",
            dataset_version="ak-2024-03-15",
            code_version="a1b2c3d",
            registered_by="alice",
        )
        assert reg.model_version == "gpt-4-0613"
        assert reg.prompt_version == "prompt-v5"
        assert reg.dataset_version == "ak-2024-03-15"
        assert reg.code_version == "a1b2c3d"

    def test_references_copied_to_registration(self) -> None:
        wf = ResearchWorkflow()
        refs = (Reference(title="Paper A"), Reference(title="Paper B"))
        h = wf.submit(_make_hypothesis(references=refs))
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="v1",
            prompt_version="v1",
            dataset_version="v1",
            code_version="v1",
            registered_by="alice",
        )
        assert reg.reference_count == 2
