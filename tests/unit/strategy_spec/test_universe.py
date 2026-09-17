"""候选池纳入/排除解释测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finboard_backtest.strategy_spec import (
    MissingDataPolicy,
    RankingDirection,
    UniverseCandidate,
    UniverseRanking,
    UniverseSpec,
    explain_universe,
)
from finboard_shared.types import AssetClass, Market


def test_universe_explains_filters_missing_values_and_rank_limit() -> None:
    spec = UniverseSpec(
        markets=(Market.A_SHARE,),
        asset_classes=(AssetClass.EQUITY,),
        min_listing_days=60,
        min_average_amount=1_000_000,
        min_price=1,
        exclude_suspended=True,
        excluded_event_types=("delisting_warning",),
        required_data_fields=("market_cap",),
        ranking=UniverseRanking(
            field="momentum",
            direction=RankingDirection.TOP,
        ),
        selection_limit=1,
        missing_data_policy=MissingDataPolicy.EXCLUDE,
    )
    candidates = (
        UniverseCandidate(
            symbol="510300.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=1000,
            average_amount=10_000_000,
            price=4,
            fields={"market_cap": 100, "momentum": 0.1},
        ),
        UniverseCandidate(
            symbol="510500.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=1000,
            average_amount=8_000_000,
            price=6,
            fields={"market_cap": 200, "momentum": 0.2},
        ),
        UniverseCandidate(
            symbol="510050.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=1000,
            average_amount=8_000_000,
            price=3,
            suspended=True,
            fields={"market_cap": 300, "momentum": 0.3},
        ),
        UniverseCandidate(
            symbol="MISSING.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=1000,
            average_amount=8_000_000,
            price=3,
            fields={"market_cap": 100, "momentum": None},
        ),
    )
    decisions = {item.symbol: item for item in explain_universe(spec, candidates)}
    assert decisions["510500.SH"].included is True
    assert decisions["510500.SH"].rank == 1
    assert decisions["510300.SH"].reasons == ("outside_selection_limit",)
    assert decisions["510050.SH"].reasons == ("suspended",)
    assert decisions["MISSING.SH"].reasons == ("missing_ranking_field:momentum",)


def test_universe_fail_closed_for_incomplete_and_event_risk() -> None:
    spec = UniverseSpec(
        markets=(Market.A_SHARE,),
        asset_classes=(AssetClass.CONVERTIBLE,),
        min_data_completeness=0.99,
        excluded_event_types=("forced_redemption",),
        selection_limit=10,
    )
    decisions = explain_universe(
        spec,
        (
            UniverseCandidate(
                symbol="110001.SH",
                market=Market.A_SHARE,
                asset_class=AssetClass.CONVERTIBLE,
                listing_days=100,
                average_amount=5_000_000,
                price=110,
                active_events=("forced_redemption",),
                data_completeness=0.9,
            ),
        ),
    )
    assert decisions[0].included is False
    assert set(decisions[0].reasons) == {
        "excluded_event",
        "data_completeness_below_minimum",
    }


def test_market_cap_bounds_filter_and_missing_exclusion() -> None:
    """issue #213:min/max_market_cap 过滤生效,缺市值按 missing_market_cap 排除。"""
    spec = UniverseSpec(
        markets=(Market.A_SHARE,),
        asset_classes=(AssetClass.EQUITY,),
        min_market_cap=1e10,
        max_market_cap=5e10,
    )
    candidates = (
        UniverseCandidate(
            symbol="IN.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=100,
            average_amount=1_000_000,
            price=10,
            market_cap=2e10,
        ),
        UniverseCandidate(
            symbol="SMALL.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=100,
            average_amount=1_000_000,
            price=10,
            market_cap=5e9,
        ),
        UniverseCandidate(
            symbol="LARGE.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=100,
            average_amount=1_000_000,
            price=10,
            market_cap=8e10,
        ),
        UniverseCandidate(
            symbol="NOCAP.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            listing_days=100,
            average_amount=1_000_000,
            price=10,
            market_cap=None,
        ),
    )
    decisions = {item.symbol: item for item in explain_universe(spec, candidates)}
    assert decisions["IN.SH"].included is True
    assert decisions["SMALL.SH"].reasons == ("market_cap_below_minimum",)
    assert decisions["LARGE.SH"].reasons == ("market_cap_above_maximum",)
    assert decisions["NOCAP.SH"].reasons == ("missing_market_cap",)


def test_st_security_excluded_when_exclude_st_enabled() -> None:
    """issue #213:exclude_st=True 时 ST 候选被排除;关闭后不排除。"""
    candidate_args: dict[str, object] = {
        "symbol": "600001.SH",
        "market": Market.A_SHARE,
        "asset_class": AssetClass.EQUITY,
        "listing_days": 100,
        "average_amount": 1_000_000,
        "price": 10,
        "is_st": True,
    }
    excluded = explain_universe(
        UniverseSpec(
            markets=(Market.A_SHARE,),
            asset_classes=(AssetClass.EQUITY,),
            exclude_st=True,
        ),
        (UniverseCandidate(**candidate_args),),  # type: ignore[arg-type]
    )
    assert excluded[0].included is False
    assert excluded[0].reasons == ("st_security",)

    allowed = explain_universe(
        UniverseSpec(
            markets=(Market.A_SHARE,),
            asset_classes=(AssetClass.EQUITY,),
            exclude_st=False,
        ),
        (UniverseCandidate(**candidate_args),),  # type: ignore[arg-type]
    )
    assert allowed[0].included is True


def test_market_cap_bounds_validation_rejects_inverted_range() -> None:
    """min_market_cap > max_market_cap 在契约校验期即拒绝。"""
    with pytest.raises(ValidationError, match="min_market_cap"):
        UniverseSpec(
            markets=(Market.A_SHARE,),
            asset_classes=(AssetClass.EQUITY,),
            min_market_cap=5e10,
            max_market_cap=1e10,
        )
