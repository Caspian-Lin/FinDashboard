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
    # 默认 universe_mode=all 保持现有行为
    assert strategy._universe.config.enabled() is False


@pytest.mark.parametrize(
    "params",
    [
        {"short_window": 20, "long_window": 5},
        {"short_window": 0, "long_window": 20},
        {"short_window": 5, "long_window": 20, "unknown": True},
        {"universe_mode": "factor"},  # 非法模式
        {"universe_lookback": 0},  # 窗口必须 >= 1
        {"universe_min_avg_amount": "-1"},  # 不能为负
    ],
)
def test_ma_cross_rejects_invalid_params(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        create_strategy("ma_cross", "invalid", **params)


def test_ma_cross_universe_params_round_trip_and_serialize() -> None:
    """universe 参数经 schema 校验、序列化后再载入仍保持等价(预设可复用)。"""
    raw = {
        "short_window": 5,
        "long_window": 20,
        "universe_mode": "liquidity_momentum",
        "universe_lookback": 30,
        "universe_min_avg_amount": "10000000",
        "universe_min_momentum": "0.05",
        "universe_exit_clear": True,
    }
    strategy = create_strategy("ma_cross", "preset", **raw)
    assert isinstance(strategy, MaCrossStrategy)
    config = strategy._universe.config
    assert config.mode.value == "liquidity_momentum"
    assert config.lookback == 30
    assert config.min_avg_amount == Decimal("10000000")
    assert config.min_momentum == Decimal("0.05")
    assert config.exit_clear is True
    assert strategy._exit_clear is True


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
