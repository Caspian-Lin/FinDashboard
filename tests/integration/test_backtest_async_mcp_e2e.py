"""backtest_run strategy 形态异步化 MCP 端到端测试(issue #189)。

用真实 PostgreSQL(测试库)+ 真实 ``BacktestRunExecutor`` + **fake runner**
验证 strategy 形态异步全链路:

* ``run_async=true``:入队 ``kind=backtest_run`` job → 返回 job_id(不阻塞),
  payload 与 REST ``POST /api/backtest/run`` 同形 ``{request, provider_name}``;
* 未显式 ``run_async`` 且估算工作量达到 ``backtest_auto_async_symbol_days``
  阈值 → 自动切换异步(``async_mode=auto_threshold``);
* worker 消费(模拟领取 → executor → finish)后 ``finboard_job_get`` 可见
  ``result_ref=str(run_id)``,``finboard_backtest_history_get`` 可查完整结果;
* 幂等重提交(相同 idempotency_key)返回同一 job。

不触实盘:只读写 ``background_jobs`` / ``backtest_runs`` 两张研究域表。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.config import Settings
from finboard_backtest.background_jobs.contracts import JobRecord, JobResult
from finboard_backtest.background_jobs.executors.backtest_run import (
    BacktestRunExecutor,
)
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import backtest as backtest_tools
from finboard_persistence import (
    BackgroundJobRepository,
    BacktestRunModel,
    Base,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import BackgroundJobStatus
from tests.integration.conftest import TEST_DB_URL as DB_URL

RUN_KWARGS: dict[str, Any] = {
    "strategy": "ma_cross",
    "symbols": ["000001.SZ", "000002.SZ"],
    "start": "2024-01-01",
    "end": "2024-06-30",
    "capital": Decimal("100000"),
    "adjust": "qfq",
    "params": None,
    "selection": None,
    "commission_rate": Decimal("0.0003"),
    "commission_min": Decimal("1"),
    "stamp_tax_rate": Decimal("0.0005"),
    "slippage_bps": Decimal("0"),
}


async def _noop_progress(done: int, total: int | None, phase: str | None) -> None:
    return None


def _record(row: Any) -> JobRecord:
    return JobRecord(
        job_id=row.job_id,
        kind=row.kind,
        queue=row.queue,
        payload=dict(row.payload or {}),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        requested_by=row.requested_by,
    )


async def _fake_runner(session: Any, request: Any, provider_name: str) -> int:
    """确定性 fake runner:把 request 落一条 ``backtest_runs`` 记录并返回 run_id。"""

    run = BacktestRunModel(
        strategy=request.strategy,
        symbols=list(request.symbols),
        start=request.start,
        end=request.end,
        capital=request.capital,
        adjust=request.adjust,
        params=dict(request.params or {}),
        metrics={
            "total_return": 0.12,
            "annualized_return": 0.24,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.10,
            "win_rate": 0.55,
            "trade_count": 8,
            "turnover": 2.0,
            "commission_paid": "100.0000",
            "stamp_tax_paid": "50.0000",
            "benchmark_return": 0.05,
            "excess_return": 0.07,
            "initial_capital": "100000.0000",
            "final_equity": "112000.0000",
        },
        equity_curve=[
            {"date": "2024-01-01", "equity": 100000.0},
            {"date": "2024-06-30", "equity": 112000.0},
        ],
        fills=[],
        summary="fake async summary",
    )
    session.add(run)
    await session.commit()
    return int(run.id)


async def _execute_job(engine: AsyncEngine, executor: BacktestRunExecutor, job_id: str) -> None:
    """模拟 worker 的单任务循环:queued → running → execute → finish。"""

    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        row = await repo.transition(
            job_id,
            expected=frozenset({BackgroundJobStatus.QUEUED.value}),
            target=BackgroundJobStatus.RUNNING.value,
        )
        try:
            result: JobResult = await executor.execute(_record(row), _noop_progress)
        except Exception as exc:
            result = JobResult(
                status=BackgroundJobStatus.FAILED.value,
                error_code=getattr(exc, "code", type(exc).__name__),
                error_summary=getattr(exc, "summary", str(exc)),
            )
        await repo.finish(
            job_id,
            target=result.status,
            result_ref=result.result_ref,
            error_code=result.error_code,
            error_summary=result.error_summary,
        )
        await repo.checkpoint()


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from tests.integration.conftest import ensure_test_db

    await ensure_test_db(DB_URL)
    eng = create_async_engine(DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
        await conn.execute(text("delete from backtest_runs"))
    await eng.dispose()


@pytest.fixture(scope="module")
def app(engine: AsyncEngine) -> McpAppContext:
    return McpAppContext(
        settings=Settings(),
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


@pytest.mark.asyncio
async def test_run_async_full_chain_and_idempotent_resubmit(
    engine: AsyncEngine, app: McpAppContext
) -> None:
    # 1) run_async=true → 入队返回 job_id,不阻塞
    env = await backtest_tools.backtest_run(app, run_async=True, **RUN_KWARGS)
    assert env.status == "ok", env.error
    data = env.data
    job_id = data["job_id"]
    assert job_id.startswith("BJ-")
    assert data["status"] == BackgroundJobStatus.QUEUED.value
    assert data["created"] is True
    assert data["async_mode"] == "explicit"
    assert data["idempotency_key"].startswith("backtest:ma_cross:")
    assert "finboard_backtest_history_get" in data["execution_path"]

    # 2) job 已入 data 队列,payload 与 REST /api/backtest/run 同形
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        row = await repo.get(job_id)
        assert row is not None
        assert row.kind == "backtest_run"
        assert row.queue == "data"
        assert row.requested_by == "agent:mcp:backtest_run"
        payload: dict[str, Any] = dict(row.payload or {})
        assert payload["provider_name"] == app.settings.data_provider
        request: dict[str, Any] = payload["request"]
        assert request["strategy"] == "ma_cross"
        assert request["symbols"] == ["000001.SZ", "000002.SZ"]
        assert request["params"]["long_window"] == 20  # 参数校验后展开默认值

    # 3) 幂等重提交(同 request 同幂等键)→ 返回同一 job,created=false
    env2 = await backtest_tools.backtest_run(app, run_async=True, **RUN_KWARGS)
    assert env2.status == "ok", env2.error
    assert env2.data["job_id"] == job_id
    assert env2.data["created"] is False
    async with session_factory(engine)() as session:
        rows = await BackgroundJobRepository(session).list_recent(
            kinds=["backtest_run"], limit=100
        )
        assert len(rows) == 1  # 没有重复入队

    # 4) 模拟 worker 消费:executor + fake runner → 落库 run_id
    executor = BacktestRunExecutor(
        session_maker=session_factory(engine),
        runner=_fake_runner,
    )
    await _execute_job(engine, executor, job_id)

    # 5) worker 消费成功 → finboard_job_get 可见 result_ref=str(run_id)
    async with session_factory(engine)() as session:
        row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.SUCCEEDED.value
        run_id = int(row.result_ref or "-1")

    # 6) finboard_backtest_history_get 可查完整结果
    env = await backtest_tools.backtest_history_get(app, run_id=run_id)
    assert env.status == "ok", env.error
    detail = env.data
    assert detail["strategy"] == "ma_cross"
    assert detail["metrics"]["total_return"] == 0.12
    assert detail["equity_point_count"] == 2


@pytest.mark.asyncio
async def test_run_async_auto_threshold_switches(
    engine: AsyncEngine, app: McpAppContext
) -> None:
    """未显式 run_async,估算工作量达到阈值 → 自动切换异步(issue #189)。"""

    auto_app = McpAppContext(
        settings=Settings(backtest_auto_async_symbol_days=1),  # 任意规模即达标
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )
    env = await backtest_tools.backtest_run(
        auto_app,
        **{**RUN_KWARGS, "symbols": ["000001.SZ"]},
    )
    assert env.status == "ok", env.error
    data = env.data
    assert data["async_mode"] == "auto_threshold"
    assert data["job_id"].startswith("BJ-")
    assert data["auto_async_threshold"] == 1
