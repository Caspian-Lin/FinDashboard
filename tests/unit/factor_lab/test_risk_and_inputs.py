"""基础风险模型与跨市场时点输入测试(issue #78)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from finboard_backtest.factor_lab import (
    MarketInputError,
    MarketInputObservation,
    RiskModelError,
    build_cross_market_snapshot,
    estimate_basic_risk_model,
)

_DECISION = datetime(2024, 1, 10, 16, tzinfo=UTC)
_REQUIRED = (
    "risk_free_rate",
    "government_bond_return",
    "fx_usdcny_return",
    "gold_return",
    "market_breadth",
    "volatility_regime",
)


def _risk_inputs() -> tuple[
    dict[str, np.ndarray],
    np.ndarray,
    dict[str, str],
    dict[str, str],
    dict[str, float],
    dict[str, float],
]:
    rng = np.random.default_rng(78)
    benchmark = rng.normal(0.0003, 0.01, 120)
    returns = {
        "A": np.asarray(1.2 * benchmark + rng.normal(0, 0.003, 120)),
        "B": np.asarray(0.7 * benchmark + rng.normal(0, 0.004, 120)),
        "C": np.asarray(-0.1 * benchmark + rng.normal(0, 0.002, 120)),
    }
    industries = {"A": "bank", "B": "tech", "C": "bond"}
    asset_classes = {"A": "equity", "B": "equity", "C": "fixed_income"}
    market_caps = {"A": 1e11, "B": 5e10, "C": 2e10}
    liquidity = {"A": 1e9, "B": 5e8, "C": 2e8}
    return (
        returns,
        np.asarray(benchmark),
        industries,
        asset_classes,
        market_caps,
        liquidity,
    )


def test_basic_risk_model_keeps_exposures_separate_from_alpha() -> None:
    (
        returns,
        benchmark,
        industries,
        asset_classes,
        market_caps,
        liquidity,
    ) = _risk_inputs()
    model = estimate_basic_risk_model(
        as_of=_DECISION,
        returns_by_symbol=returns,
        benchmark_returns=benchmark,
        industries=industries,
        asset_classes=asset_classes,
        market_caps=market_caps,
        liquidity=liquidity,
        portfolio_weights={"A": 0.4, "B": 0.4, "C": 0.2},
    )
    exposures = {item.symbol: item for item in model.exposures}
    assert exposures["A"].market_beta > exposures["B"].market_beta
    assert exposures["C"].asset_class == "fixed_income"
    assert model.covariance_shrinkage >= 0
    assert model.observations == 120
    assert model.portfolio_volatility > 0
    assert sum(model.component_risk_contribution.values()) == pytest.approx(
        model.portfolio_volatility
    )
    assert set(model.marginal_risk_contribution) == {"A", "B", "C"}
    assert model.as_dict()["covariance_method"] == "ledoit_wolf"


def test_risk_model_fails_closed_on_metadata_and_singular_benchmark() -> None:
    (
        returns,
        benchmark,
        industries,
        asset_classes,
        market_caps,
        liquidity,
    ) = _risk_inputs()
    del industries["B"]
    with pytest.raises(RiskModelError, match="元数据缺失"):
        estimate_basic_risk_model(
            as_of=_DECISION,
            returns_by_symbol=returns,
            benchmark_returns=benchmark,
            industries=industries,
            asset_classes=asset_classes,
            market_caps=market_caps,
            liquidity=liquidity,
        )
    industries["B"] = "tech"
    with pytest.raises(RiskModelError, match="方差为零"):
        estimate_basic_risk_model(
            as_of=_DECISION,
            returns_by_symbol=returns,
            benchmark_returns=np.zeros(120),
            industries=industries,
            asset_classes=asset_classes,
            market_caps=market_caps,
            liquidity=liquidity,
        )
    with pytest.raises(RiskModelError, match="权重必须为有限数"):
        estimate_basic_risk_model(
            as_of=_DECISION,
            returns_by_symbol=returns,
            benchmark_returns=benchmark,
            industries=industries,
            asset_classes=asset_classes,
            market_caps=market_caps,
            liquidity=liquidity,
            portfolio_weights={"A": float("nan")},
        )


def _market_input(
    name: str,
    value: float,
    *,
    available_at: datetime = _DECISION - timedelta(hours=1),
) -> MarketInputObservation:
    return MarketInputObservation(
        feature_name=name,
        value=value,
        observed_at=available_at,
        available_at=available_at,
        source="fixed_macro",
        source_version="2024-01-10",
    )


def test_cross_market_inputs_are_point_in_time_and_auditable() -> None:
    observations = [
        _market_input(name, float(index) / 10)
        for index, name in enumerate(_REQUIRED)
    ]
    # 同名未来记录不得覆盖决策时点前的记录。
    observations.append(
        _market_input(
            "risk_free_rate",
            99.0,
            available_at=_DECISION + timedelta(seconds=1),
        )
    )
    snapshot = build_cross_market_snapshot(
        decision_at=_DECISION,
        observations=observations,
    )
    values = {item.feature_name: item.value for item in snapshot.features}
    assert values["risk_free_rate"] == 0.0
    assert all(item.available_at <= _DECISION for item in snapshot.features)
    assert snapshot.missing_policies["gold_return"] == "fail_closed"
    assert snapshot.as_dict()["decision_at"] == _DECISION.isoformat()


def test_cross_market_inputs_fail_on_missing_or_stale_data() -> None:
    with pytest.raises(MarketInputError, match="缺少"):
        build_cross_market_snapshot(
            decision_at=_DECISION,
            observations=[_market_input("risk_free_rate", 0.02)],
        )
    stale = [
        _market_input(
            name,
            float(index),
            available_at=_DECISION - timedelta(days=10),
        )
        for index, name in enumerate(_REQUIRED)
    ]
    with pytest.raises(MarketInputError, match="已过期"):
        build_cross_market_snapshot(
            decision_at=_DECISION,
            observations=stale,
        )
