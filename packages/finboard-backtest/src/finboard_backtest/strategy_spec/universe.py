"""候选池纳入/排除的可解释纯函数。"""

from __future__ import annotations

from dataclasses import dataclass, field

from finboard_backtest.strategy_spec.contracts import (
    MissingDataPolicy,
    RankingDirection,
    UniverseSpec,
)
from finboard_shared.types import AssetClass, Market


@dataclass(frozen=True, slots=True)
class UniverseCandidate:
    symbol: str
    market: Market
    asset_class: AssetClass
    listing_days: int
    average_amount: float | None
    price: float | None
    suspended: bool = False
    delisted: bool = False
    is_st: bool = False
    active_events: tuple[str, ...] = ()
    data_completeness: float = 1.0
    fields: dict[str, float | str | bool | None] = field(default_factory=dict)

    def value(self, name: str) -> float | str | bool | None:
        builtins: dict[str, float | str | bool | None] = {
            "listing_days": self.listing_days,
            "average_amount": self.average_amount,
            "price": self.price,
            "data_completeness": self.data_completeness,
        }
        return builtins.get(name, self.fields.get(name))


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    symbol: str
    included: bool
    reasons: tuple[str, ...]
    rank: int | None = None


def explain_universe(
    spec: UniverseSpec,
    candidates: tuple[UniverseCandidate, ...],
) -> tuple[UniverseDecision, ...]:
    """按静态过滤、缺失处理和排名顺序返回每个标的的决策原因。"""

    prelim: dict[str, list[str]] = {}
    eligible: list[UniverseCandidate] = []
    explicit = set(spec.explicit_symbols)
    excluded_events = set(spec.excluded_event_types)

    for candidate in candidates:
        reasons: list[str] = []
        if candidate.market not in spec.markets:
            reasons.append("market_not_allowed")
        if candidate.asset_class not in spec.asset_classes:
            reasons.append("asset_class_not_allowed")
        if explicit and candidate.symbol not in explicit:
            reasons.append("not_in_explicit_symbols")
        if candidate.listing_days < spec.min_listing_days:
            reasons.append("listing_age_below_minimum")
        if spec.min_average_amount is not None:
            if candidate.average_amount is None:
                reasons.append("missing_average_amount")
            elif candidate.average_amount < spec.min_average_amount:
                reasons.append("liquidity_below_minimum")
        if spec.min_price is not None:
            if candidate.price is None:
                reasons.append("missing_price")
            elif candidate.price < spec.min_price:
                reasons.append("price_below_minimum")
        if spec.max_price is not None:
            if candidate.price is None:
                reasons.append("missing_price")
            elif candidate.price > spec.max_price:
                reasons.append("price_above_maximum")
        if spec.exclude_suspended and candidate.suspended:
            reasons.append("suspended")
        if spec.exclude_delisted and candidate.delisted:
            reasons.append("delisted")
        if spec.exclude_st and candidate.is_st:
            reasons.append("st_security")
        if excluded_events.intersection(candidate.active_events):
            reasons.append("excluded_event")
        if candidate.data_completeness < spec.min_data_completeness:
            reasons.append("data_completeness_below_minimum")
        for field_name in spec.required_data_fields:
            if candidate.value(field_name) is None:
                reasons.append(f"missing_required_field:{field_name}")
        if (
            spec.ranking is not None
            and candidate.value(spec.ranking.field) is None
            and spec.missing_data_policy is MissingDataPolicy.EXCLUDE
        ):
            reasons.append(f"missing_ranking_field:{spec.ranking.field}")

        prelim[candidate.symbol] = reasons
        if not reasons:
            eligible.append(candidate)

    ranking = spec.ranking
    if ranking is not None:
        reverse = ranking.direction is RankingDirection.TOP

        def rank_key(candidate: UniverseCandidate) -> tuple[bool, float]:
            raw = candidate.value(ranking.field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return (True, 0.0)
            value = float(raw)
            return (False, -value if reverse else value)

        eligible.sort(key=lambda item: (*rank_key(item), item.symbol))
    else:
        eligible.sort(key=lambda item: item.symbol)

    selected = eligible[: spec.selection_limit]
    selected_ranks = {candidate.symbol: index + 1 for index, candidate in enumerate(selected)}
    eligible_symbols = {candidate.symbol for candidate in eligible}
    decisions: list[UniverseDecision] = []
    for candidate in candidates:
        reasons = list(prelim[candidate.symbol])
        if not reasons and candidate.symbol in eligible_symbols:
            if candidate.symbol in selected_ranks:
                reasons.append("included")
            else:
                reasons.append("outside_selection_limit")
        decisions.append(
            UniverseDecision(
                symbol=candidate.symbol,
                included=candidate.symbol in selected_ranks,
                reasons=tuple(reasons),
                rank=selected_ranks.get(candidate.symbol),
            )
        )
    return tuple(decisions)


__all__ = ["UniverseCandidate", "UniverseDecision", "explain_universe"]
