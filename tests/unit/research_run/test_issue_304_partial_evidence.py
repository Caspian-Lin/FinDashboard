"""组合阶段拒绝保留 factor_screen 部分证据(issue #304)。

组合阶段硬约束失败(如候选 2 只时 ``max_risk_contribution`` 数学不可行)使
run REJECTED 时,不依赖组合阶段的 factor_screen / strategy_screen 证据(只
消费已构建的冻结决策输入)不再陪葬:

* Coordinator 在 ``ResearchConstraintViolationError`` 分支尽力补算 screen、
  落库 report artifact / result(标注 ``partial`` + ``constraint_failure``),
  终态保持 REJECTED / ``hard_constraint_rejected`` 不变;
* 信号引擎 / user_code 适配器经 ``compute_partial_evidence`` 暴露同一补算;
* 成功路径不注入任何新键(report artifact payload / result 序列化零漂移);
* 补算本身失败不掩盖原硬约束错误。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from finboard_backtest.research_run import (
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchConstraintViolationError,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    FrozenArtifactRef,
    JsonValue,
    ResearchRunManifest,
    ResearchRunReport,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
)
from finboard_backtest.research_run.user_code_engine import (
    UserCodeStrategyAdapter,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import (
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
)

from .conftest import fixed_report
from .test_signal_engine import (
    _obs,
    _StubInstrument,
    _StubProvider,
    _StubRelease,
    _StubSnapshot,
)

pytestmark = pytest.mark.asyncio

SYMBOLS = ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")
DECISION_AT = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
DECISION_AT_2 = datetime(2024, 2, 1, 15, 0, tzinfo=UTC)
U_FACTOR = "u_agent_alpha"


def _closes(*, n_days: int = 30) -> dict[str, dict[date, Decimal]]:
    """固定 seed 随机游走,样本协方差满秩(风险贡献约束可评估)。"""
    days = [date(2023, 12, 14) + timedelta(days=i) for i in range(n_days)]
    rng = np.random.default_rng(20240101)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in SYMBOLS:
        price = 10.0
        closes[symbol] = {}
        for day in days:
            price *= 1.0 + float(rng.normal(0.001, 0.01))
            closes[symbol][day] = Decimal(str(round(price, 4)))
    return closes


def _provider(closes: dict[str, dict[date, Decimal]]) -> _StubProvider:
    return _StubProvider(
        release=_StubRelease(
            "release-v1", tuple(_StubInstrument(code=s) for s in SYMBOLS)
        ),
        closes_by_symbol=closes,
    )


def _snapshot(decision_at: datetime, *, alpha_seed: float = 6.0) -> _StubSnapshot:
    at = datetime(2023, 12, 29, tzinfo=UTC)
    observations = []
    for index, symbol in enumerate(SYMBOLS):
        observations.append(_obs(symbol, "pb", 1.0 + index * 0.1, available_at=at))
        observations.append(
            _obs(symbol, "momentum", 0.05 * index, available_at=at)
        )
        observations.append(
            _obs(symbol, "volatility_20d", 0.3 - 0.03 * index, available_at=at)
        )
        observations.append(
            _obs(symbol, U_FACTOR, alpha_seed - index, available_at=at)
        )
    return _StubSnapshot(
        snapshot_id="factor-v1", decision_at=decision_at, observations=tuple(observations)
    )


def _spec(*, rank_threshold: float = 0.2) -> Any:
    """multi_factor 规格;默认模板 rank_top 0.2 → 2 只多头 → 风险贡献
    1/2=0.5 > 0.35 数学不可行(issue #304 的真实失败形态)。"""
    return build_strategy_template(
        "multi_factor", strategy_id="issue304_spec", dataset_release_ids=("release-v1",)
    ).model_copy(
        update={
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=rank_threshold,
                        action=SignalAction.BUY,
                        rationale="复合得分头部纳入目标仓位。",
                    ),
                    SignalRule(
                        rule_id="bottom_score_sell",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_BOTTOM,
                        threshold=0.5,
                        action=SignalAction.SELL,
                        priority=10,
                        rationale="复合得分尾部退出。",
                    ),
                ),
                conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
            ),
        }
    )


