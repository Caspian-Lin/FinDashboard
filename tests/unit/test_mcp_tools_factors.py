"""``finboard.factor.*`` / ``finboard.feature_snapshot.*`` 工具 ——
因子实验室工具测试(mock session / monkeypatch,无需 DB)。

覆盖:

* 只读查询(catalog / snapshot list/get / signal list/get / experiment list/get /
  job status)—— monkeypatch repository 返回 duck-typed 领域对象;
* 写操作(snapshot create / job start / experiment create / sync validation)——
  monkeypatch ``build_price_feature_snapshot`` / domain 函数 / repository,
  验证 ``write_tools_enabled=False`` 时拒绝;
* catalog 工具无 DB 依赖,直接验证内存目录返回。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import factors as factor_tools

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    return value


def _snapshot_domain(snapshot_id: str = "FSS-1") -> SimpleNamespace:
    """构造一个 duck-typed FeatureSnapshot 领域对象。"""
    return SimpleNamespace(
        snapshot_id=snapshot_id,
        dataset_release_id="REL-1",
        dataset_release_checksum="abc123",
        decision_at=datetime(2026, 1, 15, tzinfo=UTC),
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        framework_version="v2",
        calculation_windows={"momentum": 20},
        transformations={},
        neutralization={},
        code_version="abc123",
        observations=[],
        checksum="chk1",
        issues=[],
        as_dict=lambda sid=snapshot_id: {
            "snapshot_id": sid,
            "dataset_release_id": "REL-1",
            "checksum": "chk1",
        },
    )


def _signal_domain(signal_id: str = "SIG-1") -> SimpleNamespace:
    return SimpleNamespace(
        signal_id=signal_id,
        factor_name="momentum",
        factor_version="v1",
        feature_snapshot_id="FSS-1",
        feature_snapshot_checksum="chk1",
        candidate_universe_version="1",
        research_status="hypothesis",
        validation_experiment_id=None,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        items=[],
        checksum="sigchk1",
        as_dict=lambda sid=signal_id: {
            "signal_id": sid,
            "factor_name": "momentum",
            "checksum": "sigchk1",
        },
    )


def _experiment_domain(experiment_id: str = "EXP-1") -> SimpleNamespace:
    return SimpleNamespace(
        experiment_id=experiment_id,
        hypothesis="momentum predicts returns",
        factor_names=["momentum"],
        dataset_release_id="REL-1",
        dataset_release_checksum="abc123",
        feature_snapshot_id="FSS-1",
        plan={"quantiles": 5},
        comparison_group="baseline",
        status="hypothesis",
        validation_experiment_id=None,
        result=None,
        failure_reason=None,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        updated_at=datetime(2026, 1, 15, tzinfo=UTC),
        as_dict=lambda eid=experiment_id: {
            "experiment_id": eid,
            "hypothesis": "momentum predicts returns",
            "status": "hypothesis",
        },
    )


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    provider = FakeLLMProvider()
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or cast("async_sessionmaker[AsyncSession]", cm),
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _get_session(app: McpAppContext) -> AsyncMock:
    """从 mock session_maker 提取内部 session 对象。"""
    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


# ---------------------------------------------------------------------------
# finboard.factor.catalog(无 DB)
# ---------------------------------------------------------------------------


class TestFactorCatalog:
    async def test_returns_catalog(self) -> None:
        app = _make_app()
        env = await factor_tools.factor_catalog(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert len(env.data) > 0
        first = env.data[0]
        assert "name" in first
        assert "version" in first
        assert "checksum" in first

    async def test_records_audit(self) -> None:
        app = _make_app()
        await factor_tools.factor_catalog(app)
        assert len(app.audit.records) == 1
        assert app.audit.records[0].tool_name == "finboard.factor.catalog"
        assert app.audit.records[0].status == "ok"

    async def test_role_filter_invalid(self) -> None:
        app = _make_app()
        env = await factor_tools.factor_catalog(app, role="invalid_role")
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# finboard.feature_snapshot.list / get
# ---------------------------------------------------------------------------


class TestFeatureSnapshotList:
    async def test_returns_snapshots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import FeatureSnapshotRepository

        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "list",
            lambda self, **kw: _async_return([_snapshot_domain()]),
        )
        env = await factor_tools.feature_snapshot_list(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert env.data[0]["snapshot_id"] == "FSS-1"

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FeatureSnapshotRepository

        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "list",
            lambda self, **kw: _async_return([]),
        )
        await factor_tools.feature_snapshot_list(app)
        assert app.audit.records[0].tool_name == "finboard.feature_snapshot.list"


class TestFeatureSnapshotGet:
    async def test_returns_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FeatureSnapshotRepository

        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "get",
            lambda self, sid: _async_return(_snapshot_domain(sid)),
        )
        env = await factor_tools.feature_snapshot_get(app, "FSS-1")
        assert env.status == "ok"
        assert env.data["snapshot_id"] == "FSS-1"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FeatureSnapshotRepository

        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "get",
            lambda self, sid: _async_return(None),
        )
        env = await factor_tools.feature_snapshot_get(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "not_found"


# ---------------------------------------------------------------------------
# finboard.feature_snapshot.job_status / job_start(持久化队列,#136 适配)
# ---------------------------------------------------------------------------


class TestFeatureSnapshotJobStatus:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """job_status 读 background_jobs 表,未找到返回 not_found。"""

        app = _make_app()
        from finboard_persistence.background_job_repo import BackgroundJobRepository

        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(None),
        )
        env = await factor_tools.feature_snapshot_job_status(app, "BJ-NOPE")
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_returns_jobout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """job_status 返回 JobOut 结构(result_ref=snapshot_id)。"""

        app = _make_app()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        _row = SimpleNamespace(
            job_id="BJ-1",
            kind="feature_snapshot",
            queue="research",
            status="succeeded",
            priority=0,
            payload={},
            payload_checksum="abc",
            idempotency_key="idem-key-1",
            progress_total=100,
            progress_done=100,
            phase="feature_snapshot:done",
            result_ref="FSS-1",
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="mcp",
            created_at=now,
            started_at=now,
            finished_at=now,
            updated_at=now,
        )
        from finboard_persistence.background_job_repo import BackgroundJobRepository

        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(_row),
        )
        env = await factor_tools.feature_snapshot_job_status(app, "BJ-1")
        assert env.status == "ok"
        assert env.data["job_id"] == "BJ-1"
        assert env.data["status"] == "succeeded"
        assert env.data["result_ref"] == "FSS-1"


class TestFeatureSnapshotJobStart:
    """job_start 登记任务到统一队列(复用 enqueue_job),不再进程内执行(#136)。"""

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await factor_tools.feature_snapshot_job_start(
            app,
            dataset_release_id="REL-1",
            decision_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_enqueues_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """job_start 校验发布后 enqueue,返回 JobOut(status=queued)。"""

        app = _make_app()
        _release = SimpleNamespace(
            release_id="REL-1",
            instruments=["000001"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        from finboard_persistence import ResearchDatasetReleaseRepository

        monkeypatch.setattr(
            ResearchDatasetReleaseRepository,
            "require_usable",
            lambda self, rid: _async_return(_release),
        )

        # patch enqueue_job 返回一个 duck-typed JobOut
        async def _fake_enqueue(session: Any, response: Any, **kw: Any) -> Any:
            from finboard_api.job_schemas import JobOut

            return JobOut.model_validate(
                SimpleNamespace(
                    job_id="BJ-NEW",
                    kind="feature_snapshot",
                    queue="research",
                    status="queued",
                    priority=0,
                    payload=kw.get("payload", {}),
                    payload_checksum="abc",
                    idempotency_key=kw["idempotency_key"],
                    progress_total=0,
                    progress_done=0,
                    phase=None,
                    result_ref=None,
                    error_code=None,
                    error_summary=None,
                    attempt=0,
                    max_attempts=3,
                    worker_id=None,
                    heartbeat_at=None,
                    lease_until=None,
                    requested_by=kw["requested_by"],
                    created_at=datetime(2026, 1, 15, tzinfo=UTC),
                    started_at=None,
                    finished_at=None,
                    updated_at=datetime(2026, 1, 15, tzinfo=UTC),
                )
            )

        monkeypatch.setattr(
            "finboard_api.job_helpers.enqueue_job", _fake_enqueue
        )
        env = await factor_tools.feature_snapshot_job_start(
            app,
            dataset_release_id="REL-1",
            decision_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        assert env.status == "ok"
        assert env.data["job_id"] == "BJ-NEW"
        assert env.data["status"] == "queued"
        assert env.data["kind"] == "feature_snapshot"


# ---------------------------------------------------------------------------
# finboard.feature_snapshot.create(写)
# ---------------------------------------------------------------------------


class TestFeatureSnapshotCreate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await factor_tools.feature_snapshot_create(
            app,
            dataset_release_id="REL-1",
            decision_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import ResearchDatasetReleaseRepository

        async def _fail(self: Any, rid: str) -> Any:
            raise ValueError(f"发布不存在: {rid}")

        monkeypatch.setattr(
            ResearchDatasetReleaseRepository, "require_usable", _fail
        )
        env = await factor_tools.feature_snapshot_create(
            app,
            dataset_release_id="REL-NOPE",
            decision_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# finboard.factor.signal.list / get
# ---------------------------------------------------------------------------


class TestFactorSignalList:
    async def test_returns_signals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import FactorSignalRepository

        monkeypatch.setattr(
            FactorSignalRepository,
            "list",
            lambda self, **kw: _async_return([_signal_domain()]),
        )
        env = await factor_tools.factor_signal_list(app)
        assert env.status == "ok"
        assert env.data[0]["signal_id"] == "SIG-1"

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FactorSignalRepository

        monkeypatch.setattr(
            FactorSignalRepository,
            "list",
            lambda self, **kw: _async_return([]),
        )
        await factor_tools.factor_signal_list(app)
        assert app.audit.records[0].tool_name == "finboard.factor.signal.list"


class TestFactorSignalGet:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FactorSignalRepository

        monkeypatch.setattr(
            FactorSignalRepository,
            "get",
            lambda self, sid: _async_return(None),
        )
        env = await factor_tools.factor_signal_get(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "not_found"


# ---------------------------------------------------------------------------
# finboard.factor.experiment.list / get / create / sync_validation
# ---------------------------------------------------------------------------


class TestFactorExperimentList:
    async def test_returns_experiments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import FactorExperimentRepository

        monkeypatch.setattr(
            FactorExperimentRepository,
            "list",
            lambda self, **kw: _async_return([_experiment_domain()]),
        )
        env = await factor_tools.factor_experiment_list(app)
        assert env.status == "ok"
        assert env.data[0]["experiment_id"] == "EXP-1"


class TestFactorExperimentGet:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FactorExperimentRepository

        monkeypatch.setattr(
            FactorExperimentRepository,
            "get",
            lambda self, eid: _async_return(None),
        )
        env = await factor_tools.factor_experiment_get(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestFactorExperimentCreate:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await factor_tools.factor_experiment_create(
            app,
            hypothesis="momentum predicts returns",
            factor_names=["momentum"],
            dataset_release_id="REL-1",
            feature_snapshot_id="FSS-1",
            plan={
                "in_sample_start": "2024-01-01",
                "in_sample_end": "2024-06-30",
                "oos_start": "2024-07-01",
                "oos_end": "2024-12-31",
                "trial_budget": 10,
                "benchmark_symbol": "000300",
                "transaction_cost_bps": 3.0,
            },
            comparison_group="baseline",
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_snapshot_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        from finboard_persistence import (
            FeatureSnapshotRepository,
            ResearchDatasetReleaseRepository,
        )

        _release = SimpleNamespace(
            release_id="REL-1",
            release_checksum="abc123",
        )
        monkeypatch.setattr(
            ResearchDatasetReleaseRepository,
            "require_usable",
            lambda self, rid: _async_return(_release),
        )
        monkeypatch.setattr(
            FeatureSnapshotRepository,
            "get",
            lambda self, sid: _async_return(None),
        )
        env = await factor_tools.factor_experiment_create(
            app,
            hypothesis="momentum predicts returns",
            factor_names=["momentum"],
            dataset_release_id="REL-1",
            feature_snapshot_id="FSS-MISSING",
            plan={
                "in_sample_start": "2024-01-01",
                "in_sample_end": "2024-06-30",
                "oos_start": "2024-07-01",
                "oos_end": "2024-12-31",
                "trial_budget": 10,
                "benchmark_symbol": "000300",
                "transaction_cost_bps": 3.0,
            },
            comparison_group="baseline",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestFactorExperimentSyncValidation:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await factor_tools.factor_experiment_sync_validation(
            app, "EXP-1"
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        from finboard_persistence import FactorExperimentValidationService

        monkeypatch.setattr(
            FactorExperimentValidationService,
            "sync",
            lambda self, eid: _async_return(_experiment_domain(eid)),
        )
        env = await factor_tools.factor_experiment_sync_validation(
            app, "EXP-1"
        )
        assert env.status == "ok"
        assert env.data["experiment_id"] == "EXP-1"
