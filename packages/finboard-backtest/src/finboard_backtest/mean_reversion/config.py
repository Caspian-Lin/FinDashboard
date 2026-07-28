"""高流动性 ETF 短周期均值回归策略 — 版本化配置 schema。

issue #62 的核心目标:建立一个 long-only、严格成本约束的短周期均值回归
研究基线。所有信号族、参数网格和试验预算在运行前预冻结,**禁止事后调参**。

设计原则:
1. **预注册**:信号族和参数网格在实验前固定,``ParameterGrid`` 的
   ``total_candidates`` 是试验预算的硬上限。
2. **下一 Bar 执行**:T 日收盘信号 → T+1 开盘成交,禁止同 Bar 成交。
3. **趋势过滤**:单边下跌中禁止入场,避免摊平式无限加仓。
4. **硬约束**:持仓数 / 持有期 / 冷却期 / 换手 / 参与率不可绕过。
5. **成本敏感**:毛收益与净收益分开报告,成本 x2 后失效即标记 rejected。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MEAN_REVERSION_VERSION = "v1"
"""均值回归策略框架版本号;参数语义变更时递增。"""


class SignalFamily(StrEnum):
    """预注册的信号族 —— 不得在看结果后切换。"""

    Z_SCORE = "z_score"
    """滚动 z-score:z = (close - SMA) / std。"""

    BOLLINGER = "bollinger"
    """Bollinger %B:%B = (close - lower) / (upper - lower)。"""

    RSI = "rsi"
    """RSI 震荡指标。"""

    REVERSAL = "reversal"
    """N 日反转:N 日收益率为负时入场。"""


class RegimeMethod(StrEnum):
    """市场状态过滤方法。"""

    SMA = "sma"
    """SMA 趋势过滤:close > SMA(N) 为多头状态。"""

    NONE = "none"
    """不做状态过滤(仅供对比测试,默认不使用)。"""


@dataclass(frozen=True, slots=True)
class ParameterGrid:
    """预冻结的参数网格。

    所有候选参数组合的笛卡尔积必须在实验开始前声明;
    ``total_candidates`` 是 ``trial_budget`` 的硬上限。
    """

    families: tuple[SignalFamily, ...]
    lookbacks: tuple[int, ...]
    entry_thresholds: tuple[float, ...]
    exit_thresholds: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.families:
            raise ValueError("families 不能为空")
        if not self.lookbacks:
            raise ValueError("lookbacks 不能为空")
        if not self.entry_thresholds:
            raise ValueError("entry_thresholds 不能为空")
        if not self.exit_thresholds:
            raise ValueError("exit_thresholds 不能为空")
        for lb in self.lookbacks:
            if lb < 2:
                raise ValueError("lookback 必须 >= 2")
        for t in self.entry_thresholds:
            if t <= 0:
                raise ValueError("entry_threshold 必须 > 0")
        for t in self.exit_thresholds:
            if t <= 0:
                raise ValueError("exit_threshold 必须 > 0")
        if len(set(self.families)) != len(self.families):
            raise ValueError("families 不允许重复")
        if len(set(self.lookbacks)) != len(self.lookbacks):
            raise ValueError("lookbacks 不允许重复")

    @property
    def total_candidates(self) -> int:
        """参数网格的候选组合总数。"""
        return (
            len(self.families)
            * len(self.lookbacks)
            * len(self.entry_thresholds)
            * len(self.exit_thresholds)
        )

    def candidates(self) -> list[dict[str, object]]:
        """枚举所有参数组合。"""
        result: list[dict[str, object]] = []
        for fam in self.families:
            for lb in self.lookbacks:
                for ent in self.entry_thresholds:
                    for ex in self.exit_thresholds:
                        result.append({
                            "family": fam.value,
                            "lookback": lb,
                            "entry_threshold": ent,
                            "exit_threshold": ex,
                        })
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "families": [f.value for f in self.families],
            "lookbacks": list(self.lookbacks),
            "entry_thresholds": list(self.entry_thresholds),
            "exit_thresholds": list(self.exit_thresholds),
            "total_candidates": self.total_candidates,
        }


@dataclass(frozen=True, slots=True)
class MeanReversionConfig:
    """均值回归策略完整配置。

    所有参数在实验前声明并冻结;``as_dict()`` 输出可归档到
    :class:`~finboard_backtest.validation.contracts.VersionStamp`。
    """

    # ── 信号 ──────────────────────────────────────────────────────────
    family: SignalFamily = SignalFamily.Z_SCORE
    lookback: int = 20
    """信号计算窗口(交易日)。"""
    entry_threshold: float = 2.0
    """入场阈值(z-score / %B / RSI / 反转 的含义不同,见 signals.py)。"""
    exit_threshold: float = 0.5
    """出场阈值。"""
    bollinger_num_std: float = 2.0
    """Bollinger 带宽标准差倍数(仅 BOLLINGER 家族使用)。"""
    rsi_period: int = 14
    """RSI 计算周期(仅 RSI 家族使用)。"""

    # ── 趋势 / 波动状态过滤 ───────────────────────────────────────────
    regime_method: RegimeMethod = RegimeMethod.SMA
    regime_sma_window: int = 200
    """趋势过滤 SMA 窗口。"""
    regime_vol_lookback: int = 60
    """波动率状态回溯窗口。"""
    regime_vol_max_ratio: float = 3.0
    """当前波动率 / 历史均值波动的最大允许比值,超出则标记 HIGH_VOL。"""

    # ── 仓位 / 持有期 / 冷却 ──────────────────────────────────────────
    max_positions: int = 5
    """最大同时持仓数。"""
    max_weight_per_position: float = 0.20
    """单标的最大权重。"""
    max_holding_days: int = 10
    """最长持有天数,到期强制平仓。"""
    cooldown_days: int = 5
    """平仓后的冷却期(交易日),冷却期内不允许重新入场。"""
    no_averaging_down: bool = True
    """禁止对浮亏头寸加仓(摊平)。"""

    # ── 换手 / 参与率 ─────────────────────────────────────────────────
    max_daily_turnover: float = 0.30
    """单日换手率上限(买入+卖出总额 / 组合净值)。"""
    max_participation: float = 0.10
    """单标的单日最大成交量占当日成交量的比例。"""

    # ── 费用 ──────────────────────────────────────────────────────────
    commission_rate: float = 0.0003
    """佣金费率(万三)。"""
    commission_min: float = 5.0
    """单笔最低佣金(元)。"""
    stamp_tax_rate: float = 0.0005
    """印花税率(卖出,千分之五)。"""
    slippage_bps: float = 5.0
    """滑点(bps)。"""

    # ── 版本 ──────────────────────────────────────────────────────────
    version: str = MEAN_REVERSION_VERSION

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback 必须 >= 2")
        if self.entry_threshold <= 0:
            raise ValueError("entry_threshold 必须 > 0")
        if self.exit_threshold <= 0:
            raise ValueError("exit_threshold 必须 > 0")
        if self.bollinger_num_std <= 0:
            raise ValueError("bollinger_num_std 必须 > 0")
        if self.rsi_period < 2:
            raise ValueError("rsi_period 必须 >= 2")
        if self.regime_sma_window < 5:
            raise ValueError("regime_sma_window 必须 >= 5")
        if self.regime_vol_lookback < 5:
            raise ValueError("regime_vol_lookback 必须 >= 5")
        if self.regime_vol_max_ratio <= 0:
            raise ValueError("regime_vol_max_ratio 必须 > 0")
        if self.max_positions < 1:
            raise ValueError("max_positions 必须 >= 1")
        if not (0 < self.max_weight_per_position <= 1.0):
            raise ValueError("max_weight_per_position 必须落在 (0, 1]")
        if self.max_holding_days < 1:
            raise ValueError("max_holding_days 必须 >= 1")
        if self.cooldown_days < 0:
            raise ValueError("cooldown_days 必须 >= 0")
        if not (0 < self.max_daily_turnover <= 1.0):
            raise ValueError("max_daily_turnover 必须落在 (0, 1]")
        if not (0 < self.max_participation <= 1.0):
            raise ValueError("max_participation 必须落在 (0, 1]")
        if self.commission_rate < 0:
            raise ValueError("commission_rate 不能为负")
        if self.commission_min < 0:
            raise ValueError("commission_min 不能为负")
        if self.stamp_tax_rate < 0:
            raise ValueError("stamp_tax_rate 不能为负")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps 不能为负")

    @property
    def min_data_days(self) -> int:
        """策略预热所需的最少历史数据天数。"""
        signal_days = self.lookback
        if self.family is SignalFamily.RSI:
            signal_days = max(signal_days, self.rsi_period)
        regime_days = (
            self.regime_sma_window
            if self.regime_method is RegimeMethod.SMA
            else 0
        )
        vol_days = self.regime_vol_lookback
        return max(signal_days, regime_days, vol_days) + 1

    def cost_multiplier_config(self, multiplier: float) -> MeanReversionConfig:
        """返回成本放大 ``multiplier`` 倍后的配置副本(用于成本敏感性测试)。

        使用 ``dataclasses.replace`` 保持不可变性。
        """
        from dataclasses import replace

        return replace(
            self,
            commission_rate=self.commission_rate * multiplier,
            stamp_tax_rate=self.stamp_tax_rate * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
        )

    def as_dict(self) -> dict[str, object]:
        """序列化为可归档字典。"""
        return {
            "version": self.version,
            "family": self.family.value,
            "lookback": self.lookback,
            "entry_threshold": self.entry_threshold,
            "exit_threshold": self.exit_threshold,
            "bollinger_num_std": self.bollinger_num_std,
            "rsi_period": self.rsi_period,
            "regime_method": self.regime_method.value,
            "regime_sma_window": self.regime_sma_window,
            "regime_vol_lookback": self.regime_vol_lookback,
            "regime_vol_max_ratio": self.regime_vol_max_ratio,
            "max_positions": self.max_positions,
            "max_weight_per_position": self.max_weight_per_position,
            "max_holding_days": self.max_holding_days,
            "cooldown_days": self.cooldown_days,
            "no_averaging_down": self.no_averaging_down,
            "max_daily_turnover": self.max_daily_turnover,
            "max_participation": self.max_participation,
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax_rate": self.stamp_tax_rate,
            "slippage_bps": self.slippage_bps,
        }
