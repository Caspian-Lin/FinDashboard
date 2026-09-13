"""issue #463 下半场:决策产物落库后瘦身 —— 重对象驻留与期数解耦。

锁定五组不变量:

* 瘦身等值(逐字节):同一 manifest 走完整 coordinator 链路(含
  coordinator / 适配器两处瘦身)与「兼容包装全量物化 + 裸组合管线」对照,
  13 stage artifact 载荷 / checksum、report artifact、result_checksum 口径
  全部一致;#305 确定性重放 result_checksum 逐字节不变;
* coordinator 侧瘦身生效:``build_report`` 收到的决策列表(runner 主循环
  的 ``decisions`` 列表)每期 ``features == ()``,而 candidates / ledger /
  orders / fills / positions 与全量对照逐值一致;
* adapter 侧瘦身生效:``SignalEnginePipelineAdapter.decisions()`` 的
  ``collected`` 列表传入 ``build_daily_equity_curve`` 时已瘦身为轻副本,
  而 yield 出去的仍是完整 bundle(校验 / 持久化语义不变);
* 内存解耦:≥13 期合成 run 结束后,存活 ``FeatureValue`` 数量远低于
  「chunk 期数 x 每期对象数 x 3 倍冗余」阈值 —— 未瘦身形态必然钉住
  ``期数 x 每期对象数`` 全量,断言成立即证明驻留与期数解耦;
* 价格特征预计算表逐期释放:每期 context 构建完成后对应槽位置 None,
  ``feature_values`` 消费值不受影响(释放前后同值)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import gc
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finboard_backtest.research_run.signal_engine as signal_engine_module
from finboard_backtest.research_run import (
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    _slim_decision,
    pipeline_output_checksum,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.runner import (
    _DECISION_STAGES,
    _decision_stage_payloads,
    _stable_decision_suffix,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
    build_daily_equity_curve,
    build_decision_inputs,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

from .test_issue_306_load_probe import (
    _RELEASE_ID,
    _SYMBOLS,
    _build_release,
    _month_end_decisions,
    _noop_snapshot_provider,
)
from .test_issue_308_progress import _multi_period_manifest

pytestmark = pytest.mark.asyncio


def _factory(provider: FrozenReleaseProvider) -> Any:
    def _make(release_id: str) -> FrozenReleaseProvider:
        assert release_id == _RELEASE_ID
        return provider

    return _make


def _run_manifest(run_id: str, idempotency_key: str) -> ResearchRunManifest:
    """6 期 multi_period manifest(3 标的真实发布,风险贡献约束关闭)。"""
    return replace(
        _multi_period_manifest(),
        run_id=run_id,
        idempotency_key=idempotency_key,
        # 关闭风险贡献硬约束(=1 不构成约束):3 标的小池不因 #303 的 1/n
        # 可行性下限被拒,聚焦瘦身等值本身。
        portfolio_config={"overrides": {"max_risk_contribution": 1}},
    )


class _ReportDecisionsSpy(SignalEnginePipelineAdapter):
    """记录 coordinator 传入 build_report 的决策列表(瘦身观测点)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.report_decisions: list[list[Any]] = []

    def build_report(self, manifest: Any, decisions: Any) -> Any:
        self.report_decisions.append(list(decisions))
        return super().build_report(manifest, decisions)


async def _materialized_reference(
    manifest: ResearchRunManifest, provider: FrozenReleaseProvider
) -> tuple[list[Any], Any]:
    """对照路径:兼容包装全量物化 + 裸组合管线(不经任何瘦身)。"""
    inputs = await build_decision_inputs(
        manifest,
        release_provider_factory=_factory(provider),
        snapshot_provider=_noop_snapshot_provider,
    )
    pipeline = PortfolioPipelineAdapter(
        strategy_kind="multi_factor", decision_inputs=inputs
    )
    bundles = [item async for item in pipeline.decisions(manifest)]
    curve = await build_daily_equity_curve(provider, manifest, bundles)
    report = pipeline.build_report(
        manifest, bundles, equity_curve=curve, benchmark_curve=()
    )
    return bundles, report


