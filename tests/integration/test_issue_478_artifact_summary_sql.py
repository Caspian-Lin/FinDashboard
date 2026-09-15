"""``ResearchRunRepository.summarize_artifacts`` PostgreSQL 集成测试(issue #478)。

验证数据库侧聚合与 Python 参考实现(``finboard_mcp.reporting.summarize_run_artifacts``)
逐字段一致,覆盖:多决策、空 / 多 / 缺失 / None reasons、``decision_id=None →
空串键``、0 长度 fills 保键、非 universe/fills stage 只进 artifact_count、
空 run 零值;并回归断言聚合全程不发起 artifact 实体 / payload 的全量
SELECT(真实事故:7203 artifacts 全量物化把进程顶到 13.27GB)。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_mcp.reporting import summarize_run_artifacts
from finboard_persistence import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchRunRepository,
)

pytestmark = pytest.mark.asyncio

_RUN_ID = "RR-integration-478"


def _artifact(
    *,
    artifact_id: str,
    sequence: int,
    stage: str,
    decision_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ResearchRunArtifactModel:
    return ResearchRunArtifactModel(
        run_id=_RUN_ID,
        artifact_id=artifact_id,
        decision_id=decision_id,
        sequence=sequence,
        stage=stage,
        trace_id=f"RRT-{sequence:04d}",
        parent_trace_ids=[],
        payload=payload if payload is not None else {"report": {"ok": True}},
        checksum="c1",
    )


def _sample_artifacts() -> list[ResearchRunArtifactModel]:
    """多决策 + 边界 reasons 形态 + decision_id=None + 非 universe/fills stage。"""
    return [
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:universe",
            sequence=1,
            stage="universe",
            decision_id="D1",
            payload={
                "candidates": [
                    {"symbol": "A", "included": True, "reasons": ["ignored"]},
                    {"symbol": "B", "included": False, "reasons": ["st"]},
                    {"symbol": "C", "included": False, "reasons": ["st", "thin"]},
                    {"symbol": "D", "included": False, "reasons": []},
                    {"symbol": "E", "included": False},
                ]
            },
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D2:universe",
            sequence=2,
            stage="universe",
            decision_id="D2",
            payload={
                "candidates": [
                    {"symbol": "F", "included": False, "reasons": None},
                    {"symbol": "G", "included": False, "reasons": ["late"]},
                ]
            },
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D3:universe",
            sequence=3,
            stage="universe",
            decision_id="D3",
            payload={"candidates": None},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:fills",
            sequence=4,
            stage="fills",
            decision_id="D1",
            payload={"fills": [{"symbol": "A"}, {"symbol": "B"}]},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D2:fills",
            sequence=5,
            stage="fills",
            decision_id="D2",
            payload={"fills": []},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D4:fills",
            sequence=6,
            stage="fills",
            decision_id=None,
            payload={"fills": [{"symbol": "C"}]},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:features",
            sequence=7,
            stage="features",
            decision_id="D1",
            payload={"features": {"A": {"momentum": 0.1}}},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:report",
            sequence=8,
            stage="report",
            decision_id=None,
            payload={"report": {"strategy_return": 0.5}},
        ),
    ]


async def _insert_run(session: AsyncSession) -> None:
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
            requested_by="integration-test",
        )
    )
    await session.flush()


async def test_summary_matches_python_reference_field_by_field(
    db_session: AsyncSession,
) -> None:
    await _insert_run(db_session)
    db_session.add_all(_sample_artifacts())
    await db_session.flush()

    repo = ResearchRunRepository(db_session)
    rows = await repo.list_artifacts(_RUN_ID)
    summary = await repo.summarize_artifacts(_RUN_ID)

    # 与 Python 参考实现逐字段一致
    assert summary.artifact_count == len(rows)
    assert summary.summary_dict() == summarize_run_artifacts(rows)
    # 手算锚点:防止「两个实现一起错」——
    # 候选池 total=5(D1)+2(D2)+0(D3 candidates=null 记 0)=7、included=1;
    # reasons:st x2、thin x1、unknown x3(空/缺失/None 各 1)、late x1;
    # fills:D1=2、D2=0(空数组保键)、""=1(decision_id=None → 空串键)。
    assert summary.universe_total == 7
    assert summary.universe_included == 1
    assert summary.universe_excluded_by_reason == {
        "st": 2,
        "thin": 1,
        "unknown": 3,
        "late": 1,
    }
    assert summary.fills_total == 3
    assert summary.fills_by_decision == {"D1": 2, "D2": 0, "": 1}
    # 非 universe/fills stage 只进 artifact_count
    assert summary.artifact_count == 8


async def test_empty_run_returns_zero_summary(db_session: AsyncSession) -> None:
    await _insert_run(db_session)
    repo = ResearchRunRepository(db_session)
    summary = await repo.summarize_artifacts(_RUN_ID)
    assert summary.artifact_count == 0
    assert summary.summary_dict() == summarize_run_artifacts([])


async def test_summary_never_selects_payload(db_session: AsyncSession) -> None:
    await _insert_run(db_session)
    db_session.add_all(_sample_artifacts())
    await db_session.flush()

    captured: list[str] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        captured.append(statement)

    sync_engine = db_session.get_bind()
    event.listen(sync_engine, "before_cursor_execute", _capture)
    try:
        repo = ResearchRunRepository(db_session)
        summary = await repo.summarize_artifacts(_RUN_ID)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)

    assert summary.artifact_count == 8
    # 恰好三条聚合 SQL(count / universe / fills),且全部命中 artifact 表;
    # ORM 实体全列加载会生成 "… AS research_run_artifacts_<col>" 风格标签,
    # 出现即意味着 payload 被整列取回(回归)。
    assert len(captured) == 3
    assert all("research_run_artifacts" in s for s in captured)
    assert all("AS research_run_artifacts_" not in s for s in captured)
