from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from finboard_backtest.research_run import (
    EquityPoint,
    LedgerSnapshot,
    ResearchActorType,
    ResearchExecutionMode,
    ResearchRunReport,
    execution_mode_for,
    manifest_from_json,
    report_from_json,
    to_json_value,
)


def test_manifest_round_trip_freezes_every_input(manifest_factory) -> None:
    manifest = manifest_factory()
    payload = to_json_value(manifest)
    assert isinstance(payload, dict)

    restored = manifest_from_json(payload)

    assert restored == manifest
    assert restored.checksum == manifest.checksum
    assert restored.dataset_releases[0].checksum == "a" * 64
    assert restored.factor_snapshots[0].checksum == "b" * 64


def test_execution_mode_for_rebalance_frequency() -> None:
    assert (
        execution_mode_for({"rebalance_frequency": "monthly"}) is ResearchExecutionMode.MULTI_PERIOD
    )
    assert (
        execution_mode_for({"rebalance_frequency": "quarterly"})
        is ResearchExecutionMode.MULTI_PERIOD
    )
    assert execution_mode_for({}) is ResearchExecutionMode.SINGLE_SHOT
    # 非法值按 single_shot 兜底(执行时由信号引擎 fail-closed)。
    assert (
        execution_mode_for({"rebalance_frequency": "weekly"}) is ResearchExecutionMode.SINGLE_SHOT
    )


def test_report_round_trip_with_equity_curve() -> None:
    """多期报告:execution_mode/equity_curve/annualized 完整 JSON 往返。"""
    report = ResearchRunReport(
        strategy_kind="multi_factor",
        strategy_return=0.123,
        benchmark_symbol="510300.SH",
        benchmark_return=0.05,
        excess_return=0.073,
        sharpe_ratio=1.2,
        max_drawdown=0.1,
        final_equity=Decimal("112300"),
        final_cash=Decimal("12300"),
        commission_paid=Decimal("100"),
        tax_paid=Decimal("0"),
        slippage_paid=Decimal("50"),
        fill_shortfall=Decimal("0"),
        constraint_impact={"max_weight": 0.0},
        decision_count=3,
        order_count=3,
        fill_count=3,
        execution_mode=ResearchExecutionMode.MULTI_PERIOD,
        annualized_return=0.25,
        equity_curve=(
            EquityPoint(trade_date=date(2024, 1, 31), equity=Decimal("100000")),
            EquityPoint(trade_date=date(2024, 2, 29), equity=Decimal("112300")),
        ),
    )
    payload = to_json_value(report)
    assert isinstance(payload, dict)
    restored = report_from_json(cast(dict[str, object], payload))
    assert restored == report
    assert restored.execution_mode is ResearchExecutionMode.MULTI_PERIOD
    assert restored.equity_curve[0].trade_date == date(2024, 1, 31)


def test_report_round_trip_legacy_payload_defaults() -> None:
    """旧版 result JSON(无新字段)反序列化时回落到 single_shot 默认。"""
    report = ResearchRunReport(
        strategy_kind="multi_factor",
        strategy_return=0.0,
        benchmark_symbol="510300.SH",
        benchmark_return=0.0,
        excess_return=0.0,
        sharpe_ratio=0.0,
        max_drawdown=0.0,
        final_equity=Decimal("100000"),
        final_cash=Decimal("100000"),
        commission_paid=Decimal("0"),
        tax_paid=Decimal("0"),
        slippage_paid=Decimal("0"),
        fill_shortfall=Decimal("0"),
        constraint_impact={},
        decision_count=1,
        order_count=0,
        fill_count=0,
    )
    payload = cast(dict[str, object], to_json_value(report))
    payload.pop("execution_mode")
    payload.pop("annualized_return")
    payload.pop("equity_curve")
    restored = report_from_json(payload)
    assert restored.execution_mode is ResearchExecutionMode.SINGLE_SHOT
    assert restored.annualized_return == 0.0
    assert restored.equity_curve == ()
    assert restored == report


@pytest.mark.parametrize("capital", ["99999", "500001"])
def test_manifest_rejects_capital_outside_milestone_range(manifest_factory, capital: str) -> None:
    manifest = manifest_factory()
    with pytest.raises(ValueError, match="10 万至 50 万元"):
        replace(manifest, initial_capital=Decimal(capital))


def test_manifest_rejects_llm_trigger(manifest_factory) -> None:
    manifest = manifest_factory()
    with pytest.raises(ValueError, match="LLM"):
        replace(manifest, actor_type=ResearchActorType.LLM)


def test_manifest_rejects_executable_strategy_parameter(manifest_factory) -> None:
    manifest = manifest_factory()
    with pytest.raises(ValueError, match="禁止提交代码"):
        replace(
            manifest,
            parameters={"python_code": "print('never execute')"},
        )


def test_manifest_rejects_strategy_or_dataset_checksum_drift(manifest_factory) -> None:
    manifest = manifest_factory()
    with pytest.raises(ValueError, match="strategy_spec_checksum"):
        replace(manifest, strategy_spec_checksum="0" * 64)
    with pytest.raises(ValueError, match="验证计划"):
        replace(
            manifest,
            dataset_releases=(
                replace(
                    manifest.dataset_releases[0],
                    artifact_id="different-release",
                ),
            ),
        )


def test_ledger_enforces_accounting_identity() -> None:
    with pytest.raises(ValueError, match="权益恒等式"):
        LedgerSnapshot(
            cash=Decimal("10"),
            market_value=Decimal("20"),
            margin_used=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            equity=Decimal("31"),
            fees_paid=Decimal("0"),
            tax_paid=Decimal("0"),
            slippage_paid=Decimal("0"),
        )