class TestSlimmingEquivalence:
    async def test_artifacts_report_result_checksum_match_reference(
        self, tmp_path
    ) -> None:
        """瘦身 run 与全量对照:artifact 载荷 / report / result 逐字节一致,
        #305 确定性重放 result_checksum 不变。"""
        provider = await _build_release(tmp_path)
        manifest = _run_manifest("RR-issue463bslm0001", "issue463-b-slim-0001")
        store = InMemoryResearchRunStore()
        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=_factory(provider),
            snapshot_provider=_noop_snapshot_provider,
        )
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

        control_bundles, control_report = await _materialized_reference(
            manifest, provider
        )
        assert len(control_bundles) == len(_month_end_decisions()) == 6
        # 对照是全量 bundle(features 非空),证明参照未被瘦身污染。
        assert all(bundle.features for bundle in control_bundles)

        artifacts = await store.list_artifacts(manifest.run_id)
        # 13 stage x 6 决策 + report = 79 行。
        assert len(artifacts) == 13 * 6 + 1
        by_artifact_id = {item.artifact_id: item for item in artifacts}

        # 逐决策逐 stage:对照 bundle 与实际落库 artifact 的载荷/校验和一致
        # (瘦身为逐字节零影响 —— persist 发生在瘦身之前,这里验证的正是
        # 「瘦身后的 coordinator 跑出的 artifact 流」与全量对照等值)。
        for index, bundle in enumerate(control_bundles):
            payloads = _decision_stage_payloads(bundle)
            for stage in _DECISION_STAGES:
                expected_payload = to_json_value(
                    {
                        "business_date": bundle.business_date,
                        "decision_at": bundle.decision_at,
                        **payloads[stage],
                    }
                )
                artifact_id = f"{manifest.run_id}:A:{index:08d}:{stage.value}"
                actual = by_artifact_id[artifact_id]
                assert actual.stage is stage
                assert actual.payload == expected_payload
                assert actual.checksum == stable_checksum(expected_payload)

        # report artifact:对照报告与落库载荷逐字节一致(瘦身决策列表构建
        # 的 report 与全量对照构建的 report 无差异)。
        expected_report_payload = to_json_value({"report": control_report})
        report_artifact = by_artifact_id[f"{manifest.run_id}:A:report"]
        assert report_artifact.payload == expected_report_payload
        assert report_artifact.checksum == stable_checksum(expected_report_payload)

        # result_checksum 口径:artifact 行指纹,与全量对照可复算的口径一致。
        mirrored = stable_checksum(
            [
                {
                    "stage": item.stage.value,
                    "decision_id": _stable_decision_suffix(item.decision_id),
                    "checksum": item.checksum,
                }
                for item in artifacts
            ]
        )
        assert record.result_checksum == mirrored

        # #305 确定性重放:同 manifest 新适配器重放,result_checksum 逐字节
        # 不变(瘦身不改变任何进入 checksum 的内容)。
        replay = await ResearchRunCoordinator(store).replay(
            source_run_id=manifest.run_id,
            new_run_id="RR-issue463bslm0002",
            idempotency_key="issue463-b-slim-0002",
            requested_by="unit-test",
            adapter=SignalEnginePipelineAdapter(
                manifest=replace(
                    manifest, run_id="RR-issue463bslm0002"
                ),
                release_provider_factory=_factory(provider),
                snapshot_provider=_noop_snapshot_provider,
            ),
        )
        assert replay.status is ResearchRunStatus.COMPLETED, replay.error_summary
        assert replay.result_checksum == record.result_checksum

    async def test_coordinator_decision_list_is_slimmed_after_persist(
        self, tmp_path
    ) -> None:
        """coordinator 侧:build_report 收到的决策列表每期 features == (),
        其余字段与全量对照逐值一致(candidates 保留)。"""
        provider = await _build_release(tmp_path)
        manifest = _run_manifest("RR-issue463bslm0003", "issue463-b-slim-0003")
        spy = _ReportDecisionsSpy(
            manifest=manifest,
            release_provider_factory=_factory(provider),
            snapshot_provider=_noop_snapshot_provider,
        )
        record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
            manifest, spy
        )
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

        control_bundles, _ = await _materialized_reference(manifest, provider)
        assert len(spy.report_decisions) == 1
        slimmed = spy.report_decisions[0]
        assert len(slimmed) == len(control_bundles) == 6
        for index, (actual, expected) in enumerate(
            zip(slimmed, control_bundles, strict=True)
        ):
            # 瘦身生效:重字段置空。
            assert actual.features == ()
            # 轻字段与全量对照逐值一致(报告 / 校验的消费面)。
            assert actual.candidates == expected.candidates
            assert actual.ledger == expected.ledger
            assert actual.orders == expected.orders
            assert actual.fills == expected.fills
            assert actual.positions == expected.positions
            assert actual.constraints == expected.constraints
            assert actual.business_date == expected.business_date
            # decision_id 由 coordinator 按期序绑定(对照裸管线未绑定)。
            assert actual.decision_id == f"{manifest.run_id}:D:{index:08d}"

    async def test_adapter_collected_list_is_slimmed_but_yield_is_full(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """adapter 侧:``collected`` 传入 equity 曲线构建前已瘦身;yield 出
        去的仍是完整 bundle。"""
        provider = await _build_release(tmp_path)
        manifest = _run_manifest("RR-issue463bslm0004", "issue463-b-slim-0004")
        real_curve = signal_engine_module.build_daily_equity_curve
        captured: list[list[Any]] = []

        async def spy_curve(
            curve_provider: Any, curve_manifest: Any, decisions: Any
        ) -> Any:
            captured.append(list(decisions))
            return await real_curve(curve_provider, curve_manifest, decisions)

        monkeypatch.setattr(signal_engine_module, "build_daily_equity_curve", spy_curve)

        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=_factory(provider),
            snapshot_provider=_noop_snapshot_provider,
        )
        yielded = [item async for item in adapter.decisions(manifest)]
        assert len(yielded) == 6
        # yield 出去的仍是完整 bundle(coordinator 校验 / 持久化的输入)。
        assert all(item.features for item in yielded)
        assert captured
        assert len(captured[0]) == 6
        for collected_item, full in zip(captured[0], yielded, strict=True):
            assert collected_item.features == ()
            assert collected_item.candidates == full.candidates
            assert collected_item.ledger is full.ledger
            assert collected_item.fills is full.fills


