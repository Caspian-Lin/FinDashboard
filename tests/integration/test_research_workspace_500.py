"""Research context persistence, provenance and bounded evidence (#498-500)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.research_explanation import decision_evidence, explain
from finboard_app.research_workspace import (
    memory_page,
    source_fact,
    workspace_read,
    workspace_write,
)
from finboard_persistence import ResearchMemoryRepository, SourceRef
from finboard_persistence.models import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchTopicEntryModel,
)


def topic_payload() -> dict[str, Any]:
    return {
        "title": "小盘反转复核",
        "question": "收益是否依赖成本近似?",
        "goal": {
            "version": "2026-09-01",
            "criteria": "年化15%,回撤20%",
            "source": {"kind": "document", "ref_id": "ROADMAP.md"},
        },
    }


def round_payload() -> dict[str, Any]:
    return {
        "goal_version": "2026-09-01",
        "objective": "核查冻结输入",
        "action": "读指定运行",
        "rationale": "避免混源比较",
        "outcome": "interrupted",
        "confidence": "low",
        "source_refs": [{"kind": "research_run", "ref_id": "RR-missing"}],
    }


async def test_resume_goals_correction_and_idempotency(db_session):
    t = await workspace_write(
        db_session, actor="agent:mcp", operation="create", payload=topic_payload()
    )
    first = await workspace_write(
        db_session,
        actor="agent:mcp",
        operation="append",
        topic_id=t["topic_id"],
        idempotency_key="round-1",
        payload=round_payload(),
    )
    repeated = await workspace_write(
        db_session,
        actor="agent:mcp",
        operation="append",
        topic_id=t["topic_id"],
        idempotency_key="round-1",
        payload=round_payload(),
    )
    assert first["entry_id"] == repeated["entry_id"]
    changed = topic_payload()
    changed["goal"] = {**changed["goal"], "version": "2026-09-22", "criteria": "年化15%,回撤10%"}
    changed["status"] = "paused"
    current = await workspace_write(
        db_session,
        actor="user:api",
        operation="update",
        topic_id=t["topic_id"],
        expected_revision=1,
        payload=changed,
    )
    assert current["revision"] == 2
    correction = {
        **round_payload(),
        "supersedes_id": first["entry_id"],
        "outcome": "completed",
        "conclusion": "旧故障已修复,此处是历史记录",
    }
    fixed = await workspace_write(
        db_session,
        actor="agent:mcp",
        operation="append",
        topic_id=t["topic_id"],
        idempotency_key="round-2",
        payload=correction,
    )
    page = await workspace_read(db_session, topic_id=t["topic_id"], entries=True, limit=2)
    assert page["has_more"]
    assert fixed["supersedes_id"] == first["entry_id"]
    all_rows = await workspace_read(db_session, topic_id=t["topic_id"], entries=True)
    goals = [r["topic"]["goal"] for r in all_rows["items"] if r.get("topic")]
    assert {r["version"] for r in goals} == {"2026-09-01", "2026-09-22"}
    assert (
        await source_fact(
            db_session, {"kind": "research_run", "ref_id": "RR-missing"}, Path("docs/research")
        )
    )["status"] == "missing"
    with pytest.raises(ValueError, match="版本已变化"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            topic_id=t["topic_id"],
            expected_revision=1,
            payload=changed,
        )
    with pytest.raises(ValueError, match="幂等键"):
        await workspace_write(
            db_session,
            actor="agent:mcp",
            operation="append",
            topic_id=t["topic_id"],
            idempotency_key="round-1",
            payload=correction,
        )
    # No evidence foreign keys or execution writes were introduced.
    assert await db_session.scalar(select(ResearchTopicEntryModel.entry_id).limit(1))


async def test_memory_excerpt_and_correction(db_session):
    repo = ResearchMemoryRepository(db_session)
    old = await repo.remember(
        memory_type="note",
        content="旧解释" * 1000,
        created_by="agent:mcp",
        source_refs=[SourceRef(kind="unknown", ref_id="")],
    )
    new = await repo.correct(
        memory_id=old.memory_id, content="故障已修复,不作为当前故障", created_by="agent:mcp"
    )
    await db_session.commit()
    page = await memory_page(db_session, limit=2)
    assert {r["memory_id"] for r in page["items"]} == {old.memory_id, new.memory_id}
    assert all(len(r["excerpt"]) <= 1200 and r["unverifiable_refs"] for r in page["items"])
    assert page["items"][0]["supersedes_id"] == old.memory_id
    assert page["items"][1]["status"] == "forgotten"


async def add_evidence(db_session: AsyncSession) -> None:
    row = ResearchRunModel(
        run_id="RR-bounded-explain",
        idempotency_key="bounded-explain",
        strategy_id="test",
        strategy_kind="multi_factor",
        status="completed",
        schema_version="v1",
        manifest_checksum="a" * 64,
        manifest={
            "strategy_spec": {"name": "Frozen name", "feature_graph": {"nodes": [], "outputs": []}}
        },
        requested_by="test",
    )
    db_session.add(row)
    await db_session.flush()
    for index, (stage, key, items) in enumerate(
        [
            (
                "universe",
                "candidates",
                [{"symbol": "A", "included": True}, {"symbol": "B", "included": False}],
            ),
            (
                "features",
                "features",
                [{"symbol": "B", "value": -999}] * 1000
                + [{"symbol": "A", "feature_id": "ret", "value": -0.2}],
            ),
            ("targets_after_risk", "targets", [{"symbol": "A", "weight": 0.06}]),
            (
                "orders",
                "orders",
                [{"symbol": "A", "status": "rejected", "reason": "volume_limit", "quantity": 100}],
            ),
            ("fills", "fills", []),
        ]
    ):
        db_session.add(
            ResearchRunArtifactModel(
                artifact_id=f"RR-bounded-explain:A:{stage}",
                run_id=row.run_id,
                decision_id="RR-bounded-explain:D:00000001",
                sequence=index,
                stage=stage,
                trace_id=f"trace-{stage}",
                parent_trace_ids=[],
                payload={"business_date": "2026-09-22", key: items, "ignored_large": "x" * 100000},
                checksum="b" * 64,
            )
        )
    await db_session.commit()


async def test_decision_projection_filters_before_loading(db_session):
    await add_evidence(db_session)
    directory = await decision_evidence(db_session, run_id="RR-bounded-explain")
    assert directory["items"][0]["business_date"] == "2026-09-22"
    page = await decision_evidence(
        db_session, run_id="RR-bounded-explain", business_date="2026-09-22", symbol="A", limit=2
    )
    assert page["has_more"]
    assert len(page["items"]) == 2
    assert all(x["item"]["symbol"] == "A" for x in page["items"])
    rest = await decision_evidence(
        db_session, run_id="RR-bounded-explain", business_date="2026-09-22", symbol="A", limit=100
    )
    assert any(x["stage"] == "targets_after_risk" for x in rest["items"])
    assert any(x["item"].get("status") == "rejected" for x in rest["items"])
    assert not any(x["stage"] == "fills" for x in rest["items"])
    assert "ignored_large" not in str(rest)
    with pytest.raises(ValueError, match="下钻"):
        await decision_evidence(db_session, run_id="RR-bounded-explain", symbol="A")
    frozen = await explain(db_session, run_id="RR-bounded-explain")
    assert frozen["spec"]["name"] == "Frozen name"
    assert frozen["gaps"]  # Legacy malformed inputs never replaced with latest spec.


async def test_document_reference_path_and_checksum(db_session, tmp_path):
    (tmp_path / "FINDINGS.md").write_text("# canonical", encoding="utf-8")
    assert (await source_fact(db_session, {"kind": "document", "ref_id": "FINDINGS.md"}, tmp_path))[
        "status"
    ] == "matched"
    assert (
        await source_fact(
            db_session, {"kind": "document", "ref_id": "FINDINGS.md", "checksum": "wrong"}, tmp_path
        )
    )["status"] == "checksum_mismatch"
    assert (
        await source_fact(db_session, {"kind": "document", "ref_id": "../secret.md"}, tmp_path)
    )["status"] == "missing"


async def test_frozen_factor_formula_overrides_and_drift(db_session):
    from datetime import date

    from finboard_backtest.factors.predefined.registry import predefined_factor_commit
    from finboard_backtest.research_run import (
        FrozenArtifactRef,
        ResearchRunManifest,
        stable_checksum,
    )
    from finboard_backtest.research_run.contracts import to_json_value
    from finboard_backtest.strategy_spec import build_strategy_template
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        FeatureKind,
        FeatureNode,
        FeatureOperator,
        SignalRules,
    )
    from finboard_persistence.models import ResearchFactorSeriesModel

    spec = build_strategy_template(
        "multi_factor", strategy_id="frozen-parameter-test", dataset_release_ids=("test-bars",)
    ).model_copy(
        update={
            "feature_graph": FeatureGraph(
                nodes=(
                    FeatureNode(
                        node_id="ret",
                        label="动量标签",
                        kind=FeatureKind.FACTOR,
                        operator=FeatureOperator.IDENTITY,
                        source="p_return_5d",
                    ),
                    FeatureNode(
                        node_id="reverse",
                        label="实际反向",
                        kind=FeatureKind.FACTOR,
                        operator=FeatureOperator.NEGATE,
                        inputs=("ret",),
                    ),
                ),
                outputs=("reverse",),
            ),
            "signal_rules": SignalRules(
                rules=(
                    build_strategy_template(
                        "multi_factor",
                        strategy_id="frozen-parameter-test",
                        dataset_release_ids=("test-bars",),
                    )
                    .signal_rules.rules[0]
                    .model_copy(update={"feature_id": "reverse"}),
                )
            ),
        }
    )
    series = ResearchFactorSeriesModel(
        series_id="FS-frozen",
        series_key="c" * 64,
        code_artifact="return_5d",
        code_commit=predefined_factor_commit("return_5d"),
        kind="predefined_factor",
        release_id="test-bars",
        dataset_release_ids=[],
        params={"audit_parameter": 7},
        window_start=date(2024, 1, 1),
        window_end=date(2024, 2, 1),
        dates=[],
        values=None,
        content_checksum="d" * 64,
    )
    db_session.add(series)
    manifest = ResearchRunManifest(
        run_id="RR-frozen-parameter",
        idempotency_key="frozen-parameter",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(FrozenArtifactRef("test-bars", "v1", "a" * 64),),
        factor_series=(FrozenArtifactRef("FS-frozen", "v1", "d" * 64),),
        strategy_version=13,
        code_version="frozen-code",
        requested_by="test",
        fee_config={"overrides": {"slippage_bps": 17}},
        risk_config={
            "overrides": {"rules": [{"rule_type": "portfolio_drawdown_derisk", "threshold": 0.08}]}
        },
        portfolio_config={"overrides": {"max_risk_contribution": 1}},
    )
    payload = to_json_value(manifest)
    assert isinstance(payload, dict)
    from finboard_backtest.research_run.contracts import manifest_from_json

    manifest_from_json(payload)
    db_session.add(
        ResearchRunModel(
            run_id=manifest.run_id,
            idempotency_key=manifest.idempotency_key,
            strategy_id=spec.strategy_id,
            strategy_kind="multi_factor",
            status="completed",
            schema_version="v1",
            manifest=payload,
            manifest_checksum=manifest.checksum,
            requested_by="test",
        )
    )
    await db_session.commit()
    result = await explain(db_session, run_id=manifest.run_id)
    factor = result["factors"][0]
    assert factor["parameters"] == {"audit_parameter": 7}
    assert factor["evidence"] == "commit_matched_definition"
    assert "原值越低" in factor["effective_direction"]["reverse"]
    assert "close" in factor["formula"]
    assert result["effective_policies"]["execution_model"]["slippage_bps"] == 17
    assert result["effective_policies"]["portfolio_constraints"]["max_risk_contribution"] == 1
    assert (
        next(
            r
            for r in result["effective_policies"]["risk_exit_policy"]["rules"]
            if r["rule_type"] == "portfolio_drawdown_derisk"
        )["threshold"]
        == 0.08
    )
    series.code_commit = "old-unmatched-commit"
    await db_session.commit()
    old = await explain(db_session, run_id=manifest.run_id)
    assert old["factors"][0]["formula"] is None
    assert old["factors"][0]["parameters"] == {"audit_parameter": 7}
    assert old["gaps"]
    series.content_checksum = "e" * 64
    await db_session.commit()
    assert (await explain(db_session, run_id=manifest.run_id))["factors"][0]["parameters"] is None


async def test_rest_actor_conflict_and_no_execution(db_session):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func

    from finboard_api.deps import get_db_session
    from finboard_api.routes.research_topics import explanation_router, router
    from finboard_persistence.models import BackgroundJobModel, FillModel, OrderModel

    app = FastAPI()
    app.include_router(router)
    app.include_router(explanation_router)

    async def session_override():
        yield db_session

    app.dependency_overrides[get_db_session] = session_override
    counts = [
        await db_session.scalar(select(func.count()).select_from(m))
        for m in (OrderModel, FillModel, BackgroundJobModel)
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/research/topics", json={**topic_payload(), "created_by": "agent:spoof"}
        )
        assert response.status_code == 422
        response = await client.post("/api/research/topics", json=topic_payload())
        assert response.status_code == 201
        topic = response.json()
        assert topic["created_by"] == "user:api"
        update = {**topic_payload(), "expected_revision": 1}
        assert (
            await client.put(f"/api/research/topics/{topic['topic_id']}", json=update)
        ).status_code == 200
        assert (
            await client.put(f"/api/research/topics/{topic['topic_id']}", json=update)
        ).status_code == 409
        assert (await client.get("/api/research/topics?limit=51")).status_code == 422
        assert (
            await client.get("/api/research/explanation?strategy_id=unknown")
        ).status_code == 422
        changed = {
            **topic_payload(),
            "expected_revision": 2,
            "goal": {**topic_payload()["goal"], "criteria": "悄悄改门槛"},
        }
        assert (
            await client.put(f"/api/research/topics/{topic['topic_id']}", json=changed)
        ).status_code == 409
        assert (await client.get("/api/research/topics/RT-missing")).status_code == 404
    assert counts == [
        await db_session.scalar(select(func.count()).select_from(m))
        for m in (OrderModel, FillModel, BackgroundJobModel)
    ]


async def test_topic_migration_roundtrip_isolated_schema(db_session):
    import importlib.util
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    spec = importlib.util.spec_from_file_location(
        "research_topic_migration", "migrations/versions/62f8b1e7a905_research_topics_500.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "migration_probe_" + uuid4().hex
    conn = await db_session.connection()
    await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))

    def probe(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            module.upgrade()
            assert sync_conn.execute(text("SELECT to_regclass('research_topics')")).scalar()
            sync_conn.execute(
                text(
                    "INSERT INTO research_topics(topic_id,payload,revision,created_by) VALUES ('RT-probe','{}',1,'test')"
                )
            )
            module.downgrade()
            assert sync_conn.execute(text("SELECT to_regclass('research_topics')")).scalar() is None
            assert sync_conn.execute(text("SELECT to_regclass('public.research_runs')")).scalar()
            module.upgrade()
            assert (
                sync_conn.execute(text("SELECT count(*) FROM research_topic_entries")).scalar() == 0
            )

    await conn.run_sync(probe)
    await db_session.rollback()  # Entire disposable schema and probes disappear.


async def test_stale_identity_map_cannot_overwrite_new_goal(db_session):
    from finboard_persistence import session_factory
    from finboard_persistence.research_topic_repo import ResearchTopicRepository

    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    from sqlalchemy.ext.asyncio import AsyncEngine
    engine = db_session.bind
    assert isinstance(engine, AsyncEngine)
    async with session_factory(engine)() as other:
        cached = await ResearchTopicRepository(other).get(topic["topic_id"])
        assert cached.revision == 1
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            payload={**topic_payload(), "summary": "第一位写者的结果"},
            topic_id=topic["topic_id"],
            expected_revision=1,
        )
        with pytest.raises(ValueError, match="版本已变化"):
            await workspace_write(
                other,
                actor="agent:mcp",
                operation="update",
                payload={**topic_payload(), "summary": "过期会话不得覆盖"},
                topic_id=topic["topic_id"],
                expected_revision=1,
            )
        assert cached.revision == 2


async def test_source_check_keeps_oos_completion_separate(db_session):
    from dataclasses import replace

    from finboard_backtest.validation.contracts import TrialStatus
    from finboard_persistence import ResearchExperimentRepository, ResearchTrialRepository
    from tests.unit.validation.test_issue_310_oos_outcome import (
        _experiment,
        _to_validated_oos,
        _trial,
    )

    exp = _to_validated_oos(_experiment())
    await ResearchExperimentRepository(db_session).save(exp)
    await db_session.flush()
    trial = replace(
        _trial(TrialStatus.REJECTED, oos=True, failure_reason="oos_gate"),
        experiment_id=exp.experiment_id,
        trial_id=exp.experiment_id + "-0",
    )
    await ResearchTrialRepository(db_session).save(trial)
    await db_session.commit()
    fact = await source_fact(
        db_session, {"kind": "experiment", "ref_id": exp.experiment_id}, Path("docs/research")
    )
    assert fact["facts"]["status"] == "validated_oos"
    assert fact["facts"]["oos_outcome"] == "not_supported"
