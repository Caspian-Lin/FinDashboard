"""补数请求端到端集成测试(issue #511)。

真实 PostgreSQL + 真实仓储/服务/执行器编排,**同步与发布用假执行器**
(monkeypatch 风格的 ``executor_factory``):不连 tushare、不写真实缓存/发布
目录,只验证补数语义:

* 两课题并发补相同范围 → 只建一个请求;
* 部分重叠 → 复用已完成/在途覆盖,保留独立缺口;
* 异源与不同复权不混接;
* 配额不足 → 预检 ``defer``(不拒绝);
* 质量门失败 → 不发布,请求/明细保留可复核状态;
* 取消后重试只补剩余项;
* 补数成功后产生新发布,历史发布不被修改(冻结不漂移)。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.data_backfill.contracts import (
    BackfillDatasetSpec,
    BackfillPlanDriftError,
    BackfillRequestSpec,
    BackfillRequestStatus,
)
from finboard_backtest.data_backfill.executor import DataBackfillExecutor
from finboard_backtest.data_backfill.service import (
    cancel_backfill,
    retry_backfill,
    submit_backfill,
)
from finboard_backtest.data_backfill.support import BackfillCapabilities, BudgetSnapshot
from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeDatasets,
    ScopeDecisionWindow,
    ScopeUniverse,
)
from finboard_backtest.research_run.contracts import ResearchActorType
from finboard_data.datasets import CatalogDataset
from finboard_persistence import session_factory
from finboard_persistence.models import (
    BackgroundJobModel,
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
    InstrumentModel,
    ResearchDatasetReleaseInstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchSyncBatchModel,
)
from finboard_persistence.trade_calendar_repo import TradeCalRepository

pytestmark = pytest.mark.asyncio

_SYMBOLS = ("000001.SZ", "600000.SH")
_SEED_START = date(2024, 1, 1)
_SEED_END = date(2024, 3, 31)


# ---------------------------------------------------------------------------
# 夹具与假执行器
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def maker(_engine: Any, db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """与 ``db_session`` 共享 engine 的 session maker(执行器自己开 session)。"""

    return session_factory(_engine)


@pytest_asyncio.fixture
async def calendar(db_session: AsyncSession) -> AsyncIterator[None]:
    """写入覆盖 2024-01-01..2024-03-31 的 SSE 日历与两只 A 股标的。"""

    days = {
        _SEED_START + timedelta(days=offset)
        for offset in range((_SEED_END - _SEED_START).days + 1)
        if (_SEED_START + timedelta(days=offset)).weekday() < 5
    }
    await TradeCalRepository(db_session).upsert_trading_days(
        days, source="test", exchanges=("SSE",)
    )
    for code in _SYMBOLS:
        db_session.add(
            InstrumentModel(
                code=code,
                name=code,
                market="a_share",
                instrument_type="stock",
                exchange="SZSE",
                listing_board="szse_main",
                status="active",
            )
        )
    await db_session.commit()
    yield


def _caps(*, remaining: int = 100_000) -> BackfillCapabilities:
    return BackfillCapabilities(
        tushare_token=True,
        cache_dir_writable=True,
        release_root_writable=True,
        budget=BudgetSnapshot(
            remaining_today=remaining,
            requests_per_minute=200,
            daily_limit=100_000,
        ),
    )


def _scope(
    *, start: date = _SEED_START, end: date = _SEED_END
) -> DataScopeSpec:
    return DataScopeSpec(
        universe=ScopeUniverse(explicit_symbols=_SYMBOLS),
        decision=ScopeDecisionWindow(start=start, end=end),
        datasets=ScopeDatasets(required=(CatalogDataset.BARS,)),
    )


def _spec(
    *,
    dataset: CatalogDataset = CatalogDataset.DAILY_METRICS,
    start: date = _SEED_START,
    end: date = _SEED_END,
    source: str | None = None,
    adjustment: str = "none",
    publish: bool = True,
) -> BackfillRequestSpec:
    return BackfillRequestSpec(
        scope=_scope(start=start, end=end),
        datasets=(
            BackfillDatasetSpec(
                dataset=dataset,
                source=source,
                adjustment=adjustment,  # type: ignore[arg-type]
                publish=publish,
            ),
        ),
    )


_KIND_BY_RELEASE_KIND = {
    "multi_asset_mixed": "bars",
    "a_share_tushare": "bars",
    "daily_metrics": "daily_metrics",
    "financial_indicators": "financial_indicators",
    "convertible_metrics": "convertible_metrics",
    "income_statements": "income_statements",
    "balance_sheets": "balance_sheets",
    "cashflow_statements": "cashflow_statements",
    "dividends": "dividends",
}


async def _seed_release(
    session: AsyncSession,
    *,
    release_id: str,
    dataset: str,
    kind: str,
    source: str,
    adjustment: str,
    start: date,
    end: date,
    symbols: tuple[str, ...],
    anomalies: int = 0,
) -> None:
    """写入一份可用发布 + 逐标的读模型(模拟既有共享发布)。"""

    session.add(
        ResearchDatasetReleaseModel(
            release_id=release_id,
            dataset_name=dataset,
            source=source,
            version="v-seed",
            schema_version="v1",
            start_date=start,
            end_date=end,
            period="1d",
            adjustment=adjustment,
            code_version="test",
            metadata_version="v1",
            symbol_count=len(symbols),
            row_count=len(symbols) * 10,
            coverage_pct=Decimal("1.0"),
            quality_status="passed",
            capabilities=[],
            quality_report={},
            known_limitations=[],
            storage_uri=f"file:///tmp/{release_id}",
            release_checksum=hashlib.sha256(release_id.encode()).hexdigest(),
            manifest={"dataset_kind": kind, "instruments": []},
            published_at=datetime.now(UTC),
        )
    )
    await session.flush()
    for code in symbols:
        session.add(
            ResearchDatasetReleaseInstrumentModel(
                release_id=release_id,
                code=code,
                name=code,
                market="a_share",
                instrument_type="stock",
                asset_class="equity",
                exchange="SZSE",
                listing_board="szse_main",
                start_date=start,
                end_date=end,
                row_count=10,
                expected_sessions=20,
                missing_sessions=0,
                suspended_sessions=0,
                anomaly_count=anomalies,
                duplicate_count=0,
                coverage_pct=Decimal("1.0"),
                ready=True,
                issues=[],
                status="active",
            )
        )
    await session.commit()


class _FakeDatasetSync:
    """替身:仅写一条 ``research_sync_batches`` 行(不触网)."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        status: str = "published",
        quality_status: str = "passed",
    ) -> None:
        self._maker = maker
        self._status = status
        self._quality_status = quality_status
        self.calls: list[dict[str, Any]] = []

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        payload = job.payload
        self.calls.append(dict(payload))
        datasets = payload["datasets"]
        assert isinstance(datasets, list)
        dataset = str(datasets[0])
        async with self._maker() as session:
            session.add(
                ResearchSyncBatchModel(
                    dataset=dataset,
                    source="tushare",
                    dataset_version=(
                        f"{dataset}:{payload['start_date']}:{payload['end_date']}:"
                        f"{len(self.calls)}"
                    ),
                    code_version="test",
                    parameters={
                        "start_date": payload["start_date"],
                        "end_date": payload["end_date"],
                    },
                    status=self._status,
                    quality_status=self._quality_status,
                    expected_rows=1,
                    received_rows=1,
                    accepted_rows=1,
                    rejected_rows=0,
                    error_summary=(
                        "质量门拒绝:覆盖率不足"
                        if self._quality_status == "failed"
                        else None
                    ),
                )
            )
            await session.commit()
        await progress(1, 1, f"dataset_sync:{dataset}:done")
        return JobResult(status="succeeded")


