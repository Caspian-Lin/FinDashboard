"""Server-enforced research topic lifecycle: draft, activate, pause, close (#514).

The lifecycle is deliberately small and explicit: the database column is the
authority, every transition needs ``expected_revision`` and appends an
``entry_type="lifecycle"`` round, and nothing here starts research work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.research_workspace import (
    Goal,
    TopicInput,
    WorkspaceInput,
    record_out,
)
from finboard_persistence.models import ResearchTopicModel
from finboard_persistence.research_topic_repo import ResearchTopicRepository

TopicStatus = Literal["draft", "active", "paused", "closed"]
TopicAction = Literal["pause", "resume", "close", "reopen", "discard"]

#: action -> target status
ACTION_TARGETS: dict[str, str] = {
    "activate": "active",
    "pause": "paused",
    "resume": "active",
    "close": "closed",
    "reopen": "active",
    "discard": "closed",
}

#: action -> statuses it may start from
ACTION_SOURCES: dict[str, tuple[str, ...]] = {
    "activate": ("draft",),
    "pause": ("active",),
    "resume": ("paused",),
    "close": ("active", "paused"),
    "reopen": ("closed",),
    "discard": ("draft",),
}

REASON_REQUIRED_ACTIONS = frozenset({"close", "reopen", "discard"})


def allowed_transitions(current: str) -> list[str]:
    """Target statuses reachable from ``current`` (the ``allowed`` 409 payload)."""
    return sorted(
        {
            ACTION_TARGETS[action]
            for action, sources in ACTION_SOURCES.items()
            if current in sources
        }
    )


def allowed_actions(current: str) -> list[str]:
    return sorted(action for action, sources in ACTION_SOURCES.items() if current in sources)


class TopicWriteConflictError(ValueError):
    """Structured 409 conflict; REST renders ``detail``, MCP maps it to conflict."""

    code = "topic_conflict"

    def __init__(self, message: str, *, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail: dict[str, Any] = {"code": self.code, **detail}


class TopicRevisionConflictError(TopicWriteConflictError):
    code = "topic_revision_conflict"

    def __init__(
        self,
        *,
        current_revision: int,
        your_revision: int,
        changed_fields: list[str],
        current_summary: dict[str, Any],
    ) -> None:
        super().__init__(
            f"课题版本已变化:当前 r{current_revision},你的 r{your_revision};请重新加载后再续接",
            detail={
                "current_revision": current_revision,
                "your_revision": your_revision,
                "changed_fields": changed_fields,
                "current_summary": current_summary,
            },
        )


class IllegalTransitionError(TopicWriteConflictError):
    code = "illegal_transition"

    def __init__(
        self,
        *,
        current: str,
        allowed: list[str],
        action: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message or f"课题状态 {current} 不允许 {action};允许的目标状态:{allowed}",
            detail={"allowed": allowed, "current": current, "action": action},
        )


class IdempotencyConflictError(TopicWriteConflictError):
    code = "idempotency_conflict"

    def __init__(self, key: str, message: str | None = None) -> None:
        super().__init__(
            message or f"幂等键 {key} 已绑定内容不同的课题;请换新键或读取原课题",
            detail={"idempotency_key": key},
        )


class TopicInvalidInputError(ValueError):
    """Request is well-formed but violates a topic invariant (REST 422)."""


def _assert_same_create(
    existing: ResearchTopicModel, incoming: dict[str, Any], *, draft: bool
) -> None:
    keys = ("title", "question") if draft else ("title", "question", "goal")
    current = existing.payload if isinstance(existing.payload, dict) else {}
    if any(current.get(key) != incoming.get(key) for key in keys):
        raise IdempotencyConflictError(str(existing.idempotency_key))
    # A key must not be reused across creation modes: a full create never turns
    # into a draft retry (or vice versa), even when title/question match.
    created_as = current.get("created_as")
    if created_as is None:
        created_as = "draft" if current.get("goal") is None else "full"
    if created_as != ("draft" if draft else "full"):
        raise IdempotencyConflictError(
            str(existing.idempotency_key),
            message=(
                f"幂等键 {existing.idempotency_key} 已绑定另一种创建模式"
                f"({created_as});draft_create 与 create 不能复用同一键"
            ),
        )


class TopicDraftInput(WorkspaceInput):
    """A draft only needs the question; the goal is added before activation."""

    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=4000)
    conclusion: Literal["unknown", "supported", "not_supported", "insufficient_evidence"] = (
        "unknown"
    )
    summary: str = Field(default="", max_length=4000)
    open_questions: list[str] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bounded_questions(self) -> TopicDraftInput:
        if any(not x.strip() or len(x) > 1000 for x in self.open_questions):
            raise ValueError("开放问题须非空且每项最多1000字")
        return self


class TopicActivateInput(WorkspaceInput):
    goal: Goal
    expected_revision: int | None = Field(default=None, ge=1)
    conclusion: Literal["unknown", "supported", "not_supported", "insufficient_evidence"] = (
        "unknown"
    )
    summary: str = Field(default="", max_length=4000)
    open_questions: list[str] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bounded_questions(self) -> TopicActivateInput:
        if any(not x.strip() or len(x) > 1000 for x in self.open_questions):
            raise ValueError("开放问题须非空且每项最多1000字")
        return self


class TopicTransitionInput(WorkspaceInput):
    action: TopicAction
    reason: str = Field(default="", max_length=2000)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def reason_required(self) -> TopicTransitionInput:
        if self.action in REASON_REQUIRED_ACTIONS and not self.reason.strip():
            raise ValueError(f"{self.action} 须填写原因,保留可复核的生命周期记录")
        return self


def _summary(row: ResearchTopicModel) -> dict[str, Any]:
    payload: Mapping[str, Any] = row.payload if isinstance(row.payload, dict) else {}
    goal = payload.get("goal")
    return {
        "status": row.status,
        "revision": row.revision,
        "goal_version": goal.get("version") if isinstance(goal, dict) else None,
        "title": payload.get("title"),
    }


def _changed_fields(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> list[str]:
    return sorted(
        key
        for key in ("title", "question", "goal", "status", "conclusion", "summary", "next_step")
        if current.get(key) != incoming.get(key)
    )


def _revision_conflict(
    row: ResearchTopicModel,
    *,
    expected: int,
    incoming: dict[str, Any],
    changed_fields: list[str] | None = None,
) -> TopicRevisionConflictError:
    return TopicRevisionConflictError(
        current_revision=row.revision,
        your_revision=expected,
        changed_fields=changed_fields or _changed_fields(row.payload, incoming),
        current_summary=_summary(row),
    )


async def create_topic(
    session: AsyncSession,
    *,
    actor: str,
    payload: dict[str, Any],
    draft: bool,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a draft or a full topic; returns ``(record, created)``."""
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
        raise TopicInvalidInputError("幂等键长度须为 1-128 字符")
    repo = ResearchTopicRepository(session)
    if draft:
        draft_data = TopicDraftInput.model_validate(payload)
        stored: dict[str, Any] = {
            "title": draft_data.title,
            "question": draft_data.question,
            "goal": None,
            "status": "draft",
            "conclusion": draft_data.conclusion,
            "summary": draft_data.summary,
            "open_questions": draft_data.open_questions,
            "next_step": draft_data.next_step,
            "created_as": "draft",
        }
        status = "draft"
    else:
        full = TopicInput.model_validate(payload)
        stored = {**full.model_dump(mode="json"), "created_as": "full"}
        status = str(stored.get("status") or "active")
        if status != "active":
            # Creating paused/closed topics would skip the lifecycle audit; create
            # active and use an explicit transition instead.
            raise TopicInvalidInputError(
                f"创建课题只支持 active;{status} 请先创建再用 /transitions 转移"
            )

    if idempotency_key:
        existing = await repo.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            _assert_same_create(existing, stored, draft=draft)
            return record_out(existing), False

    try:
        async with session.begin_nested():
            row = await repo.create(
                stored, actor, status=status, idempotency_key=idempotency_key
            )
    except IntegrityError:
        # Concurrent retry with the same key: the partial unique index decided.
        if not idempotency_key:
            raise
        existing = await repo.find_by_idempotency_key(idempotency_key)
        if existing is None:
            raise
        _assert_same_create(existing, stored, draft=draft)
        return record_out(existing), False
    return record_out(row), True


