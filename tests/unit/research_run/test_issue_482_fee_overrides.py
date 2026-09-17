"""issue #482:政策覆盖死分区收口 —— fee_config.overrides 接线 + 死分区具名拒绝。

覆盖验收:

* ``fee_config.overrides`` 按费用键名对 ``strategy_spec.execution_model`` 生效
  (佣金率 / 最低佣金 / 卖出印花税 / 滑点;未声明键继承规格值;此前该 manifest
  分区自 #127 起只存不用,任何覆盖都被静默丢弃);
* 未知键 / 非法值 fail-closed:入队门控 ``research_policy_gate_error`` 秒级拒绝,
  运行期经 ``ResearchConstraintViolationError`` 使 run REJECTED;
* ``execution_config`` / ``validation_config`` 非空覆盖入队具名拒绝(此前静默
  no-op 死分区),空 dict 零影响;
* ``fee_policy_summary`` 读侧派生:生效费用参数与来源(规格 vs 覆盖)可审计,
  历史 manifest 残留的未知键不进入 effective;
* 端到端:费用覆盖 run 的成交费用项与基线出现预期差异;规格本身不被修改。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
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
    fee_policy_summary,
    merge_fee_overrides,
    reject_dead_policy_overrides,
    research_policy_gate_error,
)
from finboard_backtest.research_run.contracts import JsonValue
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _execution_model_from_manifest,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import ExecutionModel

SYMBOLS = ("A.SH", "B.SH", "C.SH")


def _execution_model() -> ExecutionModel:
    return build_strategy_template(
        "multi_factor",
        strategy_id="fee_overrides_research",
        dataset_release_ids=("release-v1",),
    ).execution_model


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


# ---------------------------------------------------------------------------
# fee_config.overrides 合并语义
# ---------------------------------------------------------------------------
class TestMergeFeeOverrides:
    def test_empty_overrides_returns_base_identity(self) -> None:
        base = _execution_model()
        assert merge_fee_overrides(base, {}) is base

    def test_named_key_override_others_inherit(self) -> None:
        base = _execution_model()
        merged = merge_fee_overrides(
            base, {"commission_rate": base.commission_rate * 2}
        )
        assert merged.commission_rate == base.commission_rate * 2
        # 未声明键继承规格值;timing 等非费用字段不受影响。
        assert merged.minimum_commission == base.minimum_commission
        assert merged.sell_tax_rate == base.sell_tax_rate
        assert merged.slippage_bps == base.slippage_bps
        assert merged.timing is base.timing
        # 规格本身不被修改(manifest 原样冻结,覆盖只发生在消费端)。
        assert base.commission_rate == _execution_model().commission_rate

    def test_all_four_fee_keys_override(self) -> None:
        merged = merge_fee_overrides(
            _execution_model(),
            {
                "commission_rate": 0.0006,
                "minimum_commission": 10,
                "sell_tax_rate": 0.001,
                "slippage_bps": 10,
            },
        )
        assert merged.commission_rate == 0.0006
        assert merged.minimum_commission == 10
        assert merged.sell_tax_rate == 0.001
        assert merged.slippage_bps == 10

    def test_unknown_key_rejected_with_shape_hint(self) -> None:
        with pytest.raises(ValueError, match=r"未知键.*fee_config\.overrides"):
            merge_fee_overrides(_execution_model(), {"stamp_tax": 0.001})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("commission_rate", 0.5),
            ("commission_rate", -0.001),
            ("sell_tax_rate", 0.5),
            ("slippage_bps", 20000),
            ("minimum_commission", -1),
        ],
    )
    def test_out_of_domain_value_rejected(self, key: str, value: float) -> None:
        with pytest.raises(ValueError, match="执行模型校验"):
            merge_fee_overrides(_execution_model(), {key: value})

    def test_bool_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="执行模型校验"):
            merge_fee_overrides(_execution_model(), {"commission_rate": True})


# ---------------------------------------------------------------------------
# manifest → adapter 接线
# ---------------------------------------------------------------------------
class TestManifestToEffectiveExecutionModel:
    def test_absent_overrides_returns_spec_identity(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        manifest = manifest_factory()
        assert (
            _execution_model_from_manifest(manifest)
            is manifest.strategy_spec.execution_model
        )

    def test_fee_overrides_effective(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        manifest = manifest_factory()
        spec_model = manifest.strategy_spec.execution_model
        overridden = replace(
            manifest,
            fee_config=cast(
                dict[str, JsonValue],
                {
                    "overrides": {
                        "commission_rate": spec_model.commission_rate * 2,
                        "slippage_bps": spec_model.slippage_bps * 4,
                    }
                },
            ),
        )
        effective = _execution_model_from_manifest(overridden)
        assert effective.commission_rate == spec_model.commission_rate * 2
        assert effective.slippage_bps == spec_model.slippage_bps * 4
        assert effective.minimum_commission == spec_model.minimum_commission
        # 规格原值不被修改。
        assert overridden.strategy_spec.execution_model is spec_model

    def test_invalid_overrides_rejected(
        self, manifest_factory: Callable[..., ResearchRunManifest]
    ) -> None:
        manifest = replace(
            manifest_factory(),
            fee_config=cast(
                dict[str, JsonValue],
                {"overrides": {"commission_rate": 5.0}},
            ),
        )
        with pytest.raises(ValueError, match=r"fee_config\.overrides"):
            _execution_model_from_manifest(manifest)


# ---------------------------------------------------------------------------
# 死分区具名拒绝
# ---------------------------------------------------------------------------
class TestRejectDeadPolicyOverrides:
    def test_empty_sections_pass(self) -> None:
        reject_dead_policy_overrides({}, {})  # 不抛

    def test_non_empty_execution_config_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"execution_config.*fee_config"):
            reject_dead_policy_overrides({"commission_rate": 0.001}, {})

    def test_non_empty_validation_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="validation_config"):
            reject_dead_policy_overrides({}, {"require_walk_forward": False})


# ---------------------------------------------------------------------------
# 入队门控(REST 422 / MCP invalid_argument 共用)
# ---------------------------------------------------------------------------
class TestResearchPolicyGate:
    def test_valid_fee_overrides_pass(self) -> None:
        assert (
            research_policy_gate_error(
                execution_model=_execution_model(),
                fee_overrides={"commission_rate": 0.0006},
                execution_overrides={},
                validation_overrides={},
            )
            is None
        )

    def test_unknown_fee_key_rejected(self) -> None:
        error = research_policy_gate_error(
            execution_model=_execution_model(),
            fee_overrides={"no_such_key": 1},
            execution_overrides={},
            validation_overrides={},
        )
        assert error is not None
        assert "fee_config.overrides" in error

    def test_invalid_fee_value_rejected(self) -> None:
        error = research_policy_gate_error(
            execution_model=_execution_model(),
            fee_overrides={"slippage_bps": -1},
            execution_overrides={},
            validation_overrides={},
        )
        assert error is not None
        assert "fee_config.overrides" in error

    def test_non_empty_execution_config_rejected(self) -> None:
        error = research_policy_gate_error(
            execution_model=_execution_model(),
            fee_overrides={},
            execution_overrides={"timing": "next_open"},
            validation_overrides={},
        )
        assert error is not None
        assert "execution_config" in error
        assert "fee_config.overrides" in error

    def test_non_empty_validation_config_rejected(self) -> None:
        error = research_policy_gate_error(
            execution_model=_execution_model(),
            fee_overrides={},
            execution_overrides={},
            validation_overrides={"trial_budget": 5},
        )
        assert error is not None
        assert "validation_config" in error


# ---------------------------------------------------------------------------
# fee_policy_summary 读侧派生
# ---------------------------------------------------------------------------
class TestFeePolicySummary:
    def test_no_overrides(self) -> None:
        model = _execution_model()
        summary = fee_policy_summary(
            {
                "strategy_spec": {"execution_model": model.model_dump()},
                "fee_config": {"overrides": {}},
            }
        )
        assert summary["overrides_applied"] is False
        assert summary["overrides"] == {}
        assert summary["effective"] == summary["spec"]

    def test_overrides_applied(self) -> None:
        model = _execution_model()
        summary = fee_policy_summary(
            {
                "strategy_spec": {"execution_model": model.model_dump()},
                "fee_config": {
                    "commission_rate": model.commission_rate,
                    "overrides": {"commission_rate": 0.0006},
                },
            }
        )
        assert summary["overrides_applied"] is True
        assert summary["effective"]["commission_rate"] == 0.0006
        assert summary["effective"]["slippage_bps"] == model.slippage_bps
        assert summary["spec"]["commission_rate"] == model.commission_rate

    def test_legacy_unknown_keys_not_in_effective(self) -> None:
        """接线前静默存储的未知键:仅留在 overrides,不进 effective。"""
        model = _execution_model()
        summary = fee_policy_summary(
            {
                "strategy_spec": {"execution_model": model.model_dump()},
                "fee_config": {"overrides": {"stamp_tax": 0.001}},
            }
        )
        assert summary["overrides_applied"] is True
        assert summary["overrides"] == {"stamp_tax": 0.001}
        assert summary["effective"] == summary["spec"]


# ---------------------------------------------------------------------------
# 端到端(旁路入队门控直跑 adapter)
# ---------------------------------------------------------------------------
async def _collect(
    adapter: PortfolioPipelineAdapter, manifest: ResearchRunManifest
) -> list[DecisionBundle]:
    adapter.validate_manifest(manifest)
    return [decision async for decision in adapter.decisions(manifest)]


def _total_commission(decisions: list[DecisionBundle]) -> Decimal:
    return sum((fill.commission for d in decisions for fill in d.fills), Decimal("0"))


@pytest.mark.asyncio
async def test_fee_overrides_change_fill_costs_end_to_end(
    manifest_factory: Callable[..., ResearchRunManifest],
) -> None:
    """fee_config.overrides 提高佣金率:同输入下成交费用严格高于基线。

    与 #303 的 stop-loss 端到端同构 —— 策略不经 spec 修改,费用纯粹经
    manifest.fee_config 覆盖。
    """
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(_input(),),
    )
    baseline = await _collect(adapter, manifest_factory(run_id="RR-482-base", idempotency_key="482-base"))
    assert baseline[0].fills, "基线 run 应产生成交"

    spec_model = manifest_factory().strategy_spec.execution_model
    overridden_manifest = replace(
        manifest_factory(run_id="RR-482-cost2x", idempotency_key="482-cost2x"),
        fee_config=cast(
            dict[str, JsonValue],
            {"overrides": {"commission_rate": spec_model.commission_rate * 10}},
        ),
    )
    overridden = await _collect(
        PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(_input(),),
        ),
        overridden_manifest,
    )
    assert _total_commission(overridden) > _total_commission(baseline) > 0


@pytest.mark.asyncio
async def test_invalid_fee_config_rejects_run_fail_closed(
    manifest_factory: Callable[..., ResearchRunManifest],
) -> None:
    """非法 fee_config.overrides 直跑(旁路入队门控)也 fail-closed REJECTED。"""
    manifest = replace(
        manifest_factory(run_id="RR-482-invalid", idempotency_key="482-invalid"),
        fee_config=cast(
            dict[str, JsonValue],
            {"overrides": {"no_such_fee_key": 1}},
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
    assert "fee_config.overrides" in (record.error_summary or "")
