"""#516 unit contracts: evidence kinds, plan/review entries, action whitelist.

No DB / no network: these are the parts that must fail at import or validation
time, before any transaction is opened.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from finboard_app.research_operation import (
    ResearchInvalidInputError,
    ResearchOperationContext,
)
from finboard_app.research_topic_artifacts import (
    ARTIFACT_ENTITY_TYPES,
    CODE_FORBIDDEN_FIELDS,
    TOPIC_ACTION_ENTITY_TYPES,
    TOPIC_ACTION_SCOPES,
    CodeRunExecuteIn,
    FactorExperimentCreateIn,
    ValidationUnsealIn,
    _reject_code_fields,
    _validate_action_payload,
)
from finboard_app.research_workspace import (
    ENTRY_EVIDENCE_LEVELS,
    EvidenceRef,
    RoundInput,
)

_NEW_KINDS = {
    "factor_experiment",
    "code_artifact",
    "code_run",
    "validation_experiment",
    "factor_snapshot",
}
_OLD_KINDS = {
    "research_run",
    "strategy",
    "dataset",
    "experiment",
    "memory",
    "document",
    "factor_series",
    "simulation",
    "backtest",
}


def test_evidence_ref_literal_keeps_old_kinds_and_adds_new() -> None:
    kinds = set(get_args(EvidenceRef.model_fields["kind"].annotation))
    assert kinds >= _OLD_KINDS
    assert kinds >= _NEW_KINDS
    assert kinds == _OLD_KINDS | _NEW_KINDS


@pytest.mark.parametrize(
    "payload",
    [
        {
            "entry_type": "review",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
        },
        {
            "entry_type": "review",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
            "document_ref": {"kind": "document", "ref_id": "rounds/x.md"},
        },
        {
            "entry_type": "review",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
            "document_ref": {
                "kind": "research_run",
                "ref_id": "rounds/x.md",
                "checksum": "c" * 64,
            },
        },
        {
            "entry_type": "review",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
            "document_ref": {"kind": "document", "ref_id": "/etc/passwd.md", "checksum": "c"},
        },
        {
            "entry_type": "plan",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
            "outcome": "failed",
        },
        {
            "entry_type": "round",
            "goal_version": "v1",
            "objective": "o",
            "action": "a",
            "rationale": "r",
            "document_ref": {"kind": "document", "ref_id": "rounds/x.md", "checksum": "c"},
        },
    ],
)
def test_invalid_plan_or_review_entries_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RoundInput.model_validate(payload)


def test_review_entry_is_a_reviewed_conclusion() -> None:
    entry = RoundInput.model_validate(
        {
            "entry_type": "review",
            "goal_version": "v1",
            "objective": "复核",
            "action": "读正式文档",
            "rationale": "结论一致",
            "document_ref": {
                "kind": "document",
                "ref_id": "ROADMAP.md",
                "checksum": "c" * 64,
            },
        }
    )
    assert entry.document_ref is not None
    assert entry.document_ref.kind == "document"
    assert ENTRY_EVIDENCE_LEVELS[entry.entry_type] == "reviewed_conclusion"
    assert ENTRY_EVIDENCE_LEVELS["plan"] == "research_plan"


def test_action_whitelist_and_scopes_are_consistent() -> None:
    assert set(TOPIC_ACTION_ENTITY_TYPES) == {
        "run.submit",
        "run.replay",
        "factor_experiment.create",
        "code_run.execute",
        "validation_experiment.create",
        "validation_experiment.add_trial",
        "validation_experiment.unseal",
    }
    assert set(TOPIC_ACTION_SCOPES) == set(TOPIC_ACTION_ENTITY_TYPES)
    assert set(TOPIC_ACTION_SCOPES.values()) <= {"research.write", "research.run"}
    assert set(TOPIC_ACTION_ENTITY_TYPES.values()) >= set(ARTIFACT_ENTITY_TYPES)
    # Execution-capable actions need research.run.
    for action in ("run.submit", "run.replay", "code_run.execute"):
        assert TOPIC_ACTION_SCOPES[action] == "research.run"


def test_request_models_have_no_code_field() -> None:
    for field in CODE_FORBIDDEN_FIELDS:
        assert field not in CodeRunExecuteIn.model_fields
        assert field not in FactorExperimentCreateIn.model_fields
        assert field not in ValidationUnsealIn.model_fields
    # extra="forbid" => unknown (potentially code-bearing) fields are rejected.
    with pytest.raises(ValidationError):
        CodeRunExecuteIn.model_validate(
            {
                "artifact_id": "RC-1",
                "dataset_release_ids": ["rel-1"],
                "decision_at": "2024-06-03T15:00:00+08:00",
                "module_path": "factors/momentum.py",
            }
        )
    with pytest.raises(ResearchInvalidInputError) as excinfo:
        _validate_action_payload(
            "code_run.execute",
            {
                "artifact_id": "RC-1",
                "dataset_release_ids": ["rel-1"],
                "decision_at": "2024-06-03T15:00:00+08:00",
                "code": "print(1)",
            },
        )
    assert excinfo.value.code == "code_channel_forbidden"
    # Every action rejects code-bearing keys before touching the DB, including
    # delegated run payloads (where pydantic would otherwise report invalid_*).
    for action in ("run.submit", "run.replay", "validation_experiment.add_trial"):
        with pytest.raises(ResearchInvalidInputError) as run_excinfo:
            _validate_action_payload(action, {"run_id": "RR-x", "code": "print(1)"})
        assert run_excinfo.value.code == "code_channel_forbidden"
    with pytest.raises(ResearchInvalidInputError) as raw_excinfo:
        _reject_code_fields({"module_path": "factors/x.py"})
    assert raw_excinfo.value.code == "code_channel_forbidden"


def test_code_run_target_is_artifact_or_name_only() -> None:
    with pytest.raises(ValidationError):
        CodeRunExecuteIn.model_validate(
            {
                "dataset_release_ids": ["rel-1"],
                "decision_at": "2024-06-03T15:00:00+08:00",
            }
        )
    with pytest.raises(ValidationError):
        CodeRunExecuteIn.model_validate(
            {
                "artifact_id": "RC-1",
                "name": "momentum",
                "dataset_release_ids": ["rel-1"],
                "decision_at": "2024-06-03T15:00:00+08:00",
            }
        )
    with pytest.raises(ValidationError):
        CodeRunExecuteIn.model_validate(
            {
                "name": "momentum",
                "dataset_release_ids": ["rel-1"],
                "decision_at": "2024-06-03T15:00:00",
            }
        )
    named = CodeRunExecuteIn.model_validate(
        {
            "name": "momentum",
            "dataset_release_ids": ["rel-1"],
            "decision_at": "2024-06-03T15:00:00+08:00",
        }
    )
    assert named.name == "momentum"
    assert named.artifact_id is None


def test_per_action_scope_enforcement() -> None:
    web = ResearchOperationContext.for_web()
    assert web.allows_scope("research.write") is True
    assert web.allows_scope("research.run") is True
    readonly = ResearchOperationContext.for_mcp(
        client_id="MC-read", scopes=frozenset({"research.read"})
    )
    assert readonly.allows_scope("research.write") is False
    assert readonly.allows_scope("research.run") is False
    writer = ResearchOperationContext.for_mcp(
        client_id="MC-write", scopes=frozenset({"research.write"})
    )
    assert writer.allows_scope("research.write") is True
    assert writer.allows_scope("research.run") is False
    # MCP context without resolved scopes is fail-closed.
    unresolved = ResearchOperationContext.for_mcp(client_id="MC-x")
    assert unresolved.allows_scope("research.write") is False


def test_web_sources_have_no_mcp_code_channel() -> None:
    sources = list(Path("web/src").rglob("*.ts")) + list(Path("web/src").rglob("*.tsx"))
    assert sources
    for path in sources:
        body = path.read_text(encoding="utf-8")
        assert "finboard_research_code_submit" not in body, path
        assert "source_code" not in body, path

