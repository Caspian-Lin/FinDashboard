"""issue #470 后半场:断点续算前缀的 stub-only 装载(前缀零重算)。

锁定五组不变量:

* 装载层 :meth:`FrozenInputLoader.load_resume_stub` 只取 resume 校验 /
  账本重放消费的五个字段,且与全量 ``load_context`` 逐值一致(收窄域 ⊆
  全量域,交集逐值相同);features / 研究观测 / 因子序列装载入口被替换为
  抛错仍照常装载(前缀零重算的直接证据);
* 生成器 ``skip_prefix``:尾部期次与不跳过时逐字段等值;前缀期
  ``load_context`` / ``_estimate_covariance`` 零调用;分块探针首帧从
  ``skip_prefix`` 起报(#308 k = 已完成期数);
* 适配器端到端(真实 SignalEnginePipelineAdapter + 真实冻结发布 +
  coordinator 续跑):续算产出与一次跑完 result_checksum / artifact 指纹
  等值,前缀期零完整装载,``_business_dates`` 覆盖全期次(#304 partial
  证据期次定位不错位),``_period_cross_sections`` 只覆盖续算期次;
* 种子被拒的兜底:stub 预取后种子被拒 → 流重建全量重算(skip_prefix 归零、
  全部期次完整装载),产出与一次跑完等值;
* 消费审计门:seed 携带 ``u_`` 用户因子观测(manifest 扫描漏报的兜底路径)
  时前缀恒走全量装载,factor_screen 口径不受续跑影响。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from finboard_backtest.portfolio.contracts import AssetLotInfo
from finboard_backtest.research_run import (
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run import (
    signal_engine as signal_engine_module,
)
from finboard_backtest.research_run.checkpoint_resume import (
    completed_decision_prefix,
)
from finboard_backtest.research_run.contracts import (
    ResearchRunInterruptedError,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    ReleaseProviderFactory,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioPipelineAdapter,
    _ResumeReplayStub,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
    build_decision_load_contexts,
    iter_resume_stub_inputs,
)

from .test_issue_288_load_multiproc import _assert_contexts_equal
from .test_issue_304_partial_evidence import (
    DECISION_AT,
    DECISION_AT_2,
    _closes_provider,
)
from .test_issue_304_partial_evidence import (
    _manifest as _signal_manifest,
)
from .test_issue_304_partial_evidence import (
    _spec as _signal_spec,
)
from .test_issue_306_load_probe import (
    _SYMBOLS,
    _build_release,
    _decision_at,
    _month_end_decisions,
    _noop_snapshot_provider,
)
from .test_issue_306_load_probe import (
    _manifest as _release_manifest,
)
from .test_issue_314_checkpoint_resume import (
    _artifact_fingerprint,
    _plain_snapshot,
    _signal_setup,
)

pytestmark = pytest.mark.asyncio

_MONTH_ENDS = _month_end_decisions()
_DAY_COUNT = len(_MONTH_ENDS)
_DAY_ATS = [_decision_at(day) for day in _MONTH_ENDS]


class _Counters:
    """load_context / load_resume_stub / _estimate_covariance 调用计数。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.load_context = 0
        self.load_stub = 0
        self.covariance = 0
        real_context = FrozenInputLoader.load_context
        real_stub = FrozenInputLoader.load_resume_stub
        real_covariance = signal_engine_module._estimate_covariance
        counters = self

        async def counting_context(loader: Any, *args: Any, **kwargs: Any) -> Any:
            counters.load_context += 1
            return await real_context(loader, *args, **kwargs)

        async def counting_stub(loader: Any, *args: Any, **kwargs: Any) -> Any:
            counters.load_stub += 1
            return await real_stub(loader, *args, **kwargs)

        def counting_covariance(*args: Any, **kwargs: Any) -> Any:
            counters.covariance += 1
            return real_covariance(*args, **kwargs)

        monkeypatch.setattr(FrozenInputLoader, "load_context", counting_context)
        monkeypatch.setattr(FrozenInputLoader, "load_resume_stub", counting_stub)
        monkeypatch.setattr(
            signal_engine_module, "_estimate_covariance", counting_covariance
        )


