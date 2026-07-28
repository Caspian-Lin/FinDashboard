"""Config 和 ContractSpec 测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.futures_tsmom import (
    DEFAULT_CONTRACT_SPECS,
    FUTURES_TSMOM_VERSION,
    IF_SPEC,
    T_SPEC,
    ContractSpec,
    FuturesMarket,
    FuturesTsmomConfig,
    TsmomParameterGrid,
    resolve_contract_spec,
)


class TestFuturesTsmomConfig:
    def test_default_config(self) -> None:
        cfg = FuturesTsmomConfig()
        assert cfg.version == FUTURES_TSMOM_VERSION
        assert cfg.lookbacks == (21, 63, 126, 252)
        assert cfg.vol_target == 0.10
        assert cfg.max_leverage == 2.0
        assert cfg.commission_rate == pytest.approx(0.000023)

    def test_min_data_days(self) -> None:
        cfg = FuturesTsmomConfig(lookbacks=(21, 63), vol_lookback=42)
        assert cfg.min_data_days == 63 + 42

    def test_invalid_lookback(self) -> None:
        with pytest.raises(ValueError, match="lookback 必须 >= 5"):
            FuturesTsmomConfig(lookbacks=(3,))

    def test_duplicate_lookbacks(self) -> None:
        with pytest.raises(ValueError, match="lookbacks 不允许重复"):
            FuturesTsmomConfig(lookbacks=(21, 21))

    def test_invalid_vol_target(self) -> None:
        with pytest.raises(ValueError, match="vol_target 必须 > 0"):
            FuturesTsmomConfig(vol_target=0.0)

    def test_invalid_margin_usage(self) -> None:
        with pytest.raises(ValueError, match="max_margin_usage"):
            FuturesTsmomConfig(max_margin_usage=0.0)

    def test_invalid_leverage(self) -> None:
        with pytest.raises(ValueError, match="max_leverage 必须 > 0"):
            FuturesTsmomConfig(max_leverage=0.0)

    def test_invalid_commission(self) -> None:
        with pytest.raises(ValueError, match="commission_rate 不能为负"):
            FuturesTsmomConfig(commission_rate=-0.01)

    def test_cost_multiplier_config(self) -> None:
        cfg = FuturesTsmomConfig(commission_rate=0.0001, slippage_bps=3.0, commission_per_lot=5.0)
        doubled = cfg.cost_multiplier_config(2.0)
        assert doubled.commission_rate == pytest.approx(0.0002)
        assert doubled.slippage_bps == pytest.approx(6.0)
        assert doubled.commission_per_lot == pytest.approx(10.0)
        assert cfg.commission_rate == pytest.approx(0.0001)

    def test_margin_multiplier_config(self) -> None:
        cfg = FuturesTsmomConfig(vol_target=0.10, max_margin_usage=0.80)
        doubled = cfg.margin_multiplier_config(2.0)
        assert doubled.vol_target == pytest.approx(0.05)
        assert doubled.max_margin_usage == pytest.approx(0.40)

    def test_as_dict(self) -> None:
        cfg = FuturesTsmomConfig()
        d = cfg.as_dict()
        assert d["version"] == FUTURES_TSMOM_VERSION
        assert "lookbacks" in d
        assert "vol_target" in d


class TestTsmomParameterGrid:
    def test_total_candidates(self) -> None:
        grid = TsmomParameterGrid(
            lookbacks=((21,), (63,), (21, 63, 126)),
            vol_targets=(0.10, 0.15),
            max_leverages=(1.0, 2.0),
        )
        assert grid.total_candidates == 3 * 2 * 2

    def test_candidates_enumeration(self) -> None:
        grid = TsmomParameterGrid(
            lookbacks=((21,),),
            vol_targets=(0.10,),
            max_leverages=(2.0,),
        )
        candidates = grid.candidates()
        assert len(candidates) == 1
        assert candidates[0]["lookbacks"] == [21]

    def test_empty_lookbacks(self) -> None:
        with pytest.raises(ValueError, match="lookbacks 不能为空"):
            TsmomParameterGrid(lookbacks=(), vol_targets=(0.1,), max_leverages=(1.0,))

    def test_empty_tuple_in_lookbacks(self) -> None:
        with pytest.raises(ValueError, match="lookback 元组不能为空"):
            TsmomParameterGrid(lookbacks=((),), vol_targets=(0.1,), max_leverages=(1.0,))


class TestContractSpec:
    def test_if_spec(self) -> None:
        assert IF_SPEC.symbol == "IF"
        assert IF_SPEC.market is FuturesMarket.EQUITY_INDEX
        assert IF_SPEC.multiplier == 300.0
        assert IF_SPEC.margin_rate == pytest.approx(0.12)
        assert IF_SPEC.tick_size == pytest.approx(0.2)

    def test_t_spec(self) -> None:
        assert T_SPEC.symbol == "T"
        assert T_SPEC.market is FuturesMarket.TREASURY_BOND
        assert T_SPEC.multiplier == 10000.0
        assert T_SPEC.commission_per_lot == 3.0

    def test_notional_value(self) -> None:
        assert IF_SPEC.notional_value(3500.0, 2) == 3500.0 * 300.0 * 2

    def test_margin_required(self) -> None:
        margin = IF_SPEC.margin_required(3500.0, 1)
        assert margin == pytest.approx(3500.0 * 300.0 * 0.12)

    def test_commission_stock_index(self) -> None:
        comm = IF_SPEC.commission(3500.0, 1)
        expected = 3500.0 * 300.0 * 0.000023
        assert comm == pytest.approx(expected)

    def test_commission_bond(self) -> None:
        comm = T_SPEC.commission(100.0, 1)
        assert comm == pytest.approx(3.0)

    def test_commission_min_bond(self) -> None:
        comm = T_SPEC.commission(0.001, 1)
        notional_rate = 0.001 * 10000.0 * 0.0
        assert comm == pytest.approx(max(notional_rate, 3.0))

    def test_round_to_tick(self) -> None:
        assert IF_SPEC.round_to_tick(3500.0) == pytest.approx(3500.0)
        assert T_SPEC.round_to_tick(100.007) == pytest.approx(100.005)
        assert T_SPEC.round_to_tick(100.0) == pytest.approx(100.0)

    def test_invalid_multiplier(self) -> None:
        with pytest.raises(ValueError, match="multiplier 必须 > 0"):
            ContractSpec("X", "X", FuturesMarket.EQUITY_INDEX, 0, 0.1, 0.1, 0.0, 0.0, 0.1)

    def test_invalid_margin_rate(self) -> None:
        with pytest.raises(ValueError, match="margin_rate"):
            ContractSpec("X", "X", FuturesMarket.EQUITY_INDEX, 100, 0.0, 0.1, 0.0, 0.0, 0.1)

    def test_resolve_contract_spec(self) -> None:
        spec = resolve_contract_spec("IF")
        assert spec.symbol == "IF"
        with pytest.raises(KeyError):
            resolve_contract_spec("UNKNOWN")

    def test_default_contract_specs_complete(self) -> None:
        for sym in ["IF", "IC", "IH", "T", "TF", "TS"]:
            assert sym in DEFAULT_CONTRACT_SPECS

    def test_as_dict(self) -> None:
        d = IF_SPEC.as_dict()
        assert d["symbol"] == "IF"
        assert d["market"] == "equity_index"
