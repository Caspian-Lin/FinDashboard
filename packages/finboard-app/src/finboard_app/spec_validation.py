"""冻结正式 ResearchRun 的验证入口;不构造替代注册表策略 (#502/#503)。"""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.research_diagnostics import bounded_read, frozen_input
from finboard_backtest.background_jobs.executors.research_run import (
    SessionPerOperationResearchRunStore,
    default_store_factory,
)
from finboard_backtest.research_run.config_overrides import merge_fee_overrides, section_overrides
from finboard_backtest.research_run.contracts import (
    ResearchRunManifest,
    ResearchRunStatus,
    manifest_from_json,
    stable_checksum,
)
from finboard_backtest.research_run.runner import ResearchRunCoordinator
from finboard_backtest.research_run.signal_engine import build_signal_engine_adapter_factory
from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.contracts import ResearchExperiment
from finboard_backtest.validation.runner import TrialRunner


async def freeze_spec_runner(
    session: AsyncSession, experiment: ResearchExperiment
) -> ResearchExperiment:
    raw = experiment.version_stamp.selection_config.get("validation_trial_runner", {})
    if not isinstance(raw, dict):
        raise ValueError("invalid_trial_runner")
    kind = raw.get("kind", "registry")
    if kind not in {"registry", "research_spec"}:
        raise ValueError("unsupported_trial_runner_kind")
    if kind == "registry":
        return experiment
    if "frozen_manifest" in raw:
        raise ValueError("frozen_manifest_is_server_owned")
    if set(raw) - {"kind", "baseline_run_id", "used_windows", "warmup_start"}:
        raise ValueError("unknown_spec_runner_configuration")
    async with bounded_read(session):
        source = await frozen_input(session, str(raw.get("baseline_run_id", "")))
        if source["status"] != "completed":
            raise ValueError("spec_validation_requires_completed_baseline")
        manifest = manifest_from_json(source["manifest"])
        if manifest.strategy_version is None or manifest.strategy_kind != "multi_factor":
            raise ValueError("unsupported_spec_runner: published multi_factor version required")
        if experiment.version_stamp.strategy_kind != manifest.strategy_kind:
            raise ValueError("validation_strategy_kind_must_match_frozen_source")
        if experiment.strategy_params_space:
            raise ValueError(
                "unsupported_spec_parameter_grid: use explicit versioned specs; no silent parameter no-op"
            )
        if "used_windows" not in raw or not isinstance(raw["used_windows"], list):
            raise ValueError(
                "used_windows_required: declare previously used tuning/evaluation intervals"
            )
        used = copy.deepcopy(raw["used_windows"])
        if len(used) > 100:
            raise ValueError("used_window_budget_exceeded: 100 intervals")
        for interval in used:
            if not isinstance(interval, dict) or not all(
                isinstance(interval.get(key), str) for key in ("start", "end")
            ):
                raise ValueError("invalid_used_window: start/end date strings required")
        bounds = (
            await session.execute(
                text("""
            SELECT min(p->>'trade_date'),max(p->>'trade_date') FROM research_runs
            CROSS JOIN LATERAL jsonb_array_elements(result::jsonb->'equity_curve') p
            WHERE run_id=:run_id
        """),
                {"run_id": raw["baseline_run_id"]},
            )
        ).one()
        if bounds[0]:
            used.append({"start": bounds[0], "end": bounds[1], "source": raw["baseline_run_id"]})
        for interval in used:
            lo, hi = date.fromisoformat(interval["start"]), date.fromisoformat(interval["end"])
            if lo > hi:
                raise ValueError("invalid_used_window")
            if lo <= experiment.plan.test_end and hi >= experiment.plan.test_start:
                raise ValueError(
                    "test_window_already_used: retrospective sensitivity is not fresh OOS"
                )
        if experiment.plan.train_end >= experiment.plan.validation_start or experiment.plan.validation_end >= experiment.plan.test_start:
            raise ValueError("formal_validation_windows_must_not_overlap")
        benchmark = manifest.benchmark_config.get("overrides", {})
        benchmark_symbol = benchmark.get("symbol", manifest.benchmark_config.get("symbol")) if isinstance(benchmark, dict) else manifest.benchmark_config.get("symbol")
        if experiment.plan.benchmark_symbol != benchmark_symbol:
            raise ValueError("validation_benchmark_must_match_frozen_source")
        await preflight_spec_window(session, manifest, experiment)
        await asyncio.to_thread(preflight_frozen_policies, manifest)
        frozen = {
            **raw,
            "frozen_manifest": source["manifest"],
            "baseline_manifest_checksum": source["manifest_checksum"],
            "used_windows": used,
            "schema_version": "spec_validation_v1",
        }
    stamp = replace(
        experiment.version_stamp,
        selection_config={
            **experiment.version_stamp.selection_config,
            "validation_trial_runner": frozen,
        },
    )
    return replace(experiment, version_stamp=stamp)