async def update_topic(
    session: AsyncSession,
    *,
    actor: str,
    topic_id: str,
    payload: dict[str, Any],
    expected_revision: int,
    status_declared: bool,
) -> dict[str, Any]:
    """Full-payload update. Never changes lifecycle state (that needs transitions)."""
    repo = ResearchTopicRepository(session)
    row = await repo.get(topic_id, lock=True)
    if row.revision != expected_revision:
        raise _revision_conflict(row, expected=expected_revision, incoming=payload)
    declared_status = str(payload.get("status")) if status_declared else None
    if row.status == "draft":
        if payload.get("goal") is not None:
            raise TopicInvalidInputError("草稿不能直接设置目标;请使用 /activate 补齐目标并激活")
        if declared_status is not None and declared_status != row.status:
            raise IllegalTransitionError(
                current=row.status,
                allowed=allowed_actions(row.status),
                action="update_status",
                message=(
                    f"PUT 不能改变课题状态({row.status} -> {declared_status});"
                    f"请使用 /activate 或 /transitions 执行 "
                    f"{'/'.join(allowed_actions(row.status)) or '无可用转移'}"
                ),
            )
        draft_payload = {k: v for k, v in payload.items() if k not in {"goal", "status"}}
        draft_data = TopicDraftInput.model_validate(draft_payload)
        data: dict[str, Any] = draft_data.model_dump(mode="json")
        data["goal"] = None
        data["created_as"] = "draft"
    else:
        data = TopicInput.model_validate(payload).model_dump(mode="json")
        if declared_status is not None and declared_status != row.status:
            raise IllegalTransitionError(
                current=row.status,
                allowed=allowed_actions(row.status),
                action="update_status",
                message=(
                    f"PUT 不能改变课题状态({row.status} -> {declared_status});"
                    f"请使用 POST /api/research/topics/{{id}}/transitions 执行 "
                    f"{'/'.join(allowed_actions(row.status)) or '无可用转移'}"
                ),
            )
        data["created_as"] = "full"
    # The lifecycle column is authoritative; PUT only ever keeps it in sync.
    data["status"] = row.status
    updated = await repo.update(topic_id, data, expected_revision, actor)
    return record_out(updated)


