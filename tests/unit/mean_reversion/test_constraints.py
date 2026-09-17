"""均值回归风控约束测试。"""

from __future__ import annotations

from finboard_backtest.mean_reversion.config import MeanReversionConfig
from finboard_backtest.mean_reversion.constraints import (
    _CooldownState,
    _OpenPosition,
    check_entry,
    check_max_holding,
    check_participation,
)


class TestCheckEntry:
    def test_allowed_no_positions(self) -> None:
        cfg = MeanReversionConfig()
        result = check_entry("AAA", [], [], cfg)
        assert result.allowed

    def test_blocked_by_cooldown(self) -> None:
        cfg = MeanReversionConfig(cooldown_days=5)
        cooldowns = [_CooldownState(symbol="AAA", remaining=3)]
        result = check_entry("AAA", [], cooldowns, cfg)
        assert result.blocked
        assert "冷却期" in result.reason

    def test_blocked_by_max_positions(self) -> None:
        cfg = MeanReversionConfig(max_positions=2)
        positions = [
            _OpenPosition("AAA", 0, 10.0, 100, 0.1),
            _OpenPosition("BBB", 0, 10.0, 100, 0.1),
        ]
        result = check_entry("CCC", positions, [], cfg)
        assert result.blocked
        assert "最大持仓数" in result.reason

    def test_blocked_existing_position(self) -> None:
        cfg = MeanReversionConfig(no_averaging_down=True)
        positions = [_OpenPosition("AAA", 0, 10.0, 100, 0.1)]
        result = check_entry("AAA", positions, [], cfg)
        assert result.blocked
        assert "摊平" in result.reason

    def test_blocked_existing_position_no_averaging(self) -> None:
        cfg = MeanReversionConfig(no_averaging_down=False)
        positions = [_OpenPosition("AAA", 0, 10.0, 100, 0.1)]
        result = check_entry("AAA", positions, [], cfg)
        assert result.blocked

    def test_blocked_by_turnover(self) -> None:
        cfg = MeanReversionConfig(max_daily_turnover=0.10)
        result = check_entry(
            "AAA", [], [], cfg,
            pending_buy_value=50_000,
            portfolio_value=100_000,
            daily_turnover_so_far=0.08,
        )
        assert result.blocked
        assert "换手率" in result.reason


class TestCheckParticipation:
    def test_within_limit(self) -> None:
        cfg = MeanReversionConfig(max_participation=0.10)
        result = check_participation(100, 10.0, 10_000, cfg)
        assert result.allowed

    def test_exceeds_limit(self) -> None:
        cfg = MeanReversionConfig(max_participation=0.10)
        result = check_participation(2000, 10.0, 10_000, cfg)
        assert result.blocked
        assert "参与率" in result.reason

    def test_zero_volume_blocked(self) -> None:
        cfg = MeanReversionConfig()
        result = check_participation(100, 10.0, 0, cfg)
        assert result.blocked


class TestCheckMaxHolding:
    def test_at_limit(self) -> None:
        cfg = MeanReversionConfig(max_holding_days=5)
        pos = _OpenPosition("AAA", 0, 10.0, 100, 0.1, bars_held=5)
        assert check_max_holding(pos, 5, cfg)

    def test_below_limit(self) -> None:
        cfg = MeanReversionConfig(max_holding_days=5)
        pos = _OpenPosition("AAA", 0, 10.0, 100, 0.1, bars_held=3)
        assert not check_max_holding(pos, 3, cfg)
