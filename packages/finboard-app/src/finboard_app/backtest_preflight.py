"""Read-only parameter and data checks before an exploratory backtest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.background_jobs import payload_checksum
from finboard_app.selection_schema import FactorSelectionParams
from finboard_app.strategies import get_strategy_definition
from finboard_backtest.config import BenchmarkConfig, MatchingModel
from finboard_persistence import ResearchDatasetRepository


async def describe_backtest_inputs(
    session: AsyncSession,
    request: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    definition = get_strategy_definition(str(request["strategy"]))
    if not definition.supports_backtest:
        raise ValueError("unsupported_backtest: the selected strategy is unavailable in historical replay")
    if not request["symbols"]:
        raise ValueError("empty_backtest_universe: choose at least one instrument")
    if date.fromisoformat(request["start"]) > date.fromisoformat(request["end"]):
        raise ValueError("invalid_backtest_window: start is later than end")
    if Decimal(str(request["capital"])) <= 0:
        raise ValueError("invalid_backtest_capital: use positive initial capital")
    if any(Decimal(str(request[key])) < 0 for key in (
        "commission_rate", "commission_min", "stamp_tax_rate", "slippage_bps",
    )):
        raise ValueError("invalid_backtest_fees: use nonnegative costs")
    selection = FactorSelectionParams.model_validate(request["selection"])
    batches, unpublished = await ResearchDatasetRepository(session).resolve_selection_batches(selection.to_domain())
    if unpublished:
        raise ValueError("dataset_unpublished: " + ", ".join(unpublished))
    effective_selection = selection.model_dump(mode="json")
    effective_selection["dataset_versions"] = {
        **selection.dataset_versions,
        **{name: batch.dataset_version for name, batch in batches.items()},
    }
    resolved = {
        "request": {
            **request,
            "params": definition.params_model.model_validate(request["params"]).model_dump(mode="json"),
            "selection": effective_selection,
        },
        "provider_name": provider,
        "benchmark": BenchmarkConfig(**(request.get("benchmark") or {})).as_dict(),
        "matching": MatchingModel().as_dict(),
    }
    return {"preview_checksum": payload_checksum(resolved), **resolved}