def preflight_frozen_policies(source: ResearchRunManifest) -> None:
    """冻结派生入口仍复用正式入队组合/政策门,执行期硬约束保持双防线。"""
    from finboard_backtest.research_run.config_overrides import (
        research_policy_gate_error,
        research_portfolio_gate_error,
    )
    from finboard_backtest.research_run.portfolio_pipeline import _constraints_from_manifest
    from finboard_backtest.research_run.signal_engine import _bars_release_ref
    from finboard_backtest.strategy_spec.universe_precheck import (
        preview_universe_pool,
        resolvable_feature_names,
    )
    from finboard_data.releases import FrozenReleaseProvider

    _constraints_from_manifest(source)
    error = research_policy_gate_error(execution_model=source.strategy_spec.execution_model,
        fee_overrides=section_overrides(source.fee_config), execution_overrides=section_overrides(source.execution_config),
        validation_overrides=section_overrides(source.validation_config))
    if error:
        raise ValueError(error)
    def provider(release_id: str) -> FrozenReleaseProvider:
        return FrozenReleaseProvider(release_root=os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"),
            release_id=release_id, expected_checksum=next(r.checksum for r in source.dataset_releases if r.artifact_id == release_id))
    bars = provider(_bars_release_ref(source, provider).artifact_id)
    preview = preview_universe_pool(source.strategy_spec.universe, bars.release.instruments,
        decision_date=bars.release.end_date, available_features=resolvable_feature_names(
            feature_graph_sources=[n.source for n in source.strategy_spec.feature_graph.nodes if n.source],
            snapshot_feature_names=[cap.removeprefix("factor:") for ref in (*source.factor_series, *source.factor_snapshots) for cap in ref.capabilities],
            research_release_kinds=[provider(ref.artifact_id).release.dataset_kind for ref in source.dataset_releases]))
    if preview.is_empty:
        raise ValueError("frozen_candidate_pool_empty")
    error = research_portfolio_gate_error(preview=preview, risk_exit_policy=source.strategy_spec.risk_exit_policy,
        portfolio_overrides=section_overrides(source.portfolio_config), risk_overrides=section_overrides(source.risk_config),
        decision_date=bars.release.end_date)
    if error:
        raise ValueError(error)