class _FakePublish:
    """替身:写一份新发布 + 逐标的读模型(不写文件)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self.calls: list[dict[str, Any]] = []

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        payload = job.payload
        self.calls.append(dict(payload))
        release_kind = str(payload["release_kind"])
        kind = _KIND_BY_RELEASE_KIND[release_kind]
        raw_symbols = payload["symbols"]
        assert isinstance(raw_symbols, list)
        symbols = [str(item) for item in raw_symbols]
        async with self._maker() as session:
            session.add(
                ResearchDatasetReleaseModel(
                    release_id=str(payload["release_id"]),
                    dataset_name=str(payload["dataset_name"]),
                    source="tushare" if release_kind != "multi_asset_mixed" else "mixed",
                    version=str(payload["version"]),
                    schema_version="v1",
                    start_date=date.fromisoformat(str(payload["start_date"])),
                    end_date=date.fromisoformat(str(payload["end_date"])),
                    period="1d",
                    adjustment=str(payload["adjustment"]),
                    code_version="test",
                    metadata_version="v1",
                    symbol_count=len(symbols),
                    row_count=len(symbols) * 20,
                    coverage_pct=Decimal("0.99"),
                    quality_status="passed",
                    capabilities=[],
                    quality_report={},
                    known_limitations=[],
                    storage_uri=f"file:///tmp/{payload['release_id']}",
                    release_checksum=hashlib.sha256(
                        str(payload["release_id"]).encode()
                    ).hexdigest(),
                    manifest={"dataset_kind": kind, "instruments": []},
                    published_at=datetime.now(UTC),
                )
            )
            await session.flush()
            for code in symbols:
                session.add(
                    ResearchDatasetReleaseInstrumentModel(
                        release_id=str(payload["release_id"]),
                        code=code,
                        name=code,
                        market="a_share",
                        instrument_type="stock",
                        asset_class="equity",
                        exchange="SZSE",
                        listing_board="szse_main",
                        start_date=date.fromisoformat(str(payload["start_date"])),
                        end_date=date.fromisoformat(str(payload["end_date"])),
                        row_count=20,
                        expected_sessions=20,
                        missing_sessions=0,
                        suspended_sessions=0,
                        anomaly_count=0,
                        duplicate_count=0,
                        coverage_pct=Decimal("0.99"),
                        ready=True,
                        issues=[],
                        status="active",
                    )
                )
            await session.commit()
        await progress(1, 1, "dataset_publish:done")
        return JobResult(status="succeeded", result_ref=str(payload["release_id"]))


class _FakeNoop:
    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        await progress(1, 1, f"{job.kind}:done")
        return JobResult(status="succeeded")


def _executor_factory(maker: async_sessionmaker[AsyncSession]) -> Any:
    sync = _FakeDatasetSync(maker)
    publish = _FakePublish(maker)
    table = {
        "dataset_sync": sync,
        "dataset_publish": publish,
        "bulk_download": _FakeNoop(),
        "quality_repair": _FakeNoop(),
    }

    def factory(kind: str) -> Any:
        return table[kind]

    return factory, sync, publish


async def _submit(
    session: AsyncSession,
    spec: BackfillRequestSpec,
    *,
    key: str = "test-key",
    caps: BackfillCapabilities | None = None,
    expected_checksum: str | None = None,
    requested_by: str = "topic-1",
    retry_of: str | None = None,
) -> Any:
    result = await submit_backfill(
        session,
        spec,
        idempotency_key=key,
        requested_by=requested_by,
        actor_type=ResearchActorType.HUMAN,
        capabilities=caps or _caps(),
        expected_plan_checksum=expected_checksum,
        retry_of_request_id=retry_of,
    )
    await session.commit()
    return result


async def _execute(
    maker: async_sessionmaker[AsyncSession], request_id: str
) -> tuple[JobResult | None, Exception | None]:
    factory, _sync, _publish = _executor_factory(maker)
    executor = DataBackfillExecutor(session_maker=maker, executor_factory=factory)
    job = JobRecord(
        job_id="BJ-TEST",
        kind="data_backfill",
        queue="data",
        payload={"request_id": request_id},
        attempt=1,
        max_attempts=3,
        requested_by="test",
    )

    async def _progress(done: int, total: int | None, phase: str | None) -> None:
        return None

    try:
        return await executor.execute(job, _progress), None
    except Exception as exc:  # 测试需要保留原始异常类型与消息
        return None, exc


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestDedupe:
    async def test_two_topics_same_range_share_one_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        spec = _spec()
        first = await _submit(db_session, spec, key="topic-a", requested_by="topic-a")
        second = await _submit(db_session, spec, key="topic-b", requested_by="topic-b")

        assert first.created is True
        assert second.created is False
        assert second.reused is True
        assert second.request.request_id == first.request.request_id
        rows = (await db_session.execute(select(DataBackfillRequestModel))).scalars().all()
        assert len(rows) == 1
        jobs = first.request.job_id
        assert jobs is not None
        assert jobs.startswith("BJ-")

    async def test_idempotency_key_hit_returns_same_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        spec = _spec()
        first = await _submit(db_session, spec, key="same-key")
        again = await _submit(
            db_session,
            _spec(start=date(2024, 2, 1)),
            key="same-key",
        )
        # 同幂等键:不新建、也不因 spec 不同而报错(幂等键优先)。
        assert again.created is False
        assert again.request.request_id == first.request.request_id

    async def test_partial_overlap_reuses_completed_coverage(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        await _seed_release(
            db_session,
            release_id="DR-daily_metrics-seed",
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            symbols=_SYMBOLS,
        )
        result = await _submit(
            db_session, _spec(end=date(2024, 3, 31)), key="partial"
        )
        item = result.plan.items[0]
        # 窗口末端按交易日历落位(2024-03-31 是周日 → 2024-03-29)。
        assert item.missing_ranges == ((date(2024, 2, 1), item.end),)
        assert [cov.id for cov in item.covered_by] == ["DR-daily_metrics-seed"]
        assert item.action.value == "sync_publish"

    async def test_partial_overlap_with_inflight_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        first = await _submit(
            db_session,
            _spec(end=date(2024, 2, 29)),
            key="inflight-a",
            requested_by="topic-a",
        )
        second = await _submit(
            db_session,
            _spec(start=date(2024, 2, 1)),
            key="inflight-b",
            requested_by="topic-b",
        )
        assert second.request.request_id != first.request.request_id
        item = second.plan.items[0]
        assert item.reused_request_ids == (first.request_id,)
        assert item.missing_ranges == ((date(2024, 3, 1), item.end),)
        assert any(
            overlap.request_id == first.request_id for overlap in second.plan.overlaps
        )

    async def test_same_fingerprint_after_failure_can_retry(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        spec = _spec()
        first = await _submit(db_session, spec, key="failed-key")
        repo_row = await db_session.get(
            DataBackfillRequestModel, first.request.id
        )
        assert repo_row is not None
        repo_row.status = BackfillRequestStatus.FAILED.value
        await db_session.commit()

        again = await _submit(db_session, spec, key="new-key")
        assert again.created is True
        assert again.request.request_id != first.request.request_id


class TestSourceIsolation:
    async def test_different_adjustment_is_not_reused(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        await _seed_release(
            db_session,
            release_id="DR-bars-qfq",
            dataset="bars",
            kind="bars",
            source="tushare",
            adjustment="qfq",
            start=_SEED_START,
            end=_SEED_END,
            symbols=_SYMBOLS,
        )
        covered = await _submit(
            db_session,
            _spec(dataset=CatalogDataset.BARS, adjustment="qfq"),
            key="bars-qfq",
        )
        assert covered.plan.items[0].action.value == "noop"
        assert covered.plan.items[0].missing_ranges == ()

        other = await _submit(
            db_session,
            _spec(dataset=CatalogDataset.BARS, adjustment="hqfq"),
            key="bars-hqfq",
        )
        item = other.plan.items[0]
        assert item.covered_by == ()
        assert item.missing_ranges == ((_SEED_START, item.end),)
        # 不同复权口径单独成项,不复用 qfq 的覆盖。
        assert item.adjustment == "hqfq"
        assert any("不同的来源/复权口径" in warning for warning in item.warnings)


class TestQuota:
    async def test_insufficient_quota_marks_defer_not_reject(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        result = await _submit(
            db_session, _spec(), key="quota", caps=_caps(remaining=1)
        )
        assert result.plan.quota.status == "defer"
        assert result.plan.quota.days_needed >= 1
        assert result.request.status == BackfillRequestStatus.QUEUED.value

    async def test_missing_source_registers_blocked_demand_without_job(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        """缺来源/额度时登记需求(blocked 行),不建 job —— 不静默跳过。"""

        caps = BackfillCapabilities(
            tushare_token=False,
            cache_dir_writable=True,
            release_root_writable=True,
        )
        result = await _submit(
            db_session, _spec(), key="blocked-demand", caps=caps
        )
        assert result.plan.blocked is True
        assert any(
            blocker.code == "backfill_source_not_configured"
            for blocker in result.plan.blockers
        )
        assert result.request.status == BackfillRequestStatus.BLOCKED.value
        assert result.request.job_id is None
        assert result.request.blockers
        jobs = (await db_session.execute(select(BackgroundJobModel))).scalars().all()
        assert jobs == []


class TestExecutor:
    async def test_executor_publishes_new_release_and_keeps_history(
        self,
        db_session: AsyncSession,
        maker: async_sessionmaker[AsyncSession],
        calendar: None,
    ) -> None:
        await _seed_release(
            db_session,
            release_id="DR-daily_metrics-old",
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            symbols=_SYMBOLS,
        )
        result = await _submit(db_session, _spec(), key="exec-ok")
        request_pk = result.request.id
        job_result, error = await _execute(maker, result.request_id)
        assert error is None, error
        assert job_result is not None
        assert job_result.status == "succeeded"

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.SUCCEEDED.value
        items = (
            await db_session.execute(
                select(DataBackfillRequestItemModel).where(
                    DataBackfillRequestItemModel.request_id == result.request_id
                )
            )
        ).scalars().all()
        assert [item.status for item in items] == ["succeeded"]
        release_id = items[0].release_id
        assert release_id is not None
        assert release_id.startswith("DR-daily_metrics-BF-")
        assert row.release_ids == [release_id]

        old = (
            await db_session.execute(
                select(ResearchDatasetReleaseModel).where(
                    ResearchDatasetReleaseModel.release_id == "DR-daily_metrics-old"
                )
            )
        ).scalar_one()
        assert old.release_checksum == hashlib.sha256(b"DR-daily_metrics-old").hexdigest()
        assert old.end_date == date(2024, 1, 31)

        # 新发布成为全局可用覆盖:其他课题再请求同一窗口即无缺口。
        replan = await _submit(db_session, _spec(), key="exec-ok-2")
        assert replan.plan.items[0].action.value == "noop"

    async def test_quality_failure_keeps_reviewable_state(
        self,
        db_session: AsyncSession,
        maker: async_sessionmaker[AsyncSession],
        calendar: None,
    ) -> None:
        result = await _submit(db_session, _spec(), key="exec-quality")
        request_pk = result.request.id
        factory, sync, _publish = _executor_factory(maker)
        sync = _FakeDatasetSync(maker, status="quality_rejected", quality_status="failed")
        table = {
            "dataset_sync": sync,
            "dataset_publish": create_publish(maker),
            "bulk_download": _FakeNoop(),
            "quality_repair": _FakeNoop(),
        }
        def _factory(kind: str) -> Any:
            return table[kind]

        executor = DataBackfillExecutor(
            session_maker=maker, executor_factory=_factory
        )
        job = JobRecord(
            job_id="BJ-TEST",
            kind="data_backfill",
            queue="data",
            payload={"request_id": result.request_id},
            attempt=1,
            max_attempts=3,
            requested_by="test",
        )

        async def _progress(done: int, total: int | None, phase: str | None) -> None:
            return None

        from finboard_backtest.background_jobs.contracts import ExecutorError

        with pytest.raises(ExecutorError) as excinfo:
            await executor.execute(job, _progress)
        assert excinfo.value.code == "backfill_quality_failed"

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.FAILED.value
        assert row.error_code == "backfill_quality_failed"
        items = (
            await db_session.execute(
                select(DataBackfillRequestItemModel).where(
                    DataBackfillRequestItemModel.request_id == result.request_id
                )
            )
        ).scalars().all()
        assert [item.status for item in items] == ["failed"]
        batches = (
            await db_session.execute(
                select(ResearchSyncBatchModel).where(
                    ResearchSyncBatchModel.dataset == "daily_metrics"
                )
            )
        ).scalars().all()
        assert len(batches) == 1
        assert batches[0].quality_status == "failed"
        # 质量失败不发布。
        releases = (
            await db_session.execute(select(ResearchDatasetReleaseModel))
        ).scalars().all()
        assert releases == []
        del factory

    async def test_cancel_then_retry_only_remaining_items(
        self,
        db_session: AsyncSession,
        calendar: None,
    ) -> None:
        result = await _submit(db_session, _spec(), key="cancel-1")
        cancelled = await cancel_backfill(db_session, result.request_id)
        await db_session.commit()
        assert cancelled.status == BackfillRequestStatus.CANCELLED.value
        items = (
            await db_session.execute(
                select(DataBackfillRequestItemModel).where(
                    DataBackfillRequestItemModel.request_id == result.request_id
                )
            )
        ).scalars().all()
        assert [item.status for item in items] == ["cancelled"]

        retried = await retry_backfill(
            db_session,
            result.request_id,
            requested_by="topic-1",
            actor_type=ResearchActorType.HUMAN,
            capabilities=_caps(),
        )
        await db_session.commit()
        assert retried.created is True
        assert retried.request.request_id != result.request_id
        assert retried.request.retry_of_request_id == result.request_id
        assert retried.request.status == BackfillRequestStatus.QUEUED.value

    async def test_plan_drift_is_detected_before_submit(
        self,
        db_session: AsyncSession,
        calendar: None,
    ) -> None:
        from finboard_backtest.data_backfill.planner import plan_backfill

        spec = _spec()
        preview = await plan_backfill(db_session, spec, capabilities=_caps())
        checksum = preview.checksum()

        # 预览之后出现新发布(缺口变小)→ 提交必须拒绝而不是按旧计划执行。
        await _seed_release(
            db_session,
            release_id="DR-daily_metrics-concurrent",
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),
            symbols=_SYMBOLS,
        )
        with pytest.raises(BackfillPlanDriftError):
            await _submit(
                db_session, spec, key="drift", expected_checksum=checksum
            )
        await db_session.rollback()


def create_publish(maker: async_sessionmaker[AsyncSession]) -> _FakePublish:
    return _FakePublish(maker)
