"""可转债双低 x 质量 x 事件风险研究策略(issue #63)。

本子包仅用于离线研究 / 回测,**不连接实盘、不申报程序化交易**。

模块组成:
* config:版本化配置(因子权重 / 约束 / 调仓参数)
* universe:PIT 候选池过滤(上市/退市/余额/成交额/价格/溢价)
* signals:双低排名 + 可选质量因子(YTM/期限/流动性)
* events:事件风险过滤(强赎/回售/下修/到期/退市),严格使用 available_at
* backtest:T+1 执行引擎(可转债 T+0 回转 / 免印花税 / 佣金万 2)
* analysis:收益归因 + 成本拆分 + 资金档位可行性
"""

from finboard_backtest.convertible_double_low.analysis import (
    CAPITAL_TIERS,
    TIER_LABELS,
    CapitalTierFeasibility,
    ConvertibleDoubleLowAnalysis,
    CostAttribution,
    ReturnAttribution,
    analyze_convertible_double_low,
    check_capital_tiers,
    compute_cost_attribution,
    compute_return_attribution,
)
from finboard_backtest.convertible_double_low.backtest import (
    ConvertibleBacktestResult,
    ConvertibleTrade,
    HoldingPeriod,
    run_backtest,
)
from finboard_backtest.convertible_double_low.config import (
    CONVERTIBLE_DOUBLE_LOW_VERSION,
    DEFAULT_FACTOR_WEIGHTS,
    ConvertibleDoubleLowConfig,
    FactorWeight,
    RebalanceFrequency,
)
from finboard_backtest.convertible_double_low.events import (
    EventRiskResult,
    check_event_risk,
    filter_event_risk,
)
from finboard_backtest.convertible_double_low.signals import (
    ConvertibleSignal,
    compute_composite_score,
    compute_rank_threshold,
    generate_signals,
)
from finboard_backtest.convertible_double_low.universe import (
    ConvertibleSnapshot,
    filter_universe,
    is_tradable_on,
)

__all__ = [
    "CAPITAL_TIERS",
    "CONVERTIBLE_DOUBLE_LOW_VERSION",
    "DEFAULT_FACTOR_WEIGHTS",
    "TIER_LABELS",
    "CapitalTierFeasibility",
    "ConvertibleBacktestResult",
    "ConvertibleDoubleLowAnalysis",
    "ConvertibleDoubleLowConfig",
    "ConvertibleSignal",
    "ConvertibleSnapshot",
    "ConvertibleTrade",
    "CostAttribution",
    "EventRiskResult",
    "FactorWeight",
    "HoldingPeriod",
    "RebalanceFrequency",
    "ReturnAttribution",
    "analyze_convertible_double_low",
    "check_capital_tiers",
    "check_event_risk",
    "compute_composite_score",
    "compute_cost_attribution",
    "compute_rank_threshold",
    "compute_return_attribution",
    "filter_event_risk",
    "filter_universe",
    "generate_signals",
    "is_tradable_on",
    "run_backtest",
]
