"""可转债双低策略版本化配置 schema(issue #63)。

设计原则:
1. **预声明**:双低公式、因子权重、约束在实验前冻结,``as_dict()`` 归档。
2. **PIT 安全**:事件过滤严格使用 ``available_at``,候选池不回填存续债券。
3. **long-only**:禁止融券对冲正股、禁止裸卖空转债。
4. **成本约束**:佣金/滑点/冲击单独归因;换手预算硬约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

CONVERTIBLE_DOUBLE_LOW_VERSION = "v1"
"""可转债双低策略框架版本号;因子公式 / 权重 / 约束语义变更时递增。"""

TRADING_DAYS_PER_MONTH = 21


class RebalanceFrequency:
    """调仓频率(字符串常量,避免 StrEnum 导入膨胀)。"""

    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"


class FactorWeight:
    """预注册的因子权重标识。"""

    PRICE = "price"
    PREMIUM = "premium"
    YTM = "ytm"
    DURATION = "duration"
    LIQUIDITY = "liquidity"


DEFAULT_FACTOR_WEIGHTS: dict[str, Decimal] = {
    FactorWeight.PRICE: Decimal("1.0"),
    FactorWeight.PREMIUM: Decimal("1.0"),
    FactorWeight.YTM: Decimal("0.0"),
    FactorWeight.DURATION: Decimal("0.0"),
    FactorWeight.LIQUIDITY: Decimal("0.0"),
}


def field_default_weights() -> dict[str, Decimal]:
    """返回因子权重的深拷贝(避免可变默认参数)。"""
    return dict(DEFAULT_FACTOR_WEIGHTS)


@dataclass(frozen=True, slots=True)
class ConvertibleDoubleLowConfig:
    """可转债双低 x 质量 x 事件风险策略完整配置。

    所有参数在实验前声明并冻结;``as_dict()`` 输出可归档到
    :class:`~finboard_backtest.validation.contracts.VersionStamp`。
    """

    universe_min_listing_days: int = 15
    """上市天数下限;过短的新券定价未稳定,排除。"""

    universe_min_remaining_days: int = 30
    """剩余天数下限;临近到期的券流动性骤降,排除。"""

    universe_min_amount_20d: Decimal = Decimal("5000000")
    """20 日日均成交额下限(元);低于此值的券不可交易。"""

    universe_min_remaining_size: Decimal = Decimal("50000000")
    """剩余规模下限(元);过小易被操纵。"""

    price_max: Decimal = Decimal("130")
    """价格上限;高于此值的转债债性弱,双低逻辑不适用。"""

    price_min: Decimal = Decimal("100")
    """价格下限;低于面值的可能有信用风险(排除已强赎的)。"""

    premium_max: Decimal = Decimal("0.50")
    """转股溢价率上限(小数);过高则股性弱。"""

    factor_weights: dict[str, Decimal] = field(default_factory=field_default_weights)
    """因子权重;价格和溢价默认各 1.0,质量因子默认 0。"""

    top_n: int = 20
    """选券数量。"""

    max_weight_per_bond: Decimal = Decimal("0.10")
    """单券权重上限。"""

    max_weight_per_issuer: Decimal = Decimal("0.15")
    """单发行人(正股)权重上限。"""

    rebalance_frequency: str = RebalanceFrequency.MONTHLY
    """调仓频率。"""

    entry_buffer_pct: Decimal = Decimal("0.02")
    """进入缓冲:排名在 top_n * (1 + buffer) 内才允许新开仓。"""

    exit_buffer_pct: Decimal = Decimal("0.05")
    """退出缓冲:排名跌出 top_n * (1 + exit_buffer) 才平仓。"""

    max_daily_turnover: Decimal = Decimal("0.30")
    """单日换手率上限。"""

    max_participation: Decimal = Decimal("0.10")
    """单券成交量参与率上限。"""

    commission_rate: Decimal = Decimal("0.0002")
    """佣金费率(万 2)。"""

    slippage_bps: Decimal = Decimal("5")
    """滑点(bps)。"""

    capital: Decimal = Decimal("100000")
    """初始资金(元)。"""

    version: str = CONVERTIBLE_DOUBLE_LOW_VERSION

    def __post_init__(self) -> None:
        if self.universe_min_listing_days < 0:
            raise ValueError("universe_min_listing_days 不能为负")
        if self.universe_min_remaining_days < 0:
            raise ValueError("universe_min_remaining_days 不能为负")
        if self.universe_min_amount_20d < 0:
            raise ValueError("universe_min_amount_20d 不能为负")
        if self.universe_min_remaining_size < 0:
            raise ValueError("universe_min_remaining_size 不能为负")
        if self.price_max <= self.price_min:
            raise ValueError("price_max 必须大于 price_min")
        if self.premium_max < 0:
            raise ValueError("premium_max 不能为负")
        if self.top_n < 1:
            raise ValueError("top_n 至少为 1")
        if not (Decimal("0") < self.max_weight_per_bond <= Decimal("1")):
            raise ValueError("max_weight_per_bond 必须落在 (0, 1]")
        if not (Decimal("0") < self.max_weight_per_issuer <= Decimal("1")):
            raise ValueError("max_weight_per_issuer 必须落在 (0, 1]")
        if self.rebalance_frequency not in (
            RebalanceFrequency.MONTHLY,
            RebalanceFrequency.BIWEEKLY,
        ):
            raise ValueError(f"未知调仓频率: {self.rebalance_frequency}")
        if self.entry_buffer_pct < 0:
            raise ValueError("entry_buffer_pct 不能为负")
        if self.exit_buffer_pct < 0:
            raise ValueError("exit_buffer_pct 不能为负")
        if not (Decimal("0") <= self.max_daily_turnover <= Decimal("1")):
            raise ValueError("max_daily_turnover 必须落在 [0, 1]")
        if not (Decimal("0") <= self.max_participation <= Decimal("1")):
            raise ValueError("max_participation 必须落在 [0, 1]")
        if self.commission_rate < 0:
            raise ValueError("commission_rate 不能为负")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps 不能为负")
        if self.capital <= 0:
            raise ValueError("capital 必须为正")

    @property
    def rebalance_interval_days(self) -> int:
        if self.rebalance_frequency == RebalanceFrequency.MONTHLY:
            return TRADING_DAYS_PER_MONTH
        return 10

    @property
    def min_data_days(self) -> int:
        """策略所需的最少历史数据天数(用于预热)。"""
        return max(self.universe_min_listing_days, 20) + self.rebalance_interval_days

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "universe_min_listing_days": self.universe_min_listing_days,
            "universe_min_remaining_days": self.universe_min_remaining_days,
            "universe_min_amount_20d": str(self.universe_min_amount_20d),
            "universe_min_remaining_size": str(self.universe_min_remaining_size),
            "price_max": str(self.price_max),
            "price_min": str(self.price_min),
            "premium_max": str(self.premium_max),
            "factor_weights": {k: str(v) for k, v in self.factor_weights.items()},
            "top_n": self.top_n,
            "max_weight_per_bond": str(self.max_weight_per_bond),
            "max_weight_per_issuer": str(self.max_weight_per_issuer),
            "rebalance_frequency": self.rebalance_frequency,
            "entry_buffer_pct": str(self.entry_buffer_pct),
            "exit_buffer_pct": str(self.exit_buffer_pct),
            "max_daily_turnover": str(self.max_daily_turnover),
            "max_participation": str(self.max_participation),
            "commission_rate": str(self.commission_rate),
            "slippage_bps": str(self.slippage_bps),
            "capital": str(self.capital),
        }


__all__ = [
    "CONVERTIBLE_DOUBLE_LOW_VERSION",
    "DEFAULT_FACTOR_WEIGHTS",
    "ConvertibleDoubleLowConfig",
    "FactorWeight",
    "RebalanceFrequency",
    "field_default_weights",
]
