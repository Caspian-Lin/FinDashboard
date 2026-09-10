"""研究因子目录:评分消费参数 + 自 FACTOR_LAB_CATALOG 投影的语义字段。

本目录(#226 起)只手写复合评分的消费参数——类别、极值处理、标准化、
缺失策略、PIT 规则与原始值单位;因子的语义字段(经济假设 / 预期失效 /
数据来源 / 方向)逐项从 ``finboard_data.factor_lab.FACTOR_LAB_CATALOG``
投影生成,不再双头维护。因子方向决定原始值到得分的映射:``LONG`` 表示
值越高得分越高,``SHORT`` 表示值越低得分越高(如 PB 越低越便宜)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from finboard_data.factor_lab import (
    FACTOR_LAB_CATALOG,
    FactorPreference,
)

FACTOR_FRAMEWORK_VERSION = "v1"


class FactorCategory(StrEnum):
    VALUE = "value"
    QUALITY = "quality"
    LOW_RISK = "low_risk"
    LIQUIDITY = "liquidity"
    MOMENTUM = "momentum"
    GROWTH = "growth"
    # 预置因子目录(p_,#403 接入 #226 投影)新增族:category=family 直映射
    REVERSAL = "reversal"
    RISK = "risk"
    SIZE = "size"
    ALPHA101 = "alpha101"


class FactorDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class MissingStrategy(StrEnum):
    EXCLUDE = "exclude"
    FILL_MEDIAN = "fill_median"
    FILL_WORST = "fill_worst"


class StandardizeMethod(StrEnum):
    ZSCORE = "zscore"
    RANK = "rank"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class FactorMeta:
    """一项因子的完整研究元数据。"""

    name: str
    category: FactorCategory
    direction: FactorDirection
    source_field: str
    economic_hypothesis: str
    expected_failure: str
    unit: str = "ratio"
    winsorize_lower_pct: float = 0.01
    winsorize_upper_pct: float = 0.99
    standardize: StandardizeMethod = StandardizeMethod.ZSCORE
    missing_strategy: MissingStrategy = MissingStrategy.EXCLUDE
    available_at_rule: str = ""
    version: str = FACTOR_FRAMEWORK_VERSION

    @property
    def direction_sign(self) -> float:
        return 1.0 if self.direction is FactorDirection.LONG else -1.0


@dataclass(frozen=True, slots=True)
class _ScoringParams:
    """评分器本地消费参数;语义字段由投影补齐。"""

    category: FactorCategory
    unit: str = "ratio"
    winsorize_lower_pct: float = 0.01
    winsorize_upper_pct: float = 0.99
    standardize: StandardizeMethod = StandardizeMethod.ZSCORE
    missing_strategy: MissingStrategy = MissingStrategy.EXCLUDE
    available_at_rule: str = ""


_SCORING_PARAMS: dict[str, _ScoringParams] = {
    # ── 估值 ──
    "pb": _ScoringParams(
        category=FactorCategory.VALUE,
        unit="multiple",
        available_at_rule="T-day daily_metrics",
    ),
    "earnings_yield": _ScoringParams(
        category=FactorCategory.VALUE,
        available_at_rule="T-day daily_metrics",
    ),
    "dividend_yield": _ScoringParams(
        category=FactorCategory.VALUE,
        available_at_rule="T-day daily_metrics",
    ),
    # ── 质量 ──
    "roe": _ScoringParams(
        category=FactorCategory.QUALITY,
        available_at_rule="announcement_date (point-in-time)",
        standardize=StandardizeMethod.ZSCORE,
    ),
    "gross_profit_margin": _ScoringParams(
        category=FactorCategory.QUALITY,
        available_at_rule="announcement_date (point-in-time)",
    ),
    "debt_to_assets": _ScoringParams(
        category=FactorCategory.QUALITY,
        available_at_rule="announcement_date (point-in-time)",
    ),
    # ── 低风险 ──
    "volatility_20d": _ScoringParams(
        category=FactorCategory.LOW_RISK,
        available_at_rule="T-day bars (20-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "volatility_60d": _ScoringParams(
        category=FactorCategory.LOW_RISK,
        available_at_rule="T-day bars (60-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "volatility_120d": _ScoringParams(
        category=FactorCategory.LOW_RISK,
        available_at_rule="T-day bars (120-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "downside_volatility": _ScoringParams(
        category=FactorCategory.LOW_RISK,
        available_at_rule="T-day bars (60-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    # ── 流动性 ──
    "turnover_rate": _ScoringParams(
        category=FactorCategory.LIQUIDITY,
        available_at_rule="T-day daily_metrics",
        missing_strategy=MissingStrategy.FILL_MEDIAN,
    ),
    # ── 动量 ──
    "momentum": _ScoringParams(
        category=FactorCategory.MOMENTUM,
        available_at_rule="T-day bars",
        winsorize_lower_pct=0.01,
        winsorize_upper_pct=0.99,
    ),
    # ── 增长 ──
    "revenue_yoy": _ScoringParams(
        category=FactorCategory.GROWTH,
        available_at_rule="announcement_date (point-in-time)",
    ),
}

_DIRECTION_FROM_PREFERENCE = {
    FactorPreference.HIGHER: FactorDirection.LONG,
    FactorPreference.LOWER: FactorDirection.SHORT,
}


def _project_research_catalog() -> dict[str, FactorMeta]:
    """把 FACTOR_LAB_CATALOG 的语义字段投影到评分消费参数上(issue #226)。

    v2 目录是唯一事实来源;评分因子名在 v2 缺失、或 preference 是
    EXPOSURE_ONLY(无方向语义,不能进复合评分)时导入期即失败,防漂移。
    """

    catalog: dict[str, FactorMeta] = {}
    for name, params in _SCORING_PARAMS.items():
        try:
            source = FACTOR_LAB_CATALOG[name]
        except KeyError as exc:
            raise RuntimeError(
                f"评分因子 {name} 在 FACTOR_LAB_CATALOG 中不存在;"
                "因子语义已收敛为 v2 唯一事实来源,请先在 factor_lab 登记该因子"
            ) from exc
        try:
            direction = _DIRECTION_FROM_PREFERENCE[source.preference]
        except KeyError as exc:
            raise RuntimeError(
                f"评分因子 {name} 的 preference={source.preference.value} "
                "无方向映射,不能进入复合评分目录"
            ) from exc
        catalog[name] = FactorMeta(
            name=name,
            category=params.category,
            direction=direction,
            source_field=source.source_fields[0],
            economic_hypothesis=source.economic_hypothesis,
            expected_failure=source.expected_failure,
            unit=params.unit,
            winsorize_lower_pct=params.winsorize_lower_pct,
            winsorize_upper_pct=params.winsorize_upper_pct,
            standardize=params.standardize,
            missing_strategy=params.missing_strategy,
            available_at_rule=params.available_at_rule,
        )
    return catalog


RESEARCH_FACTOR_CATALOG: dict[str, FactorMeta] = _project_research_catalog()


# --------------------------------------------------------------------- #
# 预置因子目录(p_)的评分投影(#403,继 #226 模式)
# --------------------------------------------------------------------- #

#: 预置因子家族 → 评分消费参数(#226 的 _SCORING_PARAMS 家族级形态:
#: 语义字段 direction/category/hypothesis 自目录投影,不逐因子手写;
#: 缺省 = winsorize 1%/99% + zscore + exclude,与 _ScoringParams 默认一致)
_PREDEFINED_FAMILY_SCORING: dict[str, _ScoringParams] = {
    "momentum": _ScoringParams(
        category=FactorCategory.MOMENTUM,
        available_at_rule="T-day bars (trailing window)",
    ),
    "reversal": _ScoringParams(
        category=FactorCategory.REVERSAL,
        available_at_rule="T-day bars (trailing window)",
    ),
    "risk": _ScoringParams(
        category=FactorCategory.RISK,
        available_at_rule="T-day bars (trailing window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "liquidity": _ScoringParams(
        category=FactorCategory.LIQUIDITY,
        available_at_rule="T-day bars/daily_metrics",
        missing_strategy=MissingStrategy.FILL_MEDIAN,
    ),
    "size": _ScoringParams(
        category=FactorCategory.SIZE,
        available_at_rule="T-day daily_metrics",
    ),
    "alpha101": _ScoringParams(
        category=FactorCategory.ALPHA101,
        available_at_rule="T-day bars (cross-sectional)",
    ),
    "growth": _ScoringParams(
        category=FactorCategory.GROWTH,
        available_at_rule="announcement_date (point-in-time)",
    ),
    "quality": _ScoringParams(
        category=FactorCategory.QUALITY,
        available_at_rule="announcement_date (point-in-time)",
    ),
    "value": _ScoringParams(
        category=FactorCategory.VALUE,
        available_at_rule="announcement_date (point-in-time)",
    ),
}


def _predefined_family_category(family: str) -> FactorCategory:
    """家族 → 评分 category(未知家族导入期即失败,防新族静默漏投影)。"""
    params = _PREDEFINED_FAMILY_SCORING.get(family)
    if params is None:
        raise RuntimeError(
            f"预置因子家族 {family!r} 无评分投影规则:请在 "
            "_PREDEFINED_FAMILY_SCORING 登记该族的 category 与评分参数"
            "(#226 fail-loud 防漂移)"
        )
    return params.category


def _hypothesis_from_title(name: str, title: str) -> str:
    """条目 title → 经济假设文本(剥 ``<name> = `` 前缀,公式即语义)。"""
    prefix = f"{name} = "
    return title.removeprefix(prefix)


def _project_predefined_catalog() -> dict[str, FactorMeta]:
    """预置因子目录(p_<bare>)→ 评分目录投影(issue #403,继 #226)。

    direction / family(=category)/ economic_hypothesis(=title 剥名)
    自 ``factors.predefined`` 目录逐项投影;评分参数按
    :data:`_PREDEFINED_FAMILY_SCORING` 家族级取值。暴露族
    (size/risk/liquidity)同样入投影(低风险/规模类别是复合评分的
    合法成分,与 #226 的 13 因子目录同口径);signal_eligible 治理
    (#214 规则校验)在 ``factors.eval`` 评估报告承担,不在投影层拦截。
    """
    from finboard_backtest.factors.predefined.registry import PREDEFINED_FACTORS

    catalog: dict[str, FactorMeta] = {}
    for bare, item in PREDEFINED_FACTORS.items():
        params = _PREDEFINED_FAMILY_SCORING.get(item.family)
        if params is None:
            raise RuntimeError(
                f"预置因子家族 {item.family!r} 无评分投影规则:请在 "
                "_PREDEFINED_FAMILY_SCORING 登记该族的 category 与评分参数"
                "(#226 fail-loud 防漂移)"
            )
        try:
            direction = _DIRECTION_FROM_PREFERENCE[item.direction]
        except KeyError as exc:  # pragma: no cover - 预置目录无 EXPOSURE_ONLY
            raise RuntimeError(
                f"预置因子 {bare} 的 direction={item.direction.value} "
                "无方向映射,不能进入评分目录"
            ) from exc
        catalog[f"p_{bare}"] = FactorMeta(
            name=f"p_{bare}",
            category=params.category,
            direction=direction,
            source_field=item.data_dependencies[0],
            economic_hypothesis=_hypothesis_from_title(bare, item.title),
            expected_failure="",
            unit="raw",
            winsorize_lower_pct=params.winsorize_lower_pct,
            winsorize_upper_pct=params.winsorize_upper_pct,
            standardize=params.standardize,
            missing_strategy=params.missing_strategy,
            available_at_rule=params.available_at_rule,
            version=item.implementation_version,
        )
    return catalog


#: 预置因子评分目录(键 = p_<bare>;导入期投影,家族/方向漂移即失败)
PREDEFINED_SCORING_CATALOG: dict[str, FactorMeta] = _project_predefined_catalog()


def get_factor_meta(name: str) -> FactorMeta:
    """评分消费的因子元数据:裸名走 #226 目录,p_ 前缀走 #403 投影。"""
    if name.startswith("p_"):
        if name not in PREDEFINED_SCORING_CATALOG:
            raise KeyError(
                f"未知预置评分因子: {name};可用: "
                f"{sorted(PREDEFINED_SCORING_CATALOG)[:8]} ..."
            )
        return PREDEFINED_SCORING_CATALOG[name]
    if name not in RESEARCH_FACTOR_CATALOG:
        raise KeyError(f"未知因子: {name};可用因子: {sorted(RESEARCH_FACTOR_CATALOG)}")
    return RESEARCH_FACTOR_CATALOG[name]


def list_factors_by_category(category: FactorCategory) -> tuple[str, ...]:
    return tuple(
        sorted(k for k, v in RESEARCH_FACTOR_CATALOG.items() if v.category is category)
    )
