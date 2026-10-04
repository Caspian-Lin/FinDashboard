"""正式规格验证与普通研究使用同一流水线;成本消费和幂等证据。"""

from dataclasses import replace
from datetime import date
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from finboard_app.spec_validation import child_manifest, spec_trial_runner_factory
from finboard_backtest.background_jobs.executors.research_run import (
    SessionPerOperationResearchRunStore,
    default_store_factory,
)
from finboard_backtest.research_run.contracts import ResearchRunStatus, to_json_value
from finboard_backtest.research_run.runner import ResearchRunCoordinator
from finboard_backtest.validation.contracts import VersionStamp, new_experiment
from tests.integration.test_research_run_signal_engine_worker import (
    _multi_period_factory,
    _multi_period_manifest,
    _multi_period_provider,
)
from tests.unit.validation.test_trial_runner_factory import _experiment


async def test_formal_pipeline_equivalence_cost_and_cached_child(db_session):
    provider = _multi_period_provider()
    source = replace(_multi_period_manifest("validation502"), strategy_version=1,
                     portfolio_config={"overrides": {"max_risk_contribution": 1}})
    template = _experiment({})
    plan = replace(template.plan, train_start=date(2024, 2, 1), train_end=date(2024, 3, 4),
                   validation_start=date(2024, 3, 5), validation_end=date(2024, 4, 2),
                   test_start=date(2024, 4, 3), test_end=date(2024, 4, 29))
    config = {"kind": "research_spec", "schema_version": "spec_validation_v1",
              "frozen_manifest": to_json_value(source)}
    experiment = new_experiment(hypothesis="同管线窗口结果与费用敏感性", plan=plan, thresholds=template.thresholds,
        version_stamp=VersionStamp(matching_model_version="v2", asset_rules_version="v1",
            factor_version=None, dataset_versions={}, strategy_kind="multi_factor",
            selection_config={"validation_trial_runner": config}))
    with patch("finboard_data.releases.FrozenReleaseProvider", new=type("FixtureRelease", (), {"__new__": lambda cls, *a, **kw: provider})), patch(
        "finboard_app.spec_validation.build_signal_engine_adapter_factory", return_value=_multi_period_factory(provider)):
        runner = await spec_trial_runner_factory(db_session, experiment)
        baseline = await runner(start=plan.train_start, end=plan.train_end, params={})
        cached = await runner(start=plan.train_start, end=plan.train_end, params={})
        stress = await runner(start=plan.train_start, end=plan.train_end, params={}, config_overrides={"cost_multiplier": 2})
    assert baseline.equity_curve == cached.equity_curve
    assert baseline.matching_model == cached.matching_model
    assert baseline.commission_paid > 0
    assert stress.commission_paid > baseline.commission_paid
    assert stress.final_equity != baseline.final_equity
    assert all(plan.train_start <= d <= plan.train_end for d, _ in baseline.equity_curve)
    # 普通 coordinator 用相同窗口规格/输入,非另一份简化策略。
    ordinary = child_manifest(source, experiment_id=experiment.experiment_id, start=plan.train_start,
        end=plan.train_end, warmup_start=provider.release.start_date,
        dates=[date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 29)], overrides={})
    ordinary = replace(ordinary, run_id="RR-ordinary502", idempotency_key="ordinary502")
    store = SessionPerOperationResearchRunStore(async_sessionmaker(db_session.bind, expire_on_commit=False), default_store_factory)
    record = await ResearchRunCoordinator(store).execute(ordinary, cast(Any, _multi_period_factory(provider))(ordinary))
    assert record.status is ResearchRunStatus.COMPLETED
    assert record.result is not None
    assert record.result.commission_paid == baseline.commission_paid
    assert [(p.trade_date, p.equity) for p in record.result.equity_curve] == baseline.equity_curve


async def test_spec_trial_rejects_outside_release_before_execution(db_session):
    provider = _multi_period_provider()
    source = replace(_multi_period_manifest("outside502"), strategy_version=1)
    exp = _experiment({"validation_trial_runner": {"kind": "research_spec", "schema_version": "spec_validation_v1",
                        "frozen_manifest": to_json_value(source)}})
    with patch("finboard_data.releases.FrozenReleaseProvider", new=type("FixtureRelease", (), {"__new__": lambda cls, *a, **kw: provider})), patch(
        "finboard_app.spec_validation.build_signal_engine_adapter_factory", return_value=_multi_period_factory(provider)), pytest.raises(ValueError, match="outside_release"):
        await spec_trial_runner_factory(db_session, exp)
