"""冻结窗口拒绝、只读写守卫与评测fixture口径;CI无模型/真实研究库调用。"""

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scripts.evaluate_research_agent_504 import NAMES, fixture_response

from finboard_app.spec_validation import freeze_spec_runner, preflight_spec_window
from finboard_backtest.research_run.contracts import FrozenArtifactRef, to_json_value
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.validation.contracts import describe_final_test_state
from finboard_mcp.tools.diagnostics import call
from tests.unit.research_run.test_signal_engine import _manifest
from tests.unit.test_mcp_tools_reports import _make_app
from tests.unit.validation.test_runner import _make_experiment


async def test_used_final_window_rejected_before_adapter_or_trial():
    source = replace(_manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",))), strategy_version=1)
    exp = _make_experiment()
    exp = replace(exp, version_stamp=replace(exp.version_stamp, strategy_kind="multi_factor",
        selection_config={"validation_trial_runner": {"kind": "research_spec", "baseline_run_id": source.run_id, "used_windows": []}}))
    result = MagicMock()
    result.one.return_value = (exp.plan.test_start.isoformat(), exp.plan.test_end.isoformat())
    session = AsyncMock()
    session.execute.return_value = result
    with patch("finboard_app.spec_validation.frozen_input", new=AsyncMock(return_value={"status": "completed", "manifest": to_json_value(source)})), patch("finboard_app.spec_validation.preflight_spec_window", new=AsyncMock()) as preflight:
        with pytest.raises(ValueError, match="test_window_already_used"):
            await freeze_spec_runner(session, exp)
        preflight.assert_not_called()


@pytest.mark.parametrize("used", [[None], [{}], [{"start": 20240101, "end": "2024-12-31"}], [{}] * 101])
async def test_malformed_used_windows_rejected_before_bounds_or_preflight(used):
    source = replace(_manifest(build_strategy_template("multi_factor", strategy_id="fixture", dataset_release_ids=("release-v1",))), strategy_version=1)
    exp = _make_experiment()
    exp = replace(exp, version_stamp=replace(exp.version_stamp, strategy_kind="multi_factor",
        selection_config={"validation_trial_runner": {"kind": "research_spec", "baseline_run_id": source.run_id, "used_windows": used}}))
    session = AsyncMock()
    with patch("finboard_app.spec_validation.frozen_input", new=AsyncMock(return_value={"status": "completed", "manifest": to_json_value(source)})), patch("finboard_app.spec_validation.preflight_spec_window", new=AsyncMock()) as preflight:
        with pytest.raises(ValueError, match=r"invalid_used_window|used_window_budget_exceeded"):
            await freeze_spec_runner(session, exp)
        preflight.assert_not_called()
        assert all("jsonb_array_elements" not in str(c.args[0]) for c in session.execute.call_args_list)


async def test_readonly_stress_cannot_open_session_or_queue():
    app = _make_app(write_enabled=False)
    with patch("finboard_mcp.tools.diagnostics.stress_matrix", new=AsyncMock()) as stress:
        result = await call(app, "stress", {"operation": "queue", "baseline_run_id": "RR-fixture", "plan_key": "readonly"})
        assert result.error is not None
        assert result.error.kind == "permission_denied"
        stress.assert_not_called()


def test_fixture_projection_defaults_and_stage_guard():
    default = fixture_response("finboard_decision_projection", {"run_id": "RR-fixture-v13", "stage": "features", "fields": None}, 0)
    assert default["status"] == "ok"
    narrow = fixture_response("finboard_decision_projection", {"run_id": "RR-fixture-v13", "stage": "features", "fields": ["symbol"]}, 0)
    assert set(narrow["data"]["items"][0]["item"]) == {"symbol"}
    assert fixture_response("finboard_decision_projection", {"stage": "decisions"}, 0)["status"] == "error"


async def test_fixture_schemas_are_current_registered_subset():
    from finboard_mcp.server import build_mcp_server
    registered = {tool.name for tool in await build_mcp_server().list_tools()}
    assert registered >= NAMES
    assert not any("broker" in name or "kill_switch" in name for name in NAMES)


def test_unsealed_label_cannot_claim_unused_final_window():
    assert describe_final_test_state(True) == "已揭盲;最终测试已使用,禁止重做"
    response = fixture_response("finboard_validation_experiment_get", {"experiment_id": "EXP-fixture-carrier"}, 0)["data"]
    assert response["final_test_unsealed"] is True
    assert response["final_test_state"] == describe_final_test_state(True)
    assert response["oos_outcome"] == "not_supported"


@pytest.mark.parametrize(("tool", "args"), [
    ("finboard_job_get", {"job_id": "RR-fixture-cache"}),
    ("finboard_run_get", {"run_id": "RR-fixture-D1"}),
    ("finboard_decision_projection", {"run_id": "RR-fixture-D1", "stage": "features"}),
    ("finboard_decision_projection", {"run_id": "RR-fixture-v13", "decision_id": "unknown", "stage": "features"}),
])
def test_fixture_rejects_wrong_identity(tool, args):
    assert fixture_response(tool, args, 2)["error"]["kind"] == "not_found"


def test_fixture_unknown_nonempty_reference_is_not_verified():
    result = fixture_response("finboard_source_check", {"kind": "research_run", "ref_id": "invented"}, 0)
    assert result["data"]["exists"] is False
    assert result["data"]["integrity"] == "unverifiable"


def test_fixture_wait_matches_registered_job_snapshot_contract():
    first = fixture_response("finboard_job_wait", {"job_id": "BJ-fixture"}, 1)["data"]
    assert first["job_id"] == "BJ-fixture"
    assert first["completed"] is False
    assert first["result_ref"] is None
    second = fixture_response("finboard_job_wait", {"job_id": "BJ-fixture"}, 2)["data"]
    assert second["completed"] is True
    assert second["result_ref"] == "RR-fixture-cache"


@pytest.mark.parametrize("fault", ["missing_date", "checksum"])
async def test_formal_factor_coverage_rejected_without_loading_values(fault):
    source = _manifest(build_strategy_template("multi_factor", strategy_id="coverage", dataset_release_ids=("release-v1",)))
    source = replace(source, parameters={"decision_schedule": {"kind": "custom", "dates": ["2024-01-02", "2024-02-02", "2024-03-04"]}},
        factor_series=(FrozenArtifactRef("FS-fixture", "1", "correct-checksum"),))
    exp = _make_experiment()
    exp = replace(exp, plan=replace(exp.plan, train_start=date(2024, 1, 1), train_end=date(2024, 1, 31),
        validation_start=date(2024, 2, 1), validation_end=date(2024, 2, 29), test_start=date(2024, 3, 1), test_end=date(2024, 3, 31)))
    bars = MagicMock()
    bars.release.start_date = date(2024, 1, 1)
    calendar = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3), date(2024, 2, 2), date(2024, 2, 5), date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 31)]
    dates = [(datetime.combine(d, datetime.min.time(), tzinfo=UTC), "fixture") for d in [date(2024, 1, 2), date(2024, 2, 2), date(2024, 3, 4)]]
    row = {"code_artifact": "fixture", "release_id": source.dataset_releases[0].artifact_id,
           "content_checksum": "wrong" if fault == "checksum" else "correct-checksum",
           "dates": ["2024-01-02", "2024-02-02"] if fault == "missing_date" else ["2024-01-02", "2024-02-02", "2024-03-04"]}
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    session = AsyncMock()
    session.execute.return_value = result
    with patch("finboard_data.releases.FrozenReleaseProvider", return_value=bars), patch("finboard_backtest.research_run.signal_engine._bars_release_ref", return_value=source.dataset_releases[0]), patch(
        "finboard_backtest.research_run.signal_engine._release_trading_days", new=AsyncMock(return_value=calendar)), patch(
        "finboard_backtest.research_run.signal_engine._derive_schedule_decision_days", new=AsyncMock(return_value=dates)), patch(
        "finboard_backtest.validation.splitter.generate_walk_forward_windows", return_value=[]):
        with pytest.raises(ValueError, match="validation_factor_coverage_missing" if fault == "missing_date" else "validation_factor_anchor_mismatch"):
            await preflight_spec_window(session, source, exp)
    assert session.execute.await_count == 1
    query = str(session.execute.call_args.args[0])
    assert "octet_length(dates::text)" in query
    assert "artifact_relpath" not in query
