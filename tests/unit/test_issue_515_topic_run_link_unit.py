"""Unit-level link semantics (#515): state derivation and repository guards."""

from __future__ import annotations

from typing import Any

import pytest

from finboard_app.research_topic_actions import derive_link_state
from finboard_persistence.models import ResearchTopicLinkModel
from finboard_persistence.research_topic_link_repo import (
    ResearchTopicLinkConflictError,
    ResearchTopicLinkRepository,
)


def _link() -> ResearchTopicLinkModel:
    return ResearchTopicLinkModel(
        topic_id="RT-unit",
        goal_version="v1",
        entity_type="research_run",
        entity_id="RR-unit",
        job_id="BJ-unit",
        operation_id="OP-unit",
        created_by="user:api",
    )


def test_derive_link_state_matrix() -> None:
    assert derive_link_state(None, None) == "queued"
    assert derive_link_state("queued", "queued") == "queued"
    assert derive_link_state("running", "running") == "running"
    assert derive_link_state("running", "retry_waiting") == "running"
    assert derive_link_state("running", "cancel_requested") == "running"
    assert derive_link_state("running", "failed") == "failed"
    assert derive_link_state("running", "interrupted") == "interrupted"
    assert derive_link_state("completed", "succeeded") == "succeeded"
    assert derive_link_state("failed", "failed") == "failed"
    assert derive_link_state("cancelled", "cancelled") == "cancelled"
    assert derive_link_state("interrupted", "interrupted") == "interrupted"


async def test_operation_id_bound_elsewhere_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ResearchTopicLinkRepository(session=Any)  # type: ignore[arg-type]
    bound = _link()

    async def _by_operation(operation_id: str) -> ResearchTopicLinkModel:
        assert operation_id == "OP-unit"
        return bound

    async def _get(_topic_id: str, _entity_type: str, _entity_id: str) -> None:
        return None

    monkeypatch.setattr(repo, "get_by_operation_id", _by_operation)
    monkeypatch.setattr(repo, "get", _get)
    with pytest.raises(ResearchTopicLinkConflictError, match="operation_id"):
        await repo.create_or_get(
            topic_id="RT-other",
            goal_version="v1",
            entity_type="research_run",
            entity_id="RR-other",
            created_by="user:api",
            operation_id="OP-unit",
        )


async def test_existing_link_is_reused_without_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ResearchTopicLinkRepository(session=Any)  # type: ignore[arg-type]
    existing = _link()

    async def _by_operation(_operation_id: str) -> None:
        return None

    async def _get(_topic_id: str, _entity_type: str, _entity_id: str) -> ResearchTopicLinkModel:
        return existing

    monkeypatch.setattr(repo, "get_by_operation_id", _by_operation)
    monkeypatch.setattr(repo, "get", _get)
    link, created = await repo.create_or_get(
        topic_id="RT-unit",
        goal_version="v1",
        entity_type="research_run",
        entity_id="RR-unit",
        created_by="user:api",
        operation_id="OP-unit",
    )
    assert created is False
    assert link is existing
