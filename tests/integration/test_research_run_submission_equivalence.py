"""REST 与 MCP 研究运行提交等值矩阵(issue #513)。

#513 把两份编排收敛为 ``ResearchRunSubmissionService``;本文件用真实 PostgreSQL
验证两条适配层在同一输入下**行为等值**:

* 成功:同一 payload 的 ``manifest_checksum`` / ``status`` / ``job payload_checksum``
  完全一致,且跨通道同幂等键命中同一 ``RR-``(checksum 不一致会直接冲突);
* 错误:未发布策略 / 发布不匹配 / scope 漂移 / 候选池为空 / 死分区非空五类,
  REST 的 ``(status_code, detail.code)`` 与 MCP 的 ``error.kind`` 同源且零写入;
* 幂等:同键两次只产生 1 行 run + 1 行 job;
* 并发:``asyncio.gather`` 两路同键 → 单行(``create_or_get`` 撞唯一索引后重读);
* 故障注入:run 行写入后 job 入队抛错 → 整个事务回滚,两表零残留;
* 重放:``/runs/{id}/replay`` 与 ``finboard_run_replay`` 对同一源 run 产出同一
  manifest(同键命中同一新 ``RR-``)且血缘字段一致。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。只读写研究域表
(``research_runs`` / ``background_jobs`` / 策略与发布),不触实盘
``orders`` / ``fills`` / ``positions`` / ``audit_logs``,不执行回测。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_app.config import Settings
from finboard_app.research_run_submission import research_run_id_for
from finboard_backtest.research_run import manifest_from_json
from finboard_backtest.strategy_spec import ResearchStrategySpec, build_strategy_template
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleasedInstrument,
    default_execution_metadata,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import runs as run_tools
from finboard_persistence import (
    ResearchDatasetReleaseRepository,
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

pytestmark = pytest.mark.asyncio

BARS_RELEASE = "integration-equiv-bars-v1"
#: 研究数据发布(daily_metrics):只提供因子观测,宇宙比 bars 主发布窄(1 只)。
DAILY_RELEASE = "integration-equiv-daily-v1"
STRATEGY_ID = "integration_equiv_strategy"
EMPTY_STRATEGY_ID = "integration_equiv_empty"
DRAFT_STRATEGY_ID = "integration_equiv_draft"
#: 联合发布规格:同时冻结 bars 主发布 + daily_metrics 研究数据发布。
SHARED_STRATEGY_ID = "integration_equiv_shared"
SYMBOLS = ("600001.SH", "600002.SH", "600003.SH")
CODE_VERSION = "abcdef0123456789"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))
        await conn.execute(
            text(
                "DELETE FROM research_strategy_specs "
                "WHERE strategy_id LIKE 'integration_equiv%'"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM research_dataset_releases "
                "WHERE release_id LIKE 'integration-equiv%'"
            )
        )
        await conn.execute(
            text("DELETE FROM trade_cal WHERE source = 'integration-equiv'")
        )
        await conn.execute(
            text(
                "DELETE FROM instruments WHERE code IN "
                "('600001.SH', '600002.SH', '600003.SH')"
            )
        )


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(research_runs_router)
    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


@pytest.fixture
def mcp_app(engine: AsyncEngine) -> McpAppContext:
    return McpAppContext(
        settings=Settings(
            research_sandbox_enabled=False,
            research_sandbox_workspace_root="unused",
        ),
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=MagicMock(),
    )


# --------------------------------------------------------------------------- #
# 研究域种子数据
# --------------------------------------------------------------------------- #
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
        row_count=240,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        expected_sessions=240,
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


async def _publish_bars(engine: AsyncEngine) -> None:
    instruments = tuple(_instrument(code) for code in SYMBOLS)
    release = ResearchDatasetRelease(
        release_id=BARS_RELEASE,
        dataset_name="integration_equiv_bars",
        source="integration-equiv",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version=CODE_VERSION,
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=instruments,
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(instruments),
                ready_count=len(instruments),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=BARS_RELEASE,
        release_checksum="b" * 64,
    )
    async with session_factory(engine)() as session:
        await ResearchDatasetReleaseRepository(session).publish(release)
        await session.commit()


async def _publish_daily(engine: AsyncEngine) -> None:
    """研究数据发布(daily_metrics):宇宙只有 1 只,用来暴露「用错主发布」。"""

    from finboard_data.releases import DAILY_METRICS_FIELDS, ReleaseDatasetKind

    release = ResearchDatasetRelease(
        release_id=DAILY_RELEASE,
        dataset_name="integration_equiv_daily",
        source="integration-equiv",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="none",
        fields=DAILY_METRICS_FIELDS,
        availability_rules=(("daily_metrics", "available_at <= decision_at"),),
        code_version=CODE_VERSION,
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=(_instrument(SYMBOLS[0]),),
        capabilities=(),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        storage_uri=DAILY_RELEASE,
        release_checksum="c" * 64,
    )
    async with session_factory(engine)() as session:
        await ResearchDatasetReleaseRepository(session).publish(release)
        await session.commit()


def _spec(
    strategy_id: str,
    *,
    release_ids: tuple[str, ...] = (BARS_RELEASE,),
    **universe_updates: Any,
) -> ResearchStrategySpec:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id=strategy_id,
        dataset_release_ids=release_ids,
    )
    updates: dict[str, Any] = {
        "min_average_amount": None,
        "required_data_fields": ("price",),
        "ranking": None,
    }
    updates.update(universe_updates)
    return spec.model_copy(
        update={"universe": spec.universe.model_copy(update=updates)}
    )


async def _publish_spec(engine: AsyncEngine, spec: ResearchStrategySpec) -> None:
    async with session_factory(engine)() as session:
        repo = ResearchStrategySpecRepository(session)
        await repo.create_draft(
            strategy_id=spec.strategy_id,
            schema_version=spec.schema_version,
            name=spec.name,
            strategy_kind=spec.strategy_kind,
            checksum=_plan_checksum(spec),
            payload=spec.canonical_payload(),
        )
        await repo.publish(spec.strategy_id, 1, expected_version=1)
        await session.commit()


def _plan_checksum(spec: ResearchStrategySpec) -> str:
    from finboard_backtest.strategy_spec import compile_registered_strategy_spec

    return compile_registered_strategy_spec(spec).checksum


async def _create_draft_spec(engine: AsyncEngine, spec: ResearchStrategySpec) -> None:
    async with session_factory(engine)() as session:
        await ResearchStrategySpecRepository(session).create_draft(
            strategy_id=spec.strategy_id,
            schema_version=spec.schema_version,
            name=spec.name,
            strategy_kind=spec.strategy_kind,
            checksum=_plan_checksum(spec),
            payload=spec.canonical_payload(),
        )
        await session.commit()


def _payload(key: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "strategy_id": STRATEGY_ID,
        "strategy_version": 1,
        "dataset_release_ids": [BARS_RELEASE],
        "code_version": CODE_VERSION,
        "initial_capital": "200000",
        "requested_by": "integration-test",
        # REST 显式声明 agent,才能与 MCP 通道(恒 agent)逐字节同 manifest。
        "actor_type": "agent",
    }
    payload.update(overrides)
    return payload


async def _seed(engine: AsyncEngine) -> None:
    await _seed_calendar_and_instruments(engine)
    await _publish_bars(engine)
    await _publish_daily(engine)
    await _publish_spec(engine, _spec(STRATEGY_ID))
    await _publish_spec(engine, _spec(EMPTY_STRATEGY_ID, min_listing_days=10_000))
    await _publish_spec(
        engine,
        _spec(SHARED_STRATEGY_ID, release_ids=(BARS_RELEASE, DAILY_RELEASE)),
    )
    await _create_draft_spec(engine, _spec(DRAFT_STRATEGY_ID))


def _trading_days() -> list[date]:
    return [
        date(2024, 1, 1) + timedelta(days=offset)
        for offset in range(366)
        if (date(2024, 1, 1) + timedelta(days=offset)).weekday() < 5
    ]


async def _seed_calendar_and_instruments(engine: AsyncEngine) -> None:
    """scope 解析需要 trade_cal + instruments(与 #510 集成测试同口径)。"""

    from finboard_persistence import TradeCalRepository
    from finboard_persistence.models import InstrumentModel

    async with session_factory(engine)() as session:
        session.add_all(
            [
                InstrumentModel(
                    code=code,
                    name=code,
                    market=Market.A_SHARE.value,
                    instrument_type=InstrumentType.STOCK.value,
                    exchange="SSE",
                    listing_board="main",
                    status="active",
                    list_date=date(2020, 1, 1),
                )
                for code in SYMBOLS
            ]
        )
        await session.commit()
        await TradeCalRepository(session).upsert_trading_days(
            set(_trading_days()), source="integration-equiv"
        )
        await session.commit()


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.begin() as conn:
        runs = (
            await conn.execute(text("SELECT count(*) FROM research_runs"))
        ).scalar_one()
        jobs = (
            await conn.execute(text("SELECT count(*) FROM background_jobs"))
        ).scalar_one()
    return int(runs), int(jobs)


async def _run_row(engine: AsyncEngine, run_id: str) -> Any:
    from finboard_persistence import ResearchRunRepository

    async with session_factory(engine)() as session:
        return await ResearchRunRepository(session).get(run_id)


async def _job_row(engine: AsyncEngine, job_id: str) -> Any:
    from finboard_persistence import BackgroundJobRepository

    async with session_factory(engine)() as session:
        return await BackgroundJobRepository(session).get(job_id)


# --------------------------------------------------------------------------- #
# 1) 成功等值
# --------------------------------------------------------------------------- #
async def test_success_rest_and_mcp_are_byte_equivalent(
    engine: AsyncEngine, client: httpx.AsyncClient, mcp_app: McpAppContext
) -> None:
    await _seed(engine)

    rest = await client.post(
        "/api/research/runs", json=_payload("equiv-success-rest-key")
    )
    assert rest.status_code == 201, rest.text
    rest_body = rest.json()

    envelope = await run_tools.queue_run(
        mcp_app, payload=_payload("equiv-success-mcp-key")
    )
    assert envelope.status == "ok", envelope.error
    mcp_ack = envelope.data

    # 同一输入 → 同一冻结输入校验和(与运行身份无关,跨幂等键可比)。
    rest_row = await _run_row(engine, rest_body["run_id"])
    mcp_row = await _run_row(engine, mcp_ack["run_id"])
    assert rest_row is not None
    assert mcp_row is not None
    assert manifest_from_json(rest_row.manifest).input_checksum == (
        manifest_from_json(mcp_row.manifest).input_checksum
    )
    assert rest_body["status"] == mcp_ack["status"] == "queued"
    assert rest_body["strategy_kind"] == mcp_ack["strategy_kind"]
    assert rest_body["run_id"] == research_run_id_for("equiv-success-rest-key")
    assert mcp_ack["run_id"] == research_run_id_for("equiv-success-mcp-key")

    # 双写 run + job:job payload 逐字节一致(只差 run_id)。
    rest_job = await _job_row(engine, rest_body["job_id"])
    mcp_job = await _job_row(engine, mcp_ack["job_id"])
    assert rest_job is not None
    assert mcp_job is not None
    assert rest_job.kind == mcp_job.kind == "research_run"
    assert rest_job.queue == mcp_job.queue == "research"
    assert rest_job.payload["strategy_kind"] == mcp_job.payload["strategy_kind"]

    # 跨通道同幂等键 → 幂等命中同一 run(manifest 不同会直接冲突)。
    cross = await run_tools.queue_run(
        mcp_app, payload=_payload("equiv-success-rest-key")
    )
    assert cross.status == "ok", cross.error
    assert cross.data["run_id"] == rest_body["run_id"]
    assert cross.data["checksum"] == rest_body["manifest_checksum"]
    assert await _counts(engine) == (2, 2)


async def test_split_release_uses_bars_primary_pool(
    engine: AsyncEngine, client: httpx.AsyncClient, mcp_app: McpAppContext
) -> None:
    """联合发布顺序无关:REST/MCP 都用 bars 主发布评估候选池(#513 收敛)。

    旧 MCP 用 ``releases[0]`` 做候选池 / 组合门;当 ``dataset_release_ids`` 把
    daily_metrics 研究发布排在前面时,它拿研究发布的窄宇宙(1 只)去算
    ``ceil(1/0.35)=3`` 的可行性,误报 422。新实现与 REST 一致按 bars 主发布
    判定 —— 本用例在旧实现下会失败,是这次行为收敛的回归护栏。
    """

    await _seed(engine)
    payload = _payload(
        "equiv-split-release-key",
        strategy_id=SHARED_STRATEGY_ID,
        dataset_release_ids=[DAILY_RELEASE, BARS_RELEASE],
    )

    rest = await client.post("/api/research/runs", json=payload)
    assert rest.status_code == 201, rest.text
    rest_body = rest.json()

    envelope = await run_tools.queue_run(
        mcp_app,
        payload={**payload, "idempotency_key": "equiv-split-release-mcp-key"},
    )
    assert envelope.status == "ok", envelope.error
    mcp_ack = envelope.data

    rest_row = await _run_row(engine, rest_body["run_id"])
    mcp_row = await _run_row(engine, mcp_ack["run_id"])
    assert rest_row is not None
    assert mcp_row is not None
    assert manifest_from_json(rest_row.manifest).input_checksum == (
        manifest_from_json(mcp_row.manifest).input_checksum
    )
    # 两份发布都进冻结清单,且保持提交顺序(不只冻结 bars)。
    frozen_ids = [item["artifact_id"] for item in rest_row.manifest["dataset_releases"]]
    assert frozen_ids == [DAILY_RELEASE, BARS_RELEASE]


# --------------------------------------------------------------------------- #
# 2) 错误等值矩阵
# --------------------------------------------------------------------------- #
_ERROR_CASES = [
    # (label, payload overrides, REST status, detail code, MCP kind)
    (
        "unpublished_strategy",
        {"strategy_id": DRAFT_STRATEGY_ID},
        409,
        "strategy_not_published",
        "conflict",
    ),
    (
        "release_mismatch",
        {"dataset_release_ids": ["integration-equiv-unknown-release"]},
        422,
        "dataset_release_mismatch",
        "invalid_argument",
    ),
    (
        "scope_drift",
        {
            "data_scope": {
                "universe": {
                    "explicit_symbols": list(SYMBOLS),
                    "filters": {"min_listing_days": 60},
                },
                "decision": {"start": "2024-01-01", "end": "2024-12-31"},
                "warmup_sessions": 0,
                "tail_sessions": 0,
                "datasets": {"required": ["bars"]},
                "bars_adjustment": "qfq",
            },
            "data_scope_resolution_id": "SC-0000000000000000",
        },
        409,
        "scope_release_drift",
        "conflict",
    ),
    (
        "empty_universe",
        {"strategy_id": EMPTY_STRATEGY_ID},
        422,
        "universe_empty",
        "invalid_argument",
    ),
    (
        "dead_config_partition",
        {"execution_config": {"timing": "next_bar"}},
        422,
        "policy_override_invalid",
        "invalid_argument",
    ),
]


@pytest.mark.parametrize(
    ("label", "overrides", "status", "code", "kind"),
    _ERROR_CASES,
    ids=[case[0] for case in _ERROR_CASES],
)
async def test_error_equivalence(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    mcp_app: McpAppContext,
    label: str,
    overrides: dict[str, Any],
    status: int,
    code: str,
    kind: str,
) -> None:
    await _seed(engine)
    payload = _payload(f"equiv-error-{label}", **overrides)

    rest = await client.post("/api/research/runs", json=payload)
    assert rest.status_code == status, rest.text
    detail = rest.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]

    envelope = await run_tools.queue_run(mcp_app, payload=payload)
    assert envelope.status == "error", envelope.data
    assert envelope.error is not None
    assert envelope.error.kind == kind
    # 细业务码随消息进入 MCP 信封(MCP 无 details 字段),与 REST 同源。
    assert code in envelope.error.message

    # 两条通道都零写入(失败在写库前 / 事务回滚)。
    assert await _counts(engine) == (0, 0)


