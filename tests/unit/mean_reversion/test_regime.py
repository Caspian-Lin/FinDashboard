"""趋势 / 波动状态过滤测试。"""

from __future__ import annotations

from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    RegimeMethod,
)
from finboard_backtest.mean_reversion.regime import (
    RegimeState,
    classify_regime,
    is_entry_allowed,
)


def _uptrend(n: int = 250, start: float = 10.0, slope: float = 0.02) -> list[float]:
    return [start + slope * i for i in range(n)]


def _downtrend(n: int = 250, start: float = 10.0, slope: float = -0.02) -> list[float]:
    return [start + slope * i for i in range(n)]


def _flat(n: int = 250, value: float = 10.0) -> list[float]:
    return [value] * n


class TestClassifyRegime:
    def test_uptrend_is_bull(self) -> None:
        closes = {"AAA": _uptrend()}
        cfg = MeanReversionConfig()
        regime = classify_regime(closes, cfg)
        assert regime["AAA"].state is RegimeState.BULL

    def test_downtrend_is_bear(self) -> None:
        closes = {"AAA": _downtrend()}
        cfg = MeanReversionConfig()
        regime = classify_regime(closes, cfg)
        assert regime["AAA"].state is RegimeState.BEAR

    def test_flat_is_neutral_or_bull(self) -> None:
        closes = {"AAA": _flat()}
        cfg = MeanReversionConfig()
        regime = classify_regime(closes, cfg)
        assert regime["AAA"].state in (RegimeState.NEUTRAL, RegimeState.BULL)

    def test_unknown_insufficient_data(self) -> None:
        closes = {"AAA": [10.0] * 10}
        cfg = MeanReversionConfig(regime_sma_window=200)
        regime = classify_regime(closes, cfg)
        assert regime["AAA"].state is RegimeState.UNKNOWN

    def test_none_method_always_bull(self) -> None:
        closes = {"AAA": _downtrend()}
        cfg = MeanReversionConfig(regime_method=RegimeMethod.NONE)
        regime = classify_regime(closes, cfg)
        assert regime["AAA"].state is RegimeState.BULL


class TestIsEntryAllowed:
    def test_bull_allowed(self) -> None:
        closes = {"AAA": _uptrend()}
        cfg = MeanReversionConfig()
        regime = classify_regime(closes, cfg)
        assert is_entry_allowed(regime["AAA"])

    def test_bear_blocked(self) -> None:
        closes = {"AAA": _downtrend()}
        cfg = MeanReversionConfig()
        regime = classify_regime(closes, cfg)
        assert not is_entry_allowed(regime["AAA"])

    def test_unknown_blocked(self) -> None:
        closes = {"AAA": [10.0] * 5}
        cfg = MeanReversionConfig(regime_sma_window=200)
        regime = classify_regime(closes, cfg)
        assert not is_entry_allowed(regime["AAA"])
