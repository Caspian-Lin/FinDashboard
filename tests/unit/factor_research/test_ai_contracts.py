"""AI 输出契约校验测试(issue #84)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finboard_backtest.factor_research.ai_contracts import (
    ALLOWED_STRATEGY_COMPONENTS,
    AnswerResult,
    Citation,
    CitationSource,
    DraftArtifact,
    DraftKind,
    DraftStatus,
    Provenance,
    StrategyDiffItem,
    StrategyDiffPayload,
    StrategyDraftPayload,
    UncertaintyLevel,
    hypothesis_to_draft_payload,
)
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    ParameterSpec,
    Reference,
)
from finboard_backtest.strategy_spec.contracts import StrategySpecError


def _provenance() -> Provenance:
    return Provenance(provider="fake", model_version="v0", prompt_version="v1")


class TestDraftStatus:
    def test_legal_transitions(self) -> None:
        draft = DraftArtifact(
            draft_id="x",
            kind=DraftKind.HYPOTHESIS,
            provenance=_provenance(),
            status=DraftStatus.PROPOSED,
            payload={},
        )
        approved = draft.with_status(
            DraftStatus.APPROVED,
            approved_by="alice",
            approved_at=datetime.now(UTC),
        )
        assert approved.is_approved
        consumed = approved.with_status(DraftStatus.CONSUMED, consumed_ref="spec-1")
        assert consumed.is_terminal

    def test_illegal_transition_raises(self) -> None:
        draft = DraftArtifact(
            draft_id="x",
            kind=DraftKind.HYPOTHESIS,
            provenance=_provenance(),
            status=DraftStatus.PROPOSED,
            payload={},
        )
        with pytest.raises(ValueError, match="非法草案状态转换"):
            draft.with_status(DraftStatus.CONSUMED)

    def test_terminal_cannot_change(self) -> None:
        draft = DraftArtifact(
            draft_id="x",
            kind=DraftKind.HYPOTHESIS,
            provenance=_provenance(),
            status=DraftStatus.REJECTED,
            payload={},
        )
        with pytest.raises(ValueError, match="非法草案状态转换"):
            draft.with_status(DraftStatus.APPROVED)


class TestStrategyDraftPayload:
    def test_valid_component(self) -> None:
        payload = StrategyDraftPayload(
            component_kind="universe",
            rationale="大盘蓝筹",
            components={"universe": {"markets": ["A"]}},
        )
        assert payload.component_kind in ALLOWED_STRATEGY_COMPONENTS

    def test_invalid_component_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="不在允许列表"):
            StrategyDraftPayload(
                component_kind="malicious_kind",
                rationale="x",
                components={},
            )

    def test_unknown_component_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="不允许的键"):
            StrategyDraftPayload(
                component_kind="universe",
                rationale="x",
                components={"python_code": "import os"},
            )

    def test_executable_payload_rejected(self) -> None:
        with pytest.raises(StrategySpecError):
            StrategyDraftPayload(
                component_kind="feature_graph",
                rationale="x",
                components={"feature_graph": {"code": "exec('rm -rf')"}},
            )


class TestStrategyDiffPayload:
    def _make(self, **overrides: object) -> StrategyDiffPayload:
        defaults: dict[str, object] = {
            "target_strategy_id": "strat-1",
            "target_version": 1,
            "summary": "调整窗口",
            "rationale": "降低换手",
            "changes": (
                StrategyDiffItem(
                    path="universe.selection_limit",
                    operation="replace",
                    old_value=50,
                    new_value=30,
                    explanation="缩小范围",
                ),
            ),
        }
        defaults.update(overrides)
        return StrategyDiffPayload(**defaults)  # type: ignore[arg-type]

    def test_valid_diff(self) -> None:
        diff = self._make()
        assert len(diff.changes) == 1

    def test_empty_changes_rejected(self) -> None:
        with pytest.raises(ValueError, match="changes 不能为空"):
            self._make(changes=())

    def test_invalid_operation_rejected(self) -> None:
        with pytest.raises(ValueError, match="operation"):
            StrategyDiffItem(path="x", operation="hack")

    def test_executable_value_rejected(self) -> None:
        with pytest.raises(StrategySpecError):
            self._make(
                changes=(
                    StrategyDiffItem(
                        path="x",
                        operation="add",
                        new_value="__import__('os')",
                    ),
                )
            )


class TestAnswerResult:
    def _citation(self) -> Citation:
        return Citation(
            source_type=CitationSource.PROJECT_DOC,
            title="AGENTS.md",
            locator="LLM 边界",
        )

    def test_valid_answer(self) -> None:
        a = AnswerResult(answer="解释", citations=(self._citation(),))
        assert a.uncertainty == UncertaintyLevel.MEDIUM

    def test_empty_citations_rejected(self) -> None:
        with pytest.raises(ValueError, match="citations 不能为空"):
            AnswerResult(answer="x", citations=())

    def test_data_insufficient_requires_disclaimer(self) -> None:
        with pytest.raises(ValueError, match="disclaimer"):
            AnswerResult(
                answer="x",
                citations=(self._citation(),),
                data_sufficient=False,
            )

    def test_empty_answer_rejected(self) -> None:
        with pytest.raises(ValueError, match="answer 不能为空"):
            AnswerResult(answer="", citations=(self._citation(),))


class TestHypothesisToDraftPayload:
    def test_roundtrip(self) -> None:
        h = FactorHypothesis(
            name="mom",
            economic_mechanism="趋势",
            input_fields=("close",),
            decision_timing="close",
            formula="rank(close)",
            direction="long_high",
            parameters=(ParameterSpec("w", 5.0, 20.0, grid_size=4),),
            references=(Reference(title="Paper"),),
        )
        payload = hypothesis_to_draft_payload(h)
        assert payload["name"] == "mom"
        assert payload["parameters"][0]["grid_size"] == 4
