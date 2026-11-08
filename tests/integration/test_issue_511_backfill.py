"""补数请求端到端集成测试(issue #511;含审核期 #511 复核修复)。

真实 PostgreSQL + 真实仓储/服务/执行器编排。子执行器用**严格替身**:每次
``execute`` 先跑真实 ``validate_job_payload`` 契约,再校验各 kind 的语义前置
(``quality_repair.symbols`` 非空、发布标的非空),因此不会掩盖 payload 不兼容
(审核 B1)或标的域未冻结(审核 B2)一类缺陷。

覆盖:

* 并发:同范围两课题(gather)、同幂等键并发 → 只建一个请求;
* 部分重叠:复用已完成 / 在途覆盖,保留独立缺口;
* bars:契约 payload(无 ``adjust`` 键)+ 发布身份含复权;非 qfq fail-closed;
  质量修复只针对冻结的异常标的;
* per-symbol 研究数据集:冻结标的并下传,发布面 == 请求面;
* 同 dataset 多复权:item 唯一键与发布身份不冲突;
* 预算耗尽 → ``retry_waiting`` 语义(请求退回 queued 可重跑);
* 一成功一失败 → 请求 partial(不掩盖已成功项的发布);
* 取消:排队中(直接 cancelled)与运行中(交给执行器收敛);
* 透明重试:发布成功后落库前崩溃,重跑按计算身份复用发布;
* REST 409/422/404 映射。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_api.routes.data_backfill import (
    BackfillPreviewRequest,
    BackfillRetryRequest,
    BackfillSubmitRequest,
    cancel_backfill_request,
    create_backfill,
    get_backfill,
    preview_backfill,
    retry_backfill_request,
)
from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.payload_contracts import validate_job_payload
from finboard_backtest.data_backfill.contracts import (
    BackfillDatasetSpec,
    BackfillPlanDriftError,
    BackfillRequestSpec,
    BackfillRequestStatus,
)
from finboard_backtest.data_backfill.executor import (
    DataBackfillExecutor,
    release_id_for,
    version_for,
)
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
from finboard_persistence.background_job_repo import BackgroundJobRepository
from finboard_persistence.models import (
    BackgroundJobModel,
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
    InstrumentModel,
    ResearchDatasetReleaseInstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchSyncBatchModel,
    TradeCalModel,
)
from finboard_persistence.trade_calendar_repo import TradeCalRepository

pytestmark = pytest.mark.asyncio

_SYMBOLS = ("000001.SZ", "600000.SH")
_SEED_START = date(2024, 1, 1)
_SEED_END = date(2024, 3, 31)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def maker(_engine: Any, db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """与 ``db_session`` 共享 engine 的 session maker(执行器/并发各自开 session)。"""

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


def _scope(*, start: date = _SEED_START, end: date = _SEED_END) -> DataScopeSpec:
    return DataScopeSpec(
        universe=ScopeUniverse(explicit_symbols=_SYMBOLS),
        decision=ScopeDecisionWindow(start=start, end=end),
        datasets=ScopeDatasets(required=(CatalogDataset.BARS,)),
    )


def _spec(
    *,
    datasets: tuple[BackfillDatasetSpec, ...] | None = None,
    start: date = _SEED_START,
    end: date = _SEED_END,
    dataset: CatalogDataset = CatalogDataset.DAILY_METRICS,
    source: str | None = None,
    adjustment: Literal["qfq", "hqfq", "none"] = "none",
    publish: bool = True,
) -> BackfillRequestSpec:
    return BackfillRequestSpec(
        scope=_scope(start=start, end=end),
        datasets=datasets
        or (
            BackfillDatasetSpec(
                dataset=dataset,
                source=source,
                adjustment=adjustment,
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
    anomaly_symbols: tuple[str, ...] = (),
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
                anomaly_count=anomalies if code in anomaly_symbols else 0,
                duplicate_count=0,
                coverage_pct=Decimal("1.0"),
                ready=True,
                issues=[],
                status="active",
            )
        )
    await session.commit()


# ---------------------------------------------------------------------------
# 严格替身:先跑真实 payload 契约,再校验语义前置
# ---------------------------------------------------------------------------


class _StrictHarness:
    """记录调用并按 kind 模拟副作用;任何契约违规立刻失败。"""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        quality_status: str = "passed",
        batch_status: str = "published",
        budget_exhausted: bool = False,
        fail_datasets: frozenset[str] = frozenset(),
    ) -> None:
        self._maker = maker
        self.quality_status = quality_status
        self.batch_status = batch_status
        self.budget_exhausted = budget_exhausted
        self.fail_datasets = fail_datasets
        self.calls: dict[str, list[dict[str, Any]]] = {
            "dataset_sync": [],
            "bulk_download": [],
            "quality_repair": [],
            "dataset_publish": [],
        }

    def factory(self, kind: str) -> _StrictExecutor:
        return _StrictExecutor(kind, self)

    async def behave(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        payload = job.payload
        if job.kind == "bulk_download":
            # 契约已拒绝 adjust;这里再钉一次语义(缓存写入只有 qfq)。
            assert "adjust" not in payload
            assert payload.get("market")
            return JobResult(status="succeeded")
        if job.kind == "quality_repair":
            symbols = payload.get("symbols")
            assert isinstance(symbols, list)
            assert symbols, "quality_repair 必须收到非空 symbols(审核 B1)"
            return JobResult(status="succeeded")
        if job.kind == "dataset_publish":
            for key in (
                "release_id",
                "dataset_name",
                "release_kind",
                "version",
                "start_date",
                "end_date",
                "adjustment",
                "symbols",
            ):
                assert payload.get(key), f"dataset_publish 缺 {key}"
            await _write_release(self._maker, payload)
            return JobResult(
                status="succeeded", result_ref=str(payload["release_id"])
            )
        datasets = payload.get("datasets")
        assert isinstance(datasets, list)
        dataset = str(datasets[0])
        if self.budget_exhausted:
            raise ExecutorError(
                code="tushare_budget_exhausted",
                summary="Tushare 每日请求预算耗尽",
                retryable=True,
            )
        if dataset in self.fail_datasets:
            raise ExecutorError(
                code="research_data_upstream",
                summary="上游数据错误",
                retryable=False,
            )
        symbols = payload.get("symbols")
        if not symbols:
            from finboard_backtest.background_jobs.dataset_sync.spec import (
                SYNC_SPECS,
                EnumShape,
            )

            shape = SYNC_SPECS.get(dataset).shape
            if shape is EnumShape.PER_SYMBOL_RANGE:
                # 逐标的数据集必须收到冻结标的或 universe 过滤器(审核 B2)。
                assert payload.get("instrument_type"), (
                    "逐标的 dataset_sync 必须收到 symbols 或 instrument_type"
                )
        async with self._maker() as session:
            session.add(
                ResearchSyncBatchModel(
                    dataset=dataset,
                    source="tushare",
                    dataset_version=(
                        f"{dataset}:{payload['start_date']}:{payload['end_date']}:"
                        f"{len(self.calls['dataset_sync'])}"
                    ),
                    code_version="test",
                    parameters={
                        "start_date": payload["start_date"],
                        "end_date": payload["end_date"],
                    },
                    status=self.batch_status,
                    quality_status=self.quality_status,
                    expected_rows=1,
                    received_rows=1,
                    accepted_rows=1,
                    rejected_rows=0,
                    error_summary=(
                        "质量门拒绝:覆盖率不足"
                        if self.quality_status == "failed"
                        else None
                    ),
                )
            )
            await session.commit()
        await progress(1, 1, f"dataset_sync:{dataset}:done")
        return JobResult(status="succeeded")


class _StrictExecutor:
    def __init__(self, kind: str, harness: _StrictHarness) -> None:
        self._kind = kind
        self._harness = harness

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        # 真实契约:未知键 / 缺失字段 / 非法来源在替身里同样 fail-fast。
        validate_job_payload(job.kind, job.payload)
        self._harness.calls[job.kind].append(dict(job.payload))
        return await self._harness.behave(job, progress)


async def _write_release(
    maker: async_sessionmaker[AsyncSession], payload: dict[str, Any]
) -> None:
    release_kind = str(payload["release_kind"])
    kind = _KIND_BY_RELEASE_KIND[release_kind]
    raw_symbols = payload["symbols"]
    assert isinstance(raw_symbols, list)
    symbols = [str(item) for item in raw_symbols]
    start = date.fromisoformat(str(payload["start_date"]))
    end = date.fromisoformat(str(payload["end_date"]))
    async with maker() as session:
        session.add(
            ResearchDatasetReleaseModel(
                release_id=str(payload["release_id"]),
                dataset_name=str(payload["dataset_name"]),
                source="tushare" if release_kind != "multi_asset_mixed" else "mixed",
                version=str(payload["version"]),
                schema_version="v1",
                start_date=start,
                end_date=end,
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
                    start_date=start,
                    end_date=end,
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


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _submit(
    session: AsyncSession,
    spec: BackfillRequestSpec,
    *,
    key: str = "test-key",
    caps: BackfillCapabilities | None = None,
    expected_checksum: str | None = None,
    requested_by: str = "topic-1",
) -> Any:
    result = await submit_backfill(
        session,
        spec,
        idempotency_key=key,
        requested_by=requested_by,
        actor_type=ResearchActorType.HUMAN,
        capabilities=caps or _caps(),
        expected_plan_checksum=expected_checksum,
    )
    await session.commit()
    return result


async def _execute(
    maker: async_sessionmaker[AsyncSession],
    request_id: str,
    *,
    harness: _StrictHarness | None = None,
) -> tuple[JobResult | None, Exception | None]:
    box = harness or _StrictHarness(maker)
    executor = DataBackfillExecutor(
        session_maker=maker, executor_factory=box.factory
    )
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


def _detail(exc: HTTPException) -> dict[str, Any]:
    assert isinstance(exc.detail, dict)
    return exc.detail


async def _items(
    session: AsyncSession, request_id: str
) -> list[DataBackfillRequestItemModel]:
    rows = await session.execute(
        select(DataBackfillRequestItemModel).where(
            DataBackfillRequestItemModel.request_id == request_id
        )
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# 去重 / 并发
# ---------------------------------------------------------------------------


class TestDedupe:
    async def test_two_topics_same_range_share_one_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        first = await _submit(db_session, _spec(), key="topic-a", requested_by="topic-a")
        second = await _submit(db_session, _spec(), key="topic-b", requested_by="topic-b")
        assert first.created is True
        assert second.created is False
        assert second.reused is True
        assert second.request.request_id == first.request.request_id
        rows = (await db_session.execute(select(DataBackfillRequestModel))).scalars().all()
        assert len(rows) == 1
        assert first.request.job_id is not None
        assert first.request.job_id.startswith("BJ-")

    async def test_concurrent_same_range_submits_create_one_request(
        self, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        """并发提交同 range(不同幂等键):advisory lock + 在途复用只建一条。"""

        async def _one(key: str, who: str) -> Any:
            async with maker() as session:
                return await _submit(session, _spec(), key=key, requested_by=who)

        results = await asyncio.gather(
            _one("concurrent-a", "topic-a"), _one("concurrent-b", "topic-b")
        )
        ids = {item.request.request_id for item in results}
        assert len(ids) == 1
        assert sum(1 for item in results if item.created) == 1
        async with maker() as session:
            rows = (
                await session.execute(select(DataBackfillRequestModel))
            ).scalars().all()
        assert len(rows) == 1

    async def test_concurrent_same_idempotency_key_returns_one_request(
        self, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        async def _one() -> Any:
            async with maker() as session:
                return await _submit(session, _spec(), key="same-key")

        results = await asyncio.gather(_one(), _one())
        assert len({item.request.request_id for item in results}) == 1
        async with maker() as session:
            rows = (
                await session.execute(select(DataBackfillRequestModel))
            ).scalars().all()
        assert len(rows) == 1

    async def test_idempotency_key_hit_returns_same_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        first = await _submit(db_session, _spec(), key="same-key")
        again = await _submit(db_session, _spec(start=date(2024, 2, 1)), key="same-key")
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
        result = await _submit(db_session, _spec(end=date(2024, 3, 31)), key="partial")
        item = result.plan.items[0]
        # 窗口末端按交易日历落位(2024-03-31 是周日 → 2024-03-29)。
        assert item.missing_ranges == ((date(2024, 2, 1), item.end),)
        assert [cov.id for cov in item.covered_by] == ["DR-daily_metrics-seed"]
        assert item.action.value == "sync_publish"

    async def test_partial_overlap_with_inflight_request(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        first = await _submit(
            db_session, _spec(end=date(2024, 2, 29)), key="inflight-a", requested_by="topic-a"
        )
        second = await _submit(
            db_session, _spec(start=date(2024, 2, 1)), key="inflight-b", requested_by="topic-b"
        )
        assert second.request.request_id != first.request.request_id
        item = second.plan.items[0]
        assert item.reused_request_ids == (first.request_id,)
        assert item.missing_ranges == ((date(2024, 3, 1), item.end),)
        assert any(overlap.request_id == first.request_id for overlap in second.plan.overlaps)

    async def test_same_fingerprint_after_failure_can_retry(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        first = await _submit(db_session, _spec(), key="failed-key")
        row = await db_session.get(DataBackfillRequestModel, first.request.id)
        assert row is not None
        row.status = BackfillRequestStatus.FAILED.value
        await db_session.commit()
        again = await _submit(db_session, _spec(), key="new-key")
        assert again.created is True
        assert again.request.request_id != first.request.request_id

    async def test_plan_drift_is_detected_before_submit(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        from finboard_backtest.data_backfill.planner import plan_backfill

        spec = _spec()
        preview = await plan_backfill(db_session, spec, capabilities=_caps())
        checksum = preview.checksum()
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
            await _submit(db_session, spec, key="drift", expected_checksum=checksum)
        await db_session.rollback()


# ---------------------------------------------------------------------------
# 异源 / 复权隔离 + item 身份
# ---------------------------------------------------------------------------


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
            db_session, _spec(dataset=CatalogDataset.BARS, adjustment="qfq"), key="bars-qfq"
        )
        assert covered.plan.items[0].action.value == "noop"
        other = await _submit(
            db_session, _spec(dataset=CatalogDataset.BARS, adjustment="hqfq"), key="bars-hqfq"
        )
        item = other.plan.items[0]
        assert item.covered_by == ()
        assert item.adjustment == "hqfq"
        assert any("不同的来源/复权口径" in warning for warning in item.warnings)
        # 非 qfq 在预检即阻塞(不建 job),并给出具名原因。
        assert other.plan.blocked is True
        assert any(
            blocker.code == "backfill_adjustment_unsupported"
            for blocker in other.plan.blockers
        )
        assert other.request.status == BackfillRequestStatus.BLOCKED.value

    async def test_same_dataset_two_adjustments_have_unique_items(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        """item 唯一键含 adjustment:同 dataset 的 qfq/hqfq 不撞唯一冲突。"""

        spec = _spec(
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.BARS, adjustment="qfq"),
                BackfillDatasetSpec(dataset=CatalogDataset.BARS, adjustment="hqfq"),
            )
        )
        result = await _submit(db_session, spec, key="bars-both")
        items = await _items(db_session, result.request_id)
        assert len(items) == 2
        assert {(item.adjustment) for item in items} == {"qfq", "hqfq"}
        assert release_id_for("bars", "BF-X", "qfq") != release_id_for(
            "bars", "BF-X", "hqfq"
        )
        assert version_for("BF-X", "qfq") != version_for("BF-X", "hqfq")


# ---------------------------------------------------------------------------
# bars 执行(B1)
# ---------------------------------------------------------------------------


class TestBarsExecution:
    async def test_bars_sync_uses_contract_payload_and_publishes(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        result = await _submit(
            db_session, _spec(dataset=CatalogDataset.BARS, adjustment="qfq"), key="bars-exec"
        )
        assert result.plan.items[0].action.value == "sync_publish"
        assert result.plan.items[0].symbols == _SYMBOLS

        harness = _StrictHarness(maker)
        job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert error is None, error
        assert job_result is not None
        assert job_result.status == "succeeded"
        # 严格替身跑过真实契约:bulk_download payload 无 unknown key(如 adjust)。
        assert len(harness.calls["bulk_download"]) == 1
        bulk_payload = harness.calls["bulk_download"][0]
        assert bulk_payload["symbols"] == list(_SYMBOLS)
        assert bulk_payload["market"] == "a_share"
        publish_payload = harness.calls["dataset_publish"][0]
        assert publish_payload["release_id"] == release_id_for(
            "bars", result.request_id, "qfq"
        )
        assert publish_payload["symbols"] == list(_SYMBOLS)

    async def test_bars_repair_only_targets_frozen_anomaly_symbols(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        await _seed_release(
            db_session,
            release_id="DR-bars-anomaly",
            dataset="bars",
            kind="bars",
            source="tushare",
            adjustment="qfq",
            start=_SEED_START,
            end=date(2024, 3, 29),
            symbols=_SYMBOLS,
            anomalies=3,
            anomaly_symbols=("000001.SZ",),
        )
        result = await _submit(
            db_session, _spec(dataset=CatalogDataset.BARS, adjustment="qfq"), key="bars-repair"
        )
        item = result.plan.items[0]
        assert item.action.value == "repair"
        assert item.repair_symbols == ("000001.SZ",)
        assert item.missing_ranges == ()

        harness = _StrictHarness(maker)
        job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert error is None, error
        assert job_result is not None
        assert harness.calls["quality_repair"][0]["symbols"] == ["000001.SZ"]
        # 覆盖不缺:不再重跑整窗 bulk_download。
        assert harness.calls["bulk_download"] == []
        assert len(harness.calls["dataset_publish"]) == 1

    async def test_strict_profile_repairs_all_bounded_symbols(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        await _seed_release(
            db_session,
            release_id="DR-bars-clean",
            dataset="bars",
            kind="bars",
            source="tushare",
            adjustment="qfq",
            start=_SEED_START,
            end=date(2024, 3, 29),
            symbols=_SYMBOLS,
        )
        spec = BackfillRequestSpec(
            scope=_scope(),
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.BARS, adjustment="qfq"),
            ),
            quality_profile="strict",
        )
        result = await _submit(db_session, spec, key="bars-strict")
        assert result.plan.items[0].repair_symbols == _SYMBOLS


# ---------------------------------------------------------------------------
# per-symbol 研究数据集(B2 / B5)
# ---------------------------------------------------------------------------


class TestPerSymbolDatasets:
    async def test_per_symbol_dataset_freezes_symbols_and_publishes_same_surface(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        result = await _submit(
            db_session, _spec(dataset=CatalogDataset.FINANCIAL_INDICATORS), key="fin"
        )
        item = result.plan.items[0]
        assert item.symbols == _SYMBOLS
        assert item.action.value == "sync_publish"

        harness = _StrictHarness(maker)
        job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert error is None, error
        assert job_result is not None
        sync_payload = harness.calls["dataset_sync"][0]
        assert sync_payload["datasets"] == ["financial_indicators"]
        assert sync_payload["symbols"] == list(_SYMBOLS)
        # 只补缺口区间(整窗从 1 月到 3 月 29),且发布面 == 请求面。
        assert sync_payload["start_date"] == "2024-01-01"
        publish_payload = harness.calls["dataset_publish"][0]
        assert publish_payload["symbols"] == list(_SYMBOLS)

    async def test_missing_symbol_triggers_sync_even_without_window_gap(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        """窗口级覆盖齐全但缺一个标的时,action 不能是 noop(审核 B5)。"""

        await _seed_release(
            db_session,
            release_id="DR-daily_metrics-one-symbol",
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 29),
            symbols=("000001.SZ",),
        )
        result = await _submit(db_session, _spec(), key="one-symbol")
        item = result.plan.items[0]
        assert item.missing_ranges == ()
        assert item.missing_symbols.count == 1
        assert item.action.value != "noop"
        assert item.action.value == "sync_publish"


# ---------------------------------------------------------------------------
# 配额 / 受阻 / 重试
# ---------------------------------------------------------------------------


class TestQuotaAndBlocked:
    async def test_insufficient_quota_marks_defer_not_reject(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        result = await _submit(db_session, _spec(), key="quota", caps=_caps(remaining=1))
        assert result.plan.quota.status == "defer"
        assert result.request.status == BackfillRequestStatus.QUEUED.value

    async def test_missing_source_registers_blocked_demand_without_job(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        caps = BackfillCapabilities(
            tushare_token=False,
            cache_dir_writable=True,
            release_root_writable=True,
        )
        result = await _submit(db_session, _spec(), key="blocked-demand", caps=caps)
        assert result.plan.blocked is True
        assert any(
            blocker.code == "backfill_source_not_configured"
            for blocker in result.plan.blockers
        )
        assert result.request.status == BackfillRequestStatus.BLOCKED.value
        assert result.request.job_id is None
        jobs = (await db_session.execute(select(BackgroundJobModel))).scalars().all()
        assert jobs == []

    async def test_blocked_request_can_retry_after_source_enabled(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        """blocked 是需求登记,来源开通后同一请求可 retry(审核 B6)。"""

        caps = BackfillCapabilities(
            tushare_token=False,
            cache_dir_writable=True,
            release_root_writable=True,
        )
        blocked = await _submit(db_session, _spec(), key="blocked-retry", caps=caps)
        assert blocked.request.status == BackfillRequestStatus.BLOCKED.value

        retried = await retry_backfill(
            db_session,
            blocked.request_id,
            requested_by="topic-1",
            actor_type=ResearchActorType.HUMAN,
            capabilities=_caps(),
        )
        await db_session.commit()
        assert retried.created is True
        assert retried.request.request_id != blocked.request_id
        assert retried.request.retry_of_request_id == blocked.request_id
        assert retried.request.status == BackfillRequestStatus.QUEUED.value


# ---------------------------------------------------------------------------
# 执行器状态机 / 失败补偿
# ---------------------------------------------------------------------------


class TestExecutor:
    async def test_executor_publishes_new_release_and_keeps_history(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
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
        harness = _StrictHarness(maker)
        job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert error is None, error
        assert job_result is not None
        assert job_result.status == "succeeded"
        # 只补缺口区间(2 月到 3 月 29),不重跑 1 月。
        assert harness.calls["dataset_sync"][0]["start_date"] == "2024-02-01"

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.SUCCEEDED.value
        items = await _items(db_session, result.request_id)
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

        replan = await _submit(db_session, _spec(), key="exec-ok-2")
        assert replan.plan.items[0].action.value == "noop"

    async def test_quality_failure_keeps_reviewable_state(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        result = await _submit(db_session, _spec(), key="exec-quality")
        request_pk = result.request.id
        harness = _StrictHarness(
            maker, batch_status="quality_rejected", quality_status="failed"
        )
        _job_result, error = await _execute(
            maker, result.request_id, harness=harness
        )
        assert isinstance(error, ExecutorError)
        assert error.code == "backfill_quality_failed"

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.FAILED.value
        assert row.error_code == "backfill_quality_failed"
        items = await _items(db_session, result.request_id)
        assert [item.status for item in items] == ["failed"]
        releases = (await db_session.execute(select(ResearchDatasetReleaseModel))).scalars().all()
        assert releases == []

    async def test_budget_exhaustion_returns_request_to_queued(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        """预算耗尽 → 可重试失败(请求回 queued、item 回 pending),重跑可成功。"""

        result = await _submit(db_session, _spec(), key="budget")
        request_pk = result.request.id
        harness = _StrictHarness(maker, budget_exhausted=True)
        _job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert isinstance(error, ExecutorError)
        assert error.code == "backfill_quota_insufficient"
        assert error.retryable is True

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.QUEUED.value
        items = await _items(db_session, result.request_id)
        assert [item.status for item in items] == ["pending"]

        retry_harness = _StrictHarness(maker)
        job_result, error = await _execute(
            maker, result.request_id, harness=retry_harness
        )
        assert error is None, error
        assert job_result is not None
        assert job_result.status == "succeeded"

    async def test_multi_item_failure_is_partial_not_total(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        spec = _spec(
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),
                BackfillDatasetSpec(dataset=CatalogDataset.FINANCIAL_INDICATORS),
            )
        )
        result = await _submit(db_session, spec, key="partial-failure")
        request_pk = result.request.id
        harness = _StrictHarness(
            maker, fail_datasets=frozenset({"financial_indicators"})
        )
        _job_result, error = await _execute(
            maker, result.request_id, harness=harness
        )
        assert isinstance(error, ExecutorError)
        assert error.code == "research_data_upstream"

        db_session.expire_all()
        row = await db_session.get(DataBackfillRequestModel, request_pk)
        assert row is not None
        assert row.status == BackfillRequestStatus.PARTIAL.value
        items = await _items(db_session, result.request_id)
        statuses = {item.dataset: item.status for item in items}
        assert statuses["daily_metrics"] == "succeeded"
        assert statuses["financial_indicators"] == "failed"
        # 成功项的发布保留(不被整体失败回滚)。
        releases = (await db_session.execute(select(ResearchDatasetReleaseModel))).scalars().all()
        assert len(releases) == 1

    async def test_publish_identity_reused_after_crash_before_item_update(
        self, db_session: AsyncSession, maker: async_sessionmaker[AsyncSession], calendar: None
    ) -> None:
        """发布成功但 item 落库前崩溃:重跑按计算出的 release_id 复用,不撞身份。"""

        result = await _submit(db_session, _spec(), key="crash-reuse")
        expected_release = release_id_for("daily_metrics", result.request_id, "none")
        await _seed_release(
            db_session,
            release_id=expected_release,
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 29),
            symbols=_SYMBOLS,
        )
        harness = _StrictHarness(maker)
        job_result, error = await _execute(maker, result.request_id, harness=harness)
        assert error is None, error
        assert job_result is not None
        assert harness.calls["dataset_publish"] == []
        items = await _items(db_session, result.request_id)
        assert items[0].release_id == expected_release


class TestCancel:
    async def test_cancel_queued_then_retry_only_remaining(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        result = await _submit(db_session, _spec(), key="cancel-1")
        cancelled = await cancel_backfill(db_session, result.request_id)
        await db_session.commit()
        assert cancelled.status == BackfillRequestStatus.CANCELLED.value
        items = await _items(db_session, result.request_id)
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

    async def test_running_cancel_is_left_to_executor(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        """运行中取消:只标未领取的 pending,job 交给执行器收敛(不抢跑状态)。

        否则执行器随后的成功/失败回写会和这里改写的 cancelled 打架(审核 B8)。
        """

        spec = _spec(
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),
                BackfillDatasetSpec(dataset=CatalogDataset.FINANCIAL_INDICATORS),
            )
        )
        result = await _submit(db_session, spec, key="cancel-running")
        assert result.request.job_id is not None
        job_repo = BackgroundJobRepository(db_session)
        await job_repo.transition(
            result.request.job_id,
            expected=frozenset({"queued"}),
            target="running",
        )
        from finboard_persistence.data_backfill_repo import DataBackfillRepository

        repo = DataBackfillRepository(db_session)
        await repo.set_request_status(
            result.request_id, status=BackfillRequestStatus.RUNNING.value
        )
        items = await _items(db_session, result.request_id)
        running_item = items[0]
        await repo.update_item(
            running_item.id, status="running", expected_statuses=("pending",)
        )
        await db_session.commit()

        row = await cancel_backfill(db_session, result.request_id)
        await db_session.commit()
        assert row.status == BackfillRequestStatus.RUNNING.value
        items = await _items(db_session, result.request_id)
        statuses = {item.id: item.status for item in items}
        assert statuses[running_item.id] == "running"
        assert statuses[items[1].id] == "cancelled"


# ---------------------------------------------------------------------------
# REST 映射
# ---------------------------------------------------------------------------


class TestRestMapping:
    async def test_get_missing_request_is_404(
        self, db_session: AsyncSession
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await get_backfill("BF-NOPE", session=db_session)
        assert excinfo.value.status_code == 404

    async def test_submit_without_idempotency_key_is_422(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        body = BackfillSubmitRequest(spec=_spec())
        with pytest.raises(HTTPException) as excinfo:
            await create_backfill(body, idempotency_key=None, requested_by="api", session=db_session)
        assert excinfo.value.status_code == 422

    async def test_submit_plan_drift_is_409(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        preview = await preview_backfill(
            BackfillPreviewRequest(spec=_spec()), session=db_session
        )
        await _seed_release(
            db_session,
            release_id="DR-daily_metrics-drift",
            dataset="daily_metrics",
            kind="daily_metrics",
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 29),
            symbols=_SYMBOLS,
        )
        with pytest.raises(HTTPException) as excinfo:
            await create_backfill(
                BackfillSubmitRequest(
                    spec=_spec(), expected_plan_checksum=preview["plan_checksum"]
                ),
                idempotency_key="drift-rest",
                requested_by="api",
                session=db_session,
            )
        assert excinfo.value.status_code == 409
        assert _detail(excinfo.value)["code"] == "backfill_plan_drift"

    async def test_preview_window_out_of_scope_is_422(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        spec = _spec()
        bad = spec.model_copy(
            update={
                "datasets": (
                    BackfillDatasetSpec(
                        dataset=CatalogDataset.DAILY_METRICS,
                        start=date(2023, 1, 1),
                        end=date(2023, 2, 1),
                    ),
                )
            }
        )
        with pytest.raises(HTTPException) as excinfo:
            await preview_backfill(BackfillPreviewRequest(spec=bad), session=db_session)
        assert excinfo.value.status_code == 422
        assert _detail(excinfo.value)["code"] == "backfill_window_out_of_scope"

    async def test_retry_without_calendar_is_422_not_500(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        caps = BackfillCapabilities(
            tushare_token=False,
            cache_dir_writable=True,
            release_root_writable=True,
        )
        blocked = await _submit(db_session, _spec(), key="retry-cal", caps=caps)
        await db_session.execute(delete(TradeCalModel))
        await db_session.commit()
        with pytest.raises(HTTPException) as excinfo:
            await retry_backfill_request(
                blocked.request_id,
                BackfillRetryRequest(),
                requested_by="api",
                session=db_session,
            )
        assert excinfo.value.status_code == 422
        assert _detail(excinfo.value)["code"] == "scope_calendar_unavailable"

    async def test_cancel_terminal_request_is_409(
        self, db_session: AsyncSession, calendar: None
    ) -> None:
        result = await _submit(db_session, _spec(), key="cancel-rest")
        row = await db_session.get(DataBackfillRequestModel, result.request.id)
        assert row is not None
        row.status = BackfillRequestStatus.SUCCEEDED.value
        await db_session.commit()
        with pytest.raises(HTTPException) as excinfo:
            await cancel_backfill_request(
                result.request_id, requested_by="api", session=db_session
            )
        assert excinfo.value.status_code == 409


async def _noop() -> None:  # pragma: no cover - 占位避免未使用告警
    return None


class TestTaskQueueWiring:
    async def test_generic_enqueue_does_not_open_kind(self) -> None:
        from finboard_api.routes.jobs import _ALLOWED_KINDS as REST_KINDS
        from finboard_mcp.tools.jobs import _ALLOWED_KINDS as MCP_KINDS

        assert "data_backfill" not in REST_KINDS
        assert "data_backfill" not in MCP_KINDS
        await _noop()
