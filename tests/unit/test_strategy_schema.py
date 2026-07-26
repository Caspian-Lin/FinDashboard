"""内置策略参数 schema 单元测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from finboard_app.strategies import (
    create_strategy,
    get_strategy_definition,
    list_strategy_definitions,
)
from finboard_app.strategies.etf_dca import EtfDcaStrategy
from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_shared.types import OrderType


def test_registry_exposes_unique_typed_definitions() -> None:
    definitions = list_strategy_definitions()
    assert {definition.kind for definition in definitions} == {
        "periodic_query",
        "etf_dca",
        "ma_cross",
    }
    assert len({definition.kind for definition in definitions}) == len(definitions)
    assert get_strategy_definition("ma_cross").supports_backtest is True
    assert get_strategy_definition("etf_dca").supports_backtest is False


def test_create_ma_cross_coerces_and_validates_params() -> None:
    strategy = create_strategy(
        "ma_cross",
        "schema-test",
        short_window="8",
        long_window="30",
        max_position_pct="0.8",
    )
    assert isinstance(strategy, MaCrossStrategy)
    assert strategy._short_window == 8
    assert strategy._long_window == 30
    assert strategy._max_position_pct == Decimal("0.8")


@pytest.mark.parametrize(
    "params",
    [
        {"short_window": 20, "long_window": 5},
        {"short_window": 0, "long_window": 20},
        {"short_window": 5, "long_window": 20, "unknown": True},
    ],
)
def test_ma_cross_rejects_invalid_params(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        create_strategy("ma_cross", "invalid", **params)


def test_etf_dca_limit_order_requires_price() -> None:
    with pytest.raises(ValidationError, match="限价单必须填写价格"):
        create_strategy(
            "etf_dca",
            "dca-invalid",
            symbol_code="510300.SH",
            quantity="100",
            order_type="limit",
        )

    strategy = create_strategy(
        "etf_dca",
        "dca-valid",
        symbol_code="510300.SH",
        quantity="100",
        order_type="limit",
        price="4.25",
    )
    assert isinstance(strategy, EtfDcaStrategy)
    assert strategy._quantity == Decimal("100")
    assert strategy._order_type is OrderType.LIMIT
    assert strategy._price == Decimal("4.25")


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知策略类型"):
        create_strategy("uploaded_module", "unsafe")