class TestSlimDecisionPurity:
    async def test_slim_copy_does_not_mutate_original(
        self, decision_factory, manifest_factory
    ) -> None:
        """``_slim_decision`` 产出独立副本:原 bundle 不被突变,输出校验和
        不变(证据 / 重放语义零影响)。"""
        manifest = manifest_factory()
        bundle = decision_factory(manifest=manifest)
        assert bundle.features

        slimmed = _slim_decision(bundle)
        assert slimmed.features == ()
        assert bundle.features  # 原 bundle 完好
        assert slimmed.candidates == bundle.candidates
        assert slimmed.ledger is bundle.ledger
        assert slimmed.decision_id == bundle.decision_id
        # 管线证据的 output_checksum 不含 features → 瘦身副本校验和一致。
        evidence = bundle.pipeline_evidence
        assert evidence is not None
        assert slimmed.pipeline_evidence is not None
        assert slimmed.pipeline_evidence.output_checksum == evidence.output_checksum
        assert pipeline_output_checksum(slimmed) == pipeline_output_checksum(bundle)


# ---------------------------------------------------------------------------
# 内存解耦:≥13 期合成 run,存活 FeatureValue 与期数解耦
# ---------------------------------------------------------------------------

#: 15 个月 x 每月 22 个交易日 → ~14 个月末决策期(≥13,使「未瘦身穿梭」
#: 的全量驻留 期数xK 严格大于阈值 12K,断言才有判别力)。
_MEMORY_SESSION_MONTHS = 15
_MEMORY_SYMBOLS = tuple(f"6001{n:02d}.SH" for n in range(18))
_MEMORY_RELEASE_ID = "shedding-463-r1"


