"""多因子评分管线:原始数据 → 标准化 → 中性化 → 组合 → 排名。

整个管线是纯函数式的:输入相同的 factor_matrix + config 永远产生相同的输出,
不依赖外部状态、随机种子或数据遍历顺序之外的隐式假设。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from finboard_backtest.factors.catalog import (
    FACTOR_FRAMEWORK_VERSION,
    FactorMeta,
    StandardizeMethod,
    get_factor_meta,
)
from finboard_backtest.factors.combine import (
    CombinationConfig,
    combine_scores,
)
from finboard_backtest.factors.standardize import (
    apply_direction,
    fill_missing,
    industry_demean,
    rank_normalize,
    regression_neutralize,
    winsorize,
    zscore,
)

FactorMatrix = dict[str, dict[str, float]]


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """多因子评分配置;实验前冻结。"""

    combination: CombinationConfig
    industry_neutralize: bool = False
    cap_neutralize: bool = False
    min_coverage: float = 0.50
    framework_version: str = FACTOR_FRAMEWORK_VERSION

    def __post_init__(self) -> None:
        if self.min_coverage < 0 or self.min_coverage > 1:
            raise ValueError(f"min_coverage 必须在 [0,1],当前: {self.min_coverage}")

    @property
    def factor_names(self) -> tuple[str, ...]:
        return self.combination.factor_names

    def as_dict(self) -> dict[str, object]:
        return {
            "combination": self.combination.as_dict(),
            "industry_neutralize": self.industry_neutralize,
            "cap_neutralize": self.cap_neutralize,
            "min_coverage": self.min_coverage,
            "framework_version": self.framework_version,
        }


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """评分管线输出。"""

    composite_scores: dict[str, float]
    factor_scores: dict[str, dict[str, float]]
    factor_ranks: dict[str, dict[str, int]]
    coverage: dict[str, float]
    n_eligible: int
    n_total: int
    config: ScoringConfig
    scored_at: date | None = None

    @property
    def ranked_symbols(self) -> list[str]:
        valid = {
            s: v
            for s, v in self.composite_scores.items()
            if np.isfinite(v)
        }
        return sorted(valid, key=lambda s: valid[s], reverse=True)

    def top_n(self, n: int) -> list[str]:
        return self.ranked_symbols[:n]


class MultiFactorScorer:
    """多因子评分器:factor_matrix → standardized → combined → ranked。"""

    def __init__(self, config: ScoringConfig) -> None:
        self._config = config

    @property
    def config(self) -> ScoringConfig:
        return self._config

    def score(
        self,
        factor_matrix: FactorMatrix,
        *,
        industry_map: dict[str, str] | None = None,
        market_caps: dict[str, float] | None = None,
        scored_at: date | None = None,
    ) -> ScoringResult:
        all_symbols = self._collect_symbols(factor_matrix)
        if not all_symbols:
            return ScoringResult(
                composite_scores={},
                factor_scores={},
                factor_ranks={},
                coverage={},
                n_eligible=0,
                n_total=0,
                config=self._config,
                scored_at=scored_at,
            )

        if self._config.cap_neutralize and market_caps is None:
            market_caps = factor_matrix.get("market_cap", {})

        factor_scores: dict[str, dict[str, float]] = {}
        coverage: dict[str, float] = {}

        for factor_name in self._config.factor_names:
            raw = factor_matrix.get(factor_name, {})
            meta = get_factor_meta(factor_name)
            processed = self._process_factor(
                raw, meta, industry_map, market_caps,
            )
            factor_scores[factor_name] = processed
            n_valid = sum(1 for v in processed.values() if np.isfinite(v))
            coverage[factor_name] = n_valid / len(all_symbols) if all_symbols else 0.0

        composite = combine_scores(factor_scores, self._config.combination)
        ranks = self._compute_ranks(factor_scores, composite)

        n_eligible = sum(1 for v in composite.values() if np.isfinite(v))

        return ScoringResult(
            composite_scores=composite,
            factor_scores=factor_scores,
            factor_ranks=ranks,
            coverage=coverage,
            n_eligible=n_eligible,
            n_total=len(all_symbols),
            config=self._config,
            scored_at=scored_at,
        )

    def _collect_symbols(self, fm: FactorMatrix) -> set[str]:
        syms: set[str] = set()
        for factor_data in fm.values():
            syms |= set(factor_data.keys())
        return syms

    def _process_factor(
        self,
        raw: dict[str, float],
        meta: FactorMeta,
        industry_map: dict[str, str] | None,
        market_caps: dict[str, float] | None,
    ) -> dict[str, float]:
        vals = dict(raw)
        vals = fill_missing(vals, meta.missing_strategy.value)
        vals = winsorize(
            vals,
            lower_pct=meta.winsorize_lower_pct,
            upper_pct=meta.winsorize_upper_pct,
        )
        if meta.standardize is StandardizeMethod.RANK:
            standardized = rank_normalize(vals)
        elif meta.standardize is StandardizeMethod.ZSCORE:
            standardized = zscore(vals)
        else:
            standardized = {k: v if np.isfinite(v) else float("nan") for k, v in vals.items()}

        if self._config.industry_neutralize and industry_map:
            standardized = industry_demean(standardized, industry_map)

        if self._config.cap_neutralize and market_caps:
            log_caps: dict[str, float] = {}
            for s, mc in market_caps.items():
                if mc > 0:
                    log_caps[s] = float(np.log(mc))
                else:
                    log_caps[s] = float("nan")
            standardized = regression_neutralize(standardized, log_caps)

        standardized = apply_direction(standardized, meta.direction_sign)
        return standardized

    def _compute_ranks(
        self,
        factor_scores: dict[str, dict[str, float]],
        composite: dict[str, float],
    ) -> dict[str, dict[str, int]]:
        ranks: dict[str, dict[str, int]] = {}
        for factor_name, scores in factor_scores.items():
            ranks[factor_name] = self._rank_dict(scores)
        ranks["__composite__"] = self._rank_dict(composite)
        return ranks

    @staticmethod
    def _rank_dict(scores: dict[str, float]) -> dict[str, int]:
        valid = {s: v for s, v in scores.items() if np.isfinite(v)}
        ranked = sorted(valid, key=lambda s: valid[s], reverse=True)
        return {s: i + 1 for i, s in enumerate(ranked)}