def _manifest(
    spec: Any,
    *,
    run_id: str = "RR-issue304unit0001",
    idempotency_key: str = "issue304-unit",
    snapshot_ids: tuple[str, ...] = ("factor-v1",),
) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id,
        idempotency_key=idempotency_key,
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-v1",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=tuple(
            FrozenArtifactRef(
                artifact_id=snapshot_id,
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:momentum",),
            )
            for snapshot_id in snapshot_ids
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _adapter(
    manifest: ResearchRunManifest,
    provider: _StubProvider,
    snapshots: dict[str, _StubSnapshot],
) -> SignalEnginePipelineAdapter:
    def release_factory(release_id: str) -> _StubProvider:
        assert release_id == "release-v1"
        return provider

    async def snapshot_provider(snapshot_id: str) -> _StubSnapshot | None:
        return snapshots.get(snapshot_id)

    return SignalEnginePipelineAdapter(
        manifest=manifest,
        release_provider_factory=release_factory,  # type: ignore[arg-type]
        snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 真实信号引擎:首期即硬约束失败 → REJECTED + partial factor_screen
# ---------------------------------------------------------------------------


class TestSignalEnginePartialEvidence:
    async def test_first_period_hard_constraint_rejection_keeps_factor_screen(
        self, manifest_factory
    ) -> None:
        """验收:首期即硬约束失败的 run 仍有 factor_screen artifact 与
        result.factor_screen(标注 partial),终态 REJECTED 语义不变。"""
        manifest = replace(
            manifest_factory("multi_factor"),
            strategy_spec=_spec(),
            strategy_spec_checksum=stable_checksum(_spec().canonical_payload()),
        )
        adapter = _adapter(
            manifest, _closes_provider(), {"factor-v1": _snapshot(DECISION_AT)}
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)

        # 终态 / error_code 零漂移(#91 fail-closed 不动)
        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"
        assert "风险贡献" in (record.error_summary or "")

        artifacts = await store.list_artifacts(manifest.run_id)
        # 首期失败:无 decision artifact,只有 report artifact(#304 前 = 0)
        assert [item.stage.value for item in artifacts] == ["report"]
        report_payload = artifacts[0].payload["report"]
        assert isinstance(report_payload, dict)
        # partial 标记 + 失败决策定位(report JSON 具名键)
        assert report_payload["partial"] is True
        failure = report_payload["constraint_failure"]
        assert isinstance(failure, dict)
        assert failure["completed_decisions"] == 0
        assert failure["failed_decision_index"] == 1
        assert failure["decision_date"] == DECISION_AT.date().isoformat()
        # factor_screen 证据与成功路径同构(只依赖前缀捕获的冻结输入投影,
        # 与组合阶段无关;#463 前缀口径 —— 首期失败时前缀 = 该期)
        screen = report_payload["factor_screen"]
        assert isinstance(screen, dict)
        raw_factors = screen["factors"]
        assert isinstance(raw_factors, dict)
        metrics = raw_factors[U_FACTOR]
        assert isinstance(metrics, dict)
        assert metrics["rank_ic"] is not None
        assert screen["n_periods"] == 1
        # result(report 对象)携带同一份 screen
        assert record.result is not None
        assert record.result.factor_screen is not None
        result_factors = record.result.factor_screen["factors"]
        assert isinstance(result_factors, dict)
        assert result_factors[U_FACTOR] == metrics

    async def test_partial_hook_reports_mid_run_position(self) -> None:
        """验收(中期失败定位):hook 按 completed_decisions 给出 1-based
        失败期序号与同序号前缀捕获的决策日。"""
        spec = _spec(rank_threshold=0.5)  # 阈值放宽,决策本身可构建
        snapshots = {
            "factor-v1": _snapshot(DECISION_AT),
            "factor-v2": _snapshot(DECISION_AT_2, alpha_seed=3.0),
        }
        manifest = _manifest(
            spec,
            run_id="RR-issue304mid000001",
            idempotency_key="issue304-mid",
            snapshot_ids=("factor-v1", "factor-v2"),
        )
        adapter = _adapter(
            manifest, _closes_provider(n_days=60), snapshots
        )
        # issue #463:输入不再由 _load 预物化,改为逐期拉取时捕获 ——
        # 驱动决策循环产出全部期次,等价旧「全部冻结输入已就绪」。
        decisions = [item async for item in adapter.decisions(manifest)]
        assert len(decisions) == 2
        assert len(adapter._business_dates) == 2

        marker = await adapter.compute_partial_evidence(manifest, completed_decisions=1)

        assert marker is not None
        assert marker["completed_decisions"] == 1
        assert marker["failed_decision_index"] == 2
        assert marker["decision_date"] == DECISION_AT_2.date().isoformat()
        # issue #463 前缀口径:run 引用 u_ 用户因子(screen 有真实产出),
        # marker.warnings 具名标注 screen 只覆盖已捕获的 0..N 期 —— 这里
        # 期次被完整驱动,前缀恰好等于全期次,但口径标注照常携带。
        warnings = marker.get("warnings")
        assert isinstance(warnings, list)
        prefix_scopes = [
            item
            for item in warnings
            if isinstance(item, dict) and item.get("factor_screen_prefix_scope")
        ]
        assert len(prefix_scopes) == 1
        assert prefix_scopes[0]["screen_periods"] == 2
        # screen 已按前缀捕获补算暂存,build_report 照常携带
        report = adapter.build_report(manifest, ())
        assert report.factor_screen is not None
        # n_periods 是前缀捕获期数(本用例完整驱动,前缀 = 全部 2 期)
        assert report.factor_screen["n_periods"] == 2

    async def test_partial_hook_reports_screen_computation_warning(self) -> None:
        """验收(补算失败):screen 计算抛错不掩盖原硬约束错误,标记携带
        具名 warning,build_report 无 factor_screen。"""
        manifest = _manifest(_spec(rank_threshold=0.5))
        adapter = _adapter(manifest, _closes_provider(), {"factor-v1": _snapshot(DECISION_AT)})
        # issue #463:输入改为逐期拉取即捕获 —— 只拉一期(不产出决策、
        # 不触发决策循环末尾的 screen 计算),等价旧「_load 后立即补算」。
        await adapter._load()
        assert adapter._input_iterator is not None
        item = await adapter._input_iterator.__anext__()
        assert item is not None
        assert len(adapter._business_dates) == 1

        def broken_factory(release_id: str) -> _StubProvider:
            raise RuntimeError(f"release io exploded: {release_id}")

        adapter._release_provider_factory = broken_factory  # type: ignore[assignment]

        marker = await adapter.compute_partial_evidence(manifest, completed_decisions=0)

        assert marker is not None
        warnings = marker.get("warnings")
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert str(warnings[0]).startswith("factor_screen_computation_failed")
        report = adapter.build_report(manifest, ())
        assert report.factor_screen is None


def _closes_provider(*, n_days: int = 30) -> _StubProvider:
    return _provider(_closes(n_days=n_days))


# ---------------------------------------------------------------------------
# Coordinator 接线:中期失败 / 补算失败 / 成功路径零漂移
# ---------------------------------------------------------------------------


class _GeneratorRejectAdapter:
    """产出 1 条合法决策后在下一期抛组合硬约束错误的 stub 适配器。"""

    strategy_kind = "multi_factor"

    def __init__(
        self,
        decision: DecisionBundle,
        report: ResearchRunReport,
        *,
        hook: Callable[
            ..., Awaitable[dict[str, JsonValue] | None]
        ]
        | None = None,
    ) -> None:
        self._decision = decision
        self._report = report
        self._hook = hook

    def validate_manifest(self, manifest: ResearchRunManifest) -> None:
        del manifest

    async def decisions(self, manifest: ResearchRunManifest):
        del manifest
        yield self._decision
        raise ResearchConstraintViolationError(
            "组合硬约束执行后仍未通过: ['max_risk_contribution']"
        )

    def build_report(
        self, manifest: ResearchRunManifest, decisions: Sequence[DecisionBundle]
    ) -> ResearchRunReport:
        del manifest
        if not decisions:
            raise AssertionError("partial 路径必须携带已完成决策")
        return self._report

    async def compute_partial_evidence(
        self, manifest: ResearchRunManifest, *, completed_decisions: int
    ) -> dict[str, JsonValue] | None:
        if self._hook is None:
            raise AssertionError("hook 不应在无标记适配器上被调用")
        return await self._hook(manifest, completed_decisions=completed_decisions)


class TestCoordinatorPartialWiring:
    async def test_mid_run_rejection_keeps_completed_artifacts_and_marker(
        self, manifest_factory, decision_factory
    ) -> None:
        """前 i-1 期 artifact 自然保留;report/result 标注失败决策定位。"""
        manifest = manifest_factory("multi_factor")
        first = decision_factory(manifest=manifest)
        captured: dict[str, Any] = {}

        async def hook(manifest: ResearchRunManifest, *, completed_decisions: int):
            captured["completed_decisions"] = completed_decisions
            return {
                "completed_decisions": completed_decisions,
                "failed_decision_index": completed_decisions + 1,
                "decision_date": "2024-01-03",
            }

        adapter = _GeneratorRejectAdapter(
            first,
            fixed_report("multi_factor", first),
            hook=hook,
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)

        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"
        assert captured["completed_decisions"] == 1

        artifacts = await store.list_artifacts(manifest.run_id)
        stages = [item.stage.value for item in artifacts]
        # 期 1 的 13 个 stage artifact + report
        assert stages[:-1] == [
            "universe",
            "features",
            "signals",
            "targets_before_constraints",
            "constraints",
            "targets_after_constraints",
            "risk_exits",
            "targets_after_risk",
            "capital_feasibility",
            "rebalance_plan",
            "orders",
            "fills",
            "ledger",
        ]
        assert stages[-1] == "report"
        report_payload = artifacts[-1].payload["report"]
        assert isinstance(report_payload, dict)
        assert report_payload["partial"] is True
        failure = report_payload["constraint_failure"]
        assert isinstance(failure, dict)
        assert failure["completed_decisions"] == 1
        assert failure["failed_decision_index"] == 2

    async def test_salvage_failure_preserves_original_rejection(
        self, manifest_factory, decision_factory
    ) -> None:
        """补算 / 落库失败不掩盖原硬约束错误:REJECTED 原语义照旧。"""
        manifest = manifest_factory("multi_factor")
        first = decision_factory(manifest=manifest)

        async def broken_hook(manifest: ResearchRunManifest, *, completed_decisions: int):
            del manifest, completed_decisions
            raise RuntimeError("screen salvage exploded")

        adapter = _GeneratorRejectAdapter(
            first, fixed_report("multi_factor", first), hook=broken_hook
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)

        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"
        assert "组合硬约束" in (record.error_summary or "")
        # 补算失败 → 无 partial report artifact(期 1 决策 artifact 仍在)
        artifacts = await store.list_artifacts(manifest.run_id)
        assert [item.stage.value for item in artifacts].count("report") == 0

    async def test_adapter_without_hook_keeps_legacy_zero_artifact(
        self, manifest_factory, decision_factory
    ) -> None:
        """无 compute_partial_evidence 的适配器(DecisionSequenceAdapter)行为
        与既有 REJECTED 路径完全一致:零 artifact、零 result。"""
        from finboard_backtest.research_run.contracts import ConstraintOutcome

        manifest = manifest_factory()
        original = decision_factory(manifest=manifest)
        rejected = replace(
            original,
            constraints=(
                ConstraintOutcome(
                    constraint="cash_buffer",
                    passed=False,
                    before_value=0.0,
                    after_value=1.0,
                    limit=0.05,
                    reason="现金缓冲不足",
                ),
            ),
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross",
                decisions=(rejected,),
                report=fixed_report("ma_cross", rejected),
            ),
        )

        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"
        assert await store.list_artifacts(manifest.run_id) == []
        assert record.result is None

    async def test_success_path_report_payload_has_no_partial_keys(
        self, manifest_factory, decision_factory
    ) -> None:
        """成功路径零漂移:report artifact payload 不含 partial 相关键。"""
        manifest = manifest_factory()
        decision = decision_factory(manifest=manifest)
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(
            manifest,
            DecisionSequenceAdapter(
                strategy_kind="ma_cross",
                decisions=(decision,),
                report=fixed_report("ma_cross", decision),
            ),
        )

        assert record.status is ResearchRunStatus.COMPLETED
        artifacts = await store.list_artifacts(manifest.run_id)
        report_payload = artifacts[-1].payload["report"]
        assert isinstance(report_payload, dict)
        assert "partial" not in report_payload
        assert "constraint_failure" not in report_payload


