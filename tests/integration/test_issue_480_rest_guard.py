"""REST 全量 payload 路径的加载前护栏集成测试(issue #480)。

``GET /api/research/runs/{run_id}/artifacts`` 与
``GET /api/research/runs/{run_id}/report/export`` 在载荷估计超限时必须以
413 具名拒绝、绝不发起全量 payload 加载 —— 真实 run(7203 artifacts /
≈5.9GB JSON)曾把 dev server 进程顶到 10GB+ 并拖到客户端超时(2026-09-16
现场:失控查询 11 分钟后才被人工取消,进程驻留 20GB)。默认阈值下行为不变。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from finboard_mcp import reporting
from finboard_persistence import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    session_factory,
)
from tests.integration.conftest import TEST_DB_URL

pytestmark = pytest.mark.asyncio

_RUN_ID = "RR-integration-480-rest"


async def _seed_run_with_artifacts() -> None:
    engine = create_async_engine(TEST_DB_URL)
    smaker = session_factory(engine)
    async with smaker() as session:
        session.add(
            ResearchRunModel(
                run_id=_RUN_ID,
                idempotency_key=f"ik-{_RUN_ID}",
                strategy_id="strat-demo",
                strategy_kind="multi_factor",
                status="completed",
                schema_version="1.0",
                manifest_checksum="mc",
                manifest={},
                result={"strategy_return": 0.5},
                requested_by="integration-test",
            )
        )
        await session.flush()
        session.add(
            ResearchRunArtifactModel(
                run_id=_RUN_ID,
                artifact_id=f"{_RUN_ID}:A:report",
                decision_id=None,
                sequence=1,
                stage="report",
                trace_id="RRT-0001",
                parent_trace_ids=[],
                payload={"report": {"strategy_return": 0.5}},
                checksum="c1",
            )
        )
        await session.commit()
    await engine.dispose()


@pytest.fixture
def _tiny_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)



async def test_artifacts_within_limit_ok(api: Any) -> None:
    await _seed_run_with_artifacts()
    resp = await api.client.get(f"/api/research/runs/{_RUN_ID}/artifacts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["payload"] == {"report": {"strategy_return": 0.5}}


@pytest.mark.usefixtures("_tiny_limit")
async def test_artifacts_over_limit_413_before_load(api: Any) -> None:
    await _seed_run_with_artifacts()
    resp = await api.client.get(f"/api/research/runs/{_RUN_ID}/artifacts")
    assert resp.status_code == 413, resp.text
    assert "不静默截断" in resp.json()["detail"]
    assert "MB" in resp.json()["detail"]


@pytest.mark.usefixtures("_tiny_limit")
async def test_report_export_over_limit_413_before_load(api: Any) -> None:
    await _seed_run_with_artifacts()
    resp = await api.client.get(
        f"/api/research/runs/{_RUN_ID}/report/export?format=csv"
    )
    assert resp.status_code == 413, resp.text
    assert "不静默截断" in resp.json()["detail"]


@pytest.mark.usefixtures("_tiny_limit")
async def test_lineage_over_limit_413_before_load(api: Any) -> None:
    await _seed_run_with_artifacts()
    resp = await api.client.get(f"/api/research/runs/{_RUN_ID}/lineage/RRT-0001")
    assert resp.status_code == 413, resp.text
    assert "不静默截断" in resp.json()["detail"]


async def test_report_export_within_limit_ok(api: Any) -> None:
    await _seed_run_with_artifacts()
    resp = await api.client.get(
        f"/api/research/runs/{_RUN_ID}/report/export?format=markdown"
    )
    assert resp.status_code == 200, resp.text
    assert "strategy_return" in resp.text
