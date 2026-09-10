"""issue #348:``background_jobs.phase`` 加宽至 256 + update_progress 截断兜底。

DB 实证(2026-09-05 data_sync 任务):收尾写完成语 phase(恒超旧列宽 64)触发
``StringDataRightTruncation``,实际已完成的任务被误报 failed。锁定:

* model 列宽与 repo 截断上限一致(256,迁移 d4e5f6a7b8c9);
* ``update_progress`` 对超长 phase 硬截到 256,写入不炸;短 phase 原样透传;
* ``data_sync`` / ``dataset_publish`` 执行器收尾 phase 为 ``*:done`` 短摘要
  (回填 / mismatch 明细由结构化日志承载,不丢信息)。

用 ``AsyncMock`` / monkeypatch 模拟 session 与上游发现 / 发布服务,无需 DB。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.executors.data_sync import DataSyncExecutor
from finboard_backtest.background_jobs.executors.dataset_publish import (
    DatasetPublishExecutor,
)
from finboard_persistence.background_job_repo import (
    _PHASE_MAX_LENGTH,
    BackgroundJobRepository,
)
from finboard_persistence.models import BackgroundJobModel


async def _async_return(value: object) -> object:
    return value


def _make_job(payload: dict[str, object], kind: str) -> JobRecord:
    return JobRecord(
        job_id="BJ-348",
        kind=kind,
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )


def _collecting_progress() -> tuple[list[str | None], Any]:
    """返回 (phases, progress 回调):按序记录每次上报的 phase。"""

    phases: list[str | None] = []

    async def progress(_done: int, _total: int | None, phase: str | None) -> None:
        phases.append(phase)

    return phases, progress


def _phase_column_length() -> int:
    """读 model ``phase`` 列宽(isinstance 收窄后取 ``String.length``)。"""
    from sqlalchemy import String

    type_ = BackgroundJobModel.__table__.columns["phase"].type
    assert isinstance(type_, String)
    return type_.length or 0


# ------------------------------------------------------------- model / repo


class TestPhaseColumnWidth:
    def test_model_column_widened_to_256(self) -> None:
        """model 与迁移 d4e5f6a7b8c9 同步:String(64) → String(256)。"""
        assert _phase_column_length() == 256

    def test_truncation_limit_matches_column_width(self) -> None:
        """repo 截断上限与列宽一致 —— 加宽列后兜底仍按同一上限硬截。"""
        assert _PHASE_MAX_LENGTH == 256


class TestUpdateProgressTruncation:
    @staticmethod
    def _repo_with_row(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[BackgroundJobRepository, SimpleNamespace]:
        """monkeypatch repo.get 返回可变行;session.flush 为 AsyncMock。"""
        row = SimpleNamespace(progress_total=0, progress_done=0, phase=None)
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id, for_update=False: _async_return(row),
        )
        session = MagicMock()
        session.flush = AsyncMock()
        repo = BackgroundJobRepository(cast(AsyncSession, session))
        return repo, row

    async def test_over_length_phase_truncated_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超 256 字符的 phase 截断写入,不触发 StringDataRightTruncation。"""
        repo, row = self._repo_with_row(monkeypatch)
        long_phase = "data_sync:done " + "x" * 400
        await repo.update_progress("BJ-348", done=1, total=1, phase=long_phase)
        assert row.phase == long_phase[:_PHASE_MAX_LENGTH]
        assert len(row.phase) == _PHASE_MAX_LENGTH

    async def test_short_phase_passthrough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, row = self._repo_with_row(monkeypatch)
        await repo.update_progress("BJ-348", done=1, total=1, phase="data_sync:done")
        assert row.phase == "data_sync:done"

    async def test_truncated_phase_fits_widened_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """截断结果长度 ≤ 加宽后的列宽,写库不再可能超限。"""
        repo, row = self._repo_with_row(monkeypatch)
        await repo.update_progress("BJ-348", done=1, total=1, phase="p" * 10_000)
        assert row.phase is not None
        assert len(row.phase) <= _phase_column_length()


# ---------------------------------------------------------------- executors