# ---------------------------------------------------------------------------
# user_code 同构:partial hook 补算 strategy_screen
# ---------------------------------------------------------------------------


class TestUserCodePartialEvidence:
    async def test_user_code_partial_strategy_screen(self, manifest_factory) -> None:
        from types import SimpleNamespace

        from finboard_backtest.portfolio.contracts import AssetLotInfo
        from finboard_backtest.research_run.factor_screen import (
            QUANTILES,
            _period_cross_section,
        )
        from finboard_backtest.research_run.portfolio_pipeline import (
            PortfolioDecisionInput,
        )

        manifest = manifest_factory("user_code")
        adapter = UserCodeStrategyAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: _provider(_closes()),  # type: ignore[arg-type]
            snapshot_provider=None,  # type: ignore[arg-type]
        )
        # 直接注入加载产物(等价于 _ensure_loaded 完成后、逐决策已收集输入);
        # 失败期次(第 2 期)的 screen_inputs / target_weights 已在 build 前收集。
        decision_at = DECISION_AT
        symbols = SYMBOLS

        def _input(at: datetime, seed: float) -> PortfolioDecisionInput:
            from finboard_backtest.research_run.contracts import (
                FeatureValue,
                NormalizedSignal,
                UniverseCandidate,
            )

            features = tuple(
                FeatureValue(
                    symbol=s,
                    feature_id=U_FACTOR,
                    value=seed - index,
                    source_artifact_ids=("release-v1",),
                    available_at=at,
                )
                for index, s in enumerate(symbols)
            )
            return PortfolioDecisionInput(
                business_date=at.date(),
                decision_at=at,
                execution_at=at.replace(hour=16),
                candidates=tuple(
                    UniverseCandidate(s, True, ("ok",), "equity", "a_share")
                    for s in symbols
                ),
                features=features,
                signals=tuple(
                    NormalizedSignal(s, 1.0, "buy", "user_code:decide", rationale="t")
                    for s in symbols
                ),
                prices=dict.fromkeys(symbols, 10.0),
                execution_prices=dict.fromkeys(symbols, 10.0),
                lot_info={s: AssetLotInfo(code=s) for s in symbols},
                input_artifact_ids=("release-v1",),
            )

        # issue #463:直接注入逐期捕获产物(等价于决策循环对前两期的捕获:
        # 失败期次(第 2 期)的投影 / 决策日 / target_weights 均在 build 前
        # 捕获),不再驻留全量上下文 / 输入。
        adapter._context_dates = [decision_at.date(), DECISION_AT_2.date()]
        adapter._screen_periods = [
            _period_cross_section(_input(decision_at, 6.0)),
            _period_cross_section(_input(DECISION_AT_2, 3.0)),
        ]
        adapter._target_weights = [
            {symbols[0]: 0.5, symbols[1]: 0.5},
            {symbols[-1]: 0.4, symbols[-2]: 0.4},
        ]
        adapter._sandbox = SimpleNamespace(  # type: ignore[assignment]
            provenance_header=lambda **kw: {
                "code_checksum": "x" * 64,
                "artifact_dir": "/tmp",
                "resource_limits": {},
            },
            commit="c" * 40,
            image="img",
            image_digest="sha256:" + "0" * 64,
        )

        marker = await adapter.compute_partial_evidence(
            manifest, completed_decisions=1
        )

        assert marker is not None
        assert marker["completed_decisions"] == 1
        assert marker["failed_decision_index"] == 2
        assert marker["decision_date"] == DECISION_AT_2.date().isoformat()
        screen = adapter._strategy_screen
        assert isinstance(screen, dict)
        assert screen["n_periods"] == 2
        strategy = screen["strategy"]
        assert strategy["origin"] == "user_code"
        # 展示层指标:IC/分层/换手结构齐全(数值不构成治理判定)
        assert len(strategy["quantile_returns"]) == QUANTILES

        report = adapter.build_report(manifest, ())
        assert report.strategy_screen is screen
        assert report.sandbox_provenance is not None

    async def test_user_code_hook_without_loaded_context_returns_none(
        self, manifest_factory
    ) -> None:
        manifest = manifest_factory("user_code")
        adapter = UserCodeStrategyAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: _provider(_closes()),  # type: ignore[arg-type]
            snapshot_provider=None,  # type: ignore[arg-type]
        )
        assert (
            await adapter.compute_partial_evidence(manifest, completed_decisions=0)
            is None
        )


__all__ = [
    "TestCoordinatorPartialWiring",
    "TestSignalEnginePartialEvidence",
    "TestUserCodePartialEvidence",
]