def _memory_sessions() -> list[date]:
    total = 22 * _MEMORY_SESSION_MONTHS
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < total:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _memory_close(code: str, day: date, sessions: list[date]) -> Decimal:
    base = Decimal("10.00") + Decimal(int(code[4:6]))
    step = sessions.index(day)
    wiggle = Decimal("1.01") if step % 3 else Decimal("0.99")
    return base * (Decimal(1) + Decimal(step) / Decimal(300)) * wiggle


async def _build_memory_release(tmp_path: Path) -> FrozenReleaseProvider:
    sessions = _memory_sessions()
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [
        ReleaseInstrumentSpec(
            code=code,
            name=f"样本{code}",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            asset_class=AssetClass.EQUITY,
            available_at=datetime(2001, 8, 27, tzinfo=UTC),
            execution=default_execution_metadata(
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            ),
            list_date=date(2001, 8, 27),
        )
        for code in _MEMORY_SYMBOLS
    ]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        symbol = Symbol(code=instrument.code, market=instrument.market)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                open=_memory_close(instrument.code, day, sessions) * Decimal("1.01"),
                high=_memory_close(instrument.code, day, sessions) * Decimal("1.02"),
                low=_memory_close(instrument.code, day, sessions) * Decimal("0.98"),
                close=_memory_close(instrument.code, day, sessions),
                volume=Decimal(10000),
                amount=Decimal("100000"),
                source="fixed_sample",
            )
            for day in sessions
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_MEMORY_RELEASE_ID,
            dataset_name="shedding_463_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=sessions[0],
            end_date=sessions[-1],
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    return FrozenReleaseProvider(
        release_root=release_root, release_id=_MEMORY_RELEASE_ID
    )


def _memory_spec() -> Any:
    """价格因子规格(与 test_issue_306_load_probe 同形,绑定本测试发布)。"""
    from finboard_backtest.strategy_spec import build_strategy_template
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        FeatureKind,
        FeatureNode,
        FeatureOperator,
        SignalAction,
        SignalComparator,
        SignalRule,
        SignalRules,
    )

    spec = build_strategy_template(
        "multi_factor",
        strategy_id="shedding_463_test",
        dataset_release_ids=(_MEMORY_RELEASE_ID,),
    )
    nodes = (
        FeatureNode(
            node_id="momentum",
            label="动量",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="momentum",
        ),
        FeatureNode(
            node_id="volatility",
            label="波动率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="volatility_20d",
        ),
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("momentum",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="momentum",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="动量前 50% 纳入目标仓位。",
                    ),
                )
            ),
        }
    )


def _memory_manifest() -> ResearchRunManifest:
    spec = _memory_spec()
    return ResearchRunManifest(
        run_id="RR-issue463mem00001",
        idempotency_key="issue463-mem-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_MEMORY_RELEASE_ID,
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
        portfolio_config={"overrides": {"max_risk_contribution": 1}},
    )


