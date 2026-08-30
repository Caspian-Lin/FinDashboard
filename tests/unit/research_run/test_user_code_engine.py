"""``user_code`` 引擎的纯函数层测试(issue #218):权重→信号映射、约束回显。"""

from __future__ import annotations

from datetime import date

from finboard_backtest.research_run.user_code_engine import (
    _constraints_echo,
    targets_to_signals,
)


class TestTargetsToSignals:
    def test_all_signalable_symbols_get_signal(self) -> None:
        signals = targets_to_signals(
            {"a": 0.3, "b": 0.0}, frozenset({"a", "b", "c"}), decision_at=date(2024, 6, 3)
        )
        by_symbol = {s.symbol: s for s in signals}
        assert set(by_symbol) == {"a", "b", "c"}
        assert by_symbol["a"].score == 0.3
        assert by_symbol["a"].action == "buy"
        assert by_symbol["b"].score == 0.0  # 未提及 = 0 → 清仓/观望
        assert by_symbol["c"].score == 0.0
        assert by_symbol["c"].action == "neutral"
        assert all(s.rule_id == "user_code:decide" for s in signals)
        assert all(s.rationale for s in signals)

    def test_outside_symbols_dropped(self) -> None:
        """池外 / 缺价格执行元数据的标的被丢弃(不进信号,记 warning)。"""
        signals = targets_to_signals(
            {"a": 0.3, "zzz": 0.4}, frozenset({"a"}), decision_at=date(2024, 6, 3)
        )
        assert {s.symbol for s in signals} == {"a"}
        assert signals[0].score == 0.3

    def test_negative_passes_through_for_pipeline_audit(self) -> None:
        """负权重不在映射层截断 —— 交由组合管线按 long_only 截断并审计。"""
        signals = targets_to_signals(
            {"a": -0.2}, frozenset({"a"}), decision_at=date(2024, 6, 3)
        )
        assert signals[0].score == -0.2


class TestConstraintsEcho:
    def test_echo_shape_from_manifest(self, tmp_path) -> None:
        from decimal import Decimal

        from finboard_backtest.research_run.contracts import (
            FrozenArtifactRef,
            ResearchRunManifest,
            stable_checksum,
        )
        from finboard_backtest.strategy_spec import build_strategy_template
        from finboard_backtest.strategy_spec.contracts import (
            FeatureGraph,
            SignalRules,
            StrategyCodeArtifactRef,
        )

        template = build_strategy_template(
            "multi_factor", strategy_id="t", dataset_release_ids=("r",)
        )
        spec = template.model_copy(
            update={
                "strategy_kind": "user_code",
                "feature_graph": FeatureGraph(nodes=(), outputs=()),
                "signal_rules": SignalRules(rules=()),
                "code_artifact": StrategyCodeArtifactRef(name="mr"),
            }
        )
        manifest = ResearchRunManifest(
            run_id="RR-test000000000001",
            idempotency_key="k",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id="r", version="v", checksum="a" * 64, capabilities=("stock",)
                ),
            ),
            factor_snapshots=(),
            code_version="c" * 16,
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )
        echo = _constraints_echo(manifest)
        assert set(echo) == {
            "max_weight_per_asset",
            "long_only",
            "max_gross_exposure",
            "min_cash_buffer",
        }
        assert echo["long_only"] is True
        assert 0 < echo["max_weight_per_asset"] <= 1