async def preflight_spec_window(session: AsyncSession, source: ResearchRunManifest, experiment: ResearchExperiment) -> None:
    """入队前校验主发布、所有决策的因子日期/checksum与成交尾段;不读取因子值。"""
    from finboard_backtest.research_run.contracts import resolve_decision_schedule
    from finboard_backtest.research_run.signal_engine import (
        _bars_release_ref,
        _derive_schedule_decision_days,
        _release_trading_days,
    )
    from finboard_backtest.validation.splitter import generate_walk_forward_windows
    from finboard_data.releases import FrozenReleaseProvider

    def provider(release_id: str) -> FrozenReleaseProvider:
        return FrozenReleaseProvider(release_root=os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"),
            release_id=release_id, expected_checksum=next(r.checksum for r in source.dataset_releases if r.artifact_id == release_id))
    bars_ref = _bars_release_ref(source, provider)
    bars = provider(bars_ref.artifact_id)
    calendar = await _release_trading_days(bars)
    schedule = resolve_decision_schedule(source.parameters)
    if schedule is None:
        raise ValueError("spec_validation_requires_multi_period")
    dates = [d.date() for d, _ in await _derive_schedule_decision_days(bars, schedule)]
    plan = experiment.plan
    if not calendar or plan.train_start < calendar[0] or plan.test_end > calendar[-1]:
        raise ValueError("validation_window_outside_release")
    raw = experiment.version_stamp.selection_config.get("validation_trial_runner", {})
    if isinstance(raw, dict) and raw.get("warmup_start", bars.release.start_date.isoformat()) != bars.release.start_date.isoformat():
        raise ValueError("unsupported_warmup_lower_bound: frozen release start required")
    windows = [(plan.train_start, plan.train_end), (plan.validation_start, plan.validation_end), (plan.test_start, plan.test_end)]
    walk = generate_walk_forward_windows(mode=plan.mode, train_start=plan.train_start, train_end=plan.train_end,
        test_start=plan.validation_start, test_end=plan.validation_end,
        train_window_days=plan.train_window_days, test_window_days=plan.test_window_days, step_days=plan.step_days)
    windows.extend((w.test.start, w.test.end) for w in walk)
    if len(windows) > 100:
        raise ValueError("validation_window_budget_exceeded: 100 windows")
    for lo, hi in windows:
        selected = [d for d in dates if lo <= d < hi]
        if not selected:
            raise ValueError("validation_window_no_decisions")
        if not any(selected[-1] < d <= hi for d in calendar):
            raise ValueError("validation_execution_tail_missing")
    needed = {d.isoformat() for d in dates if plan.train_start <= d < plan.test_end}
    covered: set[str] = set()
    for ref in source.factor_series:
        row = (await session.execute(text("""
            SELECT code_artifact,release_id,content_checksum,
            CASE WHEN octet_length(dates::text)<=1048576 THEN dates END AS dates
            FROM research_factor_series WHERE series_id=:id
        """), {"id": ref.artifact_id})).mappings().first()
        if row is None or row["content_checksum"] != ref.checksum or row["release_id"] != bars_ref.artifact_id:
            raise ValueError(f"validation_factor_anchor_mismatch: {ref.artifact_id}")
        if row["dates"] is None or needed - set(row["dates"]):
            raise ValueError(f"validation_factor_coverage_missing: {ref.artifact_id}")
        covered.update(ref.capabilities)
    missing = {node.source for node in source.strategy_spec.feature_graph.nodes if node.source and node.source.startswith(("p_", "u_")) and f"factor:{node.source}" not in covered}
    if missing:
        raise ValueError(f"validation_factor_series_required: {sorted(missing)}")


def stress_fee_values(execution: Any, overrides: dict[str, object]) -> dict[str, Any]:
    if set(overrides) - {"cost_multiplier", "slippage_bps", "execution_delay_bars"}:
        raise ValueError("unsupported_config_override")
    delay = overrides.get("execution_delay_bars", 1)
    if isinstance(delay, bool) or delay != 1:
        raise ValueError("unsupported_execution_delay: only baseline next bar is supported")
    values: dict[str, Any] = {}
    if "cost_multiplier" in overrides:
        try:
            multiplier = Decimal(str(overrides["cost_multiplier"]))
        except InvalidOperation as exc:
            raise ValueError("invalid_cost_multiplier") from exc
        if not multiplier.is_finite() or not 0 < multiplier <= 10:
            raise ValueError("invalid_cost_multiplier")
        for key in ["commission_rate", "minimum_commission", "sell_tax_rate"]:
            values[key] = float(Decimal(str(getattr(execution, key))) * multiplier)
    if "slippage_bps" in overrides:
        try:
            value = Decimal(str(overrides["slippage_bps"]))
        except InvalidOperation as exc:
            raise ValueError("invalid_slippage_bps") from exc
        if not value.is_finite() or not 0 <= value <= 1000:
            raise ValueError("invalid_slippage_bps")
        values["slippage_bps"] = float(value)
    return values


