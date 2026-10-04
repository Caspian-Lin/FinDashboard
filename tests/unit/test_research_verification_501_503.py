"""固定序列口径、窗口与真实覆盖回归 (#501-503)。"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finboard_app.research_diagnostics import interval_metrics
from finboard_app.research_stress import stress_manifests, stress_matrix
from finboard_app.spec_validation import child_manifest, stress_fee_values
from finboard_backtest.research_run.config_overrides import merge_fee_overrides, section_overrides
from finboard_backtest.research_run.contracts import ResearchActorType, to_json_value
from finboard_backtest.research_run.window import research_window
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.validation.execution import registry_stress_overrides
from tests.unit.research_run.test_signal_engine import _manifest


def test_closed_interval_includes_first_return_and_rf0():
    points = [(date(2024, 12, 30), Decimal(100)), (date(2024, 12, 31), Decimal(110)),
              (date(2025, 1, 2), Decimal(99)), (date(2025, 1, 3), Decimal(108))]
    result = interval_metrics(points, date(2025, 1, 1), date(2025, 1, 3), Decimal(100))
    assert result["net_return"] == pytest.approx(108 / 110 - 1)
    assert result["max_drawdown"] == pytest.approx(0.1)
    assert result["anchor_date"] == "2024-12-31"
    assert result["observed_start"] == "2025-01-02"
    assert result["drawdown_duration_calendar_days"] == 3
    assert result["sharpe_rf0_ddof1"] != 0
    assert interval_metrics(points, date(2026, 1, 1), date(2026, 1, 2), Decimal(100))["status"] == "unsupported"


@pytest.mark.parametrize("override", [{"execution_config": {"delay": 2}}, {"unknown": 1},
                                    {"execution_delay_bars": 2}, {"slippage_bps": -1},
                                    {"cost_multiplier": float("nan")}])
def test_illegal_overrides_fail_closed(override):
    with pytest.raises(ValueError, match=r"unsupported|invalid"):
        registry_stress_overrides(override)


def test_cost_and_slippage_are_isolated_and_consumed():
    manifest = _manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",)))
    base = merge_fee_overrides(manifest.strategy_spec.execution_model, {})
    cost = stress_fee_values(base, {"cost_multiplier": 2})
    applied = merge_fee_overrides(base, cost)
    assert applied.commission_rate == base.commission_rate * 2
    assert applied.minimum_commission == base.minimum_commission * 2
    assert applied.sell_tax_rate == base.sell_tax_rate * 2
    assert applied.slippage_bps == base.slippage_bps
    slip = merge_fee_overrides(base, stress_fee_values(base, {"slippage_bps": 20}))
    assert slip.slippage_bps == 20
    assert slip.commission_rate == base.commission_rate


@pytest.mark.parametrize("field", ["cost_multiplier", "slippage_bps"])
def test_malformed_numeric_stress_is_named_invalid_argument(field):
    source = _manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",)))
    with pytest.raises(ValueError, match="invalid_"):
        stress_fee_values(source.strategy_spec.execution_model, {field: "invalid"})
    with pytest.raises(ValueError, match="invalid_research_capital"):
        stress_manifests(source, cost_multipliers=[], slippage_bps=[], capitals=["invalid"],
                         execution_delay_bars=[], plan_key="bad-capital")


def test_stress_manifest_is_idempotent_and_keeps_dead_partitions():
    manifest = _manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",)))
    kwargs: dict[str, Any] = {"cost_multipliers": [1.0, 2.0], "slippage_bps": [10.0],
              "capitals": ["300000"], "execution_delay_bars": [1, 2], "plan_key": "fixture"}
    children, unsupported = stress_manifests(manifest, **kwargs)
    assert len(children) == 5
    assert unsupported[0]["status"] == "unsupported"
    assert children == stress_manifests(manifest, **kwargs)[0]
    assert all(child.execution_config == manifest.execution_config for _, child in children)
    assert all(child.validation_config == manifest.validation_config for _, child in children)
    assert manifest.fee_config == {}
    assert float(str(section_overrides(children[1][1].fee_config)["commission_rate"])) > 0


def test_spec_window_does_not_shift_decision_dates_and_warmup_is_not_performance():
    source = replace(_manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",))), parameters={"decision_schedule": {"kind": "weekly"}})
    child = child_manifest(source, experiment_id="exp-fixture", start=date(2026, 1, 5),
        end=date(2026, 1, 16), warmup_start=date(2025, 1, 1),
        dates=[date(2026, 1, 2), date(2026, 1, 9), date(2026, 1, 16)], overrides={})
    schedule = child.parameters["decision_schedule"]
    assert isinstance(schedule, dict)
    assert schedule["dates"] == ["2026-01-09"]
    window = research_window(child.parameters)
    assert window is not None
    assert window["decision_start"] == date(2026, 1, 5)
    with pytest.raises(ValueError, match="unsupported_execution_delay"):
        child_manifest(source, experiment_id="exp-fixture", start=date(2026, 1, 5),
            end=date(2026, 1, 16), warmup_start=date(2025, 1, 1),
            dates=[date(2026, 1, 9)], overrides={"execution_delay_bars": 2})


def test_baseline_and_stress_materialize_same_fee_policy():
    source = _manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",)))
    common: dict[str, Any] = {"experiment_id": "fee-policy", "start": date(2026, 1, 5),
              "end": date(2026, 1, 16), "warmup_start": date(2025, 1, 1),
              "dates": [date(2026, 1, 9)]}
    baseline = child_manifest(source, **common, overrides={})
    cost = child_manifest(source, **common, overrides={"cost_multiplier": 2})
    base_fee = section_overrides(baseline.fee_config)
    cost_fee = section_overrides(cost.fee_config)
    assert set(base_fee) == {"commission_rate", "minimum_commission", "sell_tax_rate", "slippage_bps"}
    assert baseline.parameters["fee_policy_version"] == cost.parameters["fee_policy_version"]
    assert base_fee["slippage_bps"] == cost_fee["slippage_bps"]
    assert float(str(cost_fee["minimum_commission"])) == float(str(base_fee["minimum_commission"])) * 2


async def test_completed_stress_read_uses_stored_checksums_across_actors():
    source = replace(_manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",))),
                     requested_by="agent:mcp", actor_type=ResearchActorType.AGENT)
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = SimpleNamespace(status="completed", job_id="BJ-existing",
        manifest_checksum="stored-original-manifest", result_checksum="stored-result")
    session.execute.return_value = result
    frozen = {"status": "completed", "manifest": to_json_value(source), "manifest_checksum": source.checksum}
    with patch("finboard_app.research_stress.frozen_input", new=AsyncMock(return_value=frozen)):
        api = await stress_matrix(session, baseline_run_id=source.run_id, plan_key="same-plan",
                                  operation="get", cost_multipliers=[1], actor="user:api")
        mcp = await stress_matrix(session, baseline_run_id=source.run_id, plan_key="same-plan",
                                  operation="get", cost_multipliers=[1], actor="agent:mcp")
    assert api["probes"][0] == mcp["probes"][0]
    assert api["probes"][0]["manifest_checksum"] == "stored-original-manifest"
    assert api["probes"][0]["result_checksum"] == "stored-result"
    session.commit.assert_not_called()
