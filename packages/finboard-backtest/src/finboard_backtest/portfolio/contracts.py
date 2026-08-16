"""组合构建层的版本化契约 — Signal / TargetWeight / PortfolioPlan。

issue #59 的核心目标:策略输出标准化 ``Signal``/``TargetWeight``,组合层统一
生成调仓订单意图,而非让每个信号各自争抢现金。

设计原则:
1. **版本化**:所有契约带 ``PORTFOLIO_CONTRACT_VERSION``,结构变更时递增。
2. **可追溯**:每个计划记录策略 ID、时间戳、因子快照来源。
3. **无杠杆默认**:``max_leverage`` 默认 1.0;期货或融资必须显式开启。
4. **约束始终成立**:权重上限、sleeve 上限、现金缓冲、集中度由构建器强制。
5. **纯离线**:不连接实盘,不修改 Risk Manager / Position Manager / 下单链路。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

PORTFOLIO_CONTRACT_VERSION = "v2"
"""组合契约版本号;Signal / TargetWeight / PortfolioPlan 结构变更时递增。"""

MAX_WEIGHT_EPSILON = 1e-9
"""权重合法比较容差:总和误差不超过此值视为合规。"""


@dataclass(frozen=True, slots=True)
class Signal:
    """策略输出的标准化信号。

    ``score`` 为正表示看多、负表示看空、零表示中性;``confidence`` 在
    [0, 1] 区间,可用于加权但不强制。策略不应直接产生订单意图,而是输出
    ``Signal`` 列表,由组合构建层统一转换为 ``TargetWeight``。
    """

    symbol: str
    score: float
    timestamp: date
    strategy_id: str
    confidence: float = 1.0
    factor_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol 不能为空")
        if not self.strategy_id:
            raise ValueError("strategy_id 不能为空")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence 必须落在 [0, 1]")
        if not (abs(self.score) <= 1e9):
            raise ValueError("score 数值异常")


class CovarianceFailureMode(StrEnum):
    """协方差缺失或不可用时的确定性处理方式。"""

    FAIL_CLOSED = "fail_closed"
    FALLBACK_EQUAL_WEIGHT = "fallback_equal_weight"


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    """组合层硬约束 —— 所有分配方法都必须满足。

    约束层级(优先级从高到低):

    1. ``min_cash_buffer`` — 最小现金比例(不可被压缩)。
    2. ``max_weight_per_asset`` — 单标的权重上限。
    3. ``max_weight_per_sleeve`` — 单资产类别(equity/etf/bond/...)
       权重上限,用于控制集中度。
    4. ``max_leverage`` — 杠杆上限;1.0 = 无杠杆(默认)。
    5. ``target_volatility`` / ``max_volatility`` — 年化波动率目标 / 上限。
    6. ``rebalance_threshold`` — 再平衡带:权重偏离不超过此阈值时不调仓。
    """

    max_weight_per_asset: float = 0.25
    max_weight_per_sleeve: float = 0.40
    min_cash_buffer: float = 0.05
    max_leverage: float = 1.0
    target_volatility: float | None = None
    max_volatility: float | None = None
    rebalance_threshold: float = 0.05
    min_weight_to_trade: float = 0.001
    max_risk_contribution: float = 1.0
    long_only: bool = True
    covariance_failure_mode: CovarianceFailureMode = CovarianceFailureMode.FAIL_CLOSED

    def __post_init__(self) -> None:
        if not (0 < self.max_weight_per_asset <= 1.0):
            raise ValueError("max_weight_per_asset 必须落在 (0, 1]")
        if not (0 < self.max_weight_per_sleeve <= 1.0):
            raise ValueError("max_weight_per_sleeve 必须落在 (0, 1]")
        if not (0 <= self.min_cash_buffer < 1.0):
            raise ValueError("min_cash_buffer 必须落在 [0, 1)")
        if self.max_leverage < 1.0:
            raise ValueError("max_leverage 不能小于 1.0(默认无杠杆)")
        if self.target_volatility is not None and self.target_volatility <= 0:
            raise ValueError("target_volatility 必须为正")
        if self.max_volatility is not None and self.max_volatility <= 0:
            raise ValueError("max_volatility 必须为正")
        if (
            self.target_volatility is not None
            and self.max_volatility is not None
            and self.target_volatility > self.max_volatility
        ):
            raise ValueError("target_volatility 不能超过 max_volatility")
        if not (0 <= self.rebalance_threshold <= 0.5):
            raise ValueError("rebalance_threshold 必须落在 [0, 0.5]")
        if self.min_weight_to_trade < 0:
            raise ValueError("min_weight_to_trade 不能为负")
        if not (0 < self.max_risk_contribution <= 1):
            raise ValueError("max_risk_contribution 必须落在 (0, 1]")

    @property
    def max_investable_weight(self) -> float:
        """现金与 gross exposure 分开约束后的最大可投资权重。

        无杠杆时现金来自本金,因此 ``gross <= 1 - cash``。显式开启杠杆后,
        ``max_leverage`` 独立限制名义 gross exposure,现金缓冲仍须保留。
        """
        if self.max_leverage <= 1.0 + MAX_WEIGHT_EPSILON:
            return max(0.0, 1.0 - self.min_cash_buffer)
        return self.max_leverage


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """目标权重向量。

    ``max_leverage`` 是配置上限,``gross_exposure`` / ``net_exposure`` 是
    实际敞口,二者不可混用。默认 ``long_only=True`` 且 ``max_leverage=1``。
    """

    weights: dict[str, float]
    as_of: date
    strategy_id: str
    cash_buffer: float = 0.0
    max_leverage: float = 1.0
    long_only: bool = True
    contract_version: str = PORTFOLIO_CONTRACT_VERSION
    factor_snapshot_id: str | None = None
    covariance_version: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.cash_buffer) or not 0 <= self.cash_buffer <= 1:
            raise ValueError("cash_buffer 必须落在 [0, 1]")
        if not math.isfinite(self.max_leverage) or self.max_leverage < 1:
            raise ValueError("max_leverage 不能小于 1")
        for code, w in self.weights.items():
            if not code:
                raise ValueError("权重 key 不能为空字符串")
            if not math.isfinite(w):
                raise ValueError(f"权重必须为有限数: {code}={w}")
            if self.long_only and w < 0:
                raise ValueError(f"权重不能为负: {code}={w}")
        if self.gross_exposure > self.max_leverage + MAX_WEIGHT_EPSILON:
            raise ValueError(
                f"实际 gross exposure {self.gross_exposure:.6f} 超过配置的 "
                f"max_leverage={self.max_leverage:.6f}"
            )
        if (
            self.long_only
            and self.max_leverage <= 1 + MAX_WEIGHT_EPSILON
            and self.net_exposure + self.cash_buffer > 1 + MAX_WEIGHT_EPSILON
        ):
            raise ValueError("默认无杠杆组合的净权重与现金之和不能超过 1")

    @property
    def gross_weight(self) -> float:
        """兼容旧调用的实际 gross exposure 别名。"""
        return self.gross_exposure

    @property
    def gross_exposure(self) -> float:
        """实际 gross exposure = sum(abs(weight))。"""
        return sum(abs(weight) for weight in self.weights.values())

    @property
    def net_exposure(self) -> float:
        """实际 net exposure = sum(weight)。"""
        return sum(self.weights.values())

    @property
    def n_assets(self) -> int:
        """持仓标的数(非零权重)。"""
        return sum(1 for w in self.weights.values() if abs(w) > MAX_WEIGHT_EPSILON)

    def weight_of(self, code: str) -> float:
        """安全取权重;未包含的标的返回 0。"""
        return self.weights.get(code, 0.0)


@dataclass(frozen=True, slots=True)
class Sleeve:
    """资产类别分组(sleeve)—— 用于集中度约束和归因。

    sleeve 是资产的逻辑分组,通常对应 ``AssetClass``(EQUITY / FIXED_INCOME /
    CONVERTIBLE / DERIVATIVE)。每个 sleeve 有权重上限,用于控制组合的
    资产类别集中度。
    """

    name: str
    max_weight: float = 0.40

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("sleeve name 不能为空")
        if not (0 < self.max_weight <= 1.0):
            raise ValueError("sleeve max_weight 必须落在 (0, 1]")


@dataclass(frozen=True, slots=True)
class RebalanceTrade:
    """单标的再平衡交易意图。

    ``delta_shares`` 为正=买入、负=卖出;``target_shares`` 为目标持仓。
    所有数量已经离散化为合法手数(整百/整十/整手)。
    """

    symbol: str
    delta_shares: int
    target_shares: int
    current_shares: int
    target_value: float
    current_value: float
    delta_value: float
    requested_target_shares: int | None = None
    unfilled_shares: int = 0
    reject_reason: str | None = None
    estimated_slippage: float = 0.0
    margin_required: float = 0.0

    def __post_init__(self) -> None:
        if self.target_shares < 0:
            raise ValueError("target_shares 不能为负")
        if self.current_shares < 0:
            raise ValueError("current_shares 不能为负")
        if self.unfilled_shares < 0:
            raise ValueError("unfilled_shares 不能为负")
        if self.estimated_slippage < 0 or self.margin_required < 0:
            raise ValueError("滑点和保证金不能为负")

    @property
    def is_buy(self) -> bool:
        return self.delta_shares > 0

    @property
    def is_sell(self) -> bool:
        return self.delta_shares < 0

    @property
    def is_noop(self) -> bool:
        return self.delta_shares == 0


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """组合再平衡计划 —— 组合层输出给执行层的统一订单意图。

    所有 trades 已经过离散手数求解,保证:
    * 无负现金(``cash_after >= 0``)
    * 无超保证金(期货)
    * 无不可成交碎片仓位
    """

    trades: list[RebalanceTrade]
    total_capital: float
    cash_before: float
    cash_after: float
    est_commission: float
    est_tax: float
    total_turnover: float
    as_of: date
    strategy_id: str
    est_slippage: float = 0.0
    margin_required: float = 0.0
    contract_version: str = PORTFOLIO_CONTRACT_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.total_capital <= 0:
            raise ValueError("total_capital 必须为正")
        if self.cash_after < -MAX_WEIGHT_EPSILON:
            raise ValueError(f"cash_after 不能为负: {self.cash_after}")
        if self.total_turnover < 0:
            raise ValueError("total_turnover 不能为负")
        if self.est_commission < 0:
            raise ValueError("est_commission 不能为负")
        if self.est_tax < 0:
            raise ValueError("est_tax 不能为负")
        if self.est_slippage < 0 or self.margin_required < 0:
            raise ValueError("滑点和保证金不能为负")

    @property
    def buy_turnover(self) -> float:
        return sum(t.delta_value for t in self.trades if t.is_buy)

    @property
    def sell_turnover(self) -> float:
        return sum(abs(t.delta_value) for t in self.trades if t.is_sell)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def n_active_trades(self) -> int:
        return sum(1 for t in self.trades if not t.is_noop)


@dataclass(frozen=True, slots=True)
class CapitalTier:
    """资金档位 —— 10万 / 20万 / 50万三档。

    不同资金量下离散手数的精度差异巨大:10 万元买 100 元/股的股票只能买
    10 手=1000 股,离散误差约 1%;而 50 万元可以买 50 手,离散误差降到
    0.2%。回测必须分档验证可行性。
    """

    name: str
    total_capital: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CapitalTier name 不能为空")
        if self.total_capital <= 0:
            raise ValueError("total_capital 必须为正")


@dataclass(frozen=True, slots=True)
class AssetLotInfo:
    """标的交易单位信息(用于离散手数求解)。

    属性:
        code: 标的代码。
        lot_size: 最小交易单位(A股=100, ETF=100/10, 可转债=10, 期货=1)。
        multiplier: 合约乘数(股票/ETF/债券=1, 股指期货=200/300, 国债期货=10000)。
    """

    code: str
    lot_size: int = 100
    multiplier: float = 1.0
    margin_rate: float | None = None
    commission_rate: float | None = None
    commission_min: float | None = None
    stamp_tax_rate: float | None = None
    slippage_bps: float = 0.0
    max_participation: float | None = None
    available_volume: int | None = None
    tradable: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code 不能为空")
        if self.lot_size <= 0:
            raise ValueError("lot_size 必须为正")
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须为正")
        if self.margin_rate is not None and not (0 < self.margin_rate <= 1):
            raise ValueError("margin_rate 必须落在 (0, 1]")
        for name, value in (
            ("commission_rate", self.commission_rate),
            ("commission_min", self.commission_min),
            ("stamp_tax_rate", self.stamp_tax_rate),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} 不能为负")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps 不能为负")
        if self.max_participation is not None and not (0 < self.max_participation <= 1):
            raise ValueError("max_participation 必须落在 (0, 1]")
        if self.available_volume is not None and self.available_volume < 0:
            raise ValueError("available_volume 不能为负")
        if not self.tradable and not self.unavailable_reason:
            raise ValueError("不可交易标的必须提供 unavailable_reason")

    @property
    def unit_factor(self) -> float:
        """单手合约价值 = lot_size * multiplier。"""
        return float(self.lot_size) * self.multiplier


CAPITAL_TIERS: dict[str, CapitalTier] = {
    "100k": CapitalTier("100k", 100_000.0),
    "200k": CapitalTier("200k", 200_000.0),
    "500k": CapitalTier("500k", 500_000.0),
}
"""预定义资金档位:10万 / 20万 / 50万元。

**注意**:这些数字仅用于回测可行性研究,不构成投资建议。实盘交易的
资金量应由用户在 RiskConfig 中显式配置。
"""


__all__ = [
    "CAPITAL_TIERS",
    "MAX_WEIGHT_EPSILON",
    "PORTFOLIO_CONTRACT_VERSION",
    "AssetLotInfo",
    "CapitalTier",
    "CovarianceFailureMode",
    "PortfolioConstraints",
    "RebalancePlan",
    "RebalanceTrade",
    "Signal",
    "Sleeve",
    "TargetWeight",
]
