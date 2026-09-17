"""``estimate_artifact_payload_bytes`` PostgreSQL 集成测试(issue #480)。

验证数据库侧 payload 体量估计与 SQLAlchemy 默认 JSON 序列化(裸
``json.dumps``,ensure_ascii)的存储文本逐字节一致 —— 这正是加载前护栏的
口径基础;并覆盖空 run 返回 0。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchRunRepository,
)

pytestmark = pytest.mark.asyncio

_RUN_ID = "RR-integration-480"


def _artifact(
    *,
    artifact_id: str,
    sequence: int,
    payload: dict[str, Any],
) -> ResearchRunArtifactModel:
    return ResearchRunArtifactModel(
        run_id=_RUN_ID,
        artifact_id=artifact_id,
        decision_id=f"D{sequence}",
        sequence=sequence,
        stage="features",
        trace_id=f"RRT-{sequence:04d}",
        parent_trace_ids=[],
        payload=payload,
        checksum="c1",
    )


async def test_estimate_matches_stored_json_text_bytes(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        ResearchRunModel(
            run_id=_RUN_ID,
            idempotency_key=f"ik-{_RUN_ID}",
            strategy_id="strat-demo",
            strategy_kind="multi_factor",
            status="completed",
            schema_version="1.0",
            manifest_checksum="mc",
            manifest={},
            requested_by="integration-test",
        )
    )
    await db_session.flush()

    payloads: list[dict[str, Any]] = [
        {"features": {"000001.SZ": {"momentum": 0.1, "rsi": 55.5}}},
        {"features": {"600000.SH": {"note": "中文键值 unicode 覆盖" * 8}}},
        {"features": {"empty": {}, "null": None, "list": [1, 2, 3]}},
    ]
    db_session.add_all(
        _artifact(
            artifact_id=f"{_RUN_ID}:A:{i}",
            sequence=i,
            payload=payload,
        )
        for i, payload in enumerate(payloads, start=1)
    )
    await db_session.flush()

    repo = ResearchRunRepository(db_session)
    estimate = await repo.estimate_artifact_payload_bytes(_RUN_ID)
    expected = sum(len(json.dumps(payload)) for payload in payloads)
    assert estimate == expected


async def test_estimate_empty_run_returns_zero(db_session: AsyncSession) -> None:
    db_session.add(
        ResearchRunModel(
            run_id=_RUN_ID,
            idempotency_key=f"ik-{_RUN_ID}",
            strategy_id="strat-demo",
            strategy_kind="multi_factor",
            status="completed",
            schema_version="1.0",
            manifest_checksum="mc",
            manifest={},
            requested_by="integration-test",
        )
    )
    await db_session.flush()
    repo = ResearchRunRepository(db_session)
    assert await repo.estimate_artifact_payload_bytes(_RUN_ID) == 0
