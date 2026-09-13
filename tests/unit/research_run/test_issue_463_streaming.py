"""issue #463 上半场:决策上下文流式化 —— 生成器化 + 适配层逐期拉取。

锁定七组不变量:

* 加载层生成器 ``iter_decision_load_contexts`` 与 ``build_decision_load_contexts``
  兼容包装逐字段等值(features / candidates / covariance / price_series /
  snapshot_id);
* 输入层生成器 ``iter_decision_inputs`` 与 ``build_decision_inputs`` 兼容
  包装逐期等值(checksum 覆盖全部输入字段);
* ``PortfolioPipelineAdapter`` 接受 AsyncIterator 输入:产出 bundle 与
  Sequence 输入逐字节一致;resume 种子经 ``aprefetch_inputs`` 预取后
  前缀原样产出 + 输出与全新 run 一致(#314);
* ``SignalEnginePipelineAdapter.decisions()`` 端到端:流式适配器输出与
  「兼容包装全量物化 + 裸组合管线」参照一致(bundles / report /
  factor_screen,含 u_ 因子 run);
* #263 加载期失败标记经适配器流式路径照常传播(类型 / 消息不变);
* #308/#306 加载帧:首帧 (0, N) 先于 close 矩阵预建、边界帧在本块
  gather 完成后、决策产出之前(帧格式与取值序列不变,#306 的
  「加载期打断先于协作取消被感知」语义保持);
* user_code 路径:流式消费 vs 全量物化消费的 decisions / report 等值;
  生成器提前 aclose 时特征进程池即时释放(finally 链)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from finboard_backtest.research_run import (
    FrozenArtifactRef,
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.factor_screen import build_factor_screen
from finboard_backtest.research_run.failure_context import read_decision_load_context
from finboard_backtest.research_run.frozen_loader import FrozenInputLoader
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.signal_engine import (
    DecisionLoadContext,
    SignalEnginePipelineAdapter,
    build_decision_inputs,
    build_decision_load_contexts,
    iter_decision_inputs,
    iter_decision_load_contexts,
)
from finboard_backtest.research_run.user_code_engine import UserCodeStrategyAdapter
from finboard_data.releases import FrozenReleaseProvider

from .test_issue_288_load_multiproc import _assert_contexts_equal
from .test_issue_304_partial_evidence import (
    DECISION_AT,
    DECISION_AT_2,
    _closes_provider,
)
from .test_issue_304_partial_evidence import (
    SYMBOLS as _SIGNAL_SYMBOLS,
)
from .test_issue_304_partial_evidence import (
    _manifest as _signal_manifest,
)
from .test_issue_304_partial_evidence import (
    _snapshot as _signal_snapshot,
)
from .test_issue_304_partial_evidence import (
    _spec as _signal_spec,
)
from .test_issue_306_load_probe import (
    _RELEASE_ID,
    _build_release,
    _month_end_decisions,
    _noop_snapshot_provider,
)
from .test_issue_308_progress import (
    _multi_period_manifest,
    _RecordingProvider,
)
from .test_issue_314_checkpoint_resume import _multi_input, _plain_snapshot
from .test_signal_engine import _obs, _StubSnapshot

pytestmark = pytest.mark.asyncio


def _agen(items: tuple[Any, ...]) -> Any:
    """把 Sequence 包成异步迭代器(模拟流式输入)。"""

    async def _gen() -> Any:
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# Part A:加载 / 输入层生成器 vs 兼容包装 等值
# ---------------------------------------------------------------------------


class TestGeneratorMaterializedEquivalence:
    async def test_iter_load_contexts_equals_wrapper(self, tmp_path) -> None:
        """6 期 multi_period:生成器逐期产出与兼容包装全量物化逐字段等值。"""
        provider = await _build_release(tmp_path)
        manifest = _multi_period_manifest()

        def factory(release_id: str) -> FrozenReleaseProvider:
            assert release_id == _RELEASE_ID
            return provider

        streamed = [
            context
            async for context in iter_decision_load_contexts(
                manifest,
                release_provider_factory=factory,
                snapshot_provider=_noop_snapshot_provider,
            )
        ]
        materialized = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
        )

        assert len(streamed) == len(_month_end_decisions())
        _assert_contexts_equal(tuple(streamed), materialized)

    async def test_iter_inputs_equals_wrapper(self, tmp_path) -> None:
        """``iter_decision_inputs`` vs ``build_decision_inputs``:逐期 checksum
        等值(checksum 覆盖 candidates/features/signals/prices/covariance)。"""
        provider = await _build_release(tmp_path)
        manifest = _multi_period_manifest()

        def factory(release_id: str) -> FrozenReleaseProvider:
            assert release_id == _RELEASE_ID
            return provider

        common: dict[str, Any] = {
            "release_provider_factory": factory,
            "snapshot_provider": _noop_snapshot_provider,
        }
        streamed = [
            item async for item in iter_decision_inputs(manifest, **common)
        ]
        materialized = await build_decision_inputs(manifest, **common)

        assert len(streamed) == len(_month_end_decisions())
        for left, right in zip(streamed, materialized, strict=True):
            assert left.business_date == right.business_date
            assert left.decision_at == right.decision_at
            assert left.execution_at == right.execution_at
            assert left.checksum == right.checksum
            assert left.suspended_symbols == right.suspended_symbols
            if left.covariance is None or right.covariance is None:
                assert left.covariance is None
                assert right.covariance is None
            else:
                assert left.covariance.tickers == right.covariance.tickers
                assert left.covariance.method == right.covariance.method
                np.testing.assert_allclose(
                    left.covariance.matrix, right.covariance.matrix
                )


# ---------------------------------------------------------------------------
# Part B:组合管线 Sequence vs AsyncIterator 输入 + 流式 resume(#314)
# ---------------------------------------------------------------------------


class TestPipelineStreamingInputs:
    async def test_iterator_inputs_equal_sequence_inputs(self, manifest_factory) -> None:
        """同一组输入分别以 tuple / AsyncIterator 供给:bundles 逐字段一致。"""
        manifest = manifest_factory()
        inputs = tuple(_multi_input(i) for i in range(3))

        from_sequence = [
            item
            async for item in PortfolioPipelineAdapter(
                strategy_kind="ma_cross", decision_inputs=inputs
            ).decisions(manifest)
        ]
        from_iterator = [
            item
            async for item in PortfolioPipelineAdapter(
                strategy_kind="ma_cross", decision_inputs=_agen(inputs)
            ).decisions(manifest)
        ]

        assert from_iterator == from_sequence

    async def test_streaming_resume_prefix_verbatim_then_continues(
        self, manifest_factory
    ) -> None:
        """#314 流式形态:aprefetch 种子校验 → 前缀原样产出 + 后续与全新
        run 一致(全局序号连续、账本逐值等值)。"""
        manifest = manifest_factory()
        inputs = tuple(_multi_input(i) for i in range(3))

        fresh = [
            item
            async for item in PortfolioPipelineAdapter(
                strategy_kind="ma_cross", decision_inputs=inputs
            ).decisions(manifest)
        ]

        # 先从一条流式管线取 2 期前缀(模拟已落库前缀读回)
        partial_driver = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=_agen(inputs)
        )
        driver_gen = cast(AsyncGenerator[DecisionBundle, None], partial_driver.decisions(manifest))
        prefix = [await driver_gen.__anext__(), await driver_gen.__anext__()]
        await driver_gen.aclose()

        resumed = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=_agen(inputs)
        )
        await resumed.aprefetch_inputs(len(prefix))
        assert resumed.resume_from(prefix) is True
        rebuilt = [item async for item in resumed.decisions(manifest)]

        assert rebuilt == fresh
        # 前缀原样产出(零重算)
        assert rebuilt[:2] == prefix


# ---------------------------------------------------------------------------
# Part C:SignalEnginePipelineAdapter 端到端(流式 vs 兼容包装参照)
# ---------------------------------------------------------------------------


def _u_factor_setup(
    run_id: str, idempotency_key: str, n_periods: int = 2
) -> tuple[ResearchRunManifest, dict[str, _StubSnapshot], Any]:
    """两期 single_shot + u_ 因子快照(factor_screen 场景)。"""
    spec = _signal_spec(rank_threshold=0.5)
    snapshot_ids = tuple(f"factor-v{i}" for i in range(1, n_periods + 1))
    manifest = _signal_manifest(
        spec,
        run_id=run_id,
        idempotency_key=idempotency_key,
        snapshot_ids=snapshot_ids,
    )
    seeds = {1: 6.0, 2: 3.0, 3: 9.0}
    days = [DECISION_AT, DECISION_AT_2, datetime(2024, 3, 1, 15, 0, tzinfo=UTC)]
    snapshots = {
        snapshot_id: _signal_snapshot(days[index], alpha_seed=seeds[index + 1])
        for index, snapshot_id in enumerate(snapshot_ids)
    }
    provider = _closes_provider(n_days=100)
    return manifest, snapshots, provider


class TestSignalEngineStreamingEndToEnd:
    async def test_streaming_adapter_matches_materialized_reference(self) -> None:
        """流式适配器 vs 「build_decision_inputs 全量物化 + 裸管线」参照:
        bundles / report / factor_screen 逐项一致(u_ 因子 run)。"""
        manifest, snapshots, provider = _u_factor_setup(
            "RR-issue463e2e000001", "issue463-e2e"
        )

        def factory(release_id: str) -> Any:
            assert release_id == "release-v1"
            return provider

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        # 参照:兼容包装全量物化 + 裸组合管线 + 兼容 screen 入口
        inputs = await build_decision_inputs(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        reference_pipeline = PortfolioPipelineAdapter(
            strategy_kind="multi_factor", decision_inputs=inputs
        )
        reference_bundles = [
            item async for item in reference_pipeline.decisions(manifest)
        ]
        reference_report = reference_pipeline.build_report(manifest, reference_bundles)
        reference_screen = await build_factor_screen(
            manifest, inputs, factory
        )

        # 流式适配器路径
        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        adapter_bundles = [item async for item in adapter.decisions(manifest)]
        adapter_report = adapter.build_report(manifest, adapter_bundles)

        assert len(adapter_bundles) == len(reference_bundles) == 2
        assert adapter_bundles == reference_bundles
        # factor_screen 是信号适配器注入的展示层字段 —— 裸组合管线的参照
        # 报告恒为 None,剔除后逐字段比较;screen 本身与兼容入口对照。
        assert replace(adapter_report, factor_screen=None) == reference_report
        assert adapter_report.factor_screen is not None
        assert adapter_report.factor_screen == reference_screen

    async def test_adapter_early_close_cascades_to_input_stream(self) -> None:
        """消费者提前 aclose 决策迭代:输入流经 finally 链立即关闭(可再次
        __anext__ 即 StopAsyncIteration),且已产出的决策内容不受影响。"""
        manifest, snapshots, provider = _u_factor_setup(
            "RR-issue463close0001", "issue463-close"
        )

        def factory(release_id: str) -> Any:
            assert release_id == "release-v1"
            return provider

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        gen = cast(AsyncGenerator[DecisionBundle, None], adapter.decisions(manifest))
        first = await gen.__anext__()
        assert first.business_date == DECISION_AT.date()
        await gen.aclose()

        # 输入流已关闭(级联:捕获包装 → 输入生成器 → 加载生成器)
        assert adapter._input_iterator is not None
        with pytest.raises(StopAsyncIteration):
            await adapter._input_iterator.__anext__()

    async def test_fast_path_never_starts_input_stream(self) -> None:
        """#314 快速路径(#463 形态):全部决策已落库 → 输入迭代器从未创建
        (「生成器不启动」),screen 显式跳过。"""
        spec = _signal_spec(rank_threshold=0.5)
        manifest = _signal_manifest(
            spec,
            run_id="RR-issue463fast00001",
            idempotency_key="issue463-fast",
            snapshot_ids=("factor-v1", "factor-v2"),
        )
        snapshots = {
            "factor-v1": _plain_snapshot(DECISION_AT),
            "factor-v2": _plain_snapshot(DECISION_AT_2),
        }
        provider = _closes_provider(n_days=100)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)

        interrupted = await coordinator.execute(
            manifest,
            _ReportCrashAdapter(
                manifest=manifest, provider=provider, snapshots=snapshots
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        resumed_adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: provider,  # type: ignore[arg-type]
            snapshot_provider=_async_snapshot_lookup(snapshots),
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        assert resumed_adapter._input_iterator is None
        assert resumed_adapter._fast_path is True
        assert resumed.result is not None
        assert resumed.result.factor_screen is None


class _ReportCrashAdapter(SignalEnginePipelineAdapter):
    """全部决策产出后在 report 段崩溃(等价进程死在报告构建,#314)。"""

    def __init__(
        self,
        *,
        manifest: Any,
        provider: Any,
        snapshots: dict[str, Any],
    ) -> None:
        super().__init__(
            manifest=manifest,
            release_provider_factory=lambda _id: provider,
            snapshot_provider=_async_snapshot_lookup(snapshots),
        )

    def build_report(self, manifest: Any, decisions: Any) -> Any:
        del manifest, decisions
        from finboard_backtest.research_run import ResearchRunInterruptedError

        raise ResearchRunInterruptedError("report-stage crash")


def _async_snapshot_lookup(snapshots: dict[str, Any]) -> Any:
    async def _lookup(snapshot_id: str) -> Any:
        return snapshots.get(snapshot_id)

    return _lookup


# ---------------------------------------------------------------------------
# Part D:#263 失败标记经适配器流式路径传播
# ---------------------------------------------------------------------------


class TestFailureMarkerThroughAdapter:
    async def test_mid_period_load_failure_carries_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第 2 期加载失败:经适配器流式路径抛出,标记定位第 2 期,类型 /
        消息不被改写(bare raise 语义)。"""
        manifest, snapshots, provider = _u_factor_setup(
            "RR-issue463marker001", "issue463-marker"
        )
        real_load = FrozenInputLoader.load_context

        async def failing_second(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> Any:
            if decision_at == DECISION_AT_2:
                raise RuntimeError("中期数据缺失:第 2 期发布损坏")
            return await real_load(
                self, manifest_arg, decision_at=decision_at, execution_at=execution_at
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", failing_second)

        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: provider,
            snapshot_provider=_async_snapshot_lookup(snapshots),
        )
        with pytest.raises(RuntimeError, match="中期数据缺失") as excinfo:
            async for _decision in adapter.decisions(manifest):
                pass

        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at == DECISION_AT_2
        assert str(excinfo.value) == "中期数据缺失:第 2 期发布损坏"


# ---------------------------------------------------------------------------
# Part E:#308/#306 加载帧 —— 帧序不变,与决策产出交错
# ---------------------------------------------------------------------------


class _CloseHistoryCountingProvider(_RecordingProvider):
    """额外计数 close 矩阵预建的列式读取入口(fetch_close_history,#300)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_history_calls = 0

    async def fetch_close_history(
        self, symbol: Any, period: Any, start: Any, end: Any,
        *, decision_at: Any, adjust: str = "qfq", include_open: bool = False,
    ) -> Any:
        self.close_history_calls += 1
        return await super().fetch_close_history(
            symbol, period, start, end,
            decision_at=decision_at, adjust=adjust, include_open=include_open,
        )


class _InterleaveProbe:
    """记录 (done, total, 当时 close 矩阵读取数, 当时已产出决策数) 的探针。"""

    def __init__(self, provider: _CloseHistoryCountingProvider) -> None:
        self.calls: list[tuple[int, int, int, int]] = []
        self._provider = provider
        self.decisions_yielded = 0

    async def __call__(self, done: int, total: int) -> None:
        self.calls.append(
            (done, total, self._provider.close_history_calls, self.decisions_yielded)
        )


class TestLoadFrameOrdering:
    @pytest.mark.timeout(240)
    async def test_first_frame_precedes_prebuild_and_frames_interleave(
        self, tmp_path
    ) -> None:
        """首帧 (0, N) 先于 close 矩阵预建(列式读取为 0);边界帧 (4, N)
        在本块 gather 完成后、决策产出之前 —— 加载帧先于其块的决策帧
        (#306「加载期打断先于协作取消被感知」的流式化形态)。"""
        await _build_release(tmp_path)
        provider = _CloseHistoryCountingProvider(
            release_root=tmp_path / "releases", release_id=_RELEASE_ID
        )
        probe = _InterleaveProbe(provider)
        manifest = replace(
            _multi_period_manifest(),
            # 关闭风险贡献硬约束(=1 不构成约束):本测试聚焦帧序,3 标的
            # 小池不因 #303 的 1/n 可行性下限被拒。
            portfolio_config={"overrides": {"max_risk_contribution": 1}},
        )

        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: provider,
            snapshot_provider=_noop_snapshot_provider,
            chunk_probe=probe,
        )
        decisions: list[Any] = []
        async for decision in adapter.decisions(manifest):
            decisions.append(decision)
            probe.decisions_yielded += 1
        assert len(decisions) == len(_month_end_decisions()) == 6

        # 帧取值序列与 #306/#308 形态一致:(0, 6) 与 (4, 6)。
        assert [(done, total) for done, total, _, _ in probe.calls] == [(0, 6), (4, 6)]
        # 首帧先于预建:0 次列式读取、0 个决策产出。
        assert probe.calls[0] == (0, 6, 0, 0)
        # 边界帧在本块(前 4 期)gather 完成后、决策产出之前:close 矩阵
        # 预建已发生、尚无任何决策产出。
        assert probe.calls[1][2] > 0
        assert probe.calls[1][3] == 0


# ---------------------------------------------------------------------------
# Part F:生成器提前关闭即时释放特征进程池
# ---------------------------------------------------------------------------


class TestGeneratorCloseReleasesPool:
    @pytest.mark.timeout(240)
    async def test_aclose_closes_pool_promptly(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拉取 1 期后 aclose 加载生成器:finally 立即关闭常驻进程池
        (真实 spawn 池,关闭计数经由包装的 aclose)。"""
        import finboard_backtest.research_run.signal_engine as signal_engine

        provider = await _build_release(tmp_path)
        manifest = _multi_period_manifest()
        real_start = signal_engine._start_period_feature_pool
        closed = {"count": 0}

        async def wrapped_start(pool_provider: Any, workers: int) -> Any:
            pool = await real_start(pool_provider, workers)
            assert pool is not None
            original = pool.aclose

            async def counting() -> None:
                closed["count"] += 1
                await original()

            pool.aclose = counting  # type: ignore[method-assign]
            return pool

        monkeypatch.setattr(signal_engine, "_start_period_feature_pool", wrapped_start)

        gen = cast(
            "AsyncGenerator[DecisionLoadContext, None]",
            iter_decision_load_contexts(
                manifest,
                release_provider_factory=lambda _id: provider,
                snapshot_provider=_noop_snapshot_provider,
                process_workers=2,
            ),
        )
        first = await gen.__anext__()
        assert first.context.business_date is not None
        assert closed["count"] == 0
        await gen.aclose()
        assert closed["count"] == 1


# ---------------------------------------------------------------------------
# Part G:user_code 路径流式 vs 全量物化消费等值
# ---------------------------------------------------------------------------


class _ScriptedSandbox:
    """确定性权重脚本沙箱(替代真实容器,#218 集成测试同思路)。"""

    def __init__(self, weights_by_index: dict[int, dict[str, float]]) -> None:
        self._weights = weights_by_index
        self.commit = "c" * 40
        self.image = "finboard-research-sandbox:test"
        self.image_digest = "sha256:" + "0" * 64

    async def decide(
        self,
        *,
        decision_index: int,
        decision_at: datetime,
        symbols: tuple[str, ...],
        current_weights: Any,
        strategy_constraints: Any,
    ) -> Any:
        del decision_at, current_weights, strategy_constraints
        weights = self._weights[decision_index]
        record = {
            "index": decision_index,
            "mount_manifest_checksum": "m" * 64,
            "targets_checksum": stable_checksum(weights),
            "n_targets": len(weights),
        }
        from finboard_backtest.research_sandbox.strategy_exec import (
            StrategyDecisionOutcome,
        )

        return StrategyDecisionOutcome(weights=weights, record=record)

    def provenance_header(self, *, mode: str, decision_count: int) -> dict[str, Any]:
        return {
            "code_checksum": "c" * 64,
            "artifact_dir": "/tmp",
            "resource_limits": {},
            "execution_mode": mode,
            "decision_count": decision_count,
        }


class _MaterializedContextsAdapter(UserCodeStrategyAdapter):
    """上下文先全量物化再消费(等价流式化前的消费形态,作对照)。"""

    async def _ensure_loaded(self) -> tuple[Any, Any]:
        stream, sandbox = await super()._ensure_loaded()
        materialized = tuple([item async for item in stream])

        async def _replay() -> Any:
            for item in materialized:
                yield item

        return _replay(), sandbox


class TestUserCodeStreamingEquivalence:
    async def test_streaming_matches_materialized_consumption(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user_code 决策循环:流式逐期拉取 vs 全量物化 —— decisions /
        report(含 strategy_screen / sandbox_provenance)逐项一致。"""
        import finboard_backtest.research_run.user_code_engine as uc_module

        base = manifest_factory("user_code")
        manifest = replace(
            base,
            run_id="RR-issue463uc000001",
            idempotency_key="issue463-uc",
            factor_snapshots=tuple(
                FrozenArtifactRef(
                    artifact_id=f"factor-v{i}",
                    version="v1",
                    checksum="b" * 64,
                    capabilities=("factor:close",),
                )
                for i in (1, 2)
            ),
        )
        days = [DECISION_AT, DECISION_AT_2]
        snapshots = {
            f"factor-v{i + 1}": _StubSnapshot(
                snapshot_id=f"factor-v{i + 1}",
                decision_at=day,
                observations=tuple(
                    _obs(symbol, name, 1.0 + 0.1 * idx - i * 0.05, available_at=days[0])
                    for idx, symbol in enumerate(_SIGNAL_SYMBOLS)
                    for name in ("pb", "momentum", "u_agent_alpha")
                ),
            )
            for i, day in enumerate(days)
        }
        provider = _closes_provider(n_days=100)

        weights_by_index = {
            0: {
                _SIGNAL_SYMBOLS[0]: 0.4,
                _SIGNAL_SYMBOLS[1]: 0.3,
                _SIGNAL_SYMBOLS[2]: 0.3,
            },
            1: {
                _SIGNAL_SYMBOLS[-1]: 0.4,
                _SIGNAL_SYMBOLS[1]: 0.3,
                _SIGNAL_SYMBOLS[2]: 0.3,
            },
        }

        async def fake_create(**kwargs: Any) -> Any:
            del kwargs
            return _ScriptedSandbox(weights_by_index)

        monkeypatch.setattr(
            uc_module, "StrategySandboxCaller", SimpleNamespace(create=fake_create)
        )

        def kwargs() -> dict[str, Any]:
            return {
                "manifest": manifest,
                "release_provider_factory": lambda _id: provider,
                "snapshot_provider": _async_snapshot_lookup(snapshots),
                "settings_factory": lambda: SimpleNamespace(
                    research_sandbox_enabled=True,
                    research_sandbox_image="finboard-research-sandbox:test",
                    research_sandbox_repo_path="unused",
                ),
            }

        streaming = UserCodeStrategyAdapter(**kwargs())
        streaming_bundles = [item async for item in streaming.decisions(manifest)]
        streaming_report = streaming.build_report(manifest, streaming_bundles)

        materialized = _MaterializedContextsAdapter(**kwargs())
        materialized_bundles = [
            item async for item in materialized.decisions(manifest)
        ]
        materialized_report = materialized.build_report(manifest, materialized_bundles)

        assert len(streaming_bundles) == len(materialized_bundles) == 2
        assert streaming_bundles == materialized_bundles
        assert streaming_report == materialized_report
        assert streaming_report.strategy_screen is not None
        assert streaming_report.strategy_screen == materialized_report.strategy_screen
