"""可转债双低策略配置测试(issue #63)。"""

from decimal import Decimal

import pytest

from finboard_backtest.convertible_double_low.config import (
    CONVERTIBLE_DOUBLE_LOW_VERSION,
    DEFAULT_FACTOR_WEIGHTS,
    ConvertibleDoubleLowConfig,
    FactorWeight,
    RebalanceFrequency,
)


class TestVersion:
    def test_version_is_string(self) -> None:
        assert isinstance(CONVERTIBLE_DOUBLE_LOW_VERSION, str)
        assert CONVERTIBLE_DOUBLE_LOW_VERSION == "v1"


class TestDefaults:
    def test_default_config(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        assert cfg.price_max == Decimal("130")
        assert cfg.price_min == Decimal("100")
        assert cfg.premium_max == Decimal("0.50")
        assert cfg.top_n == 20
        assert cfg.rebalance_frequency == RebalanceFrequency.MONTHLY
        assert cfg.capital == Decimal("100000")

    def test_default_factor_weights(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        assert cfg.factor_weights[FactorWeight.PRICE] == Decimal("1.0")
        assert cfg.factor_weights[FactorWeight.PREMIUM] == Decimal("1.0")
        assert cfg.factor_weights[FactorWeight.YTM] == Decimal("0.0")

    def test_factor_weights_not_shared(self) -> None:
        cfg1 = ConvertibleDoubleLowConfig()
        cfg2 = ConvertibleDoubleLowConfig()
        cfg1.factor_weights[FactorWeight.YTM] = Decimal("0.5")
        assert cfg2.factor_weights[FactorWeight.YTM] == Decimal("0.0")


class TestValidation:
    def test_price_max_must_exceed_min(self) -> None:
        with pytest.raises(ValueError, match="price_max"):
            ConvertibleDoubleLowConfig(price_min=Decimal("100"), price_max=Decimal("100"))

    def test_top_n_at_least_1(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            ConvertibleDoubleLowConfig(top_n=0)

    def test_max_weight_per_bond_range(self) -> None:
        with pytest.raises(ValueError, match="max_weight_per_bond"):
            ConvertibleDoubleLowConfig(max_weight_per_bond=Decimal("0"))

    def test_max_weight_per_issuer_range(self) -> None:
        with pytest.raises(ValueError, match="max_weight_per_issuer"):
            ConvertibleDoubleLowConfig(max_weight_per_issuer=Decimal("1.5"))

    def test_invalid_rebalance_frequency(self) -> None:
        with pytest.raises(ValueError, match="调仓频率"):
            ConvertibleDoubleLowConfig(rebalance_frequency="daily")

    def test_negative_commission(self) -> None:
        with pytest.raises(ValueError, match="commission_rate"):
            ConvertibleDoubleLowConfig(commission_rate=Decimal("-0.001"))

    def test_capital_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="capital"):
            ConvertibleDoubleLowConfig(capital=Decimal("0"))

    def test_turnover_range(self) -> None:
        with pytest.raises(ValueError, match="max_daily_turnover"):
            ConvertibleDoubleLowConfig(max_daily_turnover=Decimal("2"))

    def test_participation_range(self) -> None:
        with pytest.raises(ValueError, match="max_participation"):
            ConvertibleDoubleLowConfig(max_participation=Decimal("-0.1"))


class TestDerivedProperties:
    def test_monthly_interval(self) -> None:
        cfg = ConvertibleDoubleLowConfig(rebalance_frequency=RebalanceFrequency.MONTHLY)
        assert cfg.rebalance_interval_days == 21

    def test_biweekly_interval(self) -> None:
        cfg = ConvertibleDoubleLowConfig(rebalance_frequency=RebalanceFrequency.BIWEEKLY)
        assert cfg.rebalance_interval_days == 10

    def test_min_data_days(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        assert cfg.min_data_days >= 36


class TestAsDict:
    def test_as_dict_contains_all_fields(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        d = cfg.as_dict()
        assert "version" in d
        assert "factor_weights" in d
        assert "top_n" in d
        assert d["version"] == CONVERTIBLE_DOUBLE_LOW_VERSION

    def test_as_dict_serializes_decimals(self) -> None:
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("200000"))
        d = cfg.as_dict()
        assert d["capital"] == "200000"
        assert isinstance(d["factor_weights"], dict)


class TestCustomWeights:
    def test_custom_weights(self) -> None:
        weights = dict(DEFAULT_FACTOR_WEIGHTS)
        weights[FactorWeight.YTM] = Decimal("0.5")
        cfg = ConvertibleDoubleLowConfig(factor_weights=weights)
        assert cfg.factor_weights[FactorWeight.YTM] == Decimal("0.5")
        assert cfg.factor_weights[FactorWeight.PRICE] == Decimal("1.0")
