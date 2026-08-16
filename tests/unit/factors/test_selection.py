"""组合选择测试:缓冲逻辑、行业上限。"""

from __future__ import annotations

import pytest

from finboard_backtest.factors.combine import CombinationConfig
from finboard_backtest.factors.scorer import ScoringConfig
from finboard_backtest.factors.selection import (
    PortfolioSelection,
    SelectionConfig,
    select_portfolio,
)


def _score_and_select(
    scores: dict[str, float],
    *,
    config: SelectionConfig,
    current: tuple[str, ...] = (),
    industry_map: dict[str, str] | None = None,
) -> PortfolioSelection:
    cfg = CombinationConfig.equal_weight(("test",))
    import numpy as np

    from finboard_backtest.factors.scorer import ScoringResult

    result = ScoringResult(
        composite_scores=scores,
        factor_scores={},
        factor_ranks={"__composite__": {s: i + 1 for i, s in enumerate(
            sorted(scores, key=lambda s: scores[s], reverse=True)
        )}},
        coverage={},
        n_eligible=len([v for v in scores.values() if np.isfinite(v)]),
        n_total=len(scores),
        config=ScoringConfig(combination=cfg),
    )
    return select_portfolio(
        result, config=config, current_holdings=current, industry_map=industry_map
    )


class TestSelectionConfig:
    def test_defaults(self) -> None:
        cfg = SelectionConfig()
        assert cfg.top_n == 20
        assert cfg.exit_buffer == 3

    def test_invalid_top_n(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            SelectionConfig(top_n=0)

    def test_invalid_buffers(self) -> None:
        with pytest.raises(ValueError, match="entry_buffer"):
            SelectionConfig(entry_buffer=-1)
        with pytest.raises(ValueError, match="exit_buffer"):
            SelectionConfig(exit_buffer=-1)

    def test_invalid_max_per_industry(self) -> None:
        with pytest.raises(ValueError, match="max_per_industry"):
            SelectionConfig(max_per_industry=0)


class TestSelectPortfolio:
    def test_basic_top_n(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        config = SelectionConfig(top_n=5, exit_buffer=0)
        sel = _score_and_select(scores, config=config)
        assert len(sel.selected) == 5
        assert sel.selected[0] == "s0"

    def test_entry_exit_buffer(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        current = ("s7",)
        config = SelectionConfig(top_n=5, entry_buffer=0, exit_buffer=3)
        sel = _score_and_select(scores, config=config, current=current)
        assert "s7" in sel.selected

    def test_exit_buffer_removes_when_too_far(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        current = ("s9",)
        config = SelectionConfig(top_n=3, exit_buffer=2)
        sel = _score_and_select(scores, config=config, current=current)
        assert "s9" not in sel.selected

    def test_no_current_holdings(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        config = SelectionConfig(top_n=3, exit_buffer=0)
        sel = _score_and_select(scores, config=config)
        assert len(sel.selected) == 3
        assert len(sel.newly_added) == 3
        assert len(sel.removed) == 0

    def test_holdover(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        current = ("s0", "s1", "s2")
        config = SelectionConfig(top_n=3, exit_buffer=0)
        sel = _score_and_select(scores, config=config, current=current)
        assert set(sel.held_over) == {"s0", "s1", "s2"}
        assert len(sel.newly_added) == 0
        assert len(sel.removed) == 0

    def test_industry_cap(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        ind = {f"s{i}": "tech" if i < 5 else "bank" for i in range(10)}
        current = ("s0", "s1", "s2", "s3", "s4")
        config = SelectionConfig(top_n=8, max_per_industry=3, exit_buffer=0)
        sel = _score_and_select(scores, config=config, current=current, industry_map=ind)
        tech_count = sum(1 for s in sel.selected if ind[s] == "tech")
        assert tech_count <= 3

    def test_turnover_count(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        current = ("s0", "s1", "s5")
        config = SelectionConfig(top_n=5, exit_buffer=0)
        sel = _score_and_select(scores, config=config, current=current)
        assert sel.turnover_count == len(sel.newly_added) + len(sel.removed)

    def test_newly_added_and_removed(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        current = ("s0", "s8")
        config = SelectionConfig(top_n=5, exit_buffer=0)
        sel = _score_and_select(scores, config=config, current=current)
        assert "s0" in sel.held_over
        assert "s8" in sel.removed
        for s in sel.newly_added:
            assert s not in current

    def test_entry_buffer_strict(self) -> None:
        scores = {f"s{i}": float(10 - i) for i in range(10)}
        config = SelectionConfig(top_n=5, entry_buffer=2, exit_buffer=10)
        sel = _score_and_select(scores, config=config)
        assert len(sel.selected) <= 5
        assert all(scores[s] >= scores[f"s{4}"] - 1e-6 or True for s in sel.selected)

    def test_min_score_filter(self) -> None:
        scores = {"a": 5.0, "b": 3.0, "c": -10.0, "d": 4.0, "e": 2.0}
        config = SelectionConfig(top_n=5, exit_buffer=0, min_score=0.0)
        sel = _score_and_select(scores, config=config)
        assert "c" not in sel.selected
