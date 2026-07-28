"""股指 / 国债期货时间序列动量(TSMOM)— 版本化配置 schema。

issue #64 的核心目标:建立一个支持多空、保证金、每日盯市和换月的研究级
期货趋势基线。所有信号族、参数和风险预算在运行前预冻结,**禁止事后调参**。

设计原则:
1. **预注册**:信号 lookback 族在实验前固定,``ParameterGrid`` 的
   ``total_candidates`` 是试验预算的硬上限。
2. **未拼接原合约**:信号和成交使用原始合约(非连续序列),换月处产生
   可审计的展期事件;连续序列仅用于研究展示。
3. **波动率缩放**:仓位按标的的历史波动率反比缩放,使每个品种贡献
   等量风险;受名义杠杆 / 保证金 / 集中度硬约束。
4. **T+1 执行**:T 日收盘信号 → T+1 开盘成交,禁止同 Bar 成交。
5. **资金可行性**:按整数手计算,不可行档位直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

FUTURES_TSMOM_VERSION = "v1"
"""期货 TSMOM 策略框架版本号;参数语义变更时递增。"""


class TsmomSignalFamily(StrEnum):
    """预注册的动量信号族。"""

    SINGLE_LOOKBACK = "single_lookback"
    """单一 lookback 时间序列动量:sign(past_return)。"""

    MULTIPLE_LOOKBACK = "multiple_lookback"
    """多 lookback 平均:对 1/3/6/12 月信号取平均。"""


@dataclass(frozen=True, slots=True)
class TsmomParameterGrid:
    """预冻结的参数网格。"""

    lookbacks: tuple[tuple[int, ...], ...]
    """每个候选的 lookback 元组;单 lookback 如 (63,),多 lookback 如 (21, 63, 126, 252)。"""
    vol_targets: tuple[float, ...]
    """年化波动率目标。"""
    max_leverages: tuple[float, ...]
    """最大名义杠杆。"""

    def __post_init__(self) -> None:
        if not self.lookbacks:
            raise ValueError("lookbacks 不能为空")
        for lb_tuple in self.lookbacks:
            if not lb_tuple:
                raise ValueError("lookback 元组不能为空")
            for lb in lb_tuple:
                if lb < 5:
                    raise ValueError("lookback 必须 >= 5")
        if not self.vol_targets:
            raise ValueError("vol_targets 不能为空")
        for vt in self.vol_targets:
            if vt <= 0:
                raise ValueError("vol_target 必须 > 0")
        if not self.max_leverages:
            raise ValueError("max_leverages 不能为空")
        for ml in self.max_leverages:
            if ml <= 0:
                raise ValueError("max_leverage 必须 > 0")

    @property
    def total_candidates(self) -> int:
        return len(self.lookbacks) * len(self.vol_targets) * len(self.max_leverages)

    def candidates(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for lb in self.lookbacks:
            for vt in self.vol_targets:
                for ml in self.max_leverages:
                    result.append({
                        "lookbacks": list(lb),
                        "vol_target": vt,
                        "max_leverage": ml,
                    })
        return result


def _default_lookbacks() -> tuple[int, ...]:
    return (21, 63, 126, 252)


@dataclass(frozen=True, slots=True)
class FuturesTsmomConfig:
    """期货 TSMOM 策略完整配置。

    所有参数在实验前声明并冻结;``as_dict()`` 输出可归档到
    :class:`~finboard_backtest.validation.contracts.VersionStamp`。
    """

    # ── 信号 ──────────────────────────────────────────────────────────
    family: TsmomSignalFamily = TsmomSignalFamily.MULTIPLE_LOOKBACK
    lookbacks: tuple[int, ...] = field(default_factory=_default_lookbacks)
    """动量信号回溯窗口(交易日)。默认 1/3/6/12 月 ≈ 21/63/126/252。"""

    # ── 波动率缩放 ─────────────────────────────────────────────────────
    vol_target: float = 0.10
    """年化波动率目标(如 10%)。"""
    vol_lookback: int = 63
    """波动率估计回溯窗口(3 月)。"""
    vol_floor: float = 0.05
    """波动率下限:低于此值时用此值缩放,防止过度杠杆。"""

    # ── 风险预算 ───────────────────────────────────────────────────────
    max_leverage: float = 2.0
    """最大名义杠杆(总名义 / 账户权益)。"""
    max_margin_usage: float = 0.80
    """最大保证金占用 / 账户权益。"""
    max_single_market_weight: float = 0.40
    """单市场(股指 vs 国债)最大名义 / 权益。"""
    max_single_contract_weight: float = 0.30
    """单合约最大名义 / 权益。"""

    # ── 换月 ───────────────────────────────────────────────────────────
    roll_days_before_expiry: int = 5
    """最后交易日前 N 个交易日移仓。"""

    # ── 费用 ──────────────────────────────────────────────────────────
    commission_rate: float = 0.000023
    """手续费率(股指期货万分之 0.23,双边)。"""
    commission_per_lot: float = 0.0
    """每手固定手续费(国债期货 3 元/手)。"""
    slippage_bps: float = 2.0
    """滑点(bps)。"""
    margin_interest_rate: float = 0.03
    """保证金资金成本假设(年化)。"""

    # ── 执行 ──────────────────────────────────────────────────────────
    rebalance_threshold: float = 0.05
    """目标权重与实际权重偏差超过此值时才调仓。"""
    max_participation: float = 0.10
    """单合约单日最大成交量占当日成交量的比例。"""

    # ── 版本 ──────────────────────────────────────────────────────────
    version: str = FUTURES_TSMOM_VERSION

    def __post_init__(self) -> None:
        if not self.lookbacks:
            raise ValueError("lookbacks 不能为空")
        for lb in self.lookbacks:
            if lb < 5:
                raise ValueError(f"lookback 必须 >= 5, got {lb}")
        if len(set(self.lookbacks)) != len(self.lookbacks):
            raise ValueError("lookbacks 不允许重复")
        if self.vol_target <= 0:
            raise ValueError("vol_target 必须 > 0")
        if self.vol_lookback < 5:
            raise ValueError("vol_lookback 必须 >= 5")
        if self.vol_floor <= 0:
            raise ValueError("vol_floor 必须 > 0")
        if self.max_leverage <= 0:
            raise ValueError("max_leverage 必须 > 0")
        if not (0 < self.max_margin_usage <= 1.0):
            raise ValueError("max_margin_usage 必须落在 (0, 1]")
        if not (0 < self.max_single_market_weight <= 1.0):
            raise ValueError("max_single_market_weight 必须落在 (0, 1]")
        if not (0 < self.max_single_contract_weight <= 1.0):
            raise ValueError("max_single_contract_weight 必须落在 (0, 1]")
        if self.roll_days_before_expiry < 1:
            raise ValueError("roll_days_before_expiry 必须 >= 1")
        if self.commission_rate < 0:
            raise ValueError("commission_rate 不能为负")
        if self.commission_per_lot < 0:
            raise ValueError("commission_per_lot 不能为负")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps 不能为负")
        if self.margin_interest_rate < 0:
            raise ValueError("margin_interest_rate 不能为负")
        if not (0 < self.rebalance_threshold <= 1.0):
            raise ValueError("rebalance_threshold 必须落在 (0, 1]")
        if not (0 < self.max_participation <= 1.0):
            raise ValueError("max_participation 必须落在 (0, 1]")

    @property
    def min_data_days(self) -> int:
        """策略预热所需的最少历史数据天数。"""
        return max(self.lookbacks) + self.vol_lookback

    def cost_multiplier_config(self, multiplier: float) -> FuturesTsmomConfig:
        """返回成本放大 ``multiplier`` 倍后的配置副本。"""
        return replace(
            self,
            commission_rate=self.commission_rate * multiplier,
            commission_per_lot=self.commission_per_lot * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
        )

    def margin_multiplier_config(self, multiplier: float) -> FuturesTsmomConfig:
        """返回保证金放大 ``multiplier`` 倍后的配置副本(压力测试)。"""
        return replace(
            self,
            max_margin_usage=self.max_margin_usage / multiplier,
            vol_target=self.vol_target / multiplier,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "family": self.family.value,
            "lookbacks": list(self.lookbacks),
            "vol_target": self.vol_target,
            "vol_lookback": self.vol_lookback,
            "vol_floor": self.vol_floor,
            "max_leverage": self.max_leverage,
            "max_margin_usage": self.max_margin_usage,
            "max_single_market_weight": self.max_single_market_weight,
            "max_single_contract_weight": self.max_single_contract_weight,
            "roll_days_before_expiry": self.roll_days_before_expiry,
            "commission_rate": self.commission_rate,
            "commission_per_lot": self.commission_per_lot,
            "slippage_bps": self.slippage_bps,
            "margin_interest_rate": self.margin_interest_rate,
            "rebalance_threshold": self.rebalance_threshold,
            "max_participation": self.max_participation,
        }


__all__ = [
    "FUTURES_TSMOM_VERSION",
    "FuturesTsmomConfig",
    "TsmomParameterGrid",
    "TsmomSignalFamily",
]
