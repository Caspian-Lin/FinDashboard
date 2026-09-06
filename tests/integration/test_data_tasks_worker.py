"""数据域任务统一队列集成测试(issue #144)。

覆盖:
* ``claim_next`` per-kind 限额(max_per_kind):某 kind 已达 running 上限不领取;
* worker ``kind_concurrency``:feature_snapshot 单并发约束生效;
* dataset_publish 幂等(同 release_id 不重复);
* bulk_download 端到端(mock provider)→ succeeded;
* bulk_download 取消(running → cancel_requested → cancelled)。

所有用例共用一个 module 级 engine,与 ``test_background_job_persistence.py`` 隔离。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import BulkDownloadExecutor
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_persistence import (
    BackgroundJobRepository,
    Base,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)


def _checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue(
    engine: AsyncEngine,
    *,
    job_id: str | None = None,
    kind: str = "echo",
    queue: str = "default",
    priority: int = 0,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    status: str | None = None,
) -> str:
    payload = payload or {}
    job_id = job_id or generate_background_job_id()
    idem = idempotency_key or f"idem-{job_id}"
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=idem,
            kind=kind,
            queue=queue,
            status=status or BackgroundJobStatus.QUEUED.value,
            priority=priority,
            payload=payload,
            payload_checksum=_checksum(payload),
            max_attempts=3,
            requested_by="tester",
        )
        await repo.checkpoint()
        # 返回实际落库的 job_id(幂等命中时是旧记录的 id)
        row = await repo.get_by_idempotency_key(idem)
        assert row is not None
        return row.job_id


def _fresh_lease() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=600)


@pytest.fixture(scope="module")
async def engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(_db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("delete from background_jobs"))


def _session_maker(engine: AsyncEngine) -> object:
    return session_factory(engine)


def _build_worker(
    engine: AsyncEngine,
    registry: JobExecutorRegistry,
    *,
    kind_concurrency: dict[str, int] | None = None,
) -> BackgroundWorker:
    config = WorkerConfig(
        worker_id="test-worker",
        poll_interval_seconds=0.05,
        max_concurrent=4,
        lease_timeout_seconds=30,
        heartbeat_interval_seconds=5,
        queues=None,
        kind_concurrency=kind_concurrency,
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=_session_maker(engine),  # type: ignore[arg-type]
        registry=registry,
        config=config,
    )


async def _drain(worker: BackgroundWorker) -> None:
    """驱动一轮领取并等待所有 in-flight task 结束(复刻 test_background_job_persistence)。"""
    import asyncio

    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


# -------------------------------------------------------- claim_next per-kind


class TestClaimPerKind:
    async def test_max_per_kind_excludes_saturated_kind(self, engine: AsyncEngine) -> None:
        """某 kind 已有 1 个 running(max_per_kind=1),claim 不再领取该 kind。"""
        await _enqueue(engine, kind="feature_snapshot", payload={"x": 1})
        # 手动把一个 feature_snapshot 置为 running(模拟已达上限)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w0", lease_until=_fresh_lease(), limit=10
            )
            assert len(rows) == 1
            await repo.checkpoint()

        # 再入一个 feature_snapshot + 一个 echo
        await _enqueue(engine, kind="feature_snapshot", payload={"x": 2})
        await _enqueue(engine, kind="echo", payload={"x": 3})

        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w1",
                lease_until=_fresh_lease(),
                limit=10,
                max_per_kind={"feature_snapshot": 1},
            )
            await repo.checkpoint()
        claimed_kinds = [r.kind for r in rows]
        assert "echo" in claimed_kinds
        # feature_snapshot 已达上限,不应被领取
        assert "feature_snapshot" not in claimed_kinds

    async def test_max_per_kind_none_means_unlimited(self, engine: AsyncEngine) -> None:
        """max_per_kind=None 或空映射表示不限制。"""
        await _enqueue(engine, kind="echo", payload={"x": 1})
        await _enqueue(engine, kind="echo", payload={"x": 2})
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w1",
                lease_until=_fresh_lease(),
                limit=10,
                max_per_kind=None,
            )
            await repo.checkpoint()
        assert len(rows) == 2

    async def test_max_per_kind_zero_ignored(self, engine: AsyncEngine) -> None:
        """max_per_kind 值为 0 的 kind 视为不限制(忽略)。"""
        await _enqueue(engine, kind="echo", payload={"x": 1})
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w1",
                lease_until=_fresh_lease(),
                limit=10,
                max_per_kind={"echo": 0},
            )
            await repo.checkpoint()
        assert len(rows) == 1


# -------------------------------------------------------- worker kind_concurrency


class TestWorkerKindConcurrency:
    async def test_feature_snapshot_single_concurrent(self, engine: AsyncEngine) -> None:
        """两个 feature_snapshot 任务,kind_concurrency=1,领取时第二个不进 running。

        用 claim_next max_per_kind 直接验证(不需要真实 worker 执行)。
        """
        await _enqueue(
            engine,
            kind="feature_snapshot",
            payload={"echo": "first"},
            job_id="BJ-FS1",
        )
        await _enqueue(
            engine,
            kind="feature_snapshot",
            payload={"echo": "second"},
            job_id="BJ-FS2",
        )

        # 先领第一个置 running
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            first = await repo.claim_next(
                worker_id="w1", lease_until=_fresh_lease(), limit=1
            )
            assert len(first) == 1
            await repo.checkpoint()

        # 再领:max_per_kind=1 应排除 feature_snapshot(已有 1 running)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w1",
                lease_until=_fresh_lease(),
                limit=2,
                max_per_kind={"feature_snapshot": 1},
            )
            await repo.checkpoint()
        assert len(rows) == 0  # 第二个 feature_snapshot 被排除


# -------------------------------------------------------- dataset_publish 幂等


class TestDatasetPublishIdempotent:
    async def test_same_release_id_idempotent(self, engine: AsyncEngine) -> None:
        """同 idempotency_key 重复 enqueue 不创建重复任务。"""
        jid1 = await _enqueue(
            engine,
            kind="dataset_publish",
            payload={"release_id": "r1"},
            idempotency_key="publish:r1",
        )
        jid2 = await _enqueue(
            engine,
            kind="dataset_publish",
            payload={"release_id": "r1"},
            idempotency_key="publish:r1",
        )
        assert jid1 == jid2


# -------------------------------------------------------- bulk_download e2e


class TestBulkDownloadEndToEnd:
    async def test_bulk_download_succeeds_with_mock_provider(
        self, engine: AsyncEngine
    ) -> None:
        """bulk_download executor 用 mock provider 跑通 succeeded。"""
        # 准备一个 a_share stock instrument
        from finboard_persistence import InstrumentModel

        async with session_factory(engine)() as session:
            # 先清理可能残留的同 code 行(共享 DB),再插入
            from sqlalchemy import delete as sa_delete

            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "000001.SZ")
            )
            await session.flush()
            session.add(
                InstrumentModel(
                    code="000001.SZ",
                    name="平安银行",
                    market="a_share",
                    instrument_type="stock",
                    listing_board="szse_main",
                    status="active",
                )
            )
            await session.commit()

        registry = JobExecutorRegistry()
        registry.register(
            "bulk_download",
            BulkDownloadExecutor(
                session_maker=_session_maker(engine),  # type: ignore[arg-type]
                settings_factory=lambda: None,
            ),
        )
        worker = _build_worker(engine, registry)
        await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "akshare",
                "start": "2024-01-01",
            },
        )

        mock_provider = AsyncMock()
        mock_provider.update_cache_batch.return_value = {"000001.SZ": True}
        with patch(
            "finboard_backtest.background_jobs.executors.bulk_download.build_bar_provider",
            return_value=mock_provider,
        ):
            await _drain(worker)

        async with session_factory(engine)() as session:
            from sqlalchemy import select

            from finboard_persistence.models import BackgroundJobModel

            row = (
                await session.execute(
                    select(BackgroundJobModel).where(
                        BackgroundJobModel.kind == "bulk_download"
                    )
                )
            ).scalar_one()
            assert row.status == "succeeded"

    async def test_bulk_download_cancel_when_running(
        self, engine: AsyncEngine
    ) -> None:
        """running 的 bulk_download 可被 request_cancel(cancel_requested)。"""
        jid = await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "akshare",
                "start": "2024-01-01",
            },
        )
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(
                worker_id="w1", lease_until=_fresh_lease(), limit=1
            )
            await repo.request_cancel(jid)
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(jid)
            assert row is not None
            assert row.status == BackgroundJobStatus.CANCEL_REQUESTED.value

    async def test_bulk_download_index_instrument_type(
        self, engine: AsyncEngine
    ) -> None:
        """instrument_type=index 选中指数标的走 akshare 源(issue #256)。

        bulk_download 按 instrument_type 从 instruments 表筛出指数标的
        (生产路径由 data_sync 经 discover_indices + sync_with_diff 写入),
        交给 provider 拉取日线(akshare 指数接口,#184 分流)。
        """
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        from finboard_persistence import InstrumentModel
        from finboard_persistence.models import BackgroundJobModel
        from finboard_shared.models import Symbol

        codes = ("000300.SH", "000905.SH")
        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code.in_(codes))
            )
            await session.flush()
            session.add_all(
                (
                    InstrumentModel(
                        code="000300.SH",
                        name="沪深300",
                        market="a_share",
                        instrument_type="index",
                        exchange="SSE",
                        status="active",
                    ),
                    InstrumentModel(
                        code="000905.SH",
                        name="中证500",
                        market="a_share",
                        instrument_type="index",
                        exchange="SSE",
                        status="active",
                    ),
                )
            )
            await session.commit()

        registry = JobExecutorRegistry()
        registry.register(
            "bulk_download",
            BulkDownloadExecutor(
                session_maker=_session_maker(engine),  # type: ignore[arg-type]
                settings_factory=lambda: None,
            ),
        )
        worker = _build_worker(engine, registry)
        await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "akshare",
                "start": "2024-01-01",
                "instrument_type": "index",
            },
        )

        mock_provider = AsyncMock()
        mock_provider.update_cache_batch.return_value = {
            "000300.SH": True,
            "000905.SH": True,
        }
        with patch(
            "finboard_backtest.background_jobs.executors.bulk_download.build_bar_provider",
            return_value=mock_provider,
        ):
            await _drain(worker)

        async with session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(BackgroundJobModel).where(
                        BackgroundJobModel.kind == "bulk_download",
                        BackgroundJobModel.payload["instrument_type"].as_string()
                        == "index",
                    )
                )
            ).scalar_one()
            assert row.status == "succeeded"
        called = mock_provider.update_cache_batch.await_args
        assert called is not None
        symbols = list(called.args[0])
        assert sorted(s.code for s in symbols) == ["000300.SH", "000905.SH"]
        assert all(isinstance(s, Symbol) for s in symbols)

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code.in_(codes))
            )
            await session.commit()

    async def test_bulk_download_empty_source_resolves_to_default(
        self, engine: AsyncEngine
    ) -> None:
        """「默认源」入队写空串 source(#341 跟进):不再 invalid_payload,回落配置默认源。"""
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        from finboard_persistence import InstrumentModel
        from finboard_persistence.models import BackgroundJobModel

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "000852.SH")
            )
            await session.flush()
            session.add(
                InstrumentModel(
                    code="000852.SH",
                    name="中证1000",
                    market="a_share",
                    instrument_type="index",
                    exchange="SSE",
                    status="active",
                )
            )
            await session.commit()

        registry = JobExecutorRegistry()
        registry.register(
            "bulk_download",
            BulkDownloadExecutor(
                session_maker=_session_maker(engine),  # type: ignore[arg-type]
                settings_factory=lambda: None,
            ),
        )
        worker = _build_worker(engine, registry)
        await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "",
                "start": "2024-01-01",
                "instrument_type": "index",
            },
        )

        mock_provider = AsyncMock()
        mock_provider.update_cache_batch.return_value = {"000852.SH": True}
        with patch(
            "finboard_backtest.background_jobs.executors.bulk_download.build_bar_provider",
            return_value=mock_provider,
        ):
            await _drain(worker)

        async with session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(BackgroundJobModel).where(
                        BackgroundJobModel.kind == "bulk_download",
                        BackgroundJobModel.payload["source"].as_string() == "",
                    )
                )
            ).scalar_one()
            assert row.status == "succeeded"
        called = mock_provider.update_cache_batch.await_args
        assert called is not None
        symbols = list(called.args[0])
        assert [s.code for s in symbols] == ["000852.SH"]

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "000852.SH")
            )
            await session.commit()

    async def test_bulk_download_tushare_rejects_etf(
        self, engine: AsyncEngine
    ) -> None:
        """tushare 源对 ETF 保持 scope 拒绝(#341:复权口径对齐前不静默换源)。"""
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        from finboard_persistence import InstrumentModel
        from finboard_persistence.models import BackgroundJobModel

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "510300.SH")
            )
            await session.flush()
            session.add(
                InstrumentModel(
                    code="510300.SH",
                    name="沪深300ETF",
                    market="a_share",
                    instrument_type="etf",
                    exchange="SSE",
                    status="active",
                )
            )
            await session.commit()

        registry = JobExecutorRegistry()
        registry.register(
            "bulk_download",
            BulkDownloadExecutor(
                session_maker=_session_maker(engine),  # type: ignore[arg-type]
                settings_factory=lambda: None,
            ),
        )
        worker = _build_worker(engine, registry)
        await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "tushare",
                "start": "2024-01-01",
                "instrument_type": "etf",
            },
        )
        with patch(
            "finboard_backtest.background_jobs.executors.bulk_download.build_bar_provider",
        ) as build:
            await _drain(worker)
            # scope 校验先于 provider 构建失败。
            build.assert_not_called()

        async with session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(BackgroundJobModel).where(
                        BackgroundJobModel.kind == "bulk_download",
                        BackgroundJobModel.payload["source"].as_string() == "tushare",
                        BackgroundJobModel.payload["instrument_type"].as_string() == "etf",
                    )
                )
            ).scalar_one()
            assert row.status == "failed"
            assert row.error_code == "tushare_scope_mismatch"

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "510300.SH")
            )
            await session.commit()

    async def test_bulk_download_tushare_allows_index(
        self, engine: AsyncEngine
    ) -> None:
        """#341:tushare 源放行指数(index_daily 专属接口),scope 校验通过。"""
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        from finboard_persistence import InstrumentModel
        from finboard_persistence.models import BackgroundJobModel

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "000852.SH")
            )
            await session.flush()
            session.add(
                InstrumentModel(
                    code="000852.SH",
                    name="中证1000",
                    market="a_share",
                    instrument_type="index",
                    exchange="SSE",
                    status="active",
                )
            )
            await session.commit()

        registry = JobExecutorRegistry()
        registry.register(
            "bulk_download",
            BulkDownloadExecutor(
                session_maker=_session_maker(engine),  # type: ignore[arg-type]
                settings_factory=lambda: None,
            ),
        )
        worker = _build_worker(engine, registry)
        await _enqueue(
            engine,
            kind="bulk_download",
            payload={
                "market": "a_share",
                "source": "tushare",
                "start": "2024-01-01",
                "instrument_type": "index",
            },
        )

        mock_provider = AsyncMock()
        mock_provider.update_cache_batch.return_value = {"000852.SH": True}
        with patch(
            "finboard_backtest.background_jobs.executors.bulk_download.build_bar_provider",
            return_value=mock_provider,
        ):
            await _drain(worker)

        async with session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(BackgroundJobModel).where(
                        BackgroundJobModel.kind == "bulk_download",
                        BackgroundJobModel.payload["source"].as_string() == "tushare",
                        BackgroundJobModel.payload["instrument_type"].as_string() == "index",
                    )
                )
            ).scalar_one()
            assert row.status == "succeeded"
        called = mock_provider.update_cache_batch.await_args
        assert called is not None
        symbols = list(called.args[0])
        assert [s.code for s in symbols] == ["000852.SH"]

        async with session_factory(engine)() as session:
            await session.execute(
                sa_delete(InstrumentModel).where(InstrumentModel.code == "000852.SH")
            )
            await session.commit()