def _fake_instrument() -> SimpleNamespace:
    """最小 InstrumentInfo 形状(data_sync 只读这几个字段拼 dict)。"""
    return SimpleNamespace(
        code="000001.SZ",
        name="平安银行",
        market=SimpleNamespace(value="a_share"),
        instrument_type=SimpleNamespace(value="stock"),
        exchange="SZSE",
        listing_board=SimpleNamespace(value="szse_main"),
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


class TestDataSyncDonePhaseShort:
    async def test_completion_phase_is_short_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """data_sync 收尾 phase 为 ``data_sync:done`` 短摘要(≤ 旧列宽 64)。

        回填明细不再拼进 phase —— 由 data_sync.done 结构化日志承载
        (executor 原样保留该日志行)。
        """
        import finboard_data.discovery as discovery_mod
        import finboard_persistence as persistence_pkg
        from finboard_persistence import InstrumentMetadataBackfillResult

        class _FakeDiscovery:
            async def discover_all(self) -> list[SimpleNamespace]:
                return [_fake_instrument()]

        class _FakeInstrumentRepository:
            def __init__(self, session: object) -> None:
                self.session = session

            async def sync_with_diff(self, dicts: object, as_of: object) -> object:
                return object()

            async def backfill_metadata_from_profiles(
                self, symbols: list[str]
            ) -> InstrumentMetadataBackfillResult:
                return InstrumentMetadataBackfillResult(
                    profile_batch_available=True,
                    scoped=len(symbols),
                    backfilled_list_date=len(symbols),
                    missing_industry=len(symbols),
                )

            async def backfill_listing_dates(
                self, records: object
            ) -> dict[str, int]:
                # #394 起执行器在发现后恒调用(空映射 = 无可回填域)。
                return {
                    "scoped": 0,
                    "backfilled_list_date": 0,
                    "backfilled_delist_date": 0,
                    "missing_list_date": 0,
                    "missing_delist_date": 0,
                }

        monkeypatch.setattr(discovery_mod, "UniverseDiscovery", _FakeDiscovery)
        monkeypatch.setattr(
            persistence_pkg, "InstrumentRepository", _FakeInstrumentRepository
        )

        executor = DataSyncExecutor(
            session_maker=cast(async_sessionmaker[AsyncSession], _CommitSessionMaker())
        )
        phases, progress = _collecting_progress()
        result = await executor.execute(_make_job({}, "data_sync"), progress)

        assert result.status == "succeeded"
        assert phases[-1] == "data_sync:done"
        assert all(p is not None and len(p) <= 64 for p in phases)


class TestDatasetPublishDonePhaseShort:
    async def test_completion_phase_short_even_with_symbol_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#252 mismatch 路径收尾 phase 保留具名标记且不超旧列宽。

        旧完成语拼上 mismatch 摘要(含基线 release_id)约 75-85 字符;
        收尾语只留 ``dataset_publish:done symbol_set_mismatch`` 具名标记
        (任务时间线可见不一致发生),差集明细由
        ``dataset_publish.symbol_set_mismatch`` warning 承载。
        """
        import finboard_persistence as persistence_pkg

        class _FakeBaselineRepo:
            def __init__(self, session: object) -> None:
                self.session = session

            async def get(self, release_id: str) -> object:
                # 基线有 000001.SZ,本次只发 000002.SZ → 双向差集非空。
                return SimpleNamespace(
                    instruments=[SimpleNamespace(code="000001.SZ")]
                )

        class _FakeService:
            def __init__(self, session: object, **kwargs: object) -> None:
                self.session = session

            async def publish(self, spec: object, symbols: object) -> object:
                return SimpleNamespace(release_id="rel-348")

        class _FakeResult:
            def scalars(self) -> _FakeResult:
                return self

            def all(self) -> list[SimpleNamespace]:
                return [
                    SimpleNamespace(
                        code="000002.SZ",
                        market="a_share",
                        instrument_type="stock",
                    )
                ]

        class _PublishSession(_CommitSession):
            async def execute(self, stmt: object) -> _FakeResult:
                return _FakeResult()

        class _PublishSessionMaker:
            def __call__(self) -> _PublishSession:
                return _PublishSession()

        monkeypatch.setattr(
            persistence_pkg, "ResearchDatasetReleaseRepository", _FakeBaselineRepo
        )
        monkeypatch.setattr(
            persistence_pkg, "ResearchDatasetReleaseService", _FakeService
        )
        import finboard_backtest.background_jobs.executors.dataset_publish as mod

        monkeypatch.setattr(mod, "code_version", lambda: "test")

        warnings: list[dict[str, object]] = []

        def _fake_warning(event: str, **ctx: object) -> None:
            warnings.append({"event": event, **ctx})

        monkeypatch.setattr(mod.logger, "warning", _fake_warning)

        executor = DatasetPublishExecutor(
            session_maker=cast(
                async_sessionmaker[AsyncSession], _PublishSessionMaker()
            )
        )
        payload: dict[str, object] = {
            "release_id": "rel-348",
            "dataset_name": "bars",
            "release_kind": "a_share_tushare",
            "version": "v1",
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "adjustment": "qfq",
            "symbols": ["000002.SZ"],
            "consistency_baseline_release_id": "rel-base",
        }
        phases, progress = _collecting_progress()
        result = await executor.execute(_make_job(payload, "dataset_publish"), progress)

        assert result.status == "succeeded"
        assert result.result_ref == "rel-348"
        assert phases[-1] == "dataset_publish:done symbol_set_mismatch"
        assert all(p is not None and len(p) <= 64 for p in phases)
        # 明细不丢:mismatch warning 携带基线 id 与双向差集计数。
        mismatch = [
            w for w in warnings if w["event"] == "dataset_publish.symbol_set_mismatch"
        ]
        assert len(mismatch) == 1
        assert mismatch[0]["baseline_release_id"] == "rel-base"
        assert mismatch[0]["missing_in_release_count"] == 1
        assert mismatch[0]["extra_in_release_count"] == 1
