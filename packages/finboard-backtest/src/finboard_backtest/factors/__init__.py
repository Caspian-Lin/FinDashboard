"""多因子研究框架:因子目录、横截面标准化、复合评分、组合选择和因子分析。

核心流程::

    FactorInputBatch ──extract──▶ FactorMatrix
        │
        ▼
    MultiFactorScorer.score() ──▶ ScoringResult
        │                           ├── composite_scores
        │                           ├── factor_scores (标准化后)
        │                           ├── factor_ranks
        │                           └── coverage
        ▼
    select_portfolio() ──▶ PortfolioSelection
        │                     ├── selected
        │                     ├── newly_added / removed / held_over
        │                     └── composite_ranks
        ▼
    (输出 Signal → Allocator → solve_sizing → RebalancePlan)
"""

from __future__ import annotations

from finboard_backtest.factors.analysis import (
    FactorAnalysisReport,
    compute_factor_analysis,
)
from finboard_backtest.factors.catalog import (
    FACTOR_FRAMEWORK_VERSION,
    RESEARCH_FACTOR_CATALOG,
    FactorCategory,
    FactorDirection,
    FactorMeta,
    MissingStrategy,
    StandardizeMethod,
    get_factor_meta,
    list_factors_by_category,
)
from finboard_backtest.factors.combine import (
    CombinationConfig,
    CombinationMethod,
    FactorWeight,
    combine_scores,
)
from finboard_backtest.factors.extract import extract_factor_matrix
from finboard_backtest.factors.scorer import (
    FactorMatrix,
    MultiFactorScorer,
    ScoringConfig,
    ScoringResult,
)
from finboard_backtest.factors.selection import (
    PortfolioSelection,
    SelectionConfig,
    select_portfolio,
)
from finboard_backtest.factors.standardize import (
    apply_direction,
    fill_missing,
    industry_demean,
    rank_normalize,
    regression_neutralize,
    standardize_series,
    winsorize,
    zscore,
)

__all__ = [
    # catalog
    "FACTOR_FRAMEWORK_VERSION",
    "RESEARCH_FACTOR_CATALOG",
    # combine
    "CombinationConfig",
    "CombinationMethod",
    # analysis
    "FactorAnalysisReport",
    "FactorCategory",
    "FactorDirection",
    # scorer
    "FactorMatrix",
    "FactorMeta",
    "FactorWeight",
    "MissingStrategy",
    "MultiFactorScorer",
    # selection
    "PortfolioSelection",
    "ScoringConfig",
    "ScoringResult",
    "SelectionConfig",
    "StandardizeMethod",
    # standardize
    "apply_direction",
    "combine_scores",
    "compute_factor_analysis",
    # extract
    "extract_factor_matrix",
    "fill_missing",
    "get_factor_meta",
    "industry_demean",
    "list_factors_by_category",
    "rank_normalize",
    "regression_neutralize",
    "select_portfolio",
    "standardize_series",
    "winsorize",
    "zscore",
]
