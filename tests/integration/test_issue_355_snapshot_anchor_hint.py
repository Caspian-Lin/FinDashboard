"""快照锚定失配前置可见化端到端测试(issue #355,需 PostgreSQL)。

真实链路:active+passed 用户因子 artifact → 成功 RCR(锚定旧 bars 发布)
→ u_ 快照落库 → 引用该因子的已发布规格:

* REST ``POST /api/research/runs``:锚定发布 ⊄ 本次 dataset_release_ids
  → 422 具名 ``snapshot_anchor_mismatch``,逐快照列出因子名 / 锚定发布 /
  缺失清单,附两条修复路径(加入 dataset_release_ids / 对新发布重算
  RCR → 质量门 → 新快照);全匹配 → 201(零噪音,不产生新输出);
* MCP ``finboard_run_queue``(``enqueue_research_run``):同一失配
  invalid_argument,文案与 REST 同源;
* REST ``POST /api/research/strategy-specs/validate`` 与 MCP
  ``finboard_strategy_validate``:同类失配在 validate 通道给具名
  ``user_factor_anchor_warnings`` 提示(不阻断),全匹配为空列表。

⊆ 约束本身与快照 PIT 语义不变(入队仍拒绝,只是失败更可操作)。
纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_api.routes.strategy_specs import router as strategy_specs_router
from finboard_backtest.research_sandbox.factor_publish import (
    build_factor_snapshot,
    check_output_quality,
)
from finboard_backtest.strategy_spec import (
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_backtest.strategy_spec.contracts import (
    FeatureGraph,
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
)
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleasedInstrument,
    default_execution_metadata,
)
from finboard_mcp.execution import McpToolError
from finboard_mcp.tools import runs as run_tools
from finboard_mcp.tools import strategies as strategy_tools
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchCodeArtifactRepository,
    ResearchCodeRunRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
    session_factory,
)
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)
from tests.integration.test_promotion_chain_closure import (
    D1,
    D2,
    FACTOR_COMMIT,
    FACTOR_NAME,
    U_FACTOR,
    _make_app,
)
from tests.integration.test_research_run_signal_engine_worker import SYMBOLS

pytestmark = pytest.mark.asyncio

OLD_RELEASE = "anchor-old-release"
NEW_RELEASE = "anchor-new-release"
OLDEST_RELEASE = "anchor-oldest-release"


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in (
            "research_run_artifacts",
            "research_runs",
            "background_jobs",
            "research_trials",
            "research_experiments",
            "research_code_runs",
            "research_code_artifacts",
            "factor_feature_snapshots",
            "research_strategy_specs",
            "research_dataset_releases",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """最小 app:research_runs + strategy_specs 路由,独立 session。"""
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(research_runs_router)
    app.include_router(strategy_specs_router)
    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


def _instrument(code: str) -> ReleasedInstrument:
    return ReleasedInstrument(
        code=code,
        name=code,
        name_history=(),
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        artifact_path=f"bars/{code}_D1_qfq.parquet",
        artifact_checksum="a" * 64,
        artifact_size=1024,
        row_count=40,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        expected_sessions=40,
        missing_sessions=0,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("1.0"),
        category="stock",
        ready=True,
        list_date=date(2020, 1, 1),
        delist_date=None,
        industry=None,
    )


async def _register_bars_release(session: AsyncSession, release_id: str) -> None:
    """登记一个最小 bars 发布(checksum 随 release_id 派生,可多发布并存)。"""
    release = ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-bars",
        source="issue355",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=tuple(_instrument(code) for code in SYMBOLS),
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(SYMBOLS),
                ready_count=len(SYMBOLS),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=release_id,
        release_checksum=hashlib.sha256(release_id.encode()).hexdigest(),
    )
    await ResearchDatasetReleaseRepository(session).publish(release)
    await session.commit()


async def _register_active_artifact(session: AsyncSession) -> Any:
    """登记 active+passed 因子 artifact(#219 正式流程的终态快照)。"""
    return await ResearchCodeArtifactRepository(session).register(
        kind="factor",
        name=FACTOR_NAME,
        commit=FACTOR_COMMIT,
        path=f"factors/{FACTOR_NAME}",
        checksum="d" * 16,
        created_by="integration-test",
        status="active",
    )


async def _register_rcr_snapshot(
    session: AsyncSession,
    *,
    artifact: Any,
    decision_at: datetime,
    release_ids: list[str],
) -> str:
    """登记成功 RCR + u_ 快照(fake 沙箱产物,#217 形状)。"""
    run_repo = ResearchCodeRunRepository(session)
    rcr = await run_repo.create(
        kind="factor",
        name=artifact.name,
        commit=artifact.commit,
        code_checksum="deadbeef",
        dataset_release_ids=list(release_ids),
        dataset_release_checksums=dict.fromkeys(release_ids, "c" * 64),
        decision_at=decision_at,
        image="finboard-research-sandbox:test",
        image_digest="sha256:" + "9" * 64,
        artifact_dir="data_cache/research_sandbox/test",
        artifact_id=artifact.artifact_id,
    )
    scores = {symbol: float(index + 1) for index, symbol in enumerate(SYMBOLS)}
    quality = check_output_quality(
        scores, universe_size=len(SYMBOLS), max_nan_ratio=0.5, min_coverage=0.5
    )
    assert quality.passed
    snapshot = build_factor_snapshot(
        factor_artifact_name=artifact.name,
        run_id=rcr.run_id,
        decision_at=decision_at,
        commit=artifact.commit,
        mount_manifest_checksum="m" * 64,
        scores=scores,
        quality=quality,
    )
    await FeatureSnapshotRepository(session).publish(snapshot)
    await run_repo.mark_terminal(
        rcr.run_id,
        status="succeeded",
        exit_code=0,
        scores_checksum="s" * 64,
        output_snapshot_id=snapshot.snapshot_id,
    )
    return snapshot.snapshot_id


def _u_factor_spec(strategy_id: str, release_id: str) -> ResearchStrategySpec:
    """引用 u_ 用户因子的 single_shot 规格(无 screen 绑定,普通运行)。"""
    nodes = (
        FeatureNode(
            node_id="alpha",
            label="用户 alpha",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source=U_FACTOR,
        ),
        FeatureNode(
            node_id="alpha_rank",
            label="alpha 排名",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.CROSS_SECTION_RANK,
            inputs=("alpha",),
        ),
        FeatureNode(
            node_id="alpha_score",
            label="alpha 得分",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.NEGATE,
            inputs=("alpha_rank",),
        ),
        FeatureNode(
            node_id="close",
            label="收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="ret_5",
            label="5 日收益",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=5,
        ),
        FeatureNode(
            node_id="composite",
            label="复合得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("alpha_score", "ret_5"),
            weights=(0.7, 0.3),
        ),
    )
    spec = build_strategy_template(
        "multi_factor", strategy_id=strategy_id, dataset_release_ids=(release_id,)
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("composite",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="复合得分前 50% 纳入目标仓位。",
                    ),
                ),
                conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
            ),
        }
    )


