"""v1 选股必需数据集发布状态门(issue #255)。

runs 273-275 的 0 交易根因:research_db 选股必需 ``instrument_profiles`` 批次
从未发布(摄取 ≠ 发布),旧链路逐期 SKIPPED、引擎 0 交易「成功」收场。

覆盖验收:未发布批次 → 门控具名拦截;发布后 → 放行;bars/snapshot 模式与
未启用选股不拦截(按设计降级,#173);dataset_version 精确匹配口径。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finboard_data.factors import FactorSelectionConfig, InputsMode
from finboard_persistence import (
    ResearchDatasetRepository,
    ResearchSyncBatchModel,
    SyncBatchStatus,
    create_async_engine,
)

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from finboard_persistence import Base
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_sync_batches"))


def _config(**kwargs: object) -> FactorSelectionConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "inputs_mode": InputsMode.RESEARCH_DB,
        "ranking_factor": "market_cap",
    }
    defaults.update(kwargs)
    return FactorSelectionConfig(**defaults)  # type: ignore[arg-type]


def _session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _publish_batch(
    engine: AsyncEngine,
    *,
    dataset: str,
    dataset_version: str = "v-test",
) -> None:
    """直接落一行 published 批次(门控只看批次状态,不关心行数据)。"""
    import sqlalchemy as sa

    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(ResearchSyncBatchModel).values(
                dataset=dataset,
                source="tushare",
                dataset_version=dataset_version,
                code_version="test",
                status=SyncBatchStatus.PUBLISHED.value,
                parameters={},
                quality_status="pass",
                quality_report={},
                accepted_rows=10,
                rejected_rows=0,
                received_rows=10,
                started_at=NOW,
                completed_at=NOW,
                published_at=NOW,
            )
        )


async def test_gate_blocks_when_required_datasets_never_published(
    engine: AsyncEngine,
) -> None:
    """profiles / daily_metrics 批次都不存在:逐 dataset 具名缺失(#255)。"""
    async with _session_maker(engine)() as session:
        missing = await ResearchDatasetRepository(session).selection_inputs_gate(
            _config()
        )
    assert "dataset_unpublished:instrument_profiles" in missing
    assert "dataset_unpublished:daily_metrics" in missing


async def test_gate_passes_after_batches_published(engine: AsyncEngine) -> None:
    """必需批次发布后放行:同一配置从具名缺失变为空元组。"""
    await _publish_batch(engine, dataset="instrument_profiles")
    await _publish_batch(engine, dataset="daily_metrics")
    async with _session_maker(engine)() as session:
        missing = await ResearchDatasetRepository(session).selection_inputs_gate(
            _config()
        )
    assert missing == ()


async def test_gate_version_pinned_requires_exact_published_version(
    engine: AsyncEngine,
) -> None:
    """声明 dataset_versions 时按精确版本解析:版本不符仍判未发布。"""
    await _publish_batch(engine, dataset="instrument_profiles", dataset_version="v1")
    await _publish_batch(engine, dataset="daily_metrics")
    async with _session_maker(engine)() as session:
        repo = ResearchDatasetRepository(session)
        missing = await repo.selection_inputs_gate(
            _config(dataset_versions={"instrument_profiles": "v-other"})
        )
        assert "dataset_unpublished:instrument_profiles" in missing
        assert "dataset_unpublished:daily_metrics" not in missing
        assert (
            await repo.selection_inputs_gate(
                _config(dataset_versions={"instrument_profiles": "v1"})
            )
            == ()
        )


async def test_gate_skips_bars_and_snapshot_and_disabled(engine: AsyncEngine) -> None:
    """bars / snapshot 模式与未启用选股不拦截(降级语义,#173)。"""
    async with _session_maker(engine)() as session:
        repo = ResearchDatasetRepository(session)
        assert (
            await repo.selection_inputs_gate(
                _config(enabled=False, inputs_mode=InputsMode.RESEARCH_DB)
            )
            == ()
        )
        assert (
            await repo.selection_inputs_gate(_config(inputs_mode=InputsMode.BARS))
            == ()
        )
        assert (
            await repo.selection_inputs_gate(
                _config(
                    inputs_mode=InputsMode.SNAPSHOT,
                    snapshot_ids=("snapshot-test-1",),
                )
            )
            == ()
        )
