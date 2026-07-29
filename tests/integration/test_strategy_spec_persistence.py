"""无代码策略 draft/publish/supersede/rollback/restart 集成测试。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.routes.strategy_specs import _version_out
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_persistence import (
    ResearchStrategySpecRepository,
    StrategySpecChangeType,
    StrategySpecVersionConflictError,
    session_factory,
)

pytestmark = pytest.mark.asyncio


def _spec(*, name: str, max_weight: float) -> ResearchStrategySpec:
    payload = build_strategy_template(
        "multi_factor",
        strategy_id="integration_factor_strategy",
        dataset_release_ids=("frozen-release-v1",),
    ).canonical_payload()
    payload["name"] = name
    payload["portfolio_policy"]["max_target_weight"] = max_weight
    return ResearchStrategySpec.model_validate(payload)


async def test_full_version_lifecycle_and_restart(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    repo = ResearchStrategySpecRepository(db_session)
    first_spec = _spec(name="初始多因子", max_weight=0.08)
    first_plan = compile_registered_strategy_spec(first_spec)
    first = await repo.create_draft(
        strategy_id=first_spec.strategy_id,
        schema_version=first_spec.schema_version,
        name=first_spec.name,
        strategy_kind=first_spec.strategy_kind,
        checksum=first_plan.checksum,
        payload=first_spec.canonical_payload(),
        expected_version=None,
    )
    assert first.version == 1
    await repo.publish(first_spec.strategy_id, 1, expected_version=1)
    await db_session.commit()

    second_spec = _spec(name="降低集中度", max_weight=0.05)
    second_plan = compile_registered_strategy_spec(second_spec)
    second = await repo.create_draft(
        strategy_id=second_spec.strategy_id,
        schema_version=second_spec.schema_version,
        name=second_spec.name,
        strategy_kind=second_spec.strategy_kind,
        checksum=second_plan.checksum,
        payload=second_spec.canonical_payload(),
        expected_version=1,
        change_type=StrategySpecChangeType.SUPERSEDE,
    )
    assert second.version == 2
    await repo.publish(second_spec.strategy_id, 2, expected_version=2)
    rollback = await repo.rollback(
        second_spec.strategy_id,
        1,
        expected_version=2,
    )
    assert rollback.version == 3
    assert rollback.rollback_of_version == 1
    await db_session.commit()

    maker = session_factory(_engine)
    async with maker() as restarted:
        restarted_repo = ResearchStrategySpecRepository(restarted)
        history = await restarted_repo.list_history(first_spec.strategy_id)
        assert [row.version for row in history] == [3, 2, 1]
        assert [row.status for row in history] == [
            "published",
            "superseded",
            "superseded",
        ]
        restored = ResearchStrategySpec.model_validate(history[0].payload)
        runner_plan = compile_registered_strategy_spec(restored)
        api_spec = _version_out(history[0]).spec
        api_plan = compile_registered_strategy_spec(api_spec)
        assert runner_plan.checksum == api_plan.checksum == first_plan.checksum

        copied = deepcopy(history[0].payload)
        copied["name"] = "外部尝试篡改"
        unchanged = await restarted_repo.get_version(first_spec.strategy_id, 3)
        assert unchanged is not None
        assert unchanged.name == "初始多因子"


async def test_version_conflict_and_interrupted_publish_roll_back_atomically(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    repo = ResearchStrategySpecRepository(db_session)
    spec = _spec(name="并发保护", max_weight=0.08)
    plan = compile_registered_strategy_spec(spec)
    await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=plan.checksum,
        payload=spec.canonical_payload(),
        expected_version=None,
    )
    await repo.publish(spec.strategy_id, 1, expected_version=1)
    await db_session.commit()

    with pytest.raises(StrategySpecVersionConflictError, match="版本冲突"):
        await repo.create_draft(
            strategy_id=spec.strategy_id,
            schema_version=spec.schema_version,
            name=spec.name,
            strategy_kind=spec.strategy_kind,
            checksum=plan.checksum,
            payload=spec.canonical_payload(),
            expected_version=0,
        )
    await db_session.rollback()

    interrupted_spec = _spec(name="未完成发布", max_weight=0.05)
    interrupted_plan = compile_registered_strategy_spec(interrupted_spec)
    draft = await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=interrupted_spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=interrupted_plan.checksum,
        payload=interrupted_spec.canonical_payload(),
        expected_version=1,
        change_type=StrategySpecChangeType.SUPERSEDE,
    )
    await repo.publish(spec.strategy_id, draft.version, expected_version=draft.version)
    await db_session.rollback()

    maker = session_factory(_engine)
    async with maker() as restarted:
        latest = await ResearchStrategySpecRepository(restarted).get_latest(spec.strategy_id)
        assert latest is not None
        assert latest.version == 1
        assert latest.status == "published"