async def _publish_spec(session: AsyncSession, spec: ResearchStrategySpec) -> None:
    plan = compile_registered_strategy_spec(
        spec, user_factor_sources=frozenset({U_FACTOR})
    )
    repo = ResearchStrategySpecRepository(session)
    await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=plan.checksum,
        payload=spec.canonical_payload(),
    )
    await repo.publish(spec.strategy_id, 1, expected_version=1)
    await session.commit()


def _queue_payload(
    spec: ResearchStrategySpec, snapshot_ids: list[str], key: str
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "strategy_id": spec.strategy_id,
        "strategy_version": 1,
        "dataset_release_ids": list(spec.validation_plan.dataset_release_ids),
        "factor_snapshot_ids": list(snapshot_ids),
        "parameters": {},
        "code_version": "abcdef0123456789",
        "initial_capital": "200000",
        "requested_by": "integration-test",
    }


async def _seed_old_anchor(engine: AsyncEngine) -> str:
    """登记旧发布 + active 因子 + 锚定旧发布的成功 RCR 快照,返回快照 ID。"""
    async with session_factory(engine)() as session:
        await _register_bars_release(session, OLD_RELEASE)
        artifact = await _register_active_artifact(session)
        snapshot_id = await _register_rcr_snapshot(
            session, artifact=artifact, decision_at=D1, release_ids=[OLD_RELEASE]
        )
        await session.commit()
    return snapshot_id