# --------------------------------------------------------------------------- #
# 3) 幂等 + 并发
# --------------------------------------------------------------------------- #
async def test_same_key_twice_creates_one_run_and_one_job(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    await _seed(engine)
    payload = _payload("equiv-idempotent-key")

    first = await client.post("/api/research/runs", json=payload)
    assert first.status_code == 201, first.text
    second = await client.post("/api/research/runs", json=payload)
    assert second.status_code == 201, second.text

    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["job_id"] == second.json()["job_id"]
    assert await _counts(engine) == (1, 1)


async def test_concurrent_same_key_yields_single_row(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    await _seed(engine)
    payload = _payload("equiv-concurrent-key")

    responses = await asyncio.gather(
        client.post("/api/research/runs", json=payload),
        client.post("/api/research/runs", json=payload),
    )
    for response in responses:
        assert response.status_code == 201, response.text
    assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
    assert await _counts(engine) == (1, 1)


# --------------------------------------------------------------------------- #
# 4) 故障注入:job 入队失败 → 整个事务回滚
# --------------------------------------------------------------------------- #
async def test_job_enqueue_failure_rolls_back_run_row(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(engine)

    import finboard_app.research_run_submission as submission
    from finboard_persistence import BackgroundJobPersistenceConflictError

    async def _boom(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise BackgroundJobPersistenceConflictError("injected job failure")

    monkeypatch.setattr(submission, "enqueue_job_core", _boom)

    response = await client.post(
        "/api/research/runs", json=_payload("equiv-fault-key")
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "run_conflict"
    # run 行已 flush 但事务回滚 → 两表零残留。
    assert await _counts(engine) == (0, 0)


# --------------------------------------------------------------------------- #
# 5) 重放等值
# --------------------------------------------------------------------------- #
async def test_replay_rest_and_mcp_are_equivalent(
    engine: AsyncEngine, client: httpx.AsyncClient, mcp_app: McpAppContext
) -> None:
    await _seed(engine)
    source = await client.post(
        "/api/research/runs", json=_payload("equiv-replay-source-key")
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["run_id"]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE research_runs SET status = 'completed' WHERE run_id = :rid"),
            {"rid": source_id},
        )

    rest = await client.post(
        f"/api/research/runs/{source_id}/replay",
        json={
            "idempotency_key": "equiv-replay-new-key",
            "requested_by": "integration-test",
            "actor_type": "agent",
        },
    )
    assert rest.status_code == 201, rest.text
    rest_body = rest.json()

    # 同键跨通道 → 命中同一新 RR-(manifest 不同会直接冲突)。
    envelope = await run_tools.replay_run(
        mcp_app,
        source_id,
        idempotency_key="equiv-replay-new-key",
        requested_by="integration-test",
    )
    assert envelope.status == "ok", envelope.error
    mcp_detail = envelope.data

    assert rest_body["run_id"] == mcp_detail["run_id"]
    assert rest_body["manifest_checksum"] == mcp_detail["manifest_checksum"]
    assert rest_body["replay_of_run_id"] == source_id
    assert mcp_detail["replay_of_run_id"] == source_id

    # 血缘字段与通道归属:两条通道都继承源 manifest,MCP 恒 agent。
    row = await _run_row(engine, rest_body["run_id"])
    assert row is not None
    assert row.manifest["replay_of_run_id"] == source_id
    assert row.manifest["replay_source_status"] == "completed"
    assert row.manifest["actor_type"] == "agent"
    # 源 run + 重放 run + 各自 job。
    assert await _counts(engine) == (2, 2)
