"""EtfRotationConfig 验证测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.etf_rotation import (
    ETF_ROTATION_VERSION,
    AbsoluteTrendMethod,
    AllocationMethod,
    EtfRotationConfig,
    RebalanceFrequency,
)


class TestEtfRotationConfigDefaults:
    def test_default_config(self) -> None:
        cfg = EtfRotationConfig()
        assert cfg.trend_method is AbsoluteTrendMethod.SMA
        assert cfg.sma_window == 200
        assert cfg.top_n == 5
        assert cfg.allocation_method is AllocationMethod.EQUAL_WEIGHT
        assert cfg.cash_buffer == 0.05
        assert cfg.version == ETF_ROTATION_VERSION

    def test_default_momentum_weights_sum_to_one(self) -> None:
        cfg = EtfRotationConfig()
        assert abs(sum(cfg.momentum_weights) - 1.0) < 0.001

    def test_rebalance_interval_days(self) -> None:
        assert EtfRotationConfig().rebalance_interval_days == 21
        cfg = EtfRotationConfig(rebalance_frequency=RebalanceFrequency.BIWEEKLY)
        assert cfg.rebalance_interval_days == 10

    def test_min_data_days(self) -> None:
        cfg = EtfRotationConfig()
        # max(SMA 200, 12*21=252 momentum, 60 vol) = 252
        assert cfg.min_data_days == 252

    def test_min_data_days_lookback(self) -> None:
        cfg = EtfRotationConfig(
            trend_method=AbsoluteTrendMethod.LOOKBACK_RETURN,
            lookback_months=12,
            momentum_lookbacks=(3, 6, 12),
        )
        # max(12*21=252, 12*21=252, 60) = 252
        assert cfg.min_data_days == 252

    def test_min_data_days_inverse_vol(self) -> None:
        cfg = EtfRotationConfig(allocation_method=AllocationMethod.INVERSE_VOLATILITY)
        assert cfg.min_data_days >= 200  # SMA still dominates

    def test_as_dict_roundtrip(self) -> None:
        cfg = EtfRotationConfig(top_n=3)
        d = cfg.as_dict()
        assert d["top_n"] == 3
        assert d["version"] == ETF_ROTATION_VERSION
        assert d["trend_method"] == "sma"
        assert d["allocation_method"] == "equal_weight"


class TestEtfRotationConfigValidation:
    def test_sma_window_too_small(self) -> None:
        with pytest.raises(ValueError, match="sma_window"):
            EtfRotationConfig(sma_window=2)

    def test_lookback_months_too_small(self) -> None:
        with pytest.raises(ValueError, match="lookback_months"):
            EtfRotationConfig(lookback_months=0)

    def test_momentum_lookbacks_empty(self) -> None:
        with pytest.raises(ValueError, match="momentum_lookbacks"):
            EtfRotationConfig(momentum_lookbacks=(), momentum_weights=())

    def test_momentum_weights_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match=r"momentum_weights.*长度"):
            EtfRotationConfig(
                momentum_lookbacks=(3, 6, 12),
                momentum_weights=(0.5, 0.5),
            )

    def test_momentum_weights_sum_not_one(self) -> None:
        with pytest.raises(ValueError, match=r"momentum_weights.*总和"):
            EtfRotationConfig(
                momentum_lookbacks=(3, 6),
                momentum_weights=(0.1, 0.1),
            )

    def test_momentum_weights_negative(self) -> None:
        with pytest.raises(ValueError, match=r"momentum_weights.*负值"):
            EtfRotationConfig(
                momentum_lookbacks=(3, 6),
                momentum_weights=(-0.5, 1.5),
            )

    def test_top_n_too_small(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            EtfRotationConfig(top_n=0)

    def test_max_weight_per_etf_too_large(self) -> None:
        with pytest.raises(ValueError, match="max_weight_per_etf"):
            EtfRotationConfig(max_weight_per_etf=1.5)

    def test_max_weight_per_etf_exceeds_asset_class(self) -> None:
        with pytest.raises(ValueError, match=r"max_weight_per_etf.*不能超过"):
            EtfRotationConfig(
                max_weight_per_etf=0.5,
                max_weight_per_asset_class=0.4,
            )

    def test_cash_buffer_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="cash_buffer"):
            EtfRotationConfig(cash_buffer=1.5)

    def test_safe_haven_symbol_empty(self) -> None:
        with pytest.raises(ValueError, match="safe_haven_symbol"):
            EtfRotationConfig(safe_haven_symbol="")
