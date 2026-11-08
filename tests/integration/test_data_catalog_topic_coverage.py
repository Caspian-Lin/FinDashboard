"""课题数据视图接口契约(issue #512)。

覆盖:显式 data_scope 优先、轮次 refs 推导回退、研究运行展开、
不可解析引用/未注册 kind 的具名缺口、覆盖口径复用 #509、404。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.data_catalog import router
from finboard_app.research_workspace import workspace_write
from finboard_data.releases import (
    ReleaseDatasetKind,
    ReleasedInstrument,
    ResearchDatasetRelease,
    default_execution_metadata,
)
from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository
from finboard_persistence.models import ResearchRunModel, ResearchTopicModel
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    ListingStatus,
    Market,
)

pytestmark = pytest.mark.asyncio

_RELEASE_ID = "TEST-TOPIC-COVERAGE"
_RUN_ID = "RR-TOPIC-COVERAGE"


def _release(
    release_id: str = _RELEASE_ID,
    *,
    kind: ReleaseDatasetKind = ReleaseDatasetKind.DAILY_METRICS,
    quality_status: DatasetQualityStatus = DatasetQualityStatus.PASSED,
) -> ResearchDatasetRelease:
    instrument = ReleasedInstrument(
        code="TOP512.SH",
        name="课题数据样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 5, 17, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE, instrument_type=InstrumentType.STOCK
        ),
        artifact_path="daily/TOP512.SH.parquet",
        artifact_checksum="c" * 64,
        artifact_size=512,
        row_count=4,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        expected_sessions=4,
        missing_sessions=1,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("0.75"),
        category="a_share_stock",
        ready=True,
        list_date=date(2020, 1, 1),
        status=ListingStatus.ACTIVE,
    )
    return ResearchDatasetRelease(
        release_id=release_id,
        dataset_name="test-topic-coverage",
        source="tushare",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        period=BarPeriod.D1,
        adjustment="none",
        fields=("trade_date", "close"),
        availability_rules=(),
        code_version="test",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        instruments=(instrument,),
        capabilities=(),
        quality_status=quality_status,
        quality_report={},
        dataset_kind=kind,
        release_checksum="d" * 64,
    )


def _topic_payload() -> dict[str, Any]:
    return {
        "title": "共享数据基座复核",
        "question": "课题引用的发布是否可研究?",
        "goal": {
            "version": "2026-11-09",
            "criteria": "覆盖缺口可见且不静默",
            "source": {"kind": "document", "ref_id": "ROADMAP.md"},
        },
    }


def _round_payload(refs: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "goal_version": "2026-11-09",
        "objective": "组织数据引用",
        "action": "读取发布目录",
        "rationale": "避免手抄发布 ID",
        "outcome": "completed",
        "confidence": "medium",
        "source_refs": refs,
    }


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """返回挂载目录路由的 ASGI 客户端;测试自行建发布/运行/课题。"""
    app = FastAPI()
    app.include_router(router)

    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _topic_with_refs(
    session: AsyncSession, refs: list[dict[str, str]]
) -> str:
    topic = await workspace_write(
        session, actor="user:api", operation="create", payload=_topic_payload()
    )
    await workspace_write(
        session,
        actor="user:api",
        operation="append",
        topic_id=topic["topic_id"],
        idempotency_key="round-1",
        payload=_round_payload(refs),
    )
    await session.commit()
    return str(topic["topic_id"])


async def _add_run(session: AsyncSession) -> None:
    session.add(
        ResearchRunModel(
            run_id=_RUN_ID,
            idempotency_key="topic-coverage-run",
            strategy_id="test",
            strategy_kind="multi_factor",
            status="completed",
            schema_version="v1",
            manifest_checksum="a" * 64,
            manifest={
                "dataset_releases": [
                    {
                        "artifact_id": _RELEASE_ID,
                        "version": "v1",
                        "checksum": "d" * 64,
                    }
                ]
            },
            requested_by="test",
        )
    )
    await session.commit()


async def test_topic_coverage_resolves_dataset_and_run_refs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await ResearchDatasetReleaseRepository(db_session).publish(_release())
    await _add_run(db_session)
    topic_id = await _topic_with_refs(
        db_session,
        [
            {"kind": "dataset", "ref_id": _RELEASE_ID},
            {"kind": "research_run", "ref_id": _RUN_ID},
        ],
    )

    resp = await client.get(f"/api/data/catalog/topics/{topic_id}/coverage")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["topic_id"] == topic_id
    assert payload["coverage"]["resolved_by"] == "refs"
    # 发布 ID 与运行展开的同一发布去重后只出现一次。
    assert [item["release_id"] for item in payload["releases"]] == [_RELEASE_ID]
    release = payload["releases"][0]
    assert release["dataset"] == "daily_metrics"
    assert release["coverage_pct"] == 0.75
    assert release["expected_sessions"] == 4
    assert release["missing_sessions"] == 1
    assert release["denominator"]
    assert payload["coverage"]["release_count"] == 1
    assert payload["coverage"]["dataset_count"] == 1
    assert payload["coverage"]["min_coverage_pct"] == 0.75
    assert payload["coverage"]["symbols_with_gaps"] == 1
    assert str(payload["last_sync"]).startswith("2024-01-06")
    reasons = {gap["reason"] for gap in payload["gaps"]}
    assert reasons == {"coverage_gap"}
    assert payload["next_step"] == "fix_gaps"


async def test_topic_coverage_dataset_key_resolves_latest_release(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await ResearchDatasetReleaseRepository(db_session).publish(
        _release(quality_status=DatasetQualityStatus.WARNINGS)
    )
    topic_id = await _topic_with_refs(
        db_session, [{"kind": "dataset", "ref_id": "daily_metrics"}]
    )
    payload = (
        await client.get(f"/api/data/catalog/topics/{topic_id}/coverage")
    ).json()
    assert [item["release_id"] for item in payload["releases"]] == [_RELEASE_ID]
    assert [gap["reason"] for gap in payload["gaps"]] == ["coverage_gap"]


async def test_topic_coverage_prefers_explicit_data_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await ResearchDatasetReleaseRepository(db_session).publish(_release())
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=_topic_payload()
    )
    # #514 之前的 topic schema 不含 data_scope;这里直接写 payload 模拟其落地,
    # 验证接口的「显式优先、refs 回退」契约不需要再改。
    model = (
        await db_session.execute(
            select(ResearchTopicModel).where(
                ResearchTopicModel.topic_id == topic["topic_id"]
            )
        )
    ).scalar_one()
    model.payload = {
        **dict(model.payload),
        "data_scope": {"dataset_release_ids": [_RELEASE_ID]},
    }
    await db_session.commit()
    payload = (
        await client.get(f"/api/data/catalog/topics/{topic['topic_id']}/coverage")
    ).json()
    assert payload["coverage"]["resolved_by"] == "data_scope"
    assert [item["release_id"] for item in payload["releases"]] == [_RELEASE_ID]


async def test_topic_coverage_reports_named_gaps(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    topic_id = await _topic_with_refs(
        db_session,
        [
            {"kind": "dataset", "ref_id": "DR-MISSING-512"},
            {"kind": "research_run", "ref_id": "RR-MISSING-512"},
            {"kind": "dataset", "ref_id": "profiles"},
        ],
    )
    payload = (
        await client.get(f"/api/data/catalog/topics/{topic_id}/coverage")
    ).json()
    assert payload["releases"] == []
    reasons = sorted(gap["reason"] for gap in payload["gaps"])
    assert reasons == [
        "dataset_not_published",
        "release_not_found",
        "run_not_found",
    ]
    assert payload["next_step"] == "blocked"
    assert payload["coverage"]["resolved_by"] == "refs"


async def test_topic_coverage_empty_and_unknown_topic(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    topic_id = await _topic_with_refs(db_session, [])
    payload = (
        await client.get(f"/api/data/catalog/topics/{topic_id}/coverage")
    ).json()
    assert payload["releases"] == []
    assert payload["gaps"] == []
    assert payload["last_sync"] is None
    assert payload["next_step"] == "no_refs"

    missing = await client.get("/api/data/catalog/topics/RT-missing/coverage")
    assert missing.status_code == 404
    assert "topic_not_found" in missing.json()["detail"]
