"""研究代码晋级门的纯单元测试(issue #219)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from finboard_app.config import Settings
from finboard_backtest.research_code import (
    PROMOTION_FAILED,
    PROMOTION_PASSED,
    PROMOTION_PENDING,
    PromotionScreenThresholds,
    evaluate_promotion_gates,
    is_promoted_artifact,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import research_code


def _screen() -> dict[str, object]:
    return {
        "factors": {
            "u_alpha": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {"momentum": 0.35, "pb": -0.2},
            }
        }
    }


def _validation() -> dict[str, object]:
    return {
        "experiment_id": "exp-alpha",
        "status": "validated_oos",
        "final_test_unsealed": True,
        "version_stamp": {
            "code_artifact_id": "RC-alpha",
            "code_artifact_name": "alpha",
            "code_kind": "factor",
            "code_commit": "a" * 40,
        },
    }


def test_promotion_requires_both_screen_and_oos_binding() -> None:
    result = evaluate_promotion_gates(
        screen=_screen(),
        validation=_validation(),
        artifact_id="RC-alpha",
        artifact_kind="factor",
        artifact_name="alpha",
        artifact_commit="a" * 40,
        require_artifact_binding=True,
    )

    assert result.passed
    assert result.screen_passed
    assert result.validation_passed
    assert result.as_dict()["thresholds"]["min_periods"] == 2


def test_promotion_fails_closed_with_named_screen_and_oos_failures() -> None:
    screen = {
        "strategy": {
            "n_periods": 1,
            "rank_ic": 0.01,
            "average_turnover": 0.9,
            "correlation": {"momentum": 0.9},
        }
    }
    validation = {
        "status": "in_sample",
        "final_test_unsealed": False,
        "version_stamp": {},
    }
    result = evaluate_promotion_gates(screen=screen, validation=validation)

    assert not result.passed
    assert not result.screen_passed
    assert not result.validation_passed
    assert any("rank_ic_below_minimum" in failure for failure in result.failures)
    assert any("turnover_above_maximum" in failure for failure in result.failures)
    assert "validation.status_not_validated_oos" in " ".join(result.failures)
    assert "validation.final_test_not_unsealed" in result.failures


def test_missing_evidence_never_defaults_to_pass() -> None:
    result = evaluate_promotion_gates(screen=None, validation=None)

    assert not result.passed
    assert result.failures == ("screen_missing", "validation_missing")


def test_custom_thresholds_are_applied_and_serialized() -> None:
    result = evaluate_promotion_gates(
        screen={
            "rank_ic": 0.03,
            "n_periods": 4,
            "average_turnover": 0.4,
            "correlation": {"pb": 0.5},
        },
        validation={"status": "validated_oos", "final_test_unsealed": True},
        thresholds=PromotionScreenThresholds(
            min_abs_rank_ic=0.04,
            max_average_turnover=0.3,
            max_abs_correlation=0.4,
            min_periods=5,
        ),
    )

    assert not result.passed
    assert result.as_dict()["thresholds"] == {
        "min_abs_rank_ic": 0.04,
        "max_average_turnover": 0.3,
        "max_abs_correlation": 0.4,
        "min_periods": 5,
    }


def test_artifact_lifecycle_requires_active_and_passed() -> None:
    draft = SimpleNamespace(status="draft", promotion_status=PROMOTION_PENDING)
    failed = SimpleNamespace(status="draft", promotion_status=PROMOTION_FAILED)
    active = SimpleNamespace(status="active", promotion_status=PROMOTION_PASSED)
    retired = SimpleNamespace(status="retired", promotion_status=PROMOTION_PASSED)

    assert not is_promoted_artifact(draft)
    assert not is_promoted_artifact(failed)
    assert is_promoted_artifact(active)
    assert not is_promoted_artifact(retired)


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _promotion_app(tmp_path: Path) -> tuple[McpAppContext, SimpleNamespace]:
    session = SimpleNamespace(commit=AsyncMock())
    app = SimpleNamespace(
        settings=Settings(research_code_repo_path=str(tmp_path / "code.git")),
        write_tools_enabled=True,
        session_maker=lambda: _SessionContext(session),
        audit=AuditRecorder(),
    )
    return cast(McpAppContext, app), session


def _promotion_artifact() -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id="RC-alpha",
        kind="factor",
        name="alpha",
        commit="a" * 40,
        checksum="code-checksum",
        path="factors/alpha",
        status="draft",
        promotion_status=PROMOTION_PENDING,
        validation_experiment_id=None,
        screen_run_id=None,
        promotion_evidence=None,
        promoted_at=None,
        retired_at=None,
        created_by="agent:mcp",
        created_at=None,
        updated_at=None,
    )


def _promotion_validation() -> dict[str, object]:
    return {
        "experiment_id": "EXP-alpha",
        "status": "validated_oos",
        "final_test_unsealed": True,
        "version_stamp": {
            "code_artifact_id": "RC-alpha",
            "code_artifact_name": "alpha",
            "code_kind": "factor",
            "code_commit": "a" * 40,
        },
    }


def _promotion_code_run(*, metrics: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="RCR-screen",
        artifact_id="RC-alpha",
        kind="factor",
        name="alpha",
        commit="a" * 40,
        code_checksum="code-checksum",
        dataset_release_ids=["REL-1"],
        dataset_release_checksums={"REL-1": "release-checksum"},
        decision_at=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        params={"window": 20},
        image="finboard-research-sandbox:0.2.0",
        image_digest="sha256:image",
        mount_manifest_checksum="mount-checksum",
        scores_checksum="scores-checksum",
        output_snapshot_id="FS-alpha",
        status="succeeded",
        error_code=None,
        error_summary=None,
        exit_code=0,
        timed_out=False,
        oom_killed=False,
        usage={"max_mem_mb": 32},
        metrics=metrics,
        artifact_dir="data_cache/research_sandbox/RCR-screen",
    )


class _PromotionArtifactRepo:
    def __init__(self, artifact: SimpleNamespace) -> None:
        self.artifact = artifact
        self.promote_calls: list[dict[str, object]] = []
        self.failed_calls: list[dict[str, object]] = []

    async def get(self, _artifact_id: str) -> SimpleNamespace:
        return self.artifact

    async def promote(self, _artifact_id: str, **kwargs: object) -> SimpleNamespace:
        self.promote_calls.append(kwargs)
        self.artifact.status = "active"
        self.artifact.promotion_status = PROMOTION_PASSED
        self.artifact.validation_experiment_id = kwargs["validation_experiment_id"]
        self.artifact.screen_run_id = kwargs["screen_run_id"]
        self.artifact.promotion_evidence = kwargs["evidence"]
        return self.artifact

    async def mark_promotion_failed(self, _artifact_id: str, **kwargs: object) -> SimpleNamespace:
        self.failed_calls.append(kwargs)
        self.artifact.promotion_status = PROMOTION_FAILED
        self.artifact.promotion_evidence = kwargs["evidence"]
        return self.artifact


class _PromotionExperiment:
    def __init__(self, validation: dict[str, object]) -> None:
        self.validation = validation

    def as_dict(self) -> dict[str, object]:
        return self.validation


class _PromotionExperimentRepo:
    def __init__(self, validation: dict[str, object]) -> None:
        self.validation = validation

    async def get(self, _experiment_id: str) -> _PromotionExperiment:
        return _PromotionExperiment(self.validation)


class _PromotionCodeRunRepo:
    def __init__(self, code_run: SimpleNamespace) -> None:
        self.code_run = code_run

    async def get(self, _run_id: str) -> SimpleNamespace:
        return self.code_run


async def test_promote_binds_evidence_and_enters_formal_whitelist(tmp_path, monkeypatch) -> None:
    app, session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(
        metrics={
            "screen": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {"momentum": 0.35},
            }
        }
    )
    monkeypatch.setattr(
        research_code, "ResearchCodeArtifactRepository", lambda _session: artifact_repo
    )
    monkeypatch.setattr(
        research_code,
        "ResearchExperimentRepository",
        lambda _session: _PromotionExperimentRepo(_promotion_validation()),
    )
    monkeypatch.setattr(
        research_code,
        "ResearchCodeRunRepository",
        lambda _session: _PromotionCodeRunRepo(code_run),
    )

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "ok"
    data = cast(dict[str, Any], env.data)
    assert data["status"] == "active"
    assert data["promotion_status"] == PROMOTION_PASSED
    assert is_promoted_artifact(artifact)
    assert not artifact_repo.failed_calls
    assert session.commit.await_count == 1
    evidence = cast(dict[str, object], artifact_repo.promote_calls[0]["evidence"])
    gates = cast(dict[str, object], evidence["gates"])
    execution = cast(dict[str, object], evidence["execution"])
    assert gates["passed"] is True
    assert set(cast(list[str], execution["audit_refs"])) == {
        "code",
        "data",
        "parameters",
        "output",
        "container",
    }


async def test_promote_failure_keeps_draft_and_records_named_evidence(
    tmp_path, monkeypatch
) -> None:
    app, session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(
        metrics={
            "screen": {
                "n_periods": 1,
                "rank_ic": 0.01,
                "average_turnover": 0.9,
                "correlation": {"momentum": 0.9},
            }
        }
    )
    monkeypatch.setattr(
        research_code, "ResearchCodeArtifactRepository", lambda _session: artifact_repo
    )
    monkeypatch.setattr(
        research_code,
        "ResearchExperimentRepository",
        lambda _session: _PromotionExperimentRepo(_promotion_validation()),
    )
    monkeypatch.setattr(
        research_code,
        "ResearchCodeRunRepository",
        lambda _session: _PromotionCodeRunRepo(code_run),
    )

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "error"
    assert env.error is not None
    assert env.error.kind == "invalid_argument"
    assert "rank_ic_below_minimum" in env.error.message
    assert "turnover_above_maximum" in env.error.message
    assert artifact.status == "draft"
    assert artifact.promotion_status == PROMOTION_FAILED
    assert not artifact_repo.promote_calls
    evidence = cast(dict[str, object], artifact_repo.failed_calls[0]["evidence"])
    gates = cast(dict[str, object], evidence["gates"])
    assert gates["passed"] is False
    assert session.commit.await_count == 1


async def test_missing_screen_is_also_persisted_as_failed_evidence(tmp_path, monkeypatch) -> None:
    app, session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(metrics={})
    monkeypatch.setattr(
        research_code, "ResearchCodeArtifactRepository", lambda _session: artifact_repo
    )
    monkeypatch.setattr(
        research_code,
        "ResearchExperimentRepository",
        lambda _session: _PromotionExperimentRepo(_promotion_validation()),
    )
    monkeypatch.setattr(
        research_code,
        "ResearchCodeRunRepository",
        lambda _session: _PromotionCodeRunRepo(code_run),
    )

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "error"
    assert env.error is not None
    assert env.error.kind == "invalid_argument"
    assert "机器 screen 指标" in env.error.message
    assert artifact.status == "draft"
    assert artifact.promotion_status == PROMOTION_FAILED
    evidence = cast(dict[str, object], artifact_repo.failed_calls[0]["evidence"])
    assert evidence["execution"] == {}
    assert session.commit.await_count == 1
