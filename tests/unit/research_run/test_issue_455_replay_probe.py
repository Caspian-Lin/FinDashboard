"""issue #455:job-flamegraph 诊断重放的 #306 加载期探针目标必须是新建 run。

事故(BJ-1A99444AC4B743AE-20260912-165404):对 COMPLETED 源做诊断重放,
``execute_replay`` 此前按**源 manifest** 构造适配器,而 #306 探针在适配器
工厂闭包内按构造期 ``manifest.run_id`` 轮询 run status(``signal_engine.
build_run_interrupt_probe``)—— 探针看到源 run 终态(completed ≠ running),
重放在加载期首块边界被自己的探针杀死(0.515s 自打断,火焰图只采到启动栈,
result.json 的 error_summary 里写的是源 run id)。

本文件锁定三段修复链:

* 执行器接线:``execute_replay`` 以「新 run 身份」的 manifest 预构造适配器
  (身份字段与 ``coordinator.replay`` 的 replace 同构;非可重放源保持源
  manifest,交由既有守卫具名拒绝,错误口径不变);
* 工厂接线:``build_signal_engine_adapter_factory`` 按 manifest.run_id 构造
  探针(monkeypatch 探针工厂捕获收到的 run_id);
* 探针本体:对无 job 行的 run(诊断重放 run 不写 background_jobs,#383)
  优雅降级 —— 只轮询 run status、不误判、不上报进度;终态源 run 的阳性
  对照复现事故形态。

常规 worker 路径(run 行有 job_id)的探针语义由 #306/#308 既有测试覆盖,
此处仅以进度上报用例佐证共享探针行为未变。探针本体的 DB 轮询以
``finboard_persistence`` 仓储替身驱动(真实 DB 会话由集成测试覆盖)。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from finboard_backtest.background_jobs.contracts import ExecutorError
from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
)
from finboard_backtest.research_run import (
    REPLAYABLE_SOURCE_STATUSES,
    ResearchRunConflictError,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    LoadChunkProbe,
    SignalEnginePipelineAdapter,
    build_run_interrupt_probe,
    build_signal_engine_adapter_factory,
)
from finboard_backtest.strategy_spec import build_strategy_template

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_backtest.background_jobs.executors.research_run import (
        AdapterFactory,
        StoreFactory,
    )

_SRC = "RR-455source000000000000000000000000"
_NEW = "RR-455newid000000000000000000000000"
_RELEASE_ID = "replay_probe_455_release"


# ---- 探针本体的 DB 替身(finboard_persistence 仓储 monkeypatch)--------------


class _FakeProbeSessionCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeProbeSessionMaker:
    def __call__(self) -> _FakeProbeSessionCM:
        return _FakeProbeSessionCM()


class _FakeRunRepo:
    """``ResearchRunRepository`` 替身:按 run_id 返回预设 run 行。"""

    rows: ClassVar[dict[str, SimpleNamespace]] = {}

    def __init__(self, session: object) -> None:
        del session

    async def get(self, run_id: str) -> SimpleNamespace | None:
        return type(self).rows.get(run_id)


class _FakeJobRepo:
    """``BackgroundJobRepository`` 替身:记录进度上报调用。"""

    rows: ClassVar[dict[str, SimpleNamespace]] = {}
    progress_calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, session: object) -> None:
        del session

    async def get(self, job_id: str) -> SimpleNamespace | None:
        return type(self).rows.get(job_id)

    async def update_progress(self, job_id: str, **kwargs: Any) -> None:
        type(self).progress_calls.append({"job_id": job_id, **kwargs})

    async def checkpoint(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fake_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRunRepo.rows = {}
    _FakeJobRepo.rows = {}
    _FakeJobRepo.progress_calls = []
    monkeypatch.setattr("finboard_persistence.ResearchRunRepository", _FakeRunRepo)
    monkeypatch.setattr("finboard_persistence.BackgroundJobRepository", _FakeJobRepo)


def _probe_session_maker() -> async_sessionmaker[AsyncSession] | Any:
    from typing import cast

    return cast("async_sessionmaker[AsyncSession]", _FakeProbeSessionMaker())


def _run_row(status: str, job_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(status=status, job_id=job_id)


class TestProbeJobRowSemantics:
    """探针本体对 job 行有无的两种形态(#455 修复的语义边界)。"""

    async def test_running_run_without_job_row_passes(self) -> None:
        """无 job 行的重放 run(status=running)经探针不抛错、不误判。

        诊断重放 run 不写 background_jobs(#383 设计);探针只轮询 run
        status,执行期间 run 保持 RUNNING 即存活,不上报进度。
        """

        _FakeRunRepo.rows = {_NEW: _run_row("running", None)}
        probe = build_run_interrupt_probe(_probe_session_maker(), _NEW)

        await probe(0, 6)

        assert _FakeJobRepo.progress_calls == []

    async def test_new_replay_run_crosses_chunk_boundaries(self) -> None:
        """重放 run 的探针跨过全部加载分块边界(事故里 0.5s 死在首块)。"""

        _FakeRunRepo.rows = {_NEW: _run_row("running", None)}
        probe = build_run_interrupt_probe(_probe_session_maker(), _NEW)

        await probe(0, 6)
        await probe(4, 6)

        assert _FakeJobRepo.progress_calls == []

    async def test_polling_completed_source_reproduces_incident(self) -> None:
        """阳性对照:探针按**源 run id** 轮询即复现 #455 事故形态。

        源 run 已终态(completed)≠ running → 具名打断,错误文案携带源
        run id(与 result.json 里 error_summary 写源 id 的现场一致)。
        这是被 #455 修复消灭的接线形态,留作回归阴性边界。
        """

        _FakeRunRepo.rows = {_SRC: _run_row("completed", "BJ-source")}
        probe = build_run_interrupt_probe(_probe_session_maker(), _SRC)

        with pytest.raises(ResearchRunInterruptedError) as exc_info:
            await probe(0, 6)
        assert _SRC in str(exc_info.value)
        assert "completed" in str(exc_info.value)

    async def test_job_cancel_requested_still_raises(self) -> None:
        """有 job 行的 run 被 request_cancel → 照常具名打断(语义不变)。"""

        _FakeRunRepo.rows = {_NEW: _run_row("running", "BJ-1")}
        _FakeJobRepo.rows = {"BJ-1": SimpleNamespace(status="cancel_requested")}
        probe = build_run_interrupt_probe(_probe_session_maker(), _NEW)

        with pytest.raises(ResearchRunInterruptedError, match="cancel_requested"):
            await probe(0, 6)

    async def test_job_row_progress_report_phase_encoding(self) -> None:
        """有 job 行的 run 存活时照常上报 k/N phase(#308 口径不变)。"""

        _FakeRunRepo.rows = {_NEW: _run_row("running", "BJ-1")}
        _FakeJobRepo.rows = {"BJ-1": SimpleNamespace(status="running")}
        probe = build_run_interrupt_probe(_probe_session_maker(), _NEW)

        await probe(4, 6)

        assert _FakeJobRepo.progress_calls == [
            {
                "job_id": "BJ-1",
                "done": 0,
                "total": None,
                "phase": "research_run:decision_load 4/6",
            }
        ]


# ---- 执行器接线:适配器以新 run 身份构造 ------------------------------------


def _manifest(run_id: str) -> ResearchRunManifest:
    """离线构造真实 ``ResearchRunManifest``(预替换要过 __post_init__ 校验)。"""

    spec = build_strategy_template(
        "multi_factor",
        strategy_id="replay_probe_455_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    return ResearchRunManifest(
        run_id=run_id,
        idempotency_key=f"replay-probe-455:{run_id}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


class _FakeStore:
    def __init__(self, record: SimpleNamespace | None) -> None:
        self._record = record
        self.checkpoint_calls = 0

    async def get(self, run_id: str) -> SimpleNamespace | None:
        if self._record is not None and self._record.manifest.run_id == run_id:
            return self._record
        return None

    async def checkpoint(self) -> None:
        self.checkpoint_calls += 1


class _CapturingAdapterFactory:
    def __init__(self) -> None:
        self.manifests: list[ResearchRunManifest] = []

    def __call__(self, manifest: ResearchRunManifest) -> object:
        self.manifests.append(manifest)
        return object()


class _RecordingCoordinator:
    """捕获 replay kwargs 并返回预设 record(#383 测试同风格)。"""

    captured: ClassVar[list[dict[str, Any]]] = []
    exc: ClassVar[Exception | None] = None

    def __init__(self, store: object) -> None:
        self.store = store

    async def replay(self, **kwargs: Any) -> SimpleNamespace:
        type(self).captured.append(kwargs)
        exc = type(self).exc
        if exc is not None:
            raise exc
        return SimpleNamespace(
            status=ResearchRunStatus.COMPLETED,
            manifest=SimpleNamespace(run_id=kwargs["new_run_id"]),
        )


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    _RecordingCoordinator.captured = []
    _RecordingCoordinator.exc = None


def _executor(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeStore,
    adapter_factory: _CapturingAdapterFactory,
    coordinator_cls: Any = _RecordingCoordinator,
) -> ResearchRunExecutor:
    """构造被测执行器;Fake 依赖经 cast 适配注入点协议(mypy 安静)。"""

    from typing import cast as _cast

    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.research_run.ResearchRunCoordinator",
        coordinator_cls,
    )
    return ResearchRunExecutor(
        session_maker=_cast(
            "async_sessionmaker[AsyncSession]", _FakeProbeSessionMaker()
        ),
        store_factory=_cast("StoreFactory", lambda session: store),
        adapter_factory=_cast("AdapterFactory", adapter_factory),
    )


def _source_record(
    manifest: ResearchRunManifest,
    status: ResearchRunStatus,
) -> SimpleNamespace:
    return SimpleNamespace(
        manifest=manifest,
        status=status,
        result_checksum="a" * 64 if status is ResearchRunStatus.COMPLETED else None,
    )


class TestExecuteReplayAdapterIdentity:
    """execute_replay 必须以「新建 replay run」的身份构造适配器(#455)。"""

    def test_parametrized_statuses_cover_replayable_contract(self) -> None:
        """参数化状态集与 #305 可重放契约同步(契约扩列须显式更新本文件)。"""

        assert {
            ResearchRunStatus.COMPLETED,
            ResearchRunStatus.INTERRUPTED,
        } == set(REPLAYABLE_SOURCE_STATUSES)

    @pytest.mark.parametrize(
        "status",
        [ResearchRunStatus.COMPLETED, ResearchRunStatus.INTERRUPTED],
    )
    async def test_adapter_built_with_new_run_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: ResearchRunStatus,
    ) -> None:
        source_manifest = _manifest(_SRC)
        store = _FakeStore(_source_record(source_manifest, status))
        factory = _CapturingAdapterFactory()
        executor = _executor(monkeypatch, store, adapter_factory=factory)

        result = await executor.execute_replay(_SRC)

        assert result.status == "succeeded"
        kwargs = _RecordingCoordinator.captured[0]
        new_run_id = kwargs["new_run_id"]
        assert new_run_id != _SRC
        # 探针目标 = 新建 run:适配器按新 run 身份构造,而非源 run。
        adapter_manifest = factory.manifests[0]
        assert adapter_manifest is not source_manifest
        assert adapter_manifest.run_id == new_run_id
        # 身份字段与 coordinator.replay 的 replace 同构;冻结输入零变化。
        assert adapter_manifest == replace(
            source_manifest,
            run_id=new_run_id,
            idempotency_key=kwargs["idempotency_key"],
            requested_by=kwargs["requested_by"],
            replay_of_run_id=_SRC,
            replay_source_status=status.value,
        )
        # coordinator 收到的身份与预构造一致(同一次重放同一身份)。
        assert kwargs["requested_by"] == "job-flamegraph"
        assert kwargs["idempotency_key"].startswith(f"job-flamegraph:{_SRC}:")

    async def test_non_replayable_source_keeps_guard_semantics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CANCELLED 源:不预替换,源 manifest 原样进工厂,守卫口径不变。"""

        source_manifest = _manifest(_SRC)
        store = _FakeStore(
            _source_record(source_manifest, ResearchRunStatus.CANCELLED)
        )
        factory = _CapturingAdapterFactory()
        _RecordingCoordinator.exc = ResearchRunConflictError("状态 cancelled 不可重放")
        executor = _executor(monkeypatch, store, adapter_factory=factory)

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute_replay(_SRC)
        assert exc_info.value.code == "replay_guard_rejected"
        assert factory.manifests == [source_manifest]


# ---- 工厂接线:适配器工厂按 manifest.run_id 构造探针 ------------------------


class TestAdapterFactoryProbeWiring:
    async def test_factory_builds_probe_with_manifest_run_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """捕获探针工厂收到的 run_id:等于构造期 manifest.run_id。

        与执行器接线用例组合,锁死「执行器(新 run 身份 manifest)→ 工厂 →
        build_run_interrupt_probe(run_id)」全链路探针目标 = 新建 replay run。
        """

        captured_run_ids: list[str] = []
        probe_calls: list[tuple[int, int]] = []

        def _fake_probe_factory(
            session_maker: object,
            run_id: str,
        ) -> LoadChunkProbe:
            del session_maker
            captured_run_ids.append(run_id)

            async def _probe(done: int, total: int) -> None:
                probe_calls.append((done, total))

            return _probe

        monkeypatch.setattr(
            "finboard_backtest.research_run.signal_engine.build_run_interrupt_probe",
            _fake_probe_factory,
        )
        factory = build_signal_engine_adapter_factory(
            _probe_session_maker(),
            settings_factory=lambda: SimpleNamespace(
                research_price_feature_process_workers=0
            ),
        )
        manifest = _manifest(_SRC)

        adapter = factory(manifest)

        assert captured_run_ids == [manifest.run_id]
        # 探针被挂进适配器(执行期经 self._chunk_probe 在分块边界触发)。
        assert isinstance(adapter, SignalEnginePipelineAdapter)
        assert adapter._chunk_probe is not None
        await adapter._chunk_probe(0, 6)
        assert probe_calls == [(0, 6)]
