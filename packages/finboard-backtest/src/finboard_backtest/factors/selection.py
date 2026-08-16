"""组合选择:从评分结果选出目标持仓,含进入/退出缓冲和行业上限。

缓冲逻辑减少换手:新持仓需要进入 top_n - entry_buffer,旧持仓退出需要跌破
top_n + exit_buffer。这避免了在排名边缘频繁进出。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from finboard_backtest.factors.scorer import ScoringResult


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    """组合选择参数。"""

    top_n: int = 20
    max_per_industry: int | None = None
    entry_buffer: int = 0
    exit_buffer: int = 3
    min_score: float = -1e9

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n 必须大于 0: {self.top_n}")
        if self.entry_buffer < 0:
            raise ValueError(f"entry_buffer 不能为负: {self.entry_buffer}")
        if self.exit_buffer < 0:
            raise ValueError(f"exit_buffer 不能为负: {self.exit_buffer}")
        if self.max_per_industry is not None and self.max_per_industry <= 0:
            raise ValueError(f"max_per_industry 必须大于 0: {self.max_per_industry}")


@dataclass(frozen=True, slots=True)
class PortfolioSelection:
    """组合选择结果。"""

    selected: tuple[str, ...]
    newly_added: tuple[str, ...]
    removed: tuple[str, ...]
    held_over: tuple[str, ...]
    composite_ranks: dict[str, int]
    config: SelectionConfig

    @property
    def n_selected(self) -> int:
        return len(self.selected)

    @property
    def turnover_count(self) -> int:
        return len(self.newly_added) + len(self.removed)


def select_portfolio(
    result: ScoringResult,
    *,
    config: SelectionConfig,
    current_holdings: tuple[str, ...] = (),
    industry_map: dict[str, str] | None = None,
) -> PortfolioSelection:
    """从评分结果选出目标持仓。

    Args:
        result: 多因子评分结果
        config: 选择参数
        current_holdings: 当前持仓(用于计算缓冲)
        industry_map: symbol → industry_code,用于行业上限

    Returns:
        PortfolioSelection: 选中的标的 + 新增/移除/保留列表
    """
    ranked = result.ranked_symbols
    composite_ranks = result.factor_ranks.get("__composite__", {})
    if not composite_ranks and ranked:
        composite_ranks = {s: i + 1 for i, s in enumerate(ranked)}

    entry_threshold = max(config.top_n - config.entry_buffer, 1)
    exit_threshold = config.top_n + config.exit_buffer

    current_set = set(current_holdings)

    hold_candidates: list[str] = []
    entry_candidates: list[str] = []
    for sym in ranked:
        rank = composite_ranks.get(sym, 999999)
        score = result.composite_scores.get(sym, float("nan"))
        if score < config.min_score:
            continue
        if sym in current_set and rank <= exit_threshold:
            hold_candidates.append(sym)
        elif rank <= entry_threshold:
            entry_candidates.append(sym)

    selected = hold_candidates[: config.top_n]
    remaining = config.top_n - len(selected)
    if remaining > 0:
        selected.extend(entry_candidates[:remaining])

    if config.max_per_industry is not None and industry_map:
        selected = _apply_industry_cap(selected, config.max_per_industry, industry_map)

    candidates = selected

    selected_set = set(candidates)
    newly_added = tuple(s for s in candidates if s not in current_set)
    removed = tuple(s for s in current_holdings if s not in selected_set)
    held_over = tuple(s for s in candidates if s in current_set)

    return PortfolioSelection(
        selected=tuple(candidates),
        newly_added=newly_added,
        removed=removed,
        held_over=held_over,
        composite_ranks=dict(composite_ranks),
        config=config,
    )


def _apply_industry_cap(
    candidates: list[str],
    max_per_industry: int,
    industry_map: dict[str, str],
) -> list[str]:
    """限制每个行业的持仓数量。"""
    industry_count: dict[str, int] = defaultdict(int)
    result: list[str] = []
    for sym in candidates:
        ind = industry_map.get(sym, "__unknown__")
        if industry_count[ind] < max_per_industry:
            result.append(sym)
            industry_count[ind] += 1
    return result
