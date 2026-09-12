"""issue #452:再平衡带保留持仓豁免 ``targets ⊆ signals`` 校验。

RR-68a8a186 实测事故:组合构建的再平衡带(``build_portfolio`` 的带块)把
「当期无信号但偏差在带/最小交易权重内」的已成交持仓原样保留权重进入
targets → runner 校验 ``target_symbols ⊆ signal_symbols`` 以「目标仓位包
含没有标准化信号的标的」拒绝整条 run。#380 明确记录未修的残余张力,本期
兑现(用户拍板方案 B:平台层放宽校验,治本):

* ``ConstraintOutcome`` 透传标的维度 ``symbol``(None 序列化省略键,存量
  artifact payload 逐字节不变;反序列化缺键回退 None);
* runner 校验放宽为 ``targets ⊆ signals`` 与带保留标的集的并集 —— 豁免集
  从已持久化的约束审计结构化导出,不引入新的数据通道;既无信号又无带保留
  记录的目标仍 fail-closed 拒绝(防线收窄而非取消);
* 豁免发生时打具名 structlog warning
  ``research_run.band_retained_without_signal``(fail-visible,可见而非静默);
* ``to_research_constraint_outcomes`` 对逐标的 adjustment 透传 symbol,
  组合级 adjustment(gross_leverage 等)输出 None。

纯研究域(runner 校验),不连 broker 不下单。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest
import structlog

from finboard_backtest.portfolio import (
    MAX_WEIGHT_EPSILON,
    ConstraintAdjustment,
    PortfolioBuildInput,
    PortfolioBuildResult,
    PortfolioConstraints,
    PortfolioRiskReport,
    Signal,
    TargetWeight,
    build_portfolio,
    to_research_constraint_outcomes,
    to_research_targets,
)
from finboard_backtest.research_run import (
    ConstraintOutcome,
    DecisionBundle,
    ResearchConstraintViolationError,
    ResearchOrderStatus,
    ResearchPositionSide,
    ResearchRunCoordinator,
    ResearchRunManifest,
    TargetPosition,
)
from finboard_backtest.research_run.checkpoint_resume import _rebuild_constraint
from finboard_backtest.research_run.contracts import (
    canonical_json,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.runner import _band_retained_symbols

from .conftest import replace_pipeline_evidence

#: 再平衡带保留标的的样例权重(事故形态:无信号、偏差在带内、有权重)。
_HELD_WEIGHT = 0.012
_SIGNAL_SYMBOL = "510300.SH"
_HELD_SYMBOL = "000001.SZ"


@pytest.fixture(autouse=True)
def _uncached_runner_logger():
    """隔离 ``setup_logging`` 对 structlog 的全局污染(#301/#315/#316 同款)。

    CI 从仓库根跑全量,任何先行的测试调用 ``setup_logging``(级别过滤 +
    ``cache_logger_on_first_use=True``)后,runner 的模块 logger 已缓存,
    ``capture_logs`` 永远抓空。测试期重置为不缓存并重建 runner 模块 logger,
    结束恢复。
    """
    from finboard_backtest.research_run import runner as runner_module

    saved_config = structlog.get_config()
    saved_logger = runner_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    runner_module.logger = structlog.get_logger(
        "finboard_backtest.research_run.runner"
    )
    yield
    runner_module.logger = saved_logger
    structlog.configure(**saved_config)


def _band_outcome(
    *,
    constraint: str = "rebalance_band",
    passed: bool = True,
    symbol: str | None = _HELD_SYMBOL,
    after_value: float | None = _HELD_WEIGHT,
) -> ConstraintOutcome:
    return ConstraintOutcome(
        constraint=constraint,
        passed=passed,
        before_value=_HELD_WEIGHT,
        after_value=after_value,
        limit=0.05,
        reason="偏差在再平衡带/最小交易权重内则维持实际已成交持仓",
        hard=False,
        symbol=symbol,
    )


class TestBandRetainedExport:
    """``_band_retained_symbols`` 纯函数表驱动:passed ∧ after_value>eps ∧ 有 symbol。"""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            # 基准形态:带内保留且有权重 → 进入豁免集。
            (_band_outcome(), {_HELD_SYMBOL}),
            # 否决形态(超 max_weight 或近零持仓):after_value 保持 desired(0)。
            (_band_outcome(after_value=0.0), set()),
            # after_value 缺失(理论上不出现)不豁免。
            (_band_outcome(after_value=None), set()),
            # 带拒绝(hold=False,超 max_weight_per_asset)不豁免。
            (_band_outcome(passed=False), set()),
            # 无标的维度(历史 payload / 未透传)不豁免。
            (_band_outcome(symbol=None), set()),
            # 非再平衡带约束不参与豁免集。
            (_band_outcome(constraint="investable_universe"), set()),
            # eps 边界:after_value 必须严格大于 MAX_WEIGHT_EPSILON。
            (_band_outcome(after_value=MAX_WEIGHT_EPSILON), set()),
            (_band_outcome(after_value=10 * MAX_WEIGHT_EPSILON), {_HELD_SYMBOL}),
        ],
    )
    def test_export_table(
        self, outcome: ConstraintOutcome, expected: set[str]
    ) -> None:
        assert _band_retained_symbols((outcome,)) == frozenset(expected)

    def test_multiple_outcomes_union(self) -> None:
        outcomes = (
            _band_outcome(symbol="000001.SZ"),
            _band_outcome(symbol="000002.SZ"),
            _band_outcome(symbol="000003.SZ", passed=False),
            ConstraintOutcome(
                constraint="lot_size",
                passed=True,
                before_value=100.0,
                after_value=100.0,
                limit=100.0,
                reason="数量符合交易单位",
                symbol="000004.SZ",
            ),
        )
        assert _band_retained_symbols(outcomes) == frozenset(
            {"000001.SZ", "000002.SZ"}
        )

    def test_empty_constraints(self) -> None:
        assert _band_retained_symbols(()) == frozenset()


class TestConstraintOutcomeSymbolSerialization:
    """symbol 往返:None 省略键(存量 payload 逐字节不变),缺键读回 None。"""

    def test_none_symbol_omits_key(self) -> None:
        outcome = ConstraintOutcome(
            constraint="rebalance_band",
            passed=True,
            before_value=_HELD_WEIGHT,
            after_value=_HELD_WEIGHT,
            limit=0.05,
            reason="维持",
            hard=False,
        )
        payload = to_json_value(outcome)
        assert payload == {
            "constraint": "rebalance_band",
            "passed": True,
            "before_value": _HELD_WEIGHT,
            "after_value": _HELD_WEIGHT,
            "limit": 0.05,
            "reason": "维持",
            "hard": False,
        }
        assert isinstance(payload, dict)
        assert "symbol" not in payload

    def test_symbol_present_when_set(self) -> None:
        payload = to_json_value(_band_outcome())
        assert isinstance(payload, dict)
        assert payload["symbol"] == _HELD_SYMBOL

    def test_legacy_payload_rebuilds_symbol_none(self) -> None:
        """旧版形态 payload(constraints 各 dict 无 symbol 键)读回 symbol=None。"""
        legacy: dict[str, object] = {
            "constraint": "rebalance_band",
            "passed": True,
            "before_value": _HELD_WEIGHT,
            "after_value": _HELD_WEIGHT,
            "limit": 0.05,
            "reason": "维持",
            "hard": False,
        }
        outcome = _rebuild_constraint(legacy, "test")
        assert outcome.symbol is None
        # 新代码重序列化与旧版 payload 逐字节一致(canonical JSON 相等 ⇒
        # artifact checksum 稳定,#314 续算复验零漂移)。
        assert canonical_json(outcome) == canonical_json(legacy)
        assert stable_checksum(outcome) == stable_checksum(legacy)

    def test_symbol_round_trip(self) -> None:
        outcome = _band_outcome()
        payload = to_json_value(outcome)
        assert isinstance(payload, dict)
        rebuilt = _rebuild_constraint(payload, "test")
        assert rebuilt == outcome
        assert rebuilt.symbol == _HELD_SYMBOL

    def test_decision_bundle_constraints_round_trip(self) -> None:
        """经完整 DecisionBundle 约束段形态(``{"constraints": (...)}``)的往返。"""
        constraints = (
            ConstraintOutcome(
                constraint="lot_size",
                passed=True,
                before_value=100.0,
                after_value=100.0,
                limit=100.0,
                reason="数量符合交易单位",
            ),
            _band_outcome(),
        )
        payload = to_json_value({"constraints": constraints})
        assert isinstance(payload, dict)
        items = cast(list[dict[str, object]], payload["constraints"])
        assert "symbol" not in items[0]
        assert items[1]["symbol"] == _HELD_SYMBOL
        rebuilt = tuple(_rebuild_constraint(item, "test") for item in items)
        assert rebuilt == constraints


class TestToResearchConstraintOutcomesSymbol:
    """``to_research_constraint_outcomes`` 透传 source ``ConstraintAdjustment.symbol``。"""

    @staticmethod
    def _result(
        adjustments: tuple[ConstraintAdjustment, ...],
    ) -> PortfolioBuildResult:
        return PortfolioBuildResult(
            resolutions=(),
            target_before_constraints=TargetWeight(
                weights={}, as_of=date(2024, 1, 2), strategy_id="issue-452"
            ),
            target_after_constraints=TargetWeight(
                weights={}, as_of=date(2024, 1, 2), strategy_id="issue-452"
            ),
            adjustments=adjustments,
            risk=PortfolioRiskReport(
                projected_volatility=None,
                beta=None,
                asset_risk_contribution={},
                sleeve_risk_contribution={},
                max_asset_risk_contribution=None,
                concentration=0.0,
                max_drawdown=0.0,
                constraint_impact=0.0,
            ),
            covariance_fallback_used=False,
        )

    def test_symbol_passthrough(self) -> None:
        adjustments = (
            ConstraintAdjustment(
                constraint="investable_universe",
                symbol=_HELD_SYMBOL,
                before_value=0.0,
                after_value=0.0,
                limit=None,
                passed=True,
                reason="在可投资域",
            ),
            ConstraintAdjustment(
                constraint="rebalance_band",
                symbol=_SIGNAL_SYMBOL,
                before_value=0.0,
                after_value=0.25,
                limit=0.05,
                passed=False,
                reason="偏差在再平衡带/最小交易权重内则维持实际已成交持仓",
            ),
            ConstraintAdjustment(
                constraint="gross_leverage",
                symbol=None,
                before_value=0.262,
                after_value=0.262,
                limit=1.0,
                passed=True,
                reason="组合杠杆未超限",
            ),
        )
        outcomes = {
            item.constraint: item
            for item in to_research_constraint_outcomes(self._result(adjustments))
        }
        assert outcomes["investable_universe"].symbol == _HELD_SYMBOL
        assert outcomes["rebalance_band"].symbol == _SIGNAL_SYMBOL
        # 组合级约束(gross_leverage / volatility 等)恒为 None。
        assert outcomes["gross_leverage"].symbol is None


class TestValidateDecisionBandExemption:
    """runner 校验放宽:带保留豁免、无记录仍拒绝、豁免 fail-visible。"""

    @staticmethod
    def _base_constraints(decision_factory) -> tuple[ConstraintOutcome, ...]:
        base = cast(DecisionBundle, decision_factory())
        return base.constraints

    @staticmethod
    def _decision_with_extra_target(
        decision_factory,
        manifest_factory,
        *,
        constraints: tuple[ConstraintOutcome, ...],
    ) -> tuple[DecisionBundle, ResearchRunManifest]:
        """在约束后/风险后目标里加入无信号持仓(事故形态),重建流水线证据。"""
        manifest = cast(ResearchRunManifest, manifest_factory())
        base = cast(DecisionBundle, decision_factory(manifest=manifest))
        targets = (
            *base.targets_after_constraints,
            TargetPosition(
                symbol=_HELD_SYMBOL,
                weight=_HELD_WEIGHT,
                position_side=ResearchPositionSide.LONG,
            ),
        )
        decision = replace_pipeline_evidence(
            replace(
                base,
                constraints=constraints,
                targets_after_constraints=targets,
                targets_after_risk=targets,
            ),
            manifest=manifest,
        )
        return decision, manifest

    @staticmethod
    def _validate(
        decision: DecisionBundle, manifest: ResearchRunManifest
    ) -> None:
        ResearchRunCoordinator._validate_decision(
            decision,
            manifest=manifest,
            position_quantities=defaultdict(Decimal),
            seen_fill_ids=set(),
        )

    def test_targets_all_have_signals_passes(
        self, decision_factory, manifest_factory
    ) -> None:
        """回归不变:targets 全部有信号 → 通过,且无豁免 warning。"""
        manifest = manifest_factory()
        decision = decision_factory(manifest=manifest)
        with structlog.testing.capture_logs() as logs:
            self._validate(decision, manifest)
        assert not [
            event
            for event in logs
            if event["event"] == "research_run.band_retained_without_signal"
        ]

    def test_band_retained_target_passes_with_named_warning(
        self, decision_factory, manifest_factory
    ) -> None:
        """无信号持仓 + 对应带保留 outcome(passed ∧ after>eps)→ 通过 + 具名 warning。"""
        decision, manifest = self._decision_with_extra_target(
            decision_factory,
            manifest_factory,
            constraints=(
                *self._base_constraints(decision_factory),
                _band_outcome(),
            ),
        )
        with structlog.testing.capture_logs() as logs:
            self._validate(decision, manifest)
        events = [
            event
            for event in logs
            if event["event"] == "research_run.band_retained_without_signal"
        ]
        assert len(events) == 1
        assert events[0]["symbols"] == [_HELD_SYMBOL]
        assert events[0]["business_date"] == decision.business_date.isoformat()
        assert events[0]["run_id"] == manifest.run_id
        assert events[0]["log_level"] == "warning"

    def test_target_without_band_outcome_still_rejected(
        self, decision_factory, manifest_factory
    ) -> None:
        """无信号持仓但无对应带保留 outcome → 仍 fail-closed 拒绝,文案逐字不变。"""
        decision, manifest = self._decision_with_extra_target(
            decision_factory,
            manifest_factory,
            constraints=self._base_constraints(decision_factory),
        )
        with pytest.raises(
            ResearchConstraintViolationError,
            match="目标仓位包含没有标准化信号的标的",
        ):
            self._validate(decision, manifest)

    def test_vetoed_band_outcome_still_rejected(
        self, decision_factory, manifest_factory
    ) -> None:
        """带保留被否决形态(passed=True 但 after_value=0)→ 仍拒绝。"""
        decision, manifest = self._decision_with_extra_target(
            decision_factory,
            manifest_factory,
            constraints=(
                *self._base_constraints(decision_factory),
                _band_outcome(after_value=0.0),
            ),
        )
        with pytest.raises(
            ResearchConstraintViolationError,
            match="目标仓位包含没有标准化信号的标的",
        ):
            self._validate(decision, manifest)

    def test_symbolless_band_outcome_still_rejected(
        self, decision_factory, manifest_factory
    ) -> None:
        """带保留 outcome 缺标的维度(symbol=None)→ 不构成豁免,仍拒绝。"""
        decision, manifest = self._decision_with_extra_target(
            decision_factory,
            manifest_factory,
            constraints=(
                *self._base_constraints(decision_factory),
                _band_outcome(symbol=None),
            ),
        )
        with pytest.raises(
            ResearchConstraintViolationError,
            match="目标仓位包含没有标准化信号的标的",
        ):
            self._validate(decision, manifest)

    def test_builder_band_retained_end_to_end(
        self, decision_factory, manifest_factory
    ) -> None:
        """端到端:真实 ``build_portfolio`` 带保留 → outcome 透传 → 校验豁免。

        复现 RR-68a8a186 决策 #9 事故形态:当期只有 510300.SH 信号,已成交
        持仓 000001.SZ(无信号)偏差 0.012 在再平衡带(0.05)内 → 带保留
        原样进入 targets_after_constraints → 此前整条 run 被拒,现豁免。
        """
        manifest = manifest_factory()
        result = build_portfolio(
            PortfolioBuildInput(
                signals=(
                    Signal(
                        symbol=_SIGNAL_SYMBOL,
                        score=1.0,
                        confidence=0.8,
                        timestamp=date(2024, 1, 2),
                        strategy_id="issue-452",
                    ),
                ),
                method="equal_weight",
                constraints=PortfolioConstraints(
                    max_weight_per_asset=0.25,
                    max_weight_per_sleeve=0.4,
                    rebalance_threshold=0.05,
                    min_weight_to_trade=0.001,
                ),
                sleeve_map={_SIGNAL_SYMBOL: "equity", _HELD_SYMBOL: "equity"},
                current_weights={_HELD_SYMBOL: _HELD_WEIGHT},
            )
        )
        constraints = to_research_constraint_outcomes(result)
        band = [
            item
            for item in constraints
            if item.constraint == "rebalance_band" and item.symbol == _HELD_SYMBOL
        ]
        assert len(band) == 1
        assert band[0].passed
        assert (band[0].after_value or 0.0) > MAX_WEIGHT_EPSILON
        targets_after = to_research_targets(result.target_after_constraints)
        assert any(item.symbol == _HELD_SYMBOL for item in targets_after)

        base = decision_factory(manifest=manifest)
        decision = replace_pipeline_evidence(
            replace(
                base,
                constraints=constraints,
                targets_before_constraints=to_research_targets(
                    result.target_before_constraints
                ),
                targets_after_constraints=targets_after,
                targets_after_risk=targets_after,
            ),
            manifest=manifest,
        )
        with structlog.testing.capture_logs() as logs:
            self._validate(decision, manifest)
        assert [
            event
            for event in logs
            if event["event"] == "research_run.band_retained_without_signal"
        ]
        assert decision.orders[0].status is ResearchOrderStatus.FILLED