def explicit_fee_base(execution: Any) -> dict[str, Any]:
    return {key: getattr(execution, key) for key in ("commission_rate", "minimum_commission", "sell_tax_rate", "slippage_bps")}


def child_manifest(
    source: ResearchRunManifest,
    *,
    experiment_id: str,
    start: date,
    end: date,
    warmup_start: date,
    dates: list[date],
    overrides: dict[str, object],
) -> ResearchRunManifest:
    selected = [d for d in dates if start <= d < end]
    if not selected:
        raise ValueError("validation_window_no_decisions")
    effective = merge_fee_overrides(
        source.strategy_spec.execution_model, section_overrides(source.fee_config)
    )
    fee = {
        **source.fee_config,
        "overrides": {
            **explicit_fee_base(effective),
            **section_overrides(source.fee_config),
            **stress_fee_values(effective, overrides),
        },
    }
    params: dict[str, Any] = {
        **source.parameters,
        "fee_policy_version": "explicit_overrides_v1",
        "decision_schedule": {"kind": "custom", "dates": [d.isoformat() for d in selected]},
        "research_window": {
            "warmup_start": warmup_start.isoformat(),
            "decision_start": start.isoformat(),
            "decision_end": selected[-1].isoformat(),
            "valuation_end": end.isoformat(),
        },
        "validation_binding": {
            "experiment_id": experiment_id,
            "baseline_run_id": source.run_id,
            "baseline_input_checksum": source.input_checksum,
            "overrides": overrides,
            "runner_schema": "spec_validation_v1",
        },
    }
    params.pop("rebalance_frequency", None)
    key = stable_checksum(
        {"experiment": experiment_id, "source": source.input_checksum, "params": params, "fee": fee}
    )
    return replace(
        source,
        run_id=f"RR-{key[:24]}",
        idempotency_key=f"validation:{key}",
        parameters=params,
        fee_config=fee,
        replay_of_run_id=None,
        replay_source_status=None,
    )


