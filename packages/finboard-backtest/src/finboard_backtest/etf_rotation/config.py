"""ETF 轮动策略版本化配置 schema。

issue #61 的核心目标:建立一个 long-only ETF tactical allocation 基线,
所有规则(绝对趋势方法 / 相对动量参数 / 调仓频率 / Top-N / 避险资产 /
配权规则)通过 schema 暴露并版本化,**禁止事后调参**。

设计原则:
1. **预声明**:所有参数在实验开始前固定,`as_dict()` 可归档到 VersionStamp。
2. **安全**:long-only、max_leverage=1.0、cash_buffer 不可压缩。
3. **可复现**:相同 config + 相同数据 → 相同信号 → 相同权重。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

ETF_ROTATION_VERSION = "v1"
"""ETF 轮动策略框架版本号;参数语义变更时递增。"""

TRADING_DAYS_PER_MONTH = 21
"""一个月近似交易日数,用于把月数转换为交易日窗口。"""


class AbsoluteTrendMethod(StrEnum):
    """绝对趋势判定方法 —— 预声明变体,不得在看结果后切换。"""

    SMA = "sma"
    """价格 > N 日简单移动平均。"""

    LOOKBACK_RETURN = "lookback_return"
    """过去 N 月收益为正。"""


class AllocationMethod(StrEnum):
    """资金分配方法。"""

    EQUAL_WEIGHT = "equal_weight"
    """等权分配。"""

    INVERSE_VOLATILITY = "inverse_volatility"
    """逆波动率分配(波动率越低权重越高)。"""


class RebalanceFrequency(StrEnum):
    """调仓频率。"""

    MONTHLY = "monthly"
    """月度调仓(约 21 个交易日)。"""

    BIWEEKLY = "biweekly"
    """双周调仓(约 10 个交易日)。"""


@dataclass(frozen=True, slots=True)
class EtfRotationConfig:
    """ETF 轮动策略完整配置。

    所有参数在实验前声明并冻结;``as_dict()`` 输出可归档到
    :class:`~finboard_backtest.validation.contracts.VersionStamp`。
    """

    # ── 绝对趋势 ──────────────────────────────────────────────────────
    trend_method: AbsoluteTrendMethod = AbsoluteTrendMethod.SMA
    sma_window: int = 200
    """SMA 方法使用的交易日窗口(默认 200 日 ≈ 10 个月)。"""
    lookback_months: int = 10
    """LOOKBACK_RETURN 方法使用的回溯月数(默认 10 或 12)。"""

    # ── 相对动量 ──────────────────────────────────────────────────────
    momentum_lookbacks: tuple[int, ...] = (3, 6, 12)
    """相对动量评分的回溯月数(默认 3/6/12)。"""
    momentum_weights: tuple[float, ...] = (1.0 / 3, 1.0 / 3, 1.0 / 3)
    """各回溯期权重,长度必须与 ``momentum_lookbacks`` 一致,总和 ≈ 1.0。"""

    # ── 选股 ──────────────────────────────────────────────────────────
    top_n: int = 5
    """持仓 ETF 数量上限。"""

    # ── 分配 ──────────────────────────────────────────────────────────
    allocation_method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT
    vol_lookback: int = 60
    """逆波动率分配的波动率回溯窗口(交易日)。"""
    max_weight_per_etf: float = 0.30
    """单只 ETF 权重上限。"""
    max_weight_per_asset_class: float = 0.60
    """单资产大类(股票/债券/商品/跨境)权重上限。"""
    cash_buffer: float = 0.05
    """最小现金缓冲比例。"""
    rebalance_threshold: float = 0.10
    """再平衡带:权重偏离不超过此阈值时不调仓。"""

    # ── 避险资产 ──────────────────────────────────────────────────────
    safe_haven_symbol: str = "511010.SH"
    """避险债券 ETF 代码(国债 ETF)。
    国债 ETF 是**风险资产**而非保本现金等价物。"""
    safe_haven_trend_enabled: bool = True
    """避险 ETF 是否也必须通过独立的趋势检验。"""

    # ── 调仓 ──────────────────────────────────────────────────────────
    rebalance_frequency: RebalanceFrequency = RebalanceFrequency.MONTHLY

    # ── 版本 ──────────────────────────────────────────────────────────
    version: str = ETF_ROTATION_VERSION

    def __post_init__(self) -> None:
        if self.sma_window < 5:
            raise ValueError("sma_window 必须 >= 5")
        if self.lookback_months < 1:
            raise ValueError("lookback_months 必须 >= 1")
        if len(self.momentum_lookbacks) == 0:
            raise ValueError("momentum_lookbacks 不能为空")
        if len(self.momentum_weights) != len(self.momentum_lookbacks):
            raise ValueError(
                "momentum_weights 长度必须与 momentum_lookbacks 一致"
            )
        weight_sum = sum(self.momentum_weights)
        if abs(weight_sum - 1.0) > 0.02:
            raise ValueError(
                f"momentum_weights 总和必须 ≈ 1.0 (当前 {weight_sum:.4f})"
            )
        for w in self.momentum_weights:
            if w < 0:
                raise ValueError("momentum_weights 不能包含负值")
        if self.top_n < 1:
            raise ValueError("top_n 必须 >= 1")
        if self.vol_lookback < 5:
            raise ValueError("vol_lookback 必须 >= 5")
        if not (0 < self.max_weight_per_etf <= 1.0):
            raise ValueError("max_weight_per_etf 必须落在 (0, 1]")
        if not (0 < self.max_weight_per_asset_class <= 1.0):
            raise ValueError("max_weight_per_asset_class 必须落在 (0, 1]")
        if self.max_weight_per_etf > self.max_weight_per_asset_class:
            raise ValueError(
                "max_weight_per_etf 不能超过 max_weight_per_asset_class"
            )
        if not (0 <= self.cash_buffer < 1.0):
            raise ValueError("cash_buffer 必须落在 [0, 1)")
        if not (0 <= self.rebalance_threshold <= 0.5):
            raise ValueError("rebalance_threshold 必须落在 [0, 0.5]")
        if not self.safe_haven_symbol:
            raise ValueError("safe_haven_symbol 不能为空")

    @property
    def rebalance_interval_days(self) -> int:
        """调仓间隔(交易日)。"""
        if self.rebalance_frequency is RebalanceFrequency.MONTHLY:
            return TRADING_DAYS_PER_MONTH
        return 10

    @property
    def min_data_days(self) -> int:
        """策略所需的最少历史数据天数(用于预热)。"""
        trend_days = (
            self.sma_window
            if self.trend_method is AbsoluteTrendMethod.SMA
            else self.lookback_months * TRADING_DAYS_PER_MONTH
        )
        momentum_days = max(self.momentum_lookbacks) * TRADING_DAYS_PER_MONTH
        vol_days = self.vol_lookback if self.allocation_method is AllocationMethod.INVERSE_VOLATILITY else 0
        return max(trend_days, momentum_days, vol_days)

    def as_dict(self) -> dict[str, object]:
        """序列化为可归档字典。"""
        return {
            "version": self.version,
            "trend_method": self.trend_method.value,
            "sma_window": self.sma_window,
            "lookback_months": self.lookback_months,
            "momentum_lookbacks": list(self.momentum_lookbacks),
            "momentum_weights": list(self.momentum_weights),
            "top_n": self.top_n,
            "allocation_method": self.allocation_method.value,
            "vol_lookback": self.vol_lookback,
            "max_weight_per_etf": self.max_weight_per_etf,
            "max_weight_per_asset_class": self.max_weight_per_asset_class,
            "cash_buffer": self.cash_buffer,
            "rebalance_threshold": self.rebalance_threshold,
            "safe_haven_symbol": self.safe_haven_symbol,
            "safe_haven_trend_enabled": self.safe_haven_trend_enabled,
            "rebalance_frequency": self.rebalance_frequency.value,
        }
