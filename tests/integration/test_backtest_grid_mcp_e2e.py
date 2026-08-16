"""批量参数网格回测 MCP 端到端测试(issue #175)。

用真实 PostgreSQL(测试库)+ 真实 ``BacktestRunExecutor`` + **fake runner**
验证:

* ``grid_submit``:N 组参数 → N 个 kind=backtest_run job 同一事务落库,
  幂等重提交返回同一网格(created=False);
* worker 逐组合执行(模拟领取 → executor → finish),fake runner 对指定组合
  抛 ``BacktestRunError`` 模拟部分失败;
* ``grid_get``:聚合对比表正确性 —— 指标矩阵 / 竞争排名与最优标注 /
  部分失败带错误码单列 / equity 展示;all-terminal 后 complete=true。

不触实盘:只读写 ``backtest_grid_runs`` / ``background_jobs`` / ``backtest_runs``
三张研究域表。
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
    BacktestRunError,
    BacktestRunExecutor,
)
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import grid as grid_tools
from finboard_persistence import (
    BackgroundJobRepository,
    BacktestRunModel,
    Base,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import BackgroundJobStatus
from tests.integration.conftest import TEST_DB_URL as DB_URL

SUBMIT_KWARGS: dict[str, Any] = {
    "strategy": "ma_cross",
    "symbols": ["000001.SZ"],
    "start": "2024-01-01",
    "end": "2024-06-30",
    "capital": Decimal("100000"),
    "adjust": "qfq",
    "params": None,
    "selection": None,
    "params_grid": None,
    "max_combos": 20,
    "grid_idempotency_key": "grid-key-e2e-175",
    "requested_by": "test-agent",
    "commission_rate": Decimal("0.0003"),
    "commission_min": Decimal("1"),
    "stamp_tax_rate": Decimal("0.0005"),
    "slippage_bps": Decimal("0"),
}

PARAMS_LIST = [
    {"short_window": 5},
    {"short_window": 10},
    {"short_window": 15},
]


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
    """确定性 fake runner:指标由 short_window 决定;short_window=10 抛失败。"""

    short_window = int((request.params or {}).get("short_window", 5))
    if short_window == 10:
        raise BacktestRunError(code="data_error", summary="模拟数据源失败(short_window=10)")
    total_return = short_window / 100.0
    run = BacktestRunModel(
        strategy=request.strategy,
        symbols=list(request.symbols),
        start=request.start,
        end=request.end,
        capital=request.capital,
        adjust=request.adjust,
        params=dict(request.params or {}),
        metrics={
            "total_return": total_return,
            "annualized_return": total_return * 2,
            "sharpe_ratio": 1.0 + total_return,
            "max_drawdown": -0.10 + total_return,
            "win_rate": 0.5 + total_return,
            "trade_count": short_window,
            "turnover": 2.0,
            "commission_paid": "100.0000",
            "stamp_tax_paid": "50.0000",
            "benchmark_return": 0.05,
            "excess_return": total_return - 0.05,
            "initial_capital": "100000.0000",
            "final_equity": str(Decimal("100000") * (1 + Decimal(str(total_return)))),
        },
        equity_curve=[
            {"date": "2024-01-01", "equity": 100000.0},
            {"date": "2024-03-01", "equity": 100000.0 * (1 + total_return / 2)},
            {"date": "2024-06-30", "equity": 100000.0 * (1 + total_return)},
        ],
        fills=[],
        summary=f"fake summary short={short_window}",
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
            # executor 业务失败(ExecutorError,由 BacktestRunError 包装而来)
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
        await conn.execute(text("delete from backtest_grid_runs"))
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
async def test_grid_submit_enqueues_n_jobs_and_idempotent_resubmit(
    engine: AsyncEngine, app: McpAppContext
) -> None:
    env = await grid_tools.backtest_grid_submit(
        app, **{**SUBMIT_KWARGS, "params_list": PARAMS_LIST}
    )
    assert env.status == "ok", env.error
    grid_id = env.data["grid_id"]
    assert grid_id.startswith("BTG-")
    assert env.data["created"] is True
    assert env.data["combo_count"] == 3
    job_ids = [job["job_id"] for job in env.data["jobs"]]

    # 三个 backtest_run job 已在队列中,payload 带网格归属 + 展开参数
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        for index, job_id in enumerate(job_ids):
            row = await repo.get(job_id)
            assert row is not None
            assert row.kind == "backtest_run"
            assert row.status == BackgroundJobStatus.QUEUED.value
            payload: dict[str, Any] = dict(row.payload or {})
            assert payload["grid"]["grid_id"] == grid_id
            assert payload["grid"]["combo_index"] == index
            assert payload["request"]["params"]["short_window"] in (5, 10, 15)
            assert payload["request"]["params"]["long_window"] == 20  # base 合并

    # 幂等重提交(相同 grid_idempotency_key)→ 同一网格,不再入队
    env2 = await grid_tools.backtest_grid_submit(
        app, **{**SUBMIT_KWARGS, "params_list": PARAMS_LIST}
    )
    assert env2.status == "ok", env2.error
    assert env2.data["created"] is False
    assert env2.data["grid_id"] == grid_id
    assert [job["job_id"] for job in env2.data["jobs"]] == job_ids
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        rows = await repo.list_recent(kinds=["backtest_run"], limit=100)
        assert len(rows) == 3  # 没有重复入队

    # 上限校验:3 组合 vs max_combos=2 → invalid_argument
    env3 = await grid_tools.backtest_grid_submit(
        app,
        **{
            **SUBMIT_KWARGS,
            "grid_idempotency_key": "grid-key-e2e-175-b",
            "max_combos": 2,
            "params_list": PARAMS_LIST,
        },
    )
    assert env3.status == "error"
    assert env3.error is not None
    assert env3.error.kind == "invalid_argument"


@pytest.mark.asyncio
async def test_grid_get_aggregates_with_partial_failure(
    engine: AsyncEngine, app: McpAppContext
) -> None:
    env = await grid_tools.backtest_grid_submit(
        app,
        **{
            **SUBMIT_KWARGS,
            "grid_idempotency_key": "grid-key-e2e-175-c",
            "params_list": PARAMS_LIST,
        },
    )
    assert env.status == "ok", env.error
    grid_id = env.data["grid_id"]
    job_ids = [job["job_id"] for job in env.data["jobs"]]

    # 逐组合走真实 executor + fake runner(short_window=10 的组合失败)
    executor = BacktestRunExecutor(
        session_maker=session_factory(engine),
        runner=_fake_runner,
    )
    for job_id in job_ids:
        await _execute_job(engine, executor, job_id)

    # 聚合:2 成功 + 1 失败(short_window=10),complete=true
    env = await grid_tools.backtest_grid_get(app, grid_id=grid_id)
    assert env.status == "ok", env.error
    data = env.data
    assert data["complete"] is True
    assert data["completed_count"] == 3
    assert data["failed_count"] == 1
    assert data["pending_count"] == 0

    succeeded = [row for row in data["combos"] if "metrics" in row]
    assert len(succeeded) == 2
    by_short = {row["params"]["short_window"]: row for row in succeeded}
    assert by_short[5]["metrics"]["total_return"] == 0.05
    assert by_short[15]["metrics"]["total_return"] == 0.15
    # 排名与最优标注:total_return 15 > 5;max_drawdown 越高越好(-0.05 > -0.07)
    assert by_short[15]["rank"]["total_return"] == 1
    assert by_short[15]["best"]["total_return"] is True
    assert by_short[5]["rank"]["total_return"] == 2
    assert by_short[5]["best"]["total_return"] is False
    assert data["ranking"]["total_return"]["combo_index"] == by_short[15]["combo_index"]
    assert data["ranking"]["max_drawdown"]["combo_index"] == by_short[15]["combo_index"]
    # equity 曲线(默认 summary;3 个点不降采样)
    assert by_short[5]["equity_point_count"] == 3
    assert len(by_short[5]["equity_curve"]) == 3
    # 指标矩阵列按规范顺序
    assert data["metric_fields"][0] == "total_return"

    # 失败组合带错误码单列,不影响成功组合返回
    assert len(data["failures"]) == 1
    failure = data["failures"][0]
    assert failure["combo_index"] == 1  # short_window=10
    assert failure["job_status"] == "failed"
    assert failure["error_code"] == "data_error"
    assert "模拟数据源失败" in failure["error_summary"]
