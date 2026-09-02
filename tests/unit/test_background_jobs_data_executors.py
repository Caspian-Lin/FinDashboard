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

    def test_make_sync_progress_coalesces_high_frequency_callbacks(self) -> None:
        """#212:高频回调单飞合并——并发 async 上报任务至多一个。

        全市场联合因子快照逐标的回调时,旧的逐调用 fire-and-forget 会积压
        上百个并发 update_progress 任务,每个各开一个 session 写 job 行,
        打满引擎连接池(5+10)后 worker 维护循环直接被 QueuePool TimeoutError
        掀翻。合并后:在途任务期间的新帧只更新数值槽,不新增任务。
        """

        import asyncio

        from finboard_backtest.background_jobs.executors._progress import (
            make_sync_progress,
        )

        received: list[tuple[int, int | None, str | None]] = []
        in_flight = 0
        max_in_flight = 0

        async def cb(done: int, total: int | None, phase: str | None) -> None:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.001)  # 模拟一次 DB 写的耗时
            received.append((done, total, phase))
            in_flight -= 1

        async def _driver() -> None:
            sync_cb = make_sync_progress(cb, phase_prefix="test")
            for i in range(200):
                sync_cb(f"00000{i}.SZ", i, 200)
                await asyncio.sleep(0)  # 让 drain 与生产交错
            for _ in range(400):
                await asyncio.sleep(0)
            assert max_in_flight == 1  # 任意时刻至多一个在途上报
            assert received  # 有帧送达
            assert received[-1][0] == 199  # 最终帧必达
            # 高频合并:送达帧数远少于触发次数(中间帧被合并丢弃)。
            assert len(received) < 200

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
    async def test_unknown_payload_key_replayed_at_execute(self) -> None:
        """#260:执行器入口重放入队期契约 —— 未知键(data_types)fail-visible,
        不再被静默忽略后按缺省全数据集执行。"""
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        executor = ResearchDataSyncExecutor(session_maker=_fake_session_maker())
        job = _make_job(
            {
                "data_types": ["daily_metrics"],  # 拼写错误,正确为 datasets
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            "research_data_sync",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"
        assert "data_types" in exc_info.value.summary

    @pytest.mark.asyncio
    async def test_per_symbol_without_symbols_or_profiles_rejected_before_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#260:逐标的数据集缺 symbols 且缺 profiles → 执行端重放契约即拒
        (契约层拦截;不触 provider)。"""

        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        def _boom_factory() -> object:
            raise AssertionError("契约失败不应构造 provider")

        executor = ResearchDataSyncExecutor(
            session_maker=_fake_session_maker(),
            provider_factory=_boom_factory,  # type: ignore[arg-type]
        )
        job = _make_job(
            {
                "datasets": ["financial_indicators"],
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
            },
            "research_data_sync",
        )
        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _noop_progress)
        assert exc_info.value.code == "invalid_payload"
        assert "symbol 池" in exc_info.value.summary

    @pytest.mark.asyncio
    async def test_empty_symbol_pool_after_profiles_fail_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#260 静默 no-op 治理:profiles 在 datasets 但上游返回空池 →
        逐标的段落前抛具名 empty_symbol_pool,任务不再「零迭代成功」。"""

        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        class _EmptyProfilesProvider:
            async def fetch_instrument_profiles(self, **kwargs: object) -> list[object]:
                return []

        class _Service:
            async def sync_instrument_profiles(self, **kwargs: object) -> object:
                return object()

        executor = ResearchDataSyncExecutor(
            session_maker=_fake_session_maker(),
            provider_factory=lambda: _EmptyProfilesProvider(),  # type: ignore[arg-type,return-value]
        )
        import finboard_persistence.research_sync as persistence_mod

        monkeypatch.setattr(
            persistence_mod,
            "ResearchDataSyncService",
            lambda *args: _Service(),
        )
        job = _make_job(
            {
                "datasets": ["profiles", "financial_indicators"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            "research_data_sync",
        )
        phases: list[str | None] = []

        async def _progress(
            _done: int, _total: int | None, phase: str | None
        ) -> None:
            phases.append(phase)

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(job, _progress)
        assert exc_info.value.code == "empty_symbol_pool"
        assert exc_info.value.retryable is False
        # profiles 段已完成(工作不浪费),失败发生在逐标的段落之前
        assert "research_data_sync:profiles" in phases
        assert not any(
            p and p.startswith("research_data_sync:financial") for p in phases
        )

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


# ---------------------------------------------------------------------------
# #251:profiles 拉取退市档案 + name_changes 名称历史导入
# ---------------------------------------------------------------------------


def _profile_record(symbol: str, list_status: str) -> object:
    """构造最小 InstrumentProfile 形状(frozen dataclass,构造时校验)。"""
    from datetime import UTC, date, datetime

    from finboard_data.research import InstrumentProfile

    observed = datetime(2026, 9, 1, tzinfo=UTC)
    return InstrumentProfile(
        symbol=symbol,
        name=f"name-{symbol}",
        exchange="SZSE",
        market="主板",
        list_status=list_status,
        list_date=date(1991, 4, 3),
        delist_date=date(2005, 9, 1) if list_status == "D" else None,
        industry="银行",
        source="tushare",
        observed_at=observed,
        available_at=observed,
    )


class TestResearchDataSyncInstrumentGovernance:
    @pytest.mark.asyncio
    async def test_profiles_fetches_delisted_and_merges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#251:profiles 段同时拉取退市档案(D)并合并进同一批次。"""
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )

        fetch_statuses: list[str] = []

        class _Provider:
            async def fetch_instrument_profiles(
                self, *, list_status: str = "L"
            ) -> list[object]:
                fetch_statuses.append(list_status)
                if list_status == "L":
                    return [_profile_record("000001.SZ", "L")]
                return [_profile_record("000003.SZ", "D")]

        sync_calls: list[dict[str, object]] = []

        class _Service:
            async def sync_instrument_profiles(self, **kwargs: object) -> object:
                sync_calls.append(kwargs)
                return object()

        import finboard_persistence.research_sync as persistence_mod

        monkeypatch.setattr(
            persistence_mod,
            "ResearchDataSyncService",
            lambda *a, **kw: _Service(),
        )
        executor = ResearchDataSyncExecutor(
            session_maker=_fake_session_maker(),
            provider_factory=lambda: _Provider(),  # type: ignore[arg-type,return-value]
        )
        job = _make_job(
            {
                "datasets": ["profiles"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
            },
            "research_data_sync",
        )
        result = await executor.execute(job, _noop_progress)

        assert result.status == "succeeded"
        assert sorted(fetch_statuses) == ["D", "L"]
        assert len(sync_calls) == 1
        records = sync_calls[0]["records"]
        assert isinstance(records, list)
        assert len(records) == 2  # L + D 合并进同一批次
        assert sync_calls[0]["parameters"] == {"list_status": "L+D"}

    @pytest.mark.asyncio
    async def test_name_changes_dataset_imports_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#251:name_changes dataset 拉取历史名称并写入主数据表。"""
        from datetime import UTC, date, datetime

        from finboard_backtest.background_jobs.executors.research_data_sync import (
            ResearchDataSyncExecutor,
        )
        from finboard_data.research import InstrumentNameChange

        observed = datetime(2026, 9, 1, tzinfo=UTC)

        class _Provider:
            async def fetch_name_changes(self) -> list[InstrumentNameChange]:
                return [
                    InstrumentNameChange(
                        symbol="000001.SZ",
                        name="深发展A",
                        start_date=date(1991, 4, 3),
                        end_date=date(1992, 3, 9),
                        change_reason="更名",
                        source="tushare",
                        observed_at=observed,
                        available_at=observed,
                    ),
                    InstrumentNameChange(
                        symbol="000001.SZ",
                        name="平安银行",
                        start_date=date(1992, 3, 9),
                        end_date=None,
                        change_reason=None,
                        source="tushare",
                        observed_at=observed,
                        available_at=observed,
                    ),
                ]

        imported: list[list[tuple[str, str, object, object]]] = []

        class _FakeInstrumentRepository:
            def __init__(self, session: object) -> None:
                self.session = session

            async def import_name_history(
                self, records: list[tuple[str, str, object, object]]
            ) -> object:
                imported.append(records)
                return object()

        import finboard_persistence as persistence_pkg

        monkeypatch.setattr(
            persistence_pkg, "InstrumentRepository", _FakeInstrumentRepository
        )

        class _CommitSession:
            async def __aenter__(self) -> _CommitSession:
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def commit(self) -> None:
                pass

        class _CommitSessionMaker:
            def __call__(self) -> _CommitSession:
                return _CommitSession()

        executor = ResearchDataSyncExecutor(
            session_maker=_CommitSessionMaker(),  # type: ignore[arg-type]
            provider_factory=lambda: _Provider(),  # type: ignore[arg-type,return-value]
        )
        job = _make_job(
            {
                "datasets": ["name_changes"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
            },
            "research_data_sync",
        )
        result = await executor.execute(job, _noop_progress)

        assert result.status == "succeeded"
        assert len(imported) == 1
        records = imported[0]
        assert [item[0] for item in records] == ["000001.SZ", "000001.SZ"]
        assert [item[1] for item in records] == ["深发展A", "平安银行"]
        assert records[1][3] is None  # 当前名称保持开区间