class TestRestEnqueueAnchorGate:
    async def test_all_match_succeeds_with_zero_noise(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """全匹配:入队 201,无任何新输出(零噪音对照)。"""
        snapshot_id = await _seed_old_anchor(engine)
        async with session_factory(engine)() as session:
            await _publish_spec(session, _u_factor_spec("issue355_ok", OLD_RELEASE))

        response = await client.post(
            "/api/research/runs",
            json=_queue_payload(
                _u_factor_spec("issue355_ok", OLD_RELEASE),
                [snapshot_id],
                "issue355-ok",
            ),
        )
        assert response.status_code == 201, response.text
        async with session_factory(engine)() as session:
            rows = await ResearchRunRepository(session).list_recent(limit=10)
        assert any(row.run_id == response.json()["run_id"] for row in rows)

    async def test_mismatch_rejected_with_named_message_and_remediation(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """换 bars 主发布后引用旧快照:422 具名 + 逐快照清单 + 修复路径。"""
        snapshot_id = await _seed_old_anchor(engine)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, NEW_RELEASE)
            await _publish_spec(session, _u_factor_spec("issue355_new", NEW_RELEASE))

        spec = _u_factor_spec("issue355_new", NEW_RELEASE)
        response = await client.post(
            "/api/research/runs", json=_queue_payload(spec, [snapshot_id], "issue355-m1")
        )

        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        # 具名标记 + 逐快照三要素
        assert "snapshot_anchor_mismatch" in detail
        assert snapshot_id in detail
        assert U_FACTOR in detail
        assert OLD_RELEASE in detail  # 锚定发布
        assert "锚定 ⊄ 本次冻结清单" in detail
        # 修复路径 (a)/(b):加入 dataset_release_ids / 重算 RCR
        assert "dataset_release_ids" in detail
        assert "finboard_research_code_run" in detail
        assert "质量门" in detail
        # 快速失败:不产生 queued 行
        async with session_factory(engine)() as session:
            rows = await ResearchRunRepository(session).list_recent(limit=10)
        assert all(row.strategy_kind != spec.strategy_kind for row in rows)

    async def test_partial_mismatch_lists_all_offenders_only(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """部分失配:只列失配快照、全量列出;匹配快照不出现在错误里。"""
        async with session_factory(engine)() as session:
            await _register_bars_release(session, NEW_RELEASE)
            artifact = await _register_active_artifact(session)
            snap_old = await _register_rcr_snapshot(
                session, artifact=artifact, decision_at=D1, release_ids=[OLD_RELEASE]
            )
            snap_older = await _register_rcr_snapshot(
                session,
                artifact=artifact,
                decision_at=D2,
                release_ids=[OLDEST_RELEASE],
            )
            snap_new = await _register_rcr_snapshot(
                session, artifact=artifact, decision_at=D2, release_ids=[NEW_RELEASE]
            )
            await _publish_spec(session, _u_factor_spec("issue355_mix", NEW_RELEASE))

        spec = _u_factor_spec("issue355_mix", NEW_RELEASE)
        response = await client.post(
            "/api/research/runs",
            json=_queue_payload(
                spec, [snap_old, snap_older, snap_new], "issue355-m2"
            ),
        )

        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        # 两个失配快照一次性列出(旧门控只报第一个)
        assert snap_old in detail
        assert snap_older in detail
        assert OLD_RELEASE in detail
        assert OLDEST_RELEASE in detail
        # 匹配的快照不入错误文案(零噪音)
        assert snap_new not in detail


class TestMcpEnqueueAnchorGate:
    async def test_mcp_rejects_with_same_named_message(
        self, engine: AsyncEngine
    ) -> None:
        """MCP finboard_run_queue:同一失配 invalid_argument,文案与 REST 同源。"""
        snapshot_id = await _seed_old_anchor(engine)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, NEW_RELEASE)
            await _publish_spec(session, _u_factor_spec("issue355_mcp", NEW_RELEASE))

        spec = _u_factor_spec("issue355_mcp", NEW_RELEASE)
        app = _make_app(engine)
        body = run_tools.parse_queue_payload(
            _queue_payload(spec, [snapshot_id], "issue355-mcp")
        )
        with pytest.raises(McpToolError) as exc_info:
            await run_tools.enqueue_research_run(app, body)
        assert exc_info.value.kind == "invalid_argument"
        message = exc_info.value.message
        assert "snapshot_anchor_mismatch" in message
        assert snapshot_id in message
        assert U_FACTOR in message
        assert "finboard_research_code_run" in message


class TestValidateAnchorWarnings:
    async def test_validate_warns_on_mismatch_and_silent_on_match(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """REST validate:失配给具名 warning;全匹配零噪音。"""
        snapshot_id = await _seed_old_anchor(engine)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, NEW_RELEASE)
            await _publish_spec(session, _u_factor_spec("issue355_val_old", OLD_RELEASE))
            await _publish_spec(session, _u_factor_spec("issue355_val_new", NEW_RELEASE))

        spec_new = _u_factor_spec("issue355_val_new", NEW_RELEASE)
        response = await client.post(
            "/api/research/strategy-specs/validate",
            json={"spec": spec_new.canonical_payload()},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["valid"] is True  # 不阻断
        warnings = body["user_factor_anchor_warnings"]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["code"] == "user_factor_anchor_mismatch"
        assert warning["factor_name"] == U_FACTOR
        assert warning["anchored_release_ids"] == [OLD_RELEASE]
        assert warning["missing_release_ids"] == [OLD_RELEASE]
        assert warning["snapshot_id"] == snapshot_id
        assert "finboard_research_code_run" in warning["message"]

        # 全匹配(规格仍指向旧发布):零噪音
        spec_old = _u_factor_spec("issue355_val_old", OLD_RELEASE)
        response_old = await client.post(
            "/api/research/strategy-specs/validate",
            json={"spec": spec_old.canonical_payload()},
        )
        assert response_old.status_code == 200, response_old.text
        assert response_old.json()["user_factor_anchor_warnings"] == []

    async def test_mcp_validate_warns(self, engine: AsyncEngine) -> None:
        """MCP finboard_strategy_validate:同一提示口径。"""
        snapshot_id = await _seed_old_anchor(engine)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, NEW_RELEASE)
            await _publish_spec(session, _u_factor_spec("issue355_mv", NEW_RELEASE))

        spec_new = _u_factor_spec("issue355_mv", NEW_RELEASE)
        env = await strategy_tools.strategy_validate(
            _make_app(engine), spec=spec_new.canonical_payload()
        )
        assert env.status == "ok", env.error
        warnings = env.data["user_factor_anchor_warnings"]
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert warnings[0]["code"] == "user_factor_anchor_mismatch"
        assert warnings[0]["factor_name"] == U_FACTOR
        assert warnings[0]["snapshot_id"] == snapshot_id
        # 确认快照确实存在(防夹具漂移导致假阳性)
        async with session_factory(engine)() as session:
            assert await FeatureSnapshotRepository(session).get(snapshot_id)