class TestPrecomputePeriodRelease:
    async def test_release_period_drops_slots_without_touching_consumed_values(
        self, tmp_path
    ) -> None:
        """价格特征预计算表逐期释放:每期 context 构建完成后槽位置 None,
        释放前的消费值不受影响(独立对象,值语义不变)。"""
        from finboard_backtest.research_run.frozen_loader import (
            build_price_feature_precompute,
        )

        provider = await _build_release(tmp_path)
        decision_days = _month_end_decisions()
        decision_ats = tuple(
            datetime.combine(day, time(15, 0), tzinfo=UTC) for day in decision_days
        )
        precompute = await build_price_feature_precompute(
            histories={},
            provider=provider,
            decision_ats=decision_ats,
            symbols=_SYMBOLS,
        )
        first_period = decision_ats[0]
        consumed = precompute.feature_values(first_period, _RELEASE_ID)
        assert consumed  # 预计算表可消费

        released = precompute.release_period(first_period)
        assert released > 0
        # 重复释放 / 未知决策期为 no-op。
        assert precompute.release_period(first_period) == 0
        assert precompute.release_period(datetime(1990, 1, 1, tzinfo=UTC)) == 0
        # 2026-09-13 内存优化:常驻形态改为每标的紧凑矩阵(≈26KB/标的),
        # 释放不再置 None 槽位,而是登记已释放期次;已构造的 FeatureValue
        # 不受影响(独立对象),复读由 feature_values 返回 None。
        assert len(consumed) > 0
        # 释放期复读得到空集(现消费审计范围内不存在复读;值语义兜底为
        # 快照重算路径)。
        assert precompute.feature_values(first_period, _RELEASE_ID) in (None, ())


class TestMemoryDecoupling:
    @pytest.mark.timeout(600)
    async def test_live_feature_values_decoupled_from_period_count(
        self, tmp_path
    ) -> None:
        """≥13 期 run 结束后存活 FeatureValue < chunk 期数 x 每期对象数 x 3。

        算式:阈值 = ``_DECISION_LOAD_CHUNK(4) x K x 3 = 12K``(K = 每期
        FeatureValue 数,取自落库 features artifact);未瘦身形态必然钉住
        ``期数 n x K`` 全量(n ≥ 13 → nK > 12K,断言必失败),断言通过即
        证明驻留与期数解耦。"""
        from finboard_backtest.research_run.signal_engine import (
            _DECISION_LOAD_CHUNK,
        )

        provider = await _build_memory_release(tmp_path)
        manifest = _memory_manifest()
        store = InMemoryResearchRunStore()
        adapter = SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: provider,
            snapshot_provider=_noop_snapshot_provider,
        )
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary

        artifacts = await store.list_artifacts(manifest.run_id)
        feature_rows = [item for item in artifacts if item.stage.value == "features"]
        n_periods = len(feature_rows)
        # 期数判别力:全部期次的 FeatureValue 总量必须严格大于阈值,否则
        # 「未瘦身全量驻留」形态也能通过断言,失去判别力(早期期次历史短,
        # 每期对象数不等,取 K_max 定阈值、按总量做判别力断言)。
        per_period_counts: list[int] = []
        for item in feature_rows:
            features_payload = item.payload["features"]
            assert isinstance(features_payload, list)
            per_period_counts.append(len(features_payload))
        total_created = sum(per_period_counts)
        k_max = max(per_period_counts)
        assert n_periods >= 13
        assert k_max > 0
        threshold = _DECISION_LOAD_CHUNK * k_max * 3  # = 12K
        assert total_created > threshold, "合成样本判别力不足:全量驻留未超阈值"

        gc.collect()

        def _live_count() -> int:
            return sum(
                1
                for obj in gc.get_objects()
                if type(obj) is FeatureValue
                and obj.source_artifact_ids == (_MEMORY_RELEASE_ID,)
            )

        live = _live_count()
        assert live < threshold, (
            f"存活 FeatureValue={live} 应 < 阈值 {threshold}"
            f"(chunk={_DECISION_LOAD_CHUNK}, K_max={k_max}, 期数={n_periods},"
            f"全期总量={total_created});驻留未与期数解耦"
        )
        # 度量自身有效性对照:人为钉住 threshold+1 个同源 FeatureValue 后,
        # 同一计数必然同步增长 —— 证明断言对「未释放」形态有判别力,非恒真。
        probe_time = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)
        pinned = [
            FeatureValue(
                symbol="600100.SH",
                feature_id=f"probe_{index}",
                value=float(index),
                source_artifact_ids=(_MEMORY_RELEASE_ID,),
                available_at=probe_time,
            )
            for index in range(threshold + 1)
        ]
        assert _live_count() >= threshold + 1
        del pinned
        gc.collect()
