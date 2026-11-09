"""Topic-scoped factor / sandbox / OOS actions and evidence refs (#516).

The fixture seeds the #513/#515 scenario once (fixed strategy/release IDs) and
re-creates the factor snapshot / code artifact per test, so every assertion runs
against the real PostgreSQL test DB. No live trading table is written anywhere
in this module: the last test asserts that explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.research_audit import (
    ResearchAuditRecorder,
    configure_research_audit_recorder,
    reset_research_audit_recorder,
)
from finboard_app.research_operation import (
    ResearchConflictError,
    ResearchInvalidInputError,
    ResearchNotFoundError,
    ResearchOperationContext,
    ResearchPreconditionFailedError,
)
from finboard_app.research_topic_artifacts import (
    ARTIFACT_ENTITY_TYPES,
    CODE_FORBIDDEN_FIELDS,
    CodeRunExecuteIn,
    run_topic_action,
)
from finboard_app.research_workspace import (
    RoundInput,
    source_fact,
    topic_context,
    topic_refs_check,
    workspace_read,
    workspace_write,
)
from finboard_backtest.validation.contracts import TrialStatus
from finboard_data.factor_lab import (
    FeatureObservation,
    FeatureSnapshot,
    _feature_snapshot_checksum,
)
from finboard_persistence import (
    ResearchCodeArtifactRepository,
    ResearchCodeRunRepository,
    ResearchExperimentRepository,
    ResearchTrialRepository,
    session_factory,
)
from finboard_persistence.dataset_release_repo import (
    ResearchDatasetReleaseRepository,
)
from finboard_persistence.factor_lab_repo import FeatureSnapshotRepository
from finboard_persistence.models import (
    BackgroundJobModel,
    FactorExperimentModel,
    FactorFeatureSnapshotModel,
    ResearchAuditEventModel,
    ResearchCodeArtifactModel,
    ResearchCodeRunModel,
    ResearchExperimentModel,
    ResearchRunModel,
    ResearchTopicEntryModel,
    ResearchTopicLinkModel,
    ResearchTopicModel,
    ResearchTrialModel,
)
from tests.integration.conftest import clean_tables
from tests.integration.test_research_run_submission_equivalence import (
    BARS_RELEASE,
    _payload,
    _seed,
)
from tests.unit.validation.test_issue_310_oos_outcome import (
    _experiment,
    _to_validated_oos,
    _trial,
)

pytestmark = pytest.mark.asyncio

GOAL: dict[str, Any] = {
    "version": "v1",
    "criteria": "年化15%,最大回撤20%",
    "source": {"kind": "document", "ref_id": "ROADMAP.md"},
}

SNAPSHOT_ID = "integration-516-snapshot"
#: Filled by :func:`_make_snapshot` (content-addressed, depends on the release).
SNAPSHOT_CHECKSUM = ""
DOC_PATH = "workspace-contract.md"

_SEEDED = False
_SEED_LOCK = asyncio.Lock()


async def _ensure_seed(engine: AsyncEngine) -> None:
    global _SEEDED
    async with _SEED_LOCK:
        if _SEEDED:
            return
        async with engine.begin() as conn:
            await clean_tables(conn)
        await _seed(engine)
        _SEEDED = True


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncEngine:
    return _engine


async def _clean_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for model in (
            ResearchTopicLinkModel,
            ResearchTopicEntryModel,
            ResearchTopicModel,
            ResearchAuditEventModel,
            BackgroundJobModel,
            ResearchRunModel,
            ResearchTrialModel,
            ResearchExperimentModel,
            ResearchCodeRunModel,
            ResearchCodeArtifactModel,
            FactorExperimentModel,
            FactorFeatureSnapshotModel,
        ):
            await conn.execute(delete(model))


@pytest_asyncio.fixture(autouse=True)
async def _isolate(engine: AsyncEngine) -> AsyncIterator[None]:
    await _ensure_seed(engine)
    await _clean_business(engine)
    yield
    await _clean_business(engine)
    reset_research_audit_recorder()


@pytest_asyncio.fixture
async def audit(engine: AsyncEngine) -> ResearchAuditRecorder:
    recorder = ResearchAuditRecorder(session_maker=session_factory(engine))
    configure_research_audit_recorder(recorder)
    return recorder


@pytest_asyncio.fixture
async def client(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> AsyncIterator[httpx.AsyncClient]:
    from finboard_api.deps import get_db_session
    from finboard_api.routes.research_runs import router as research_runs_router
    from finboard_api.routes.research_topic_actions import topic_actions_router
    from finboard_api.routes.research_topic_runs import (
        run_links_router,
        topic_runs_router,
    )
    from finboard_api.routes.research_topics import router as research_topics_router

    smaker = session_factory(engine)
    app = FastAPI()
    app.state.settings = SimpleNamespace(research_sandbox_enabled=True)
    app.include_router(research_topics_router)
    app.include_router(topic_runs_router)
    app.include_router(topic_actions_router)
    app.include_router(run_links_router)
    app.include_router(research_runs_router)

    async def _override() -> Any:
        async with smaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def _topic_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "课题接入因子与OOS",
        "question": "因子实验、沙盒运行与OOS能否精确关联?",
        "goal": dict(GOAL),
        **overrides,
    }


async def _create_topic(engine: AsyncEngine) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        record = await workspace_write(
            session, actor="user:api", operation="create", payload=_topic_payload()
        )
    # topic.create itself is audited (#518); start from a clean audit slate.
    async with engine.begin() as conn:
        await conn.execute(delete(ResearchAuditEventModel))
    return record


def _ctx(operation_id: str | None = None) -> ResearchOperationContext:
    return ResearchOperationContext.for_web(actor="user:api", operation_id=operation_id)


async def _count(engine: AsyncEngine, model: Any) -> int:
    async with session_factory(engine)() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _audit_rows(
    engine: AsyncEngine, operation: str | None = None
) -> list[ResearchAuditEventModel]:
    async with session_factory(engine)() as session:
        stmt = select(ResearchAuditEventModel).order_by(ResearchAuditEventModel.id)
        if operation is not None:
            stmt = stmt.where(ResearchAuditEventModel.operation == operation)
        return list((await session.execute(stmt)).scalars())


async def _make_snapshot(engine: AsyncEngine) -> str:
    global SNAPSHOT_CHECKSUM
    async with session_factory(engine)() as session:
        release = await ResearchDatasetReleaseRepository(session).require_usable(
            BARS_RELEASE
        )
        snapshot = FeatureSnapshot(
            snapshot_id=SNAPSHOT_ID,
            dataset_release_id=BARS_RELEASE,
            dataset_release_checksum=release.release_checksum,
            decision_at=datetime(2024, 6, 3, 15, tzinfo=UTC),
            published_at=datetime(2024, 6, 3, 16, tzinfo=UTC),
            framework_version="v2",
            calculation_windows={},
            transformations={},
            neutralization={},
            code_version="test",
            observations=(
                FeatureObservation(
                    symbol="600000.SH",
                    feature_name="momentum",
                    value=1.0,
                    observed_at=datetime(2024, 6, 3, 14, tzinfo=UTC),
                    available_at=datetime(2024, 6, 3, 15, tzinfo=UTC),
                    source="integration",
                    source_version="v1",
                ),
            ),
            checksum=SNAPSHOT_CHECKSUM,
        )
        # Content-addressed checksum: publish re-verifies it via from_dict.
        snapshot = replace(snapshot, checksum=_feature_snapshot_checksum(snapshot))
        SNAPSHOT_CHECKSUM = snapshot.checksum
        await FeatureSnapshotRepository(session).publish(snapshot)
        await session.commit()
    return SNAPSHOT_ID


async def _make_artifact(
    engine: AsyncEngine, *, status: str = "active", name: str = "momentum_factor"
) -> Any:
    async with session_factory(engine)() as session:
        artifact = await ResearchCodeArtifactRepository(session).register(
            kind="factor",
            name=name,
            commit="a" * 40,
            path="factors/momentum.py",
            checksum="b" * 32,
            created_by="agent:test",
            status=status,
        )
        await session.commit()
    return artifact


def _factor_payload() -> dict[str, Any]:
    return {
        "hypothesis": "20日动量因子在A股中期有效",
        "factor_names": ["momentum"],
        "dataset_release_id": BARS_RELEASE,
        "feature_snapshot_id": SNAPSHOT_ID,
        "comparison_group": "momentum-516",
        "plan": {
            "in_sample_start": "2020-01-01",
            "in_sample_end": "2022-12-31",
            "oos_start": "2023-01-01",
            "oos_end": "2023-12-31",
            "trial_budget": 10,
            "benchmark_symbol": "000300.SH",
            "transaction_cost_bps": 5,
            "quantiles": 5,
        },
    }


def _code_run_payload(artifact_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "factor",
        "artifact_id": artifact_id,
        "dataset_release_ids": [BARS_RELEASE],
        "decision_at": "2024-06-03T15:00:00+08:00",
    }
    payload.update(overrides)
    return payload


async def _save_experiment(engine: AsyncEngine, experiment: Any) -> str:
    async with session_factory(engine)() as session:
        await ResearchExperimentRepository(session).save(experiment)
        await session.commit()
    return str(experiment.experiment_id)


def _validation_payload() -> dict[str, Any]:
    return {
        "hypothesis": "验证动量因子样本外有效性(#516 集成)",
        "version_stamp": {
            "matching_model_version": "v2",
            "asset_rules_version": "v1",
            "strategy_kind": "ma_cross",
        },
        "plan": {
            "mode": "rolling",
            "train_start": "2020-01-01",
            "train_end": "2020-06-30",
            "validation_start": "2020-07-01",
            "validation_end": "2020-12-31",
            "test_start": "2021-01-01",
            "test_end": "2021-06-30",
            "train_window_days": 126,
            "test_window_days": 21,
            "step_days": 21,
            "trial_budget": 4,
        },
        "robustness": {"stress_phases": []},
    }


# --------------------------------------------------------------------------- #
# B. plan / review entries
# --------------------------------------------------------------------------- #


async def test_plan_entry_writes_no_run_or_job(engine: AsyncEngine, client: httpx.AsyncClient) -> None:
    topic = await _create_topic(engine)
    payload = {
        "entry_type": "plan",
        "goal_version": "v1",
        "objective": "预注册动量因子设计",
        "action": "20日动量,月度调仓,方向为多",
        "rationale": "中期动量在A股有正溢价",
        "source_refs": [
            {"kind": "factor_snapshot", "ref_id": SNAPSHOT_ID}
        ],
    }
    response = await client.post(
        f"/api/research/topics/{topic['topic_id']}/entries",
        json={"idempotency_key": "plan-516", "round": payload},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["entry_type"] == "plan"
    assert body["evidence_level"] == "research_plan"
    # A plan never starts execution: no run, no job, no link.
    assert await _count(engine, ResearchRunModel) == 0
    assert await _count(engine, BackgroundJobModel) == 0
    assert await _count(engine, ResearchTopicLinkModel) == 0


async def test_review_requires_document_ref_and_checksum(engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="document_ref"):
        RoundInput.model_validate(
            {
                "entry_type": "review",
                "goal_version": "v1",
                "objective": "复核",
                "action": "读取正式文档",
                "rationale": "结论已复核",
            }
        )
    with pytest.raises(ValueError, match="checksum"):
        RoundInput.model_validate(
            {
                "entry_type": "review",
                "goal_version": "v1",
                "objective": "复核",
                "action": "读取正式文档",
                "rationale": "结论已复核",
                "document_ref": {"kind": "document", "ref_id": DOC_PATH},
            }
        )
    with pytest.raises(ValueError, match="document_ref"):
        RoundInput.model_validate(
            {
                "entry_type": "round",
                "goal_version": "v1",
                "objective": "执行",
                "action": "跑回测",
                "rationale": "验证",
                "document_ref": {
                    "kind": "document",
                    "ref_id": DOC_PATH,
                    "checksum": "d" * 64,
                },
            }
        )

    topic = await _create_topic(engine)
    digest = (
        Path("docs/research") / DOC_PATH
    ).read_bytes()
    import hashlib

    document_checksum = hashlib.sha256(digest).hexdigest()
    async with session_factory(engine)() as session:
        record = await workspace_write(
            session,
            actor="user:api",
            operation="append",
            topic_id=topic["topic_id"],
            idempotency_key="review-516",
            payload={
                "entry_type": "review",
                "goal_version": "v1",
                "objective": "复核工作结论",
                "action": "阅读正式契约文档",
                "rationale": "结论与文档一致",
                "conclusion": "接受为经复核结论",
                "document_ref": {
                    "kind": "document",
                    "ref_id": DOC_PATH,
                    "checksum": document_checksum,
                },
            },
        )
    assert record["entry_type"] == "review"
    assert record["evidence_level"] == "reviewed_conclusion"
    assert record["document_ref"]["checksum"] == document_checksum

    # The review document ref is probed by refs/check like any other evidence.
    async with session_factory(engine)() as session:
        probe = await topic_refs_check(
            session, topic["topic_id"], rounds=5, docs_root=Path("docs/research")
        )
    assert probe["total"] == 1
    assert probe["items"][0]["status"] == "matched"
    assert probe["items"][0]["ref"]["kind"] == "document"


async def test_context_separates_plan_executed_and_reviewed(engine: AsyncEngine) -> None:
    topic = await _create_topic(engine)
    common = {
        "goal_version": "v1",
        "objective": "条目",
        "action": "动作",
        "rationale": "理由",
    }
    async with session_factory(engine)() as session:
        await workspace_write(
            session,
            actor="user:api",
            operation="append",
            topic_id=topic["topic_id"],
            idempotency_key="plan-ctx",
            payload={**common, "entry_type": "plan"},
        )
        await workspace_write(
            session,
            actor="agent:mcp",
            operation="append",
            topic_id=topic["topic_id"],
            idempotency_key="round-ctx",
            payload={**common, "entry_type": "round", "outcome": "completed"},
        )
    async with session_factory(engine)() as session:
        context = await topic_context(session, topic["topic_id"], rounds=10, refs=0)
    assert context["entry_types"] == {"plan": 1, "round": 1}
    levels = {item["entry_type"]: item["evidence_level"] for item in context["recent_rounds"]}
    assert levels["plan"] == "research_plan"
    assert levels["round"] == "agent_interpretation"


# --------------------------------------------------------------------------- #
# C. whitelisted actions: delegate + link + audit
# --------------------------------------------------------------------------- #


async def test_factor_experiment_action_links_and_audits(engine: AsyncEngine) -> None:
    await _make_snapshot(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        result = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="factor_experiment.create",
            goal_version="v1",
            payload=_factor_payload(),
            ctx=_ctx("OP-516-factor"),
        )
    assert result["created"] is True
    assert result["entity_type"] == "factor_experiment"
    experiment_id = result["entity_id"]
    assert await _count(engine, FactorExperimentModel) == 1
    async with session_factory(engine)() as session:
        row = (
            await session.execute(
                select(ResearchTopicLinkModel).where(
                    ResearchTopicLinkModel.entity_id == experiment_id
                )
            )
        ).scalar_one()
        assert row.entity_type == "factor_experiment"
        assert row.goal_version == "v1"
        assert row.operation_id == "OP-516-factor"
    events = await _audit_rows(engine, "topic.action.factor_experiment.create")
    assert len(events) == 1
    assert events[0].outcome == "succeeded"
    assert events[0].entity_id == experiment_id
    assert events[0].correlation["topic_id"] == topic["topic_id"]
    assert await _count(engine, BackgroundJobModel) == 0  # 登记不执行


async def test_action_requires_active_topic_and_goal_version(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    await _make_snapshot(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        await workspace_write(
            session,
            actor="user:api",
            operation="transition",
            topic_id=topic["topic_id"],
            expected_revision=1,
            payload={"action": "pause", "reason": "暂停"},
        )
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchPreconditionFailedError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="factor_experiment.create",
                goal_version="v1",
                payload=_factor_payload(),
                ctx=_ctx(),
            )
    assert excinfo.value.code == "topic_not_active"
    assert await _count(engine, FactorExperimentModel) == 0
    events = await _audit_rows(engine, "topic.action.factor_experiment.create")
    assert [event.outcome for event in events] == ["rejected"]

    await _clean_business(engine)
    await _make_snapshot(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchPreconditionFailedError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="factor_experiment.create",
                goal_version="v9",
                payload=_factor_payload(),
                ctx=_ctx(),
            )
    assert excinfo.value.code == "goal_version_mismatch"


async def test_code_run_execute_requires_registered_artifact(engine: AsyncEngine) -> None:
    artifact = await _make_artifact(engine)
    topic = await _create_topic(engine)
    # Inline code is not part of any request model.
    assert "code" not in CodeRunExecuteIn.model_fields
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchInvalidInputError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="code_run.execute",
                goal_version="v1",
                payload={**_code_run_payload(artifact.artifact_id), "code": "print(1)"},
                ctx=_ctx(),
            )
    assert excinfo.value.code == "code_channel_forbidden"

    async with session_factory(engine)() as session:
        result = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="code_run.execute",
            goal_version="v1",
            payload=_code_run_payload(artifact.artifact_id, symbols=["600000.SH"]),
            ctx=_ctx("OP-516-run"),
            sandbox_enabled=True,
        )
    assert result["created"] is True
    assert result["entity_type"] == "code_run"
    assert result["entity_id"] == result["job_id"]
    assert await _count(engine, BackgroundJobModel) == 1
    async with session_factory(engine)() as session:
        job = (await session.execute(select(BackgroundJobModel))).scalar_one()
        assert job.kind == "research_code_run"
        assert job.payload["commit"] == artifact.commit
        assert "code" not in job.payload
        link = (
            await session.execute(
                select(ResearchTopicLinkModel).where(
                    ResearchTopicLinkModel.entity_type == "code_run"
                )
            )
        ).scalar_one()
        assert link.job_id == job.job_id
        assert link.entity_id == job.job_id
    assert len(await _audit_rows(engine, "topic.action.code_run.execute")) == 1

    # Lost-receipt retry: the same operation_id returns the original link/job.
    async with session_factory(engine)() as session:
        replay = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="code_run.execute",
            goal_version="v1",
            payload=_code_run_payload(artifact.artifact_id, symbols=["600000.SH"]),
            ctx=_ctx("OP-516-run"),
            sandbox_enabled=True,
        )
    assert replay["created"] is False
    assert replay["entity_id"] == result["entity_id"]
    assert await _count(engine, BackgroundJobModel) == 1
    assert await _count(engine, ResearchTopicLinkModel) == 1


async def test_code_run_execute_rejects_unknown_or_retired_artifact(
    engine: AsyncEngine,
) -> None:
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchNotFoundError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="code_run.execute",
                goal_version="v1",
                payload=_code_run_payload("RC-unknown"),
                ctx=_ctx(),
                sandbox_enabled=True,
            )
    assert excinfo.value.code == "artifact_not_found"

    artifact = await _make_artifact(engine, status="retired", name="retired_factor")
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchConflictError) as retired_exc:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="code_run.execute",
                goal_version="v1",
                payload=_code_run_payload(artifact.artifact_id),
                ctx=_ctx(),
                sandbox_enabled=True,
            )
    assert retired_exc.value.code == "artifact_retired"
    assert await _count(engine, BackgroundJobModel) == 0


async def test_code_run_execute_names_disabled_sandbox(engine: AsyncEngine) -> None:
    artifact = await _make_artifact(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchPreconditionFailedError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="code_run.execute",
                goal_version="v1",
                payload=_code_run_payload(artifact.artifact_id),
                ctx=_ctx(),
                sandbox_enabled=False,
            )
    assert excinfo.value.code == "sandbox_disabled"
    assert await _count(engine, BackgroundJobModel) == 0


async def test_validation_experiment_create_add_trial_and_unseal(
    engine: AsyncEngine,
) -> None:
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        created = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="validation_experiment.create",
            goal_version="v1",
            payload=_validation_payload(),
            ctx=_ctx("OP-516-exp"),
        )
    experiment_id = created["entity_id"]
    assert await _count(engine, ResearchExperimentModel) == 1

    async with session_factory(engine)() as session:
        trial = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="validation_experiment.add_trial",
            goal_version="v1",
            payload={
                "experiment_id": experiment_id,
                "parameters": {"window": 20},
                "status": "selected",
            },
            ctx=_ctx("OP-516-trial"),
        )
    assert trial["result"]["trial_index"] == 0
    assert await _count(engine, ResearchTrialModel) == 1
    async with session_factory(engine)() as session:
        experiment = await ResearchExperimentRepository(session).get(experiment_id)
        assert experiment is not None
        assert experiment.trials_used == 1

    async with session_factory(engine)() as session:
        unsealed = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="validation_experiment.unseal",
            goal_version="v1",
            payload={"experiment_id": experiment_id},
            ctx=_ctx("OP-516-unseal"),
        )
    assert unsealed["result"]["created"] is True
    assert unsealed["job_id"] is not None
    assert await _count(engine, BackgroundJobModel) == 1

    # One link per (topic, entity) but different operations keep their own audit.
    async with session_factory(engine)() as session:
        links = list(
            (
                await session.execute(
                    select(ResearchTopicLinkModel).where(
                        ResearchTopicLinkModel.entity_type == "validation_experiment"
                    )
                )
            ).scalars()
        )
        assert len(links) == 1
    operations = {event.operation for event in await _audit_rows(engine)}
    assert "topic.action.validation_experiment.create" in operations
    assert "topic.action.validation_experiment.add_trial" in operations
    assert "topic.action.validation_experiment.unseal" in operations
    assert "job.enqueue" in operations


async def test_duplicate_unseal_is_conflict_and_does_not_touch_state(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    experiment = _to_validated_oos(_experiment())
    # Simulate the worker having completed the one-time unseal.
    experiment = replace(experiment, final_test_unsealed=True)
    experiment_id = await _save_experiment(engine, experiment)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchConflictError) as excinfo:
            await run_topic_action(
                session,
                topic_id=topic["topic_id"],
                action="validation_experiment.unseal",
                goal_version="v1",
                payload={"experiment_id": experiment_id},
                ctx=_ctx("OP-516-dup"),
            )
    assert excinfo.value.code == "already_unsealed"
    assert await _count(engine, BackgroundJobModel) == 0
    async with session_factory(engine)() as session:
        stored = await ResearchExperimentRepository(session).get(experiment_id)
        assert stored is not None
        assert stored.final_test_unsealed is True
        assert stored.status.value == "validated_oos"
    events = await _audit_rows(engine, "topic.action.validation_experiment.unseal")
    assert [event.outcome for event in events] == ["rejected"]
    assert events[0].result_code == "already_unsealed"


async def test_run_submit_action_delegates_to_515(engine: AsyncEngine) -> None:
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        result = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="run.submit",
            goal_version="v1",
            payload=_payload("topic-action-516"),
            ctx=_ctx("OP-516-run-submit"),
        )
    assert result["action"] == "run.submit"
    assert result["entity_type"] == "research_run"
    assert result["run_id"].startswith("RR-")
    assert await _count(engine, ResearchRunModel) == 1
    assert await _count(engine, BackgroundJobModel) == 1
    # #515 owns the audit operation for run submission (no double event).
    assert len(await _audit_rows(engine, "topic.run.start")) == 1
    assert await _audit_rows(engine, "topic.action.run.submit") == []


# --------------------------------------------------------------------------- #
# A/D. evidence refs: new kinds, missing objects, oversized trial sets
# --------------------------------------------------------------------------- #


async def test_new_ref_kinds_resolve_and_missing_objects_are_named(
    engine: AsyncEngine,
) -> None:
    await _make_snapshot(engine)
    artifact = await _make_artifact(engine)
    experiment = _to_validated_oos(_experiment())
    experiment_id = await _save_experiment(engine, experiment)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        factor = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="factor_experiment.create",
            goal_version="v1",
            payload=_factor_payload(),
            ctx=_ctx("OP-516-helper"),
        )
    code_run_id = await _make_code_run_row(engine, artifact.artifact_id)

    refs = [
        {"kind": "factor_experiment", "ref_id": factor["entity_id"]},
        {
            "kind": "code_artifact",
            "ref_id": artifact.artifact_id,
            "version": artifact.commit,
            "checksum": artifact.checksum,
        },
        {"kind": "code_run", "ref_id": code_run_id},
        {
            "kind": "validation_experiment",
            "ref_id": experiment_id,
            "checksum": experiment.version_stamp.checksum(),
        },
        {
            "kind": "factor_snapshot",
            "ref_id": SNAPSHOT_ID,
            "version": "v2",
            "checksum": SNAPSHOT_CHECKSUM,
        },
    ]
    async with session_factory(engine)() as session:
        for ref in refs:
            fact = await source_fact(session, ref, Path("docs/research"))
            assert fact["status"] == "matched", (ref, fact)
            assert fact["facts"]["evidence_level"] == "automatic_fact"

    async with session_factory(engine)() as session:
        missing = await source_fact(
            session, {"kind": "code_artifact", "ref_id": "RC-missing"}, Path("docs/research")
        )
        assert missing["status"] == "missing"
        mismatch = await source_fact(
            session,
            {"kind": "code_artifact", "ref_id": artifact.artifact_id, "version": "0" * 40},
            Path("docs/research"),
        )
        assert mismatch["status"] == "version_mismatch"

    # Deleting the artifact turns the historical ref into a visible dangling ref.
    async with engine.begin() as conn:
        await conn.execute(
            delete(ResearchCodeArtifactModel).where(
                ResearchCodeArtifactModel.artifact_id == artifact.artifact_id
            )
        )
    async with session_factory(engine)() as session:
        gone = await source_fact(
            session,
            {"kind": "code_artifact", "ref_id": artifact.artifact_id},
            Path("docs/research"),
        )
        assert gone["status"] == "missing"


async def _make_code_run_row(engine: AsyncEngine, artifact_id: str) -> str:
    async with session_factory(engine)() as session:
        run = await ResearchCodeRunRepository(session).create(
            kind="factor",
            name="momentum_factor",
            commit="a" * 40,
            code_checksum="e" * 64,
            artifact_id=artifact_id,
            dataset_release_ids=[BARS_RELEASE],
            dataset_release_checksums={BARS_RELEASE: "f" * 64},
            decision_at=datetime(2024, 6, 3, 15, tzinfo=UTC),
            image="finboard/research-sandbox:test",
            image_digest="sha256:" + "0" * 64,
            artifact_dir="artifacts/rcr-test",
        )
        await session.commit()
    return str(run.run_id)


async def test_validation_experiment_facts_put_oos_first(engine: AsyncEngine) -> None:
    experiment = _to_validated_oos(_experiment())
    trial = _trial(TrialStatus.REJECTED, oos=True, failure_reason="oos_gate")
    trial = replace(trial, experiment_id=experiment.experiment_id, trial_id="t-516")
    experiment_id = await _save_experiment(engine, experiment)
    async with session_factory(engine)() as session:
        await ResearchTrialRepository(session).save(trial)
        await session.commit()
    async with session_factory(engine)() as session:
        fact = await source_fact(
            session,
            {"kind": "validation_experiment", "ref_id": experiment_id},
            Path("docs/research"),
        )
    facts = fact["facts"]
    assert facts["oos_outcome"] == "not_supported"
    assert facts["final_test_unsealed"] is True
    assert facts["trial_budget"] == {"used": 0, "limit": 4}


async def test_oversized_trial_set_returns_unknown_with_named_gap(
    engine: AsyncEngine,
) -> None:
    experiment_id = await _save_experiment(engine, _experiment())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO research_trials "
                "(trial_id, experiment_id, trial_index, parameters, status, "
                " walk_forward_windows, robustness_probes, created_at) "
                "SELECT 'bulk-516-' || g, :experiment_id, g, '{}'::json, 'candidate', "
                "'[]'::json, '[]'::json, now() "
                "FROM generate_series(1, 501) AS g"
            ),
            {"experiment_id": experiment_id},
        )
    async with session_factory(engine)() as session:
        fact = await source_fact(
            session,
            {"kind": "validation_experiment", "ref_id": experiment_id},
            Path("docs/research"),
        )
    facts = fact["facts"]
    assert fact["status"] == "matched"
    assert facts["oos_outcome"] == "unknown"
    assert facts["evidence_gaps"] == ["trial_summary_oversized"]


# --------------------------------------------------------------------------- #
# D/E. reconnect, REST surface, and the "web has no code channel" guard
# --------------------------------------------------------------------------- #


async def test_context_recovers_in_flight_experiment_links(engine: AsyncEngine) -> None:
    await _make_snapshot(engine)
    artifact = await _make_artifact(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        created = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="factor_experiment.create",
            goal_version="v1",
            payload=_factor_payload(),
            ctx=_ctx("OP-516-inflight"),
        )
    async with session_factory(engine)() as session:
        await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="code_run.execute",
            goal_version="v1",
            payload=_code_run_payload(artifact.artifact_id),
            ctx=_ctx("OP-516-inflight-run"),
            sandbox_enabled=True,
        )
    async with session_factory(engine)() as session:
        context = await topic_context(session, topic["topic_id"], rounds=5, refs=0)
    by_type = {item["entity_type"]: item for item in context["experiment_links"]}
    assert set(by_type) == {"factor_experiment", "code_run"}
    assert by_type["factor_experiment"]["entity_id"] == created["entity_id"]
    # The queued sandbox job is visible after a reconnect (nothing stored twice).
    assert by_type["code_run"]["state"] == "queued"
    assert by_type["code_run"]["job_id"] is not None
    assert by_type["code_run"]["evidence_gaps"] == ["run_not_created"]

    # The dedicated read view returns the same immutable link set.
    async with session_factory(engine)() as session:
        page = await workspace_read(
            session, topic_id=topic["topic_id"], view="artifacts", limit=10
        )
    assert page["has_more"] is False
    assert len(page["items"]) == 2


async def test_rest_action_surface_has_no_code_channel(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    await _make_snapshot(engine)
    topic = await _create_topic(engine)
    catalog = await client.get(f"/api/research/topics/{topic['topic_id']}/artifacts/actions")
    assert catalog.status_code == 200
    body = catalog.json()
    assert body["code_channel"] == "mcp_only"
    assert {item["action"] for item in body["actions"]} == {
        "run.submit",
        "run.replay",
        "factor_experiment.create",
        "code_run.execute",
        "validation_experiment.create",
        "validation_experiment.add_trial",
        "validation_experiment.unseal",
    }
    assert {item["entity_type"] for item in body["actions"]} >= set(ARTIFACT_ENTITY_TYPES)

    # Unknown top-level fields are rejected by the wire model.
    response = await client.post(
        f"/api/research/topics/{topic['topic_id']}/actions",
        json={
            "action": "code_run.execute",
            "goal_version": "v1",
            "payload": {"artifact_id": "RC-x"},
            "code": "print(1)",
        },
    )
    assert response.status_code == 422

    # Inline code inside payload is rejected before any artifact is resolved.
    response = await client.post(
        f"/api/research/topics/{topic['topic_id']}/actions",
        json={
            "action": "code_run.execute",
            "goal_version": "v1",
            "payload": {
                "artifact_id": "RC-x",
                "dataset_release_ids": [BARS_RELEASE],
                "decision_at": "2024-06-03T15:00:00+08:00",
                "code": "print(1)",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "code_channel_forbidden"
    assert set(CODE_FORBIDDEN_FIELDS) >= {"code", "module", "expression"}
    # The Web-source grep guard (no finboard_research_code_submit / source_code)
    # is covered by tests/unit/test_issue_516_topic_factor_oos_unit.py.


async def test_artifact_links_track_failed_and_interrupted(engine: AsyncEngine) -> None:
    """Failures/interruptions stay visible on the link instead of being hidden."""

    await _make_snapshot(engine)
    artifact = await _make_artifact(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        factor = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="factor_experiment.create",
            goal_version="v1",
            payload=_factor_payload(),
            ctx=_ctx("OP-516-failed"),
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE factor_experiments SET status='failed', failure_reason='boom' "
                "WHERE experiment_id=:experiment_id"
            ),
            {"experiment_id": factor["entity_id"]},
        )
    async with session_factory(engine)() as session:
        code = await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="code_run.execute",
            goal_version="v1",
            payload=_code_run_payload(artifact.artifact_id),
            ctx=_ctx("OP-516-interrupted"),
            sandbox_enabled=True,
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE background_jobs SET status='interrupted', "
                "error_summary='worker lost' WHERE job_id=:job_id"
            ),
            {"job_id": code["job_id"]},
        )
    async with session_factory(engine)() as session:
        page = await workspace_read(
            session, topic_id=topic["topic_id"], view="artifacts", limit=10
        )
    by_type = {item["entity_type"]: item for item in page["items"]}
    assert by_type["factor_experiment"]["state"] == "failed"
    assert by_type["factor_experiment"]["failure_reason"] == "boom"
    assert by_type["code_run"]["state"] == "interrupted"
    assert by_type["code_run"]["evidence_gaps"] == ["run_not_created"]


async def test_actions_never_write_live_tables(engine: AsyncEngine) -> None:
    await _make_snapshot(engine)
    artifact = await _make_artifact(engine)
    topic = await _create_topic(engine)
    async with session_factory(engine)() as session:
        await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="factor_experiment.create",
            goal_version="v1",
            payload=_factor_payload(),
            ctx=_ctx(),
        )
    async with session_factory(engine)() as session:
        await run_topic_action(
            session,
            topic_id=topic["topic_id"],
            action="code_run.execute",
            goal_version="v1",
            payload=_code_run_payload(artifact.artifact_id),
            ctx=_ctx(),
            sandbox_enabled=True,
        )
    async with session_factory(engine)() as session:
        for table in ("orders", "fills", "positions", "audit_logs"):
            count = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
            assert int(count) == 0, table