async def spec_trial_runner_factory(
    session: AsyncSession, experiment: ResearchExperiment
) -> TrialRunner:
    from finboard_app.config import Settings
    from finboard_backtest.background_jobs.executors.validation_experiment import (
        validation_job_id,
        validation_progress,
    )
    from finboard_backtest.research_run.contracts import resolve_decision_schedule
    from finboard_backtest.research_run.signal_engine import (
        _bars_release_ref,
        _derive_schedule_decision_days,
        _load_benchmark_curve,
        _release_trading_days,
    )
    from finboard_data.releases import FrozenReleaseProvider

    config = experiment.version_stamp.selection_config["validation_trial_runner"]
    if not isinstance(config, dict) or config.get("schema_version") != "spec_validation_v1":
        raise ValueError(
            "spec_runner_not_frozen: create a new experiment via the explicit entrance"
        )
    source = manifest_from_json(config["frozen_manifest"])
    settings = Settings()
    makers = async_sessionmaker(session.bind, expire_on_commit=False)
    adapters = build_signal_engine_adapter_factory(makers, settings_factory=lambda: settings)

    def provider_factory(release_id: str) -> FrozenReleaseProvider:
        return FrozenReleaseProvider(
            release_root=os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases"),
            release_id=release_id,
            expected_checksum=next(
                r.checksum for r in source.dataset_releases if r.artifact_id == release_id
            ),
        )

    bars = provider_factory(_bars_release_ref(source, provider_factory).artifact_id)
    calendar = await _release_trading_days(bars)
    schedule = resolve_decision_schedule(source.parameters)
    if schedule is None:
        raise ValueError("spec_validation_requires_multi_period")
    dates = [d.date() for d, _ in await _derive_schedule_decision_days(bars, schedule)]
    warmup = date.fromisoformat(config.get("warmup_start", bars.release.start_date.isoformat()))
    if warmup != bars.release.start_date:
        raise ValueError("unsupported_warmup_lower_bound: frozen release start required")
    if experiment.plan.train_start < calendar[0] or experiment.plan.test_end > calendar[-1]:
        raise ValueError("validation_window_outside_release")
    store = SessionPerOperationResearchRunStore(
        session_maker=makers, store_factory=default_store_factory
    )
    progress = validation_progress.get()
    parent_job_id = validation_job_id.get()

    async def run(
        *,
        start: date,
        end: date,
        params: dict[str, object],
        config_overrides: dict[str, object] | None = None,
    ) -> BacktestResult:
        if params:
            raise ValueError("unsupported_spec_parameters")
        manifest = child_manifest(
            source,
            experiment_id=experiment.experiment_id,
            start=start,
            end=end,
            warmup_start=warmup,
            dates=dates,
            overrides=config_overrides or {},
        )
        # 预检最后决策后的成交日与因子覆盖;执行端同样按 PIT/coverage fail-closed。
        from finboard_backtest.research_run.window import research_window

        window = research_window(manifest.parameters)
        assert window is not None
        if not any(window["decision_end"] < d <= end for d in calendar):
            raise ValueError("validation_execution_tail_missing")
        if parent_job_id:
            from sqlalchemy import update

            from finboard_persistence.models import ResearchRunModel
            await store.create_or_get(manifest)
            async with makers() as child_session:
                await child_session.execute(update(ResearchRunModel).where(ResearchRunModel.run_id == manifest.run_id).values(job_id=parent_job_id))
                await child_session.commit()
        record = await ResearchRunCoordinator(store).execute(manifest, adapters(manifest), progress=progress)
        if record.status is not ResearchRunStatus.COMPLETED or record.result is None:
            raise ValueError(f"spec_trial_{record.status.value}: {record.error_summary}")
        report = record.result
        from finboard_app.research_diagnostics import interval_report
        async with makers() as report_session:
            diagnostic = await interval_report(report_session, run_id=manifest.run_id,
                start=start.isoformat(), end=end.isoformat(), yearly=False)
        period = diagnostic["period"]
        mean_equity = sum(float(p.equity) for p in report.equity_curve) / len(report.equity_curve)
        turnover = float(period["fills"]["notional"]) / mean_equity * 252 / len(report.equity_curve)
        result = BacktestResult(
            equity_curve=[(p.trade_date, p.equity) for p in report.equity_curve],
            initial_capital=manifest.initial_capital,
            final_equity=report.final_equity,
            start_date=start,
            end_date=end,
            commission_paid=report.commission_paid,
            stamp_tax_paid=report.tax_paid,
            risk_free_annual=0.0,
        )
        result.benchmark_curve = list(await _load_benchmark_curve(manifest, provider_factory))
        if experiment.plan.benchmark_symbol and not result.benchmark_curve:
            raise ValueError("validation_benchmark_missing")
        result.matching_model = {
            "validation_evidence": {
                "run_id": manifest.run_id,
                "manifest_checksum": manifest.checksum,
                "result_checksum": record.result_checksum,
                "baseline_run_id": source.run_id,
                "window": manifest.parameters["research_window"],
                "effective_fee": merge_fee_overrides(
                    source.strategy_spec.execution_model, section_overrides(manifest.fee_config)
                ).model_dump(mode="json"),
                "overrides": config_overrides or {},
                "fill_count": report.fill_count,
                "research_metrics": {"annualized_return": report.annualized_return,
                                     "sharpe_ratio": report.sharpe_ratio, "turnover": turnover},
                "metric_convention": "research_rf0_ddof1_252",
                "diagnostic": period,
                "fee_policy_version": manifest.parameters["fee_policy_version"],
                "commission": str(report.commission_paid),
                "tax": str(report.tax_paid),
                "slippage": str(report.slippage_paid),
                "fill_shortfall": str(report.fill_shortfall),
                "unsupported": ["order_book_impact", "execution_delay_bars>1"],
            }
        }
        return result

    return run
