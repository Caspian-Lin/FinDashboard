"""issue #308 集成测试:research_run 实时进度透出(job 视图真实落库形态)。

覆盖验收标准:

* 加载期探针写入 background_jobs 的形态:``research_run:decision_load k/N``
  **只进 phase 字段**,done/total 数值列保持 0(#188 决策执行口径不被加载期
  数值占位污染 —— ``update_progress`` 的单调/夹紧规则会把两种口径互相卡死);
* 决策执行期 phase 编码 ``research_run:<stage>#<序号>@<YYYY-MM-DD>`` 在真实
  worker 消费路径可见,终态 phase 仍为 ``research_run:<status>``(#188 兼容);
* 进度上报故障不改变 run 状态机(尽力而为;#188 既有测试覆盖,此处锁
  probe 写入失败静默 —— job 行不存在时不抛错)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import ResearchRunExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    default_store_factory,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    NormalizedSignal,
    ResearchRunManifest,
    ResearchRunStatus,
    UniverseCandidate,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.signal_engine import (
    build_run_interrupt_probe,
)
from finboard_persistence import (
    BackgroundJobRepository,
    ResearchRunRepository,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

pytestmark = pytest.mark.asyncio

#: 决策执行期 phase 编码(issue #308)。
_DECISION_PHASE_RE = re.compile(r"^research_run:[a-z_]+#[0-9]+@\d{4}-\d{2}-\d{2}$")


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
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- 样本与 helper(与 test_issue_306_run_guard.py 同构,自包含) --------------


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _manifest(suffix: str) -> ResearchRunManifest:
    from finboard_backtest.strategy_spec import build_strategy_template

    spec = build_strategy_template(
        "ma_cross",
        strategy_id=f"ma_cross_308_{suffix}",
        dataset_release_ids=("frozen-release-v1",),
    )
    return ResearchRunManifest(
        run_id=_run_id(f"issue-308-{suffix}"),
        idempotency_key=f"issue-308-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-v1",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="issue-308-test",
    )


def _portfolio_input_at(day: int) -> PortfolioDecisionInput:
    symbols = ("A.SH", "B.SH", "C.SH")
    decision_at = datetime(2024, 1, 2 + day, 15, tzinfo=UTC)
    return PortfolioDecisionInput(
        business_date=date(2024, 1, 2 + day),
        decision_at=decision_at,
        execution_at=datetime(2024, 1, 3 + day, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("issue 308 集成测试候选池通过",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in symbols
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=10.0,
                source_artifact_ids=("frozen-release-v1",),
                available_at=decision_at,
            )
            for symbol in symbols
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="issue-308-signal",
                rationale="issue 308 集成测试冻结信号",
            )
            for symbol in symbols
        ),
        prices=dict.fromkeys(symbols, 10.0),
        execution_prices=dict.fromkeys(symbols, 10.0),
        lot_info={
            symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in symbols
        },
        input_artifact_ids=("frozen-release-v1",),
        covariance=CovarianceEstimate(
            matrix=np.diag([0.01, 0.01, 0.01]),
            tickers=list(symbols),
            shrinkage=0.0,
            n_observations=252,
        ),
        sleeve_map=dict.fromkeys(symbols, "equity"),
    )


class _SlowPortfolioAdapter(PortfolioPipelineAdapter):
    """决策间 sleep 的慢速 adapter:留出观察决策级 phase 的窗口。"""

    def __init__(self, *, decisions: int, sleep_seconds: float) -> None:
        super().__init__(
            strategy_kind="ma_cross",
            decision_inputs=tuple(
                _portfolio_input_at(day) for day in range(decisions)
            ),
        )
        self._sleep_seconds = sleep_seconds

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        index = 0
        async for decision in super().decisions(manifest):
            if index > 0:
                await asyncio.sleep(self._sleep_seconds)
            index += 1
            yield decision


async def _queue_double_write(
    engine: AsyncEngine,
    manifest: ResearchRunManifest,
) -> tuple[str, str]:
    """模拟 queue_research_run 路由双写;返回 (run_id, job_id)。"""

    async with session_factory(engine)() as session:
        repo = ResearchRunRepository(session)
        row, _ = await repo.create_or_get(
            run_id=manifest.run_id,
            idempotency_key=manifest.idempotency_key,
            replay_of_run_id=None,
            strategy_id=manifest.strategy_spec.strategy_id,
            strategy_kind=manifest.strategy_kind,
            status=ResearchRunStatus.QUEUED.value,
            schema_version=manifest.schema_version,
            manifest_checksum=manifest.checksum,
            manifest=cast(dict[str, object], to_json_value(manifest)),
            requested_by=manifest.requested_by,
        )
        job_repo = BackgroundJobRepository(session)
        payload: dict[str, object] = {
            "run_id": manifest.run_id,
            "strategy_kind": manifest.strategy_kind,
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        job_row, _ = await job_repo.create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=manifest.idempotency_key,
            kind="research_run",
            queue="research",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=checksum,
            max_attempts=3,
            requested_by=manifest.requested_by,
        )
        row.job_id = job_row.job_id
        await repo.checkpoint()
    return manifest.run_id, job_row.job_id


def _build_worker(
    engine: AsyncEngine,
    *,
    adapter_factory: object,
    worker_id: str = "w-308",
) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_factory(engine),
            store_factory=default_store_factory,
            adapter_factory=adapter_factory,  # type: ignore[arg-type]
        ),
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id=worker_id,
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60.0,
            heartbeat_interval_seconds=10.0,
            zombie_no_progress_seconds=3600.0,
            retry_backoff_seconds=30.0,
        ),
    )


async def _drain_worker(worker: BackgroundWorker) -> None:
    await worker._fill_concurrency()
    for _ in range(600):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


async def _get_run(engine: AsyncEngine, run_id: str):
    async with session_factory(engine)() as session:
        return await ResearchRunRepository(session).get(run_id)


async def _get_job(engine: AsyncEngine, job_id: str):
    async with session_factory(engine)() as session:
        return await BackgroundJobRepository(session).get(job_id)


# ---- 验收 1:加载期 phase k/N 且不污染 #188 数值口径 ---------------------------


class TestLoadProbePhaseWrite:
    async def test_probe_writes_phase_only_k_n(
        self, engine: AsyncEngine
    ) -> None:
        """探针把 ``decision_load k/N`` 写进 phase;done/total 数值列保持 0。"""

        run_id, job_id = await _queue_double_write(engine, _manifest("probe"))
        # 探针要求 run 处于 RUNNING(否则视为外部打断)。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.checkpoint()

        probe = build_run_interrupt_probe(session_factory(engine), run_id)
        await probe(4, 36)
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.phase == "research_run:decision_load 4/36"
        # 数值列未被加载期占用(#188 口径不被污染的核心断言)。
        assert row.progress_done == 0
        assert row.progress_total == 0
        # update_progress 顺带刷新心跳:长加载期间心跳与进度同源推进。
        assert row.heartbeat_at is not None

        # 推进 k:phase 覆盖写,数值列仍为 0。
        await probe(8, 36)
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.phase == "research_run:decision_load 8/36"
        assert row.progress_done == 0
        assert row.progress_total == 0

        # 决策执行期的第一帧(worker progress 回调同路径):数值口径照常
        # 生效 —— 加载期 k/N 不再像 #306 基线那样把 total 夹死在期数上。
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.update_progress(
                job_id,
                done=1,
                total=13,
                phase="research_run:universe#1@2024-01-02",
            )
            await repo.checkpoint()
        row = await _get_job(engine, job_id)
        assert row is not None
        assert (row.progress_done, row.progress_total) == (1, 13)
        assert row.phase == "research_run:universe#1@2024-01-02"

    async def test_probe_failure_is_silent(
        self, engine: AsyncEngine
    ) -> None:
        """job 行消失时探针静默返回(尽力而为可观测性,不阻断加载)。"""

        run_id, job_id = await _queue_double_write(engine, _manifest("silent"))
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.checkpoint()
        # 删掉 job 行:进度上报必然失败,探针须静默(suppress)而非炸加载。
        async with session_factory(engine)() as session:
            await session.execute(
                text("DELETE FROM background_jobs WHERE job_id = :job_id"),
                {"job_id": job_id},
            )
            await session.commit()
        probe = build_run_interrupt_probe(session_factory(engine), run_id)
        await probe(4, 36)


# ---- 验收 2:决策执行期 phase 编码可见 ----------------------------------------


@dataclass
class _PhaseObserver:
    """轮询收集 job phase 的观察器。"""

    seen: list[str] = field(default_factory=list)


class TestDecisionPhaseEncoding:
    async def test_worker_surfaces_decision_encoded_phase(
        self, engine: AsyncEngine
    ) -> None:
        """运行中 job 行可见 ``research_run:<stage>#<序号>@<日期>``,终态兼容。"""

        run_id, _job_id = await _queue_double_write(engine, _manifest("decision"))
        worker = _build_worker(
            engine,
            adapter_factory=lambda manifest: _SlowPortfolioAdapter(
                decisions=2, sleep_seconds=0.25
            ),
        )
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_id = run_row.job_id

        observer = _PhaseObserver()
        await worker._fill_concurrency()
        for _ in range(600):  # 至多 ~12s,慢速 adapter 提供足够观察窗口
            if not worker._inflight:
                break
            row = await _get_job(engine, job_id)
            if row is not None and row.phase:
                observer.seen.append(row.phase)
                if _DECISION_PHASE_RE.match(row.phase):
                    break
            await asyncio.sleep(0.02)

        encoded = [phase for phase in observer.seen if _DECISION_PHASE_RE.match(phase)]
        assert encoded, f"未观察到决策级 phase 编码: {observer.seen}"
        # 编码格式:stage 名 + 1-based 序号 + decision business_date。
        assert _DECISION_PHASE_RE.match(encoded[-1]) is not None
        assert "@2024-01-" in encoded[-1]

        await _drain_worker(worker)
        run_row = await _get_run(engine, run_id)
        assert run_row is not None
        assert run_row.status == ResearchRunStatus.COMPLETED.value, (
            f"error={run_row.error_code} {run_row.error_summary}"
        )
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
        # 终态 phase 保持 #188 兼容(编码不进入终态)。
        assert job_row.phase == "research_run:completed"
