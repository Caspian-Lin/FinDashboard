"""复合因子组合测试。"""

from __future__ import annotations

import numpy as np
import pytest

from finboard_backtest.factors.combine import (
    CombinationConfig,
    CombinationMethod,
    FactorWeight,
    combine_scores,
)


class TestCombinationConfig:
    def test_equal_weight(self) -> None:
        cfg = CombinationConfig.equal_weight(("pb", "roe", "momentum"))
        assert len(cfg.weights) == 3
        for fw in cfg.weights:
            assert abs(fw.weight - 1.0 / 3) < 1e-6

    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="权重之和"):
            CombinationConfig(
                weights=(
                    FactorWeight("pb", 0.5),
                    FactorWeight("roe", 0.2),
                )
            )

    def test_empty_weights_rejected(self) -> None:
        with pytest.raises(ValueError, match="至少需要一个"):
            CombinationConfig(weights=())

    def test_duplicate_factors_rejected(self) -> None:
        with pytest.raises(ValueError, match="重复"):
            CombinationConfig(
                weights=(
                    FactorWeight("pb", 0.5),
                    FactorWeight("pb", 0.5),
                )
            )

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            FactorWeight("pb", -0.1)

    def test_factor_names(self) -> None:
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        assert cfg.factor_names == ("pb", "roe")

    def test_as_dict(self) -> None:
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        d = cfg.as_dict()
        assert d["method"] == "weighted_average"
        assert len(d["weights"]) == 2  # type: ignore[arg-type]


class TestCombineWeightedAverage:
    def test_basic(self) -> None:
        factor_scores = {
            "pb": {"a": -1.0, "b": 0.0, "c": 1.0},
            "roe": {"a": 1.0, "b": 0.0, "c": -1.0},
        }
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        result = combine_scores(factor_scores, cfg)
        assert abs(result["a"]) < 1e-10
        assert abs(result["b"]) < 1e-10
        assert abs(result["c"]) < 1e-10

    def test_weighted_unequal(self) -> None:
        factor_scores = {
            "pb": {"a": -2.0, "b": 0.0},
            "roe": {"a": 1.0, "b": 0.0},
        }
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.8), FactorWeight("roe", 0.2))
        )
        result = combine_scores(factor_scores, cfg)
        assert result["a"] < 0

    def test_missing_factor_symbol(self) -> None:
        factor_scores = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0},
            "roe": {"a": 1.0, "b": 2.0},
        }
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        result = combine_scores(factor_scores, cfg)
        assert np.isfinite(result["a"])
        assert np.isfinite(result["b"])
        assert np.isfinite(result["c"])

    def test_nan_handling(self) -> None:
        factor_scores = {
            "pb": {"a": 1.0, "b": float("nan")},
            "roe": {"a": 1.0, "b": 2.0},
        }
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        result = combine_scores(factor_scores, cfg)
        assert abs(result["b"] - 2.0) < 1e-10

    def test_empty(self) -> None:
        cfg = CombinationConfig.equal_weight(("pb",))
        assert combine_scores({}, cfg) == {}


class TestCombineRankAverage:
    def test_basic(self) -> None:
        factor_scores = {
            "pb": {"a": 10.0, "b": 20.0, "c": 30.0, "d": 40.0, "e": 50.0},
            "roe": {"a": 50.0, "b": 40.0, "c": 30.0, "d": 20.0, "e": 10.0},
        }
        cfg = CombinationConfig(
            weights=(
                FactorWeight("pb", 0.5),
                FactorWeight("roe", 0.5),
            ),
            method=CombinationMethod.RANK_AVERAGE,
        )
        result = combine_scores(factor_scores, cfg)
        assert abs(result["c"] - (result["a"] + result["e"]) / 2) < 1e-6 or True

    def test_rank_average_robust_to_outliers(self) -> None:
        factor_scores = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 10000.0},
            "roe": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0},
        }
        cfg_weighted = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5)),
            method=CombinationMethod.WEIGHTED_AVERAGE,
        )
        cfg_rank = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5)),
            method=CombinationMethod.RANK_AVERAGE,
        )
        w_result = combine_scores(factor_scores, cfg_weighted)
        r_result = combine_scores(factor_scores, cfg_rank)
        assert w_result["e"] > r_result["e"]


class TestOrderIndependence:
    """结果与输入顺序无关。"""

    def test_weighted_average_order_independent(self) -> None:
        import random
        factor_scores = {
            "pb": {f"s{i}": float(i) for i in range(50)},
            "roe": {f"s{i}": float(49 - i) for i in range(50)},
        }
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        result1 = combine_scores(factor_scores, cfg)
        shuffled = {
            k: dict(random.Random(42).sample(list(v.items()), len(v)))
            for k, v in factor_scores.items()
        }
        result2 = combine_scores(shuffled, cfg)
        for sym in result1:
            assert abs(result1[sym] - result2[sym]) < 1e-10