class _InterruptAfterKSignalAdapter(SignalEnginePipelineAdapter):
    """产出 k 条决策后模拟进程崩溃(不改 _load / 输入流包装,生产同构)。"""

    def __init__(self, *, k: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._k = k

    async def decisions(self, manifest: Any) -> Any:
        produced = 0
        async for decision in super().decisions(manifest):
            yield decision
            produced += 1
            if produced >= self._k:
                raise ResearchRunInterruptedError("mid-stream crash")


def _factory_for(provider: Any, *, expect_release_id: str | None = None) -> Any:
    """固定 provider 的发布工厂(测试桩 provider,返回协议类型)。"""

    def factory(release_id: str) -> Any:
        if expect_release_id is not None:
            assert release_id == expect_release_id
        return provider

    return cast(ReleaseProviderFactory, factory)


# ---------------------------------------------------------------------------
# Part A:loader stub-only 装载
# ---------------------------------------------------------------------------


class TestLoadResumeStub:
    async def test_stub_fields_match_full_load(self, tmp_path: Path) -> None:
        """stub 五字段与全量 load_context 逐值一致(收窄域 ⊆ 全量域)。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        from finboard_backtest.research_run.signal_engine import _next_execution_at

        for index, decision_at in enumerate(_DAY_ATS):
            execution_at = await _next_execution_at(provider, decision_at)
            full_loader = FrozenInputLoader(
                release_provider_factory=_factory_for(provider),
                snapshot_provider=_noop_snapshot_provider,
            )
            full = await full_loader.load_context(
                manifest, decision_at=decision_at, execution_at=execution_at
            )
            narrowed_loader = FrozenInputLoader(
                release_provider_factory=_factory_for(provider),
                snapshot_provider=_noop_snapshot_provider,
            )
            stub = await narrowed_loader.load_resume_stub(
                manifest,
                decision_at=decision_at,
                execution_at=execution_at,
                symbols=frozenset({_SYMBOLS[0]}),
            )
            assert stub.business_date == full.business_date == _MONTH_ENDS[index]
            assert stub.decision_at == full.decision_at
            assert set(stub.prices) == {_SYMBOLS[0]}
            assert set(stub.lot_info) == {_SYMBOLS[0]}
            assert set(stub.execution_prices) <= set(full.execution_prices)
            for symbol, value in stub.prices.items():
                assert value == full.prices[symbol]
            for symbol, value in stub.execution_prices.items():
                assert value == full.execution_prices[symbol]
            for symbol, info in stub.lot_info.items():
                assert info == full.lot_info[symbol]

    async def test_full_domain_stub_matches_full_load(self, tmp_path: Path) -> None:
        """symbols=None(不收窄)时五字段与全量逐值一致。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        from finboard_backtest.research_run.signal_engine import _next_execution_at

        decision_at = _DAY_ATS[0]
        execution_at = await _next_execution_at(provider, decision_at)
        full = await FrozenInputLoader(
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
        ).load_context(manifest, decision_at=decision_at, execution_at=execution_at)
        stub = await FrozenInputLoader(
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
        ).load_resume_stub(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        assert stub.lot_info == full.lot_info
        assert stub.prices == full.prices
        assert stub.execution_prices == full.execution_prices

    async def test_stub_never_touches_features_or_research_loaders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """features / 研究观测 / 因子序列装载入口被替换为抛错,stub 照常装载。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        decision_at = _DAY_ATS[0]
        from finboard_backtest.research_run.signal_engine import _next_execution_at

        execution_at = await _next_execution_at(provider, decision_at)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("stub 路径不得触碰 features / 研究数据装载入口")

        monkeypatch.setattr(FrozenInputLoader, "_load_features", _boom)
        monkeypatch.setattr(FrozenInputLoader, "_load_research_features", _boom)
        monkeypatch.setattr(FrozenInputLoader, "_load_series_features", _boom)

        stub = await FrozenInputLoader(
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
        ).load_resume_stub(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        assert stub.lot_info
        assert stub.prices

    async def test_stub_stream_periods_align_with_full_stream(
        self, tmp_path: Path
    ) -> None:
        """stub 流的期次序列 = 全量流的同一前缀(两流共用决策日推导)。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        stubs = [
            item
            async for item in iter_resume_stub_inputs(
                manifest,
                release_provider_factory=_factory_for(provider),
                snapshot_provider=_noop_snapshot_provider,
                count=_DAY_COUNT,
            )
        ]
        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
        )
        assert len(stubs) == len(contexts) == _DAY_COUNT
        assert [item.decision_at for item in stubs] == [
            item.context.decision_at for item in contexts
        ]
        assert [item.business_date for item in stubs] == [
            item.context.business_date for item in contexts
        ]


# ---------------------------------------------------------------------------
# Part B:skip_prefix 生成器语义
# ---------------------------------------------------------------------------


class TestSkipPrefixGenerator:
    async def test_skip_prefix_tail_equals_unskipped_tail(
        self, tmp_path: Path
    ) -> None:
        """skip_prefix=2 的产出与不跳过的第 2 期起逐字段等值。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        full = await build_decision_load_contexts(
            manifest,
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
        )
        skipped = await build_decision_load_contexts(
            manifest,
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
            skip_prefix=2,
        )
        assert len(skipped) == len(full) - 2
        _assert_contexts_equal(tuple(full[2:]), tuple(skipped))

    async def test_skip_prefix_builds_nothing_for_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """前缀期零构建:load_context / 协方差零调用,探针首帧从 skip 起报。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        counters = _Counters(monkeypatch)
        calls: list[tuple[int, int]] = []

        async def probe(done: int, total: int) -> None:
            calls.append((done, total))

        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
            chunk_probe=probe,
            skip_prefix=2,
        )
        assert len(contexts) == _DAY_COUNT - 2
        assert [item.context.decision_at for item in contexts] == _DAY_ATS[2:]
        assert counters.load_context == _DAY_COUNT - 2
        assert counters.covariance == _DAY_COUNT - 2
        # #308 语义:首帧即报告已完成期数(种子前缀 2 期);本块(0..3)只
        # 构建 2..3,边界帧报 2 + 2 = 4;末块不再触发边界帧。
        assert calls == [(2, _DAY_COUNT), (4, _DAY_COUNT)]

    async def test_skip_all_periods_builds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skip_prefix = 总期数:零构建、零产出(全前缀续跑形态)。"""
        provider = await _build_release(tmp_path)
        manifest = _release_manifest()
        counters = _Counters(monkeypatch)
        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=_factory_for(provider),
            snapshot_provider=_noop_snapshot_provider,
            skip_prefix=_DAY_COUNT,
        )
        assert contexts == ()
        assert counters.load_context == 0
        assert counters.covariance == 0


# ---------------------------------------------------------------------------
# Part C:适配器端到端(真实 coordinator 续跑)
# ---------------------------------------------------------------------------


def _non_u_signal_setup(
    run_id: str, idempotency_key: str
) -> tuple[Any, dict[str, Any], Any]:
    """3 决策 single_shot、无 u_ 因子观测(走 stub 前缀路径的形态)。"""

    spec = _signal_spec(rank_threshold=0.5)
    manifest = _signal_manifest(
        spec,
        run_id=run_id,
        idempotency_key=idempotency_key,
        snapshot_ids=("factor-v1", "factor-v2", "factor-v3"),
    )
    snapshots = {
        "factor-v1": _plain_snapshot(DECISION_AT),
        "factor-v2": _plain_snapshot(DECISION_AT_2, momentum_shift=0.2),
        "factor-v3": _plain_snapshot(
            datetime(2024, 3, 1, 15, 0, tzinfo=UTC), momentum_shift=-0.1
        ),
    }
    return manifest, snapshots, _closes_provider(n_days=100)


class TestStubPrefixResumeEndToEnd:
    async def test_resume_equals_fresh_and_prefix_not_rebuilt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """续跑产出与一次跑完等值;前缀 2 期零 load_context / 零协方差。"""
        manifest, snapshots, provider = _non_u_signal_setup(
            "RR-issue470b-e2e-0001", "issue470b-e2e"
        )

        factory = _factory_for(provider)

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        counters = _Counters(monkeypatch)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest,
            _InterruptAfterKSignalAdapter(
                k=2,
                manifest=manifest,
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        resumed_adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        contexts_before = counters.load_context
        covariance_before = counters.covariance
        stubs_before = counters.load_stub
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 续跑段:stub 前缀 2 期只走 load_resume_stub,完整装载只剩 1 期
        # (load_context / 协方差各 1 次)—— 前缀零重算的直接计数证据。
        assert counters.load_stub - stubs_before == 2
        assert counters.load_context - contexts_before == 1
        assert counters.covariance - covariance_before == 1
        assert resumed_adapter._stub_prefix_periods == 2
        # 前缀期 business_date 由 stub 流登记(partial 证据期次定位不错位),
        # factor_screen 投影只覆盖续算期次(无 u_ 因子 → screen 恒 None)。
        assert len(resumed_adapter._business_dates) == 3
        assert len(resumed_adapter._period_cross_sections) == 1

        control_manifest, _, _ = _non_u_signal_setup(
            "RR-issue470b-e2e-ctrl", "issue470b-e2e-ctrl"
        )
        control = await coordinator.execute(
            control_manifest,
            SignalEnginePipelineAdapter(
                manifest=control_manifest,
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED, control.error_summary
        assert resumed.result_checksum == control.result_checksum
        assert _artifact_fingerprint(
            await store.list_artifacts(manifest.run_id)
        ) == _artifact_fingerprint(
            await store.list_artifacts(control_manifest.run_id)
        )
        assert resumed.result is not None
        assert resumed.result.factor_screen is None

    async def test_seed_rejected_falls_back_to_full_recompute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stub 预取后种子被拒 → 流重建全量重算(skip 归零),产出等值。"""
        manifest, snapshots, provider = _non_u_signal_setup(
            "RR-issue470b-rej-0001", "issue470b-rej"
        )

        factory = _factory_for(provider)

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        counters = _Counters(monkeypatch)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest,
            _InterruptAfterKSignalAdapter(
                k=2,
                manifest=manifest,
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        prefix = completed_decision_prefix(
            manifest.run_id, await store.list_artifacts(manifest.run_id)
        )
        assert len(prefix) == 2

        # 种子漂移:末个已完成决策的 decision_at 与冻结输入不一致 →
        # 输入-决策错位校验拒绝种子(validate-then-mutate,零突变)。
        doctored = (
            *prefix[:-1],
            replace(prefix[-1], decision_at=DECISION_AT_2.replace(day=15)),
        )

        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        assert adapter.resume_from(doctored) is True
        contexts_before = counters.load_context
        covariance_before = counters.covariance
        stubs_before = counters.load_stub
        produced = [decision async for decision in adapter.decisions(manifest)]
        delta_contexts = counters.load_context - contexts_before
        delta_covariance = counters.covariance - covariance_before
        delta_stubs = counters.load_stub - stubs_before

        control_manifest, _, _ = _non_u_signal_setup(
            "RR-issue470b-rej-ctrl", "issue470b-rej-ctrl"
        )
        control = [
            decision
            async for decision in SignalEnginePipelineAdapter(
                manifest=control_manifest,
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            ).decisions(control_manifest)
        ]

        assert len(produced) == len(control) == 3
        assert [
            (item.business_date, item.decision_at, stable_checksum(item.signals))
            for item in produced
        ] == [
            (item.business_date, item.decision_at, stable_checksum(item.signals))
            for item in control
        ]
        # 种子被拒:stub 预取过(2 期)但随即被丢弃,全量 3 期完整装载
        # (若种子被接受则只会装载 1 期续算期)。
        assert delta_stubs == 2
        assert delta_contexts == 3
        assert delta_covariance == 3
        assert adapter._stub_prefix_periods == 0
        assert len(adapter._business_dates) == 3

    async def test_user_factor_seed_keeps_full_prefix_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """seed 携带 u_ 观测(manifest 扫描漏报兜底)→ 前缀全量装载,screen 不丢。"""
        manifest, snapshots = _signal_setup(
            "RR-issue470b-ufast0001", "issue470b-ufast"
        )
        provider = _closes_provider(n_days=100)

        factory = _factory_for(provider)

        async def snapshot_provider(snapshot_id: str) -> Any:
            return snapshots.get(snapshot_id)

        counters = _Counters(monkeypatch)
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        interrupted = await coordinator.execute(
            manifest,
            _InterruptAfterKSignalAdapter(
                k=1,
                manifest=manifest,
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            ),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        resumed_adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        resumed = await coordinator.execute(manifest, resumed_adapter)
        assert resumed.status is ResearchRunStatus.COMPLETED, resumed.error_summary
        # 门二生效:seed 含 u_ 观测 → 前缀走全量(stub 路径不启用),
        # factor_screen 照常覆盖全期次(3 期)。
        assert counters.load_stub == 0
        assert resumed_adapter._stub_prefix_periods == 0
        assert resumed.result is not None
        assert resumed.result.factor_screen is not None
        assert resumed.result.factor_screen["n_periods"] == 3


# ---------------------------------------------------------------------------
# Part D:PortfolioPipelineAdapter 归一化(两个装载来源结构兼容)
# ---------------------------------------------------------------------------


class TestResumeStubNormalization:
    async def test_prefetch_uses_stub_stream_when_provided(self) -> None:
        """提供 stub 流时 aprefetch 只消费它,主输入流零拉取。"""

        async def main_stream() -> Any:
            raise AssertionError("主输入流不得被 resume 预取消费")
            yield  # pragma: no cover

        lot_info = AssetLotInfo(code="600519.SH", lot_size=100, multiplier=1.0)
        stubs = [
            _ResumeReplayStub(
                business_date=DECISION_AT.date(),
                decision_at=DECISION_AT,
                lot_info={"600519.SH": lot_info},
                prices={"600519.SH": 1.0},
                execution_prices={"600519.SH": 1.0},
            ),
            _ResumeReplayStub(
                business_date=DECISION_AT_2.date(),
                decision_at=DECISION_AT_2,
                lot_info={"600519.SH": lot_info},
                prices={"600519.SH": 2.0},
                execution_prices={"600519.SH": 2.0},
            ),
        ]

        async def stub_stream() -> Any:
            for item in stubs:
                yield item

        pipeline = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=main_stream(),
            resume_stub_inputs=stub_stream(),
        )
        await pipeline.aprefetch_inputs(2)
        assert pipeline._prefetch_stubs == stubs
        await pipeline._close_input_stream()

    async def test_prefetch_without_stub_stream_keeps_legacy_behavior(self) -> None:
        """无 stub 流(旧调用方 / Sequence 语义)时仍从主输入流抽取五字段。"""

        class _Input:
            business_date = DECISION_AT.date()
            decision_at = DECISION_AT
            lot_info: ClassVar[dict[str, AssetLotInfo]] = {
                "600519.SH": AssetLotInfo(code="600519.SH")
            }
            prices: ClassVar[dict[str, float]] = {"600519.SH": 1.0}
            execution_prices: ClassVar[dict[str, float]] = {"600519.SH": 1.0}
            features: ClassVar[tuple[str, ...]] = ("重对象占位",)

        async def main_stream() -> Any:
            yield _Input()

        pipeline = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=main_stream()
        )
        await pipeline.aprefetch_inputs(1)
        stub = pipeline._prefetch_stubs[0]
        assert isinstance(stub, _ResumeReplayStub)
        assert stub.decision_at == DECISION_AT
        assert stub.prices == {"600519.SH": 1.0}
        assert not hasattr(stub, "features")
        await pipeline._close_input_stream()
