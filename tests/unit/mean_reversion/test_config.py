"""均值回归策略配置(ParameterGrid / MeanReversionConfig)测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.mean_reversion.config import (
    MEAN_REVERSION_VERSION,
    MeanReversionConfig,
    ParameterGrid,
    RegimeMethod,
    SignalFamily,
)


class TestParameterGrid:
    def test_default_grid(self) -> None:
        grid = ParameterGrid(
            families=(SignalFamily.Z_SCORE, SignalFamily.RSI),
            lookbacks=(10, 20),
            entry_thresholds=(1.5, 2.0),
            exit_thresholds=(0.5,),
        )
        assert grid.total_candidates == 2 * 2 * 2 * 1

    def test_candidates_enumeration(self) -> None:
        grid = ParameterGrid(
            families=(SignalFamily.Z_SCORE,),
            lookbacks=(10,),
            entry_thresholds=(1.5,),
            exit_thresholds=(0.5,),
        )
        cands = grid.candidates()
        assert len(cands) == 1
        assert cands[0]["family"] == "z_score"
        assert cands[0]["lookback"] == 10

    def test_empty_families_raises(self) -> None:
        with pytest.raises(ValueError, match="families 不能为空"):
            ParameterGrid(
                families=(),
                lookbacks=(10,),
                entry_thresholds=(1.0,),
                exit_thresholds=(0.5,),
            )

    def test_duplicate_lookbacks_raises(self) -> None:
        with pytest.raises(ValueError, match="lookbacks 不允许重复"):
            ParameterGrid(
                families=(SignalFamily.Z_SCORE,),
                lookbacks=(10, 10),
                entry_thresholds=(1.0,),
                exit_thresholds=(0.5,),
            )

    def test_duplicate_families_raises(self) -> None:
        with pytest.raises(ValueError, match="families 不允许重复"):
            ParameterGrid(
                families=(SignalFamily.Z_SCORE, SignalFamily.Z_SCORE),
                lookbacks=(10,),
                entry_thresholds=(1.0,),
                exit_thresholds=(0.5,),
            )

    def test_lookback_too_small(self) -> None:
        with pytest.raises(ValueError, match="lookback 必须 >= 2"):
            ParameterGrid(
                families=(SignalFamily.Z_SCORE,),
                lookbacks=(1,),
                entry_thresholds=(1.0,),
                exit_thresholds=(0.5,),
            )

    def test_as_dict(self) -> None:
        grid = ParameterGrid(
            families=(SignalFamily.Z_SCORE, SignalFamily.RSI),
            lookbacks=(10, 20),
            entry_thresholds=(2.0,),
            exit_thresholds=(0.5,),
        )
        d = grid.as_dict()
        assert d["total_candidates"] == 4
        families = d["families"]
        assert isinstance(families, list)
        assert "z_score" in families


class TestMeanReversionConfig:
    def test_defaults(self) -> None:
        cfg = MeanReversionConfig()
        assert cfg.family is SignalFamily.Z_SCORE
        assert cfg.lookback == 20
        assert cfg.version == MEAN_REVERSION_VERSION

    def test_frozen(self) -> None:
        cfg = MeanReversionConfig()
        with pytest.raises(AttributeError):
            cfg.lookback = 30  # type: ignore[misc]

    def test_invalid_lookback(self) -> None:
        with pytest.raises(ValueError, match="lookback 必须 >= 2"):
            MeanReversionConfig(lookback=1)

    def test_invalid_entry_threshold(self) -> None:
        with pytest.raises(ValueError, match="entry_threshold 必须 > 0"):
            MeanReversionConfig(entry_threshold=0)

    def test_invalid_max_positions(self) -> None:
        with pytest.raises(ValueError, match="max_positions 必须 >= 1"):
            MeanReversionConfig(max_positions=0)

    def test_invalid_max_weight(self) -> None:
        with pytest.raises(ValueError, match="max_weight_per_position"):
            MeanReversionConfig(max_weight_per_position=0)

    def test_invalid_turnover(self) -> None:
        with pytest.raises(ValueError, match="max_daily_turnover"):
            MeanReversionConfig(max_daily_turnover=0)

    def test_invalid_participation(self) -> None:
        with pytest.raises(ValueError, match="max_participation"):
            MeanReversionConfig(max_participation=0)

    def test_invalid_negative_commission(self) -> None:
        with pytest.raises(ValueError, match="commission_rate 不能为负"):
            MeanReversionConfig(commission_rate=-0.001)

    def test_min_data_days(self) -> None:
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.SMA,
            regime_sma_window=200,
        )
        assert cfg.min_data_days == 201

    def test_min_data_days_rsi(self) -> None:
        cfg = MeanReversionConfig(
            family=SignalFamily.RSI,
            rsi_period=14,
            lookback=20,
            regime_method=RegimeMethod.SMA,
            regime_sma_window=200,
        )
        assert cfg.min_data_days == 201

    def test_min_data_days_no_regime(self) -> None:
        cfg = MeanReversionConfig(
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        assert cfg.min_data_days == max(20, 60) + 1

    def test_cost_multiplier(self) -> None:
        cfg = MeanReversionConfig(
            commission_rate=0.0003,
            stamp_tax_rate=0.0005,
            slippage_bps=5.0,
        )
        doubled = cfg.cost_multiplier_config(2.0)
        assert doubled.commission_rate == pytest.approx(0.0006)
        assert doubled.stamp_tax_rate == pytest.approx(0.001)
        assert doubled.slippage_bps == pytest.approx(10.0)
        assert cfg.commission_rate == pytest.approx(0.0003)

    def test_as_dict(self) -> None:
        cfg = MeanReversionConfig()
        d = cfg.as_dict()
        assert d["family"] == "z_score"
        assert d["lookback"] == 20
        assert d["version"] == MEAN_REVERSION_VERSION
        assert "commission_rate" in d
