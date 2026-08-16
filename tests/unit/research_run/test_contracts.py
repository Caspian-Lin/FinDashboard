from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from finboard_backtest.research_run import (
    LedgerSnapshot,
    ResearchActorType,
    manifest_from_json,
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


@pytest.mark.parametrize("capital", ["99999", "500001"])
def test_manifest_rejects_capital_outside_milestone_range(
    manifest_factory, capital: str
) -> None:
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
