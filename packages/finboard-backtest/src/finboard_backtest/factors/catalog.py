"""研究因子目录:经济假设、方向、类别、标准化参数。

每个因子声明其经济假设、预期失效场景、数据来源字段、极值处理和缺失策略,
使复合评分完全可归档、可复现、可审查。因子方向决定原始值到得分的映射:
``LONG`` 表示值越高得分越高,``SHORT`` 表示值越低得分越高(如 PB 越低越便宜)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

FACTOR_FRAMEWORK_VERSION = "v1"


class FactorCategory(StrEnum):
    VALUE = "value"
    QUALITY = "quality"
    LOW_RISK = "low_risk"
    LIQUIDITY = "liquidity"
    MOMENTUM = "momentum"
    GROWTH = "growth"


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


RESEARCH_FACTOR_CATALOG: dict[str, FactorMeta] = {
    # ── 估值 ──
    "pb": FactorMeta(
        name="pb",
        category=FactorCategory.VALUE,
        direction=FactorDirection.SHORT,
        source_field="daily.pb",
        economic_hypothesis="低市净率股票长期跑赢高市净率股票(Fama-French HML)。",
        expected_failure="成长股牛市中低 PB 可能是价值陷阱(银行/地产周期底部)。",
        unit="multiple",
        available_at_rule="T-day daily_metrics",
    ),
    "earnings_yield": FactorMeta(
        name="earnings_yield",
        category=FactorCategory.VALUE,
        direction=FactorDirection.LONG,
        source_field="derived:1/pe_ttm",
        economic_hypothesis="盈利收益率(1/PE_TTM)高的股票长期跑赢,类似 EP 因子。",
        expected_failure="周期股高点 PE 低但盈利不可持续;亏损股 PE 为负需排除。",
        unit="ratio",
        available_at_rule="T-day daily_metrics",
    ),
    "dividend_yield": FactorMeta(
        name="dividend_yield",
        category=FactorCategory.VALUE,
        direction=FactorDirection.LONG,
        source_field="daily.dividend_yield_ttm",
        economic_hypothesis="高股息率提供安全边际和复利再投资,在低利率环境下更受青睐。",
        expected_failure="特别分红或利润大幅下滑导致股息率虚高;价值陷阱。",
        unit="ratio",
        available_at_rule="T-day daily_metrics",
    ),
    # ── 质量 ──
    "roe": FactorMeta(
        name="roe",
        category=FactorCategory.QUALITY,
        direction=FactorDirection.LONG,
        source_field="financial.return_on_equity",
        economic_hypothesis="高 ROE 公司资本配置效率高,长期产生超额回报。",
        expected_failure="杠杆推高的 ROE 不可持续;行业间 ROE 中枢差异大。",
        unit="ratio",
        available_at_rule="announcement_date (point-in-time)",
        standardize=StandardizeMethod.ZSCORE,
    ),
    "gross_profit_margin": FactorMeta(
        name="gross_profit_margin",
        category=FactorCategory.QUALITY,
        direction=FactorDirection.LONG,
        source_field="financial.gross_profit_margin",
        economic_hypothesis="高毛利率反映定价权和成本优势,Novy-Marx 质量因子。",
        expected_failure="行业差异极大(科技 vs 公用事业);毛利率提升但收入下滑。",
        unit="ratio",
        available_at_rule="announcement_date (point-in-time)",
    ),
    "debt_to_assets": FactorMeta(
        name="debt_to_assets",
        category=FactorCategory.QUALITY,
        direction=FactorDirection.SHORT,
        source_field="financial.debt_to_assets",
        economic_hypothesis="低杠杆公司财务风险小,尾部风险低,长期表现更稳定。",
        expected_failure="金融/地产行业天然高杠杆;低杠杆可能是增长停滞。",
        unit="ratio",
        available_at_rule="announcement_date (point-in-time)",
    ),
    # ── 低风险 ──
    "volatility_20d": FactorMeta(
        name="volatility_20d",
        category=FactorCategory.LOW_RISK,
        direction=FactorDirection.SHORT,
        source_field="derived:daily_returns_std_20d",
        economic_hypothesis="低波动率异象:低风险股票风险调整后收益优于高波动股票。",
        expected_failure="市场底部低波动股可能补跌;短期波动率噪音大。",
        unit="ratio",
        available_at_rule="T-day bars (20-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "volatility_60d": FactorMeta(
        name="volatility_60d",
        category=FactorCategory.LOW_RISK,
        direction=FactorDirection.SHORT,
        source_field="derived:daily_returns_std_60d",
        economic_hypothesis="60日波动率比20日更稳定,捕捉中期风险水平。",
        expected_failure="波动率聚集效应(volatility clustering)使近期值滞后。",
        unit="ratio",
        available_at_rule="T-day bars (60-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "volatility_120d": FactorMeta(
        name="volatility_120d",
        category=FactorCategory.LOW_RISK,
        direction=FactorDirection.SHORT,
        source_field="derived:daily_returns_std_120d",
        economic_hypothesis="120日波动率反映长期风险水平,低频策略偏好。",
        expected_failure="长期波动率可能包含已消退的风险事件。",
        unit="ratio",
        available_at_rule="T-day bars (120-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    "downside_volatility": FactorMeta(
        name="downside_volatility",
        category=FactorCategory.LOW_RISK,
        direction=FactorDirection.SHORT,
        source_field="derived:downside_returns_std_60d",
        economic_hypothesis="下行波动率只惩罚亏损,比全样本波动率更贴合实际风险感受。",
        expected_failure="样本不足时估计不稳定;牛市中下行波动率系统性偏低。",
        unit="ratio",
        available_at_rule="T-day bars (60-day window)",
        winsorize_lower_pct=0.02,
        winsorize_upper_pct=0.98,
    ),
    # ── 流动性 ──
    "turnover_rate": FactorMeta(
        name="turnover_rate",
        category=FactorCategory.LIQUIDITY,
        direction=FactorDirection.NEUTRAL if False else FactorDirection.SHORT,
        source_field="daily.turnover_rate",
        economic_hypothesis="低换手率股票持有者更稳定,流动性溢价补偿。",
        expected_failure="低换手率可能反映无人关注;小盘股低换手率实盘冲击成本极高。",
        unit="ratio",
        available_at_rule="T-day daily_metrics",
        missing_strategy=MissingStrategy.FILL_MEDIAN,
    ),
    # ── 动量 ──
    "momentum": FactorMeta(
        name="momentum",
        category=FactorCategory.MOMENTUM,
        direction=FactorDirection.LONG,
        source_field="derived:return_excl_latest_20d",
        economic_hypothesis="中期动量(12M-1M):过去赢家继续跑赢输家。",
        expected_failure="A股短期反转效应更强;动量在市场转折点反转剧烈。",
        unit="ratio",
        available_at_rule="T-day bars",
        winsorize_lower_pct=0.01,
        winsorize_upper_pct=0.99,
    ),
    # ── 增长 ──
    "revenue_yoy": FactorMeta(
        name="revenue_yoy",
        category=FactorCategory.GROWTH,
        direction=FactorDirection.LONG,
        source_field="financial.revenue_yoy",
        economic_hypothesis="收入增长反映企业扩张能力,增长股长期享有估值溢价。",
        expected_failure="并购导致的一次性增长不可持续;周期股增长见顶信号。",
        unit="ratio",
        available_at_rule="announcement_date (point-in-time)",
    ),
}


def get_factor_meta(name: str) -> FactorMeta:
    if name not in RESEARCH_FACTOR_CATALOG:
        raise KeyError(f"未知因子: {name};可用因子: {sorted(RESEARCH_FACTOR_CATALOG)}")
    return RESEARCH_FACTOR_CATALOG[name]


def list_factors_by_category(category: FactorCategory) -> tuple[str, ...]:
    return tuple(
        sorted(k for k, v in RESEARCH_FACTOR_CATALOG.items() if v.category is category)
    )
