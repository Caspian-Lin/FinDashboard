"""数据域 executor 的 payload 校验单元测试(issue #144)。

不依赖 DB —— 只验证各 executor 对非法 payload 的 ``ExecutorError`` 校验,以及
``_over_limit_kinds`` 的纯逻辑。真正的端到端执行(调 service / 写库)由集成测试覆盖。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
)


def _make_job(payload: dict[str, object], kind: str = "test") -> JobRecord:
    return JobRecord(
        job_id=f"BJ-TEST{kind}",
        kind=kind,
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass


class TestBulkDownloadPayload:
    @pytest.mark.asyncio
    async def test_missing_market_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.bulk_download import (
            BulkDownloadExecutor,
        )

        executor = BulkDownloadExecutor(
            session_maker=_fake_session_maker(),
            settings_factory=lambda: None,
        )
        job = _make_job({"source": "akshare", "start": "2024-01-01"}, "bulk_download")
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_missing_start_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.bulk_download import (
            BulkDownloadExecutor,
        )

        executor = BulkDownloadExecutor(
            session_maker=_fake_session_maker(),
            settings_factory=lambda: None,
        )
        job = _make_job(
            {"market": "a_share", "source": "akshare"}, "bulk_download"
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_bad_date_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.bulk_download import (
            BulkDownloadExecutor,
        )

        executor = BulkDownloadExecutor(
            session_maker=_fake_session_maker(),
            settings_factory=lambda: None,
        )
        job = _make_job(
            {"market": "a_share", "source": "akshare", "start": "not-a-date"},
            "bulk_download",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"


class TestFeatureSnapshotPayload:
    @pytest.mark.asyncio
    async def test_missing_release_id_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.feature_snapshot import (
            FeatureSnapshotExecutor,
        )

        executor = FeatureSnapshotExecutor(session_maker=_fake_session_maker())
        job = _make_job({"decision_at": "2026-07-31T00:00:00+00:00"}, "feature_snapshot")
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_bad_decision_at_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.feature_snapshot import (
            FeatureSnapshotExecutor,
        )

        executor = FeatureSnapshotExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {"dataset_release_id": "rel-1", "decision_at": "yesterday"},
            "feature_snapshot",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"


class TestDatasetPublishPayload:
    @pytest.mark.asyncio
    async def test_missing_release_id_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.dataset_publish import (
            DatasetPublishExecutor,
        )

        executor = DatasetPublishExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "dataset_name": "x",
                "release_kind": "a_share_tushare",
                "version": "v1",
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "adjustment": "qfq",
                "symbols": ["000001.SZ"],
            },
            "dataset_publish",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_bad_release_kind_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.dataset_publish import (
            DatasetPublishExecutor,
        )

        executor = DatasetPublishExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "release_id": "r1",
                "dataset_name": "x",
                "release_kind": "unknown_kind",
                "version": "v1",
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "adjustment": "qfq",
                "symbols": ["000001.SZ"],
            },
            "dataset_publish",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_empty_symbols_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.dataset_publish import (
            DatasetPublishExecutor,
        )

        executor = DatasetPublishExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "release_id": "r1",
                "dataset_name": "x",
                "release_kind": "a_share_tushare",
                "version": "v1",
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "adjustment": "qfq",
                "symbols": [],
            },
            "dataset_publish",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"


class TestBacktestRunPayload:
    @pytest.mark.asyncio
    async def test_missing_request_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.backtest_run import (
            BacktestRunExecutor,
        )

        executor = BacktestRunExecutor(
            session_maker=_fake_session_maker(),
            runner=AsyncMock(),
        )
        job = _make_job({"provider_name": "akshare"}, "backtest_run")
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"


class TestDataSyncAndRepairPayload:
    @pytest.mark.asyncio
    async def test_quality_repair_missing_symbols_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.quality_repair import (
            QualityRepairExecutor,
        )

        executor = QualityRepairExecutor(
            session_maker=_fake_session_maker(),
            settings_factory=lambda: None,
        )
        job = _make_job({"source": "akshare"}, "quality_repair")
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_quality_repair_bad_source_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.quality_repair import (
            QualityRepairExecutor,
        )

        executor = QualityRepairExecutor(
            session_maker=_fake_session_maker(),
            settings_factory=lambda: None,
        )
        job = _make_job(
            {"symbols": ["000001.SZ"], "source": 123},
            "quality_repair",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"


class TestProgressBridge:
    def test_make_sync_progress_schedules_async_callback(self) -> None:
        """同步 on_progress 回调能桥接到 async ProgressCallback。"""
        import asyncio

        from finboard_backtest.background_jobs.executors._progress import (
            make_sync_progress,
        )

        received: list[tuple[int, int | None, str | None]] = []

        async def cb(done: int, total: int | None, phase: str | None) -> None:
            received.append((done, total, phase))

        async def _driver() -> None:
            holder: dict[str, int | None] = {"total": None}
            sync_cb = make_sync_progress(cb, phase_prefix="test", total_holder=holder)
            sync_cb("000001.SZ", 3, 10)
            sync_cb("000002.SZ", 4, 10)
            # 让 create_task 的协程有机会执行
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert holder["total"] == 10
            assert len(received) == 2
            assert received[0][0] == 3
            assert "test" in (received[0][2] or "")

        asyncio.run(_driver())




class TestResearchDataSyncPayload:
    @pytest.mark.asyncio
    async def test_missing_dates_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        executor = ResearchDataSyncExecutor(session_maker=_fake_session_maker())
        job = _make_job({"datasets": ["profiles"]}, "research_data_sync")
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_bad_date_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        executor = ResearchDataSyncExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "datasets": ["daily_metrics"],
                "start_date": "not-a-date",
                "end_date": "2026-01-31",
            },
            "research_data_sync",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_unknown_dataset_raises(self) -> None:
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        executor = ResearchDataSyncExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "datasets": ["orders"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            "research_data_sync",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"

    @pytest.mark.asyncio
    async def test_budget_exhausted_maps_to_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预算耗尽 → record_upstream_failure + retryable ExecutorError。"""

        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        class _ExhaustedProvider:
            async def fetch_instrument_profiles(self, **kwargs: object) -> list[object]:
                from finboard_data.tushare_budget import TushareRequestLimitError

                raise TushareRequestLimitError("预算耗尽")

        record_calls: list[tuple[object, ...]] = []

        class _Service:
            async def record_upstream_failure(self, **kwargs: object) -> object:
                record_calls.append(tuple(kwargs.items()))
                return object()

        executor = ResearchDataSyncExecutor(
            session_maker=_fake_session_maker(),
            provider_factory=lambda: _ExhaustedProvider(),  # type: ignore[arg-type,return-value]
        )
        job = _make_job(
            {
                "datasets": ["profiles"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
            },
            "research_data_sync",
        )
        # 替换 ResearchDataSyncService 构造,避免触碰 DB。
        import finboard_persistence.research_sync as persistence_mod

        def _service_factory(*args: object, **kwargs: object) -> object:
            return _Service()

        monkeypatch.setattr(
            persistence_mod, "ResearchDataSyncService", _service_factory
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "tushare_budget_exhausted"
        assert exc_info.value.retryable is True
        assert record_calls  # 上游失败已登记


def _fake_session_maker() -> Any:
    """返回一个假的 async_sessionmaker(测试中 executor 不会真正打开 session)"""
    from typing import cast

    from sqlalchemy.ext.asyncio import async_sessionmaker

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    def _maker() -> _FakeSession:
        return _FakeSession()

    return cast(async_sessionmaker[Any], _maker)