async def activate_topic(
    session: AsyncSession,
    *,
    actor: str,
    topic_id: str,
    payload: dict[str, Any],
    expected_revision: int | None,
) -> dict[str, Any]:
    data = TopicActivateInput.model_validate(payload)
    revision = expected_revision if expected_revision is not None else data.expected_revision
    if revision is None:
        raise ValueError("激活课题须提供 expected_revision")
    repo = ResearchTopicRepository(session)
    row = await repo.get(topic_id, lock=True)
    if row.revision != revision:
        raise _revision_conflict(
            row, expected=revision, incoming={"goal": data.goal.model_dump(mode="json")}
        )
    goal = data.goal.model_dump(mode="json")
    if row.status == "active":
        if row.payload.get("goal") == goal:
            # Lost receipt retry: already active with the same goal is a no-op.
            return record_out(row)
        raise IllegalTransitionError(
            current=row.status,
            allowed=allowed_transitions(row.status),
            action="activate",
            message="课题已激活且目标不同;目标变更须使用新目标版本更新,不能借激活覆盖",
        )
    if row.status != "draft":
        raise IllegalTransitionError(
            current=row.status,
            allowed=allowed_transitions(row.status),
            action="activate",
        )
    activated = await repo.apply_transition(
        row,
        target="active",
        action="activate",
        reason=data.reason or "补齐目标版本与来源后激活",
        actor=actor,
        payload_updates={
            "goal": goal,
            "conclusion": data.conclusion,
            "summary": data.summary,
            "open_questions": data.open_questions,
            "next_step": data.next_step,
        },
    )
    return record_out(activated)


async def transition_topic(
    session: AsyncSession,
    *,
    actor: str,
    topic_id: str,
    payload: dict[str, Any],
    expected_revision: int | None,
) -> dict[str, Any]:
    data = TopicTransitionInput.model_validate(payload)
    revision = expected_revision if expected_revision is not None else data.expected_revision
    if revision is None:
        raise ValueError("生命周期转移须提供 expected_revision")
    repo = ResearchTopicRepository(session)
    row = await repo.get(topic_id, lock=True)
    if row.revision != revision:
        raise _revision_conflict(row, expected=revision, incoming={}, changed_fields=["status"])
    if row.status not in ACTION_SOURCES[data.action]:
        raise IllegalTransitionError(
            current=row.status,
            allowed=allowed_transitions(row.status),
            action=data.action,
        )
    if data.action == "reopen" and not row.payload.get("goal"):
        # A discarded draft has no goal; reopening it would create an "active"
        # topic without acceptance criteria, which the activate contract forbids.
        raise IllegalTransitionError(
            current=row.status,
            allowed=allowed_transitions(row.status),
            action="reopen",
            message="重开需要已归档目标版本;被放弃的草稿没有目标,请新建课题",
        )
    changed = await repo.apply_transition(
        row,
        target=ACTION_TARGETS[data.action],
        action=data.action,
        reason=data.reason,
        actor=actor,
    )
    return record_out(changed)


__all__ = [
    "ACTION_SOURCES",
    "ACTION_TARGETS",
    "IdempotencyConflictError",
    "IllegalTransitionError",
    "TopicAction",
    "TopicActivateInput",
    "TopicDraftInput",
    "TopicRevisionConflictError",
    "TopicStatus",
    "TopicTransitionInput",
    "TopicWriteConflictError",
    "activate_topic",
    "allowed_actions",
    "allowed_transitions",
    "create_topic",
    "transition_topic",
    "update_topic",
]
