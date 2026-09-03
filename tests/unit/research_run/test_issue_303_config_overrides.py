"""issue #303:组合硬约束配置传导 —— risk_config.overrides 接线 + 入队预检。

覆盖验收:

* ``risk_config.overrides`` 对 risk_exit_policy 生效(覆盖 stop-loss 类参数,
  manifest → adapter 值变化;此前该 manifest 分区无任何业务消费者);
* ``max_risk_contribution`` 经 ``portfolio_config.overrides`` 覆盖的回归锁定
  (既有能力,键位此前无人知晓);
* 入队门控 ``research_portfolio_gate_error``:静态池 2 只 + 默认 0.35 → 拒绝,
  错误附 ceil(1/max_rc) 与键位修复路径;阈值非法值入队即拒;REST 422 /
  MCP invalid_argument 的入队接线见
  tests/integration/test_universe_precheck_enqueue.py(#303 段);
* 运行期 RiskBudgetError fail-closed 行为零变化(#91 既有测试全绿)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    InMemoryResearchRunStore,
    NormalizedSignal,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunStatus,
    UniverseCandidate,
)
from finboard_backtest.research_run.config_overrides import (
    DEFAULT_MAX_RISK_CONTRIBUTION,
    effective_max_risk_contribution,
    merge_risk_exit_policy,
    min_pool_for_risk_cap,
    research_portfolio_gate_error,
    section_overrides,
)
from finboard_backtest.research_run.contracts import JsonValue
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _constraints_from_manifest,
    _risk_exit_policy_from_manifest,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import RiskExitPolicy, RiskExitType
from finboard_backtest.strategy_spec.universe_precheck import UniversePoolPreview

SYMBOLS = ("A.SH", "B.SH", "C.SH")


def _policy() -> RiskExitPolicy:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="config_overrides_research",
        dataset_release_ids=("release-v1",),
    )
    return spec.risk_exit_policy


def _preview(
    *, total: int = 3, included: int = 3
) -> UniversePoolPreview:
    return UniversePoolPreview(
        total_candidates=total,
        included=included,
        excluded_by_condition={"listing_age_below_minimum": total - included}
        if included < total
        else {},
        missing_fields=(),
        warnings=(),
    )


# ---------------------------------------------------------------------------
# risk_config.overrides 合并语义
# ---------------------------------------------------------------------------
class TestMergeRiskExitPolicy:
    def test_empty_overrides_returns_base_identity(self) -> None:
        base = _policy()
        assert merge_risk_exit_policy(base, {}) is base

    def test_enable_price_stop_loss_overrides_named_rule(self) -> None:
        """stop-loss 类参数覆盖:启用 + threshold,未声明字段继承基准值。"""
        merged = merge_risk_exit_policy(
            _policy(),
            {
                "rules": [
                    {
                        "rule_type": "price_stop_loss",
                        "enabled": True,
                        "threshold": 0.08,
                    }
                ]
            },
        )
        base_rule = next(
            rule
            for rule in _policy().rules
            if rule.rule_type is RiskExitType.PRICE_STOP_LOSS
        )
        stop = next(
            rule
            for rule in merged.rules
            if rule.rule_type is RiskExitType.PRICE_STOP_LOSS
        )
        assert base_rule.enabled is False
        assert stop.enabled is True
        assert stop.threshold == 0.08
        # 未声明字段继承基准 rationale(合并而非整条替换)。
        assert stop.rationale == base_rule.rationale
        # 其余规则不受影响。
        other_types = {
            item for item in RiskExitType if item is not RiskExitType.PRICE_STOP_LOSS
        }
        for rule_type in other_types:
            assert next(
                rule for rule in merged.rules if rule.rule_type is rule_type
            ) == next(
                rule for rule in _policy().rules if rule.rule_type is rule_type
            )

    def test_partial_threshold_override_keeps_other_fields(self) -> None:
        """对已启用规则只改 threshold:其余字段(target_gross_exposure)保留。"""
        merged = merge_risk_exit_policy(
            _policy(),
            {
                "rules": [
                    {
                        "rule_type": "portfolio_drawdown_derisk",
                        "threshold": 0.20,
                    }
                ]
            },
        )
        derisk = next(
            rule
            for rule in merged.rules
            if rule.rule_type is RiskExitType.PORTFOLIO_DRAWDOWN_DERISK
        )
        assert derisk.enabled is True
        assert derisk.threshold == 0.20
        assert derisk.target_gross_exposure == 0.5

    def test_disable_enabled_rule(self) -> None:
        merged = merge_risk_exit_policy(
            _policy(),
            {
                "rules": [
                    {"rule_type": "portfolio_drawdown_derisk", "enabled": False}
                ]
            },
        )
        derisk = next(
            rule
            for rule in merged.rules
            if rule.rule_type is RiskExitType.PORTFOLIO_DRAWDOWN_DERISK
        )
        assert derisk.enabled is False

    def test_new_rule_type_requires_full_fields(self) -> None:
        base = RiskExitPolicy(rules=(_policy().rules[0],))
        merged = merge_risk_exit_policy(
            base,
            {
                "rules": [
                    {
                        "rule_type": "take_profit",
                        "enabled": True,
                        "threshold": 0.15,
                        "rationale": "涨 15% 止盈",
                    }
                ]
            },
        )
        assert {rule.rule_type for rule in merged.rules} == {
            _policy().rules[0].rule_type,
            RiskExitType.TAKE_PROFIT,
        }

    def test_new_rule_type_missing_required_field_rejected(self) -> None:
        base = RiskExitPolicy(rules=(_policy().rules[0],))
        with pytest.raises(ValueError, match=r"risk_config\.overrides 合并后"):
            merge_risk_exit_policy(
                base,
                {
                    "rules": [
                        {
                            "rule_type": "take_profit",
                            "enabled": True,
                            "rationale": "启用止盈但缺 threshold,校验必须拒绝",
                        },
                    ]
                },
            )

    def test_unknown_top_level_key_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"未知键.*stop_loss"):
            merge_risk_exit_policy(_policy(), {"stop_loss": 0.08})

    def test_rules_not_a_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="必须是规则对象列表"):
            merge_risk_exit_policy(_policy(), {"rules": "price_stop_loss"})

    def test_rule_entry_not_a_mapping_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"rules'\]\[0\] 必须是对象"):
            merge_risk_exit_policy(_policy(), {"rules": [0.08]})

    def test_rule_entry_missing_rule_type_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"rules'\]\[0\] 缺少合法 rule_type"):
            merge_risk_exit_policy(_policy(), {"rules": [{"threshold": 0.08}]})

    def test_duplicate_rule_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="重复声明"):
            merge_risk_exit_policy(
                _policy(),
                {
                    "rules": [
                        {"rule_type": "take_profit", "threshold": 0.1},
                        {"rule_type": "take_profit", "threshold": 0.2},
                    ]
                },
            )

    def test_unknown_field_rejected_by_model(self) -> None:
        with pytest.raises(ValueError, match="合并后未通过"):
            merge_risk_exit_policy(
                _policy(),
                {
                    "rules": [
                        {
                            "rule_type": "price_stop_loss",
                            "enabled": True,
                            "threshold": 0.08,
                            "bogus": 1,
                        }
                    ]
                },
            )


# ---------------------------------------------------------------------------
# 生效 max_risk_contribution 解析(组合约束分区,回归锁定既有覆盖能力)
# ---------------------------------------------------------------------------
class TestEffectiveMaxRiskContribution:
    def test_absent_returns_default(self) -> None:
        assert (
            effective_max_risk_contribution({})
            == DEFAULT_MAX_RISK_CONTRIBUTION
            == 0.35
        )

    def test_explicit_null_treated_as_absent(self) -> None:
        assert effective_max_risk_contribution({"max_risk_contribution": None}) == 0.35

    @pytest.mark.parametrize("raw", [0.5, 1, "0.6"])
    def test_accepts_numeric_forms(self, raw: object) -> None:
        assert effective_max_risk_contribution({"max_risk_contribution": raw}) == float(raw)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("raw", "snippet"),
        [
            (0, "合法域"),
            (-0.1, "合法域"),
            (1.5, "合法域"),
            ("abc", "无法解析"),
            (True, "bool"),
            (["0.5"], "list"),
        ],
    )
    def test_invalid_values_rejected_with_key_hint(self, raw: object, snippet: str) -> None:
        with pytest.raises(
            ValueError,
            match=r"portfolio_config\.overrides\['max_risk_contribution'\]",
        ) as excinfo:
            effective_max_risk_contribution({"max_risk_contribution": raw})
        message = str(excinfo.value)
        assert snippet in message

    def test_section_overrides_extracts_nested(self) -> None:
        assert section_overrides({"overrides": {"a": 1}}) == {"a": 1}
        assert section_overrides({}) == {}
        assert section_overrides({"overrides": "junk"}) == {}


def test_min_pool_for_risk_cap_boundaries() -> None:
    assert min_pool_for_risk_cap(0.35) == 3
    assert min_pool_for_risk_cap(0.5) == 2
    assert min_pool_for_risk_cap(0.2) == 5
    assert min_pool_for_risk_cap(1.0) == 1
    assert min_pool_for_risk_cap(0.34) == 3


# ---------------------------------------------------------------------------
# 入队门控(REST 422 / MCP invalid_argument 共用)
# ---------------------------------------------------------------------------
class TestResearchPortfolioGate:
    def test_two_symbol_pool_with_default_threshold_rejected(self) -> None:
        error = research_portfolio_gate_error(
            preview=_preview(total=2, included=2),
            risk_exit_policy=_policy(),
            portfolio_overrides={},
            risk_overrides={},
            decision_date=date(2025, 6, 30),
        )
        assert error is not None
        assert "ceil(1/0.35)=3" in error
        assert "静态候选池仅 2 只" in error
        assert "portfolio_config.overrides['max_risk_contribution']" in error
        assert "hard_constraint_rejected" in error

    def test_portfolio_override_relaxes_gate(self) -> None:
        assert (
            research_portfolio_gate_error(
                preview=_preview(total=2, included=2),
                risk_exit_policy=_policy(),
                portfolio_overrides={"max_risk_contribution": 0.5},
                risk_overrides={},
                decision_date=date(2025, 6, 30),
            )
            is None
        )

    def test_exclusion_stats_included_in_error(self) -> None:
        error = research_portfolio_gate_error(
            preview=_preview(total=5, included=2),
            risk_exit_policy=_policy(),
            portfolio_overrides={},
            risk_overrides={},
            decision_date=date(2025, 6, 30),
        )
        assert error is not None
        assert "listing_age_below_minimum=3" in error

    def test_invalid_threshold_rejected_even_when_pool_large(self) -> None:
        error = research_portfolio_gate_error(
            preview=_preview(total=5, included=5),
            risk_exit_policy=_policy(),
            portfolio_overrides={"max_risk_contribution": 0},
            risk_overrides={},
            decision_date=date(2025, 6, 30),
        )
        assert error is not None
        assert "portfolio_config.overrides['max_risk_contribution']" in error

    def test_invalid_risk_overrides_rejected(self) -> None:
        error = research_portfolio_gate_error(
            preview=_preview(),
            risk_exit_policy=_policy(),
            portfolio_overrides={},
            risk_overrides={"rules": [{"rule_type": "no_such_rule"}]},
            decision_date=date(2025, 6, 30),
        )
        assert error is not None
        assert "risk_config.overrides" in error

    def test_unevaluable_pool_skipped(self) -> None:
        assert (
            research_portfolio_gate_error(
                preview=_preview(total=0, included=0),
                risk_exit_policy=_policy(),
                portfolio_overrides={},
                risk_overrides={},
                decision_date=date(2025, 6, 30),
            )
            is None
        )

    def test_threshold_one_disables_gate(self) -> None:
        assert (
            research_portfolio_gate_error(
                preview=_preview(total=2, included=2),
                risk_exit_policy=_policy(),
                portfolio_overrides={"max_risk_contribution": 1.0},
                risk_overrides={},
                decision_date=date(2025, 6, 30),
            )
            is None
        )


# ---------------------------------------------------------------------------
# manifest → adapter 接线
# ---------------------------------------------------------------------------
def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.09, 0.01, 0.01]),
        tickers=list(SYMBOLS),
        shrinkage=0.0,
        n_observations=252,
    )


def _input(*, day: int = 2, prices: dict[str, float] | None = None) -> PortfolioDecisionInput:
    decision_at = datetime(2025, 1, day, 15, tzinfo=UTC)
    mark_prices = prices or dict.fromkeys(SYMBOLS, 10.0)
    return PortfolioDecisionInput(
        business_date=date(2025, 1, day),
        decision_at=decision_at,
        execution_at=datetime(2025, 1, day + 1, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("通过冻结候选池规则",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=mark_prices[symbol],
                source_artifact_ids=("release-v1",),
                available_at=decision_at,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="fixed-research-signal",
                factor_snapshot_id="factor-v1",
                rationale="冻结输入中的标准化多头信号",
            )
            for symbol in SYMBOLS
        ),
        prices=mark_prices,
        execution_prices=mark_prices,
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in SYMBOLS},
        input_artifact_ids=("release-v1", "factor-v1"),
        covariance=_covariance(),
        sleeve_map=dict.fromkeys(SYMBOLS, "equity"),
    )


class TestManifestToAdapter:
    def test_max_risk_contribution_default_and_override(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        """回归锁定:portfolio_config.overrides 覆盖 max_risk_contribution 生效。"""
        manifest = manifest_factory()
        assert (
            _constraints_from_manifest(manifest).max_risk_contribution
            == 0.35
        )
        overridden = replace(
            manifest,
            portfolio_config=cast(
                dict[str, JsonValue],
                {"overrides": {"max_risk_contribution": 0.5}},
            ),
        )
        assert _constraints_from_manifest(overridden).max_risk_contribution == 0.5

    def test_risk_config_overrides_effective_policy(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        manifest = manifest_factory()
        assert (
            _risk_exit_policy_from_manifest(manifest)
            is manifest.strategy_spec.risk_exit_policy
        )
        overridden = replace(
            manifest,
            risk_config=cast(
                dict[str, JsonValue],
                {
                    "overrides": {
                        "rules": [
                            {
                                "rule_type": "price_stop_loss",
                                "enabled": True,
                                "threshold": 0.08,
                            }
                        ]
                    }
                },
            ),
        )
        merged = _risk_exit_policy_from_manifest(overridden)
        stop = next(
            rule
            for rule in merged.rules
            if rule.rule_type is RiskExitType.PRICE_STOP_LOSS
        )
        assert stop.enabled is True
        assert stop.threshold == 0.08
        # 规格本身不被修改(manifest 原样冻结,覆盖只发生在消费端)。
        assert (
            next(
                rule
                for rule in overridden.strategy_spec.risk_exit_policy.rules
                if rule.rule_type is RiskExitType.PRICE_STOP_LOSS
            ).enabled
            is False
        )

    def test_invalid_risk_config_rejected(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        manifest = replace(
            manifest_factory(),
            risk_config=cast(
                dict[str, JsonValue],
                {"overrides": {"threshold": 0.08}},
            ),
        )
        with pytest.raises(ValueError, match="未知键"):
            _risk_exit_policy_from_manifest(manifest)


async def _collect(
    adapter: PortfolioPipelineAdapter, manifest: ResearchRunManifest
) -> list[DecisionBundle]:
    adapter.validate_manifest(manifest)
    return [decision async for decision in adapter.decisions(manifest)]


@pytest.mark.asyncio
async def test_stop_loss_override_triggers_exit_end_to_end(
    manifest_factory: Callable[..., ResearchRunManifest],
) -> None:
    """risk_config.overrides 启用 stop-loss:第二期跌破止损价即触发退出。

    与 #91 的 test_risk_exit_uses_filled_position_and_persists_state 同场景,
    但策略不经 spec 修改 —— 风险退出参数纯粹经 manifest.risk_config 覆盖。
    """
    manifest = replace(
        manifest_factory(run_id="RR-303-risk-exit", idempotency_key="303-risk-exit"),
        portfolio_config=cast(
            dict[str, JsonValue],
            {"overrides": {"max_risk_contribution": 0.40}},
        ),
        risk_config=cast(
            dict[str, JsonValue],
            {
                "overrides": {
                    "rules": [
                        {
                            "rule_type": "price_stop_loss",
                            "enabled": True,
                            "threshold": 0.10,
                        }
                    ]
                }
            },
        ),
    )
    second_prices = {"A.SH": 8.0, "B.SH": 10.0, "C.SH": 10.0}
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(
            _input(),
            _input(day=3, prices=second_prices),
        ),
    )
    decisions = await _collect(adapter, manifest)
    second = decisions[1]
    assert any(
        item.rule_type == RiskExitType.PRICE_STOP_LOSS.value
        and item.symbol == "A.SH"
        and item.triggered
        for item in second.risk_exits
    )
    assert "A.SH" not in {item.symbol for item in second.targets_after_risk}


@pytest.mark.asyncio
async def test_invalid_risk_config_rejects_run_fail_closed(
    manifest_factory: Callable[..., ResearchRunManifest],
) -> None:
    """非法 risk_config.overrides 直跑(旁路入队门控)也 fail-closed REJECTED。"""
    manifest = replace(
        manifest_factory(run_id="RR-303-invalid", idempotency_key="303-invalid"),
        risk_config=cast(
            dict[str, JsonValue],
            {"overrides": {"rules": [{"rule_type": "no_such_rule"}]}},
        ),
    )
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(_input(),),
    )
    record = await ResearchRunCoordinator(InMemoryResearchRunStore()).execute(
        manifest, adapter
    )
    assert record.status is ResearchRunStatus.REJECTED
    assert record.error_code == "hard_constraint_rejected"
    assert "risk_config.overrides" in (record.error_summary or "")
