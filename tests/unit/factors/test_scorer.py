"""多因子评分管线测试。"""

from __future__ import annotations

import numpy as np
import pytest

from finboard_backtest.factors.combine import CombinationConfig, FactorWeight
from finboard_backtest.factors.scorer import (
    FactorMatrix,
    MultiFactorScorer,
    ScoringConfig,
)


def _make_matrix(n: int = 30) -> FactorMatrix:
    rng = np.random.default_rng(42)
    return {
        "pb": {f"s{i}": float(rng.uniform(0.5, 10)) for i in range(n)},
        "roe": {f"s{i}": float(rng.uniform(0.01, 0.3)) for i in range(n)},
        "momentum": {f"s{i}": float(rng.standard_normal()) for i in range(n)},
    }


class TestScoringConfig:
    def test_basic(self) -> None:
        cfg = CombinationConfig(
            weights=(FactorWeight("pb", 0.5), FactorWeight("roe", 0.5))
        )
        sc = ScoringConfig(combination=cfg)
        assert sc.factor_names == ("pb", "roe")

    def test_invalid_coverage(self) -> None:
        cfg = CombinationConfig.equal_weight(("pb",))
        with pytest.raises(ValueError, match="min_coverage"):
            ScoringConfig(combination=cfg, min_coverage=-0.1)
        with pytest.raises(ValueError, match="min_coverage"):
            ScoringConfig(combination=cfg, min_coverage=1.5)


class TestMultiFactorScorer:
    def test_basic_score(self) -> None:
        matrix = _make_matrix()
        cfg = CombinationConfig(
            weights=(
                FactorWeight("pb", 0.4),
                FactorWeight("roe", 0.4),
                FactorWeight("momentum", 0.2),
            )
        )
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        assert len(result.composite_scores) == 30
        assert len(result.ranked_symbols) == 30

    def test_top_n(self) -> None:
        matrix = _make_matrix()
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        top5 = result.top_n(5)
        assert len(top5) == 5
        scores = [result.composite_scores[s] for s in top5]
        assert scores == sorted(scores, reverse=True)

    def test_coverage(self) -> None:
        matrix = _make_matrix()
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        assert result.coverage["pb"] == 1.0
        assert result.coverage["roe"] == 1.0

    def test_missing_factor_symbol(self) -> None:
        matrix = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0},
            "roe": {"a": 0.1, "b": 0.2},
        }
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        assert np.isfinite(result.composite_scores["c"])

    def test_nan_handling(self) -> None:
        matrix = {
            "pb": {"a": 1.0, "b": float("nan"), "c": 3.0},
            "roe": {"a": 0.1, "b": 0.2, "c": 0.3},
        }
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        assert np.isfinite(result.composite_scores["a"])
        assert np.isfinite(result.composite_scores["b"])
        assert np.isfinite(result.composite_scores["c"])

    def test_industry_neutralize(self) -> None:
        matrix = {
            "roe": {"a": 0.05, "b": 0.15, "c": 0.10, "d": 0.20, "e": 0.25, "f": 0.30},
        }
        ind = {"a": "tech", "b": "tech", "c": "tech", "d": "bank", "e": "bank", "f": "bank"}
        cfg = CombinationConfig.equal_weight(("roe",))
        scorer = MultiFactorScorer(
            ScoringConfig(combination=cfg, industry_neutralize=True)
        )
        result = scorer.score(matrix, industry_map=ind)
        assert len(result.composite_scores) == 6

    def test_cap_neutralize(self) -> None:
        rng = np.random.default_rng(42)
        n = 50
        matrix = {
            "roe": {f"s{i}": float(rng.uniform(0.05, 0.3)) for i in range(n)},
            "market_cap": {f"s{i}": float(rng.uniform(1e9, 1e11)) for i in range(n)},
        }
        cfg = CombinationConfig.equal_weight(("roe",))
        scorer = MultiFactorScorer(
            ScoringConfig(combination=cfg, cap_neutralize=True)
        )
        result = scorer.score(matrix)
        assert len(result.composite_scores) == n

    def test_empty_matrix(self) -> None:
        cfg = CombinationConfig.equal_weight(("pb",))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score({})
        assert result.n_total == 0
        assert result.n_eligible == 0

    def test_direction_applied(self) -> None:
        matrix = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0},
            "roe": {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04, "e": 0.05},
        }
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        pb_scores = result.factor_scores["pb"]
        assert pb_scores["a"] > pb_scores["e"]

    def test_order_independence(self) -> None:
        matrix1 = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0},
            "roe": {"a": 0.05, "b": 0.04, "c": 0.03, "d": 0.02, "e": 0.01},
        }
        matrix2 = {
            "pb": {"e": 5.0, "d": 4.0, "c": 3.0, "b": 2.0, "a": 1.0},
            "roe": {"e": 0.01, "d": 0.02, "c": 0.03, "b": 0.04, "a": 0.05},
        }
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        r1 = scorer.score(matrix1)
        r2 = scorer.score(matrix2)
        for sym in r1.composite_scores:
            assert abs(r1.composite_scores[sym] - r2.composite_scores[sym]) < 1e-10

    def test_factor_ranks(self) -> None:
        matrix = {
            "pb": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0},
            "roe": {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04, "e": 0.05},
        }
        cfg = CombinationConfig.equal_weight(("pb", "roe"))
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        result = scorer.score(matrix)
        assert "pb" in result.factor_ranks
        assert "__composite__" in result.factor_ranks
        assert result.factor_ranks["pb"]["a"] == 1

    def test_unknown_factor_raises(self) -> None:
        cfg = CombinationConfig(
            weights=(FactorWeight("nonexistent", 1.0),)
        )
        scorer = MultiFactorScorer(ScoringConfig(combination=cfg))
        with pytest.raises(KeyError):
            scorer.score({"nonexistent": {"a": 1.0}})
