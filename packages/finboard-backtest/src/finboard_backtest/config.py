"""回测配置。

issue #56 引入研究级成交语义:

* ``MatchingModel`` 标识当前使用的撮合模型版本(归档到 ``BacktestResult``);
* ``FillTiming`` 决定信号 T 日收盘 → 订单在哪一根 Bar / 哪个价格点成交;
* ``max_participation`` 限制单 Bar 成交量,避免吃掉整根 Bar 流动性;
* ``asset_rule_overrides`` 让回测请求可以全局覆盖 commission / 印花税 / 滑点,
  资产类型化规则仍由 ``AssetRuleTable`` 提供;
* ``BenchmarkConfig`` 允许显式选择基准(单标的 / 等权 / 自定义权重),
  不再默认只取请求中的第一个标的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finboard_backtest.asset_rules import (
    ASSET_RULES_VERSION,
    AssetRuleTable,
    default_rule_table,
)
from finboard_data.factors import FactorSelectionConfig

MATCHING_MODEL_VERSION = "v2"
"""本撮合模型的版本号;从 ``v1``(同 Bar 收盘成交 + A 股统一规则)升级到
``v2``(next-bar + 资产规则化)。任何对撮合时点 / 资产规则 / 费用假设的
变更都必须 bump 此版本,使历史回测结果不可横向比较。
"""


class FillTiming(StrEnum):
    """订单的成交价取自下一可交易 Bar 的哪个价格点。"""

    NEXT_BAR_OPEN = "next_bar_open"
    NEXT_BAR_CLOSE = "next_bar_close"
    NEXT_BAR_VWAP_PROXY = "next_bar_vwap_proxy"


@dataclass(frozen=True, slots=True)
class MatchingModel:
    """撮合模型版本与全部假设。

    归档到 ``BacktestResult`` 后,任何字段变化都会让历史 run 失去可比性。
    """

    matching_model_version: str = MATCHING_MODEL_VERSION
    asset_rules_version: str = ASSET_RULES_VERSION
    fill_timing: FillTiming = FillTiming.NEXT_BAR_OPEN
    next_bar_only: bool = True
    max_participation: Decimal | None = None
    allow_partial_fill: bool = True
    honour_gaps: bool = True
    enforce_suspension: bool = True
    enforce_price_limit: bool = True
    enforce_lot_rounding: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "matching_model_version": self.matching_model_version,
            "asset_rules_version": self.asset_rules_version,
            "fill_timing": self.fill_timing.value,
            "next_bar_only": self.next_bar_only,
            "max_participation": (
                str(self.max_participation)
                if self.max_participation is not None
                else None
            ),
            "allow_partial_fill": self.allow_partial_fill,
            "honour_gaps": self.honour_gaps,
            "enforce_suspension": self.enforce_suspension,
            "enforce_price_limit": self.enforce_price_limit,
            "enforce_lot_rounding": self.enforce_lot_rounding,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """基准选择;不再默认只取请求中的第一个标的。"""

    symbol: str | None = None
    """显式基准标的代码;``None`` 表示使用等权候选池基准。"""

    equal_weight_universe: bool = True
    """无显式基准时,等权再平衡候选池为基准(避免多标的默认取首标的)。"""

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "equal_weight_universe": self.equal_weight_universe,
        }


@dataclass(frozen=True, slots=True)
class FeeOverrides:
    """回测请求可以全局覆盖的费用 / 滑点参数;``None`` 表示沿用资产规则默认。"""

    commission_rate: Decimal | None = None
    commission_min: Decimal | None = None
    stamp_tax_rate: Decimal | None = None
    slippage_bps: Decimal | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "commission_rate": (
                str(self.commission_rate)
                if self.commission_rate is not None
                else None
            ),
            "commission_min": (
                str(self.commission_min)
                if self.commission_min is not None
                else None
            ),
            "stamp_tax_rate": (
                str(self.stamp_tax_rate)
                if self.stamp_tax_rate is not None
                else None
            ),
            "slippage_bps": (
                str(self.slippage_bps) if self.slippage_bps is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """回测引擎配置(研究级成交语义)。

    费用参数兼容旧调用方:若 ``fee_overrides`` 与顶层佣金 / 印花税字段同时
    提供了不同值,以 ``fee_overrides`` 为准(显式覆盖);否则继续读取顶层
    字段以便向后兼容 ``#40`` / ``#41`` / ``#43`` 时期的 API。
    """

    symbols: list[str]
    start: date
    end: date
    initial_capital: Decimal = Decimal("100000")

    # 兼容旧 API 的顶层费用字段 —— 仅在 fee_overrides 未覆盖时生效
    commission_rate: Decimal = Decimal("0.0003")
    commission_min: Decimal = Decimal("1")
    stamp_tax_rate: Decimal = Decimal("0.0005")
    slippage_bps: Decimal = Decimal("0")

    # 资产类型化规则(默认目录覆盖 A 股股票/ETF/指数)
    asset_rules: AssetRuleTable = field(default_factory=default_rule_table)

    # 研究级撮合模型
    matching_model: MatchingModel = field(default_factory=MatchingModel)
    fee_overrides: FeeOverrides = field(default_factory=FeeOverrides)

    # 基准选择
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)

    # 交易规则(资产规则不覆盖时使用)
    allow_short: bool = False
    allow_market_order: bool = True

    # 数据
    adjust: str = "qfq"

    # 策略参数(传入 strategy kwargs)
    strategy_params: dict[str, object] | None = None

    # 因子选股(默认关闭,保持旧回测行为)
    selection: FactorSelectionConfig = field(default_factory=FactorSelectionConfig)

    @property
    def enforce_t_plus_1(self) -> bool:
        """兼容旧调用:默认 True;实际 T+N 由资产规则决定。"""
        return True

    @property
    def lot_size(self) -> int:
        """兼容旧调用:返回 A 股股票规则的 lot_size(整数)。"""
        from finboard_backtest.asset_rules import A_SHARE_STOCK

        return int(A_SHARE_STOCK.lot_size)

    def resolved_fees(self) -> FeeOverrides:
        """合并顶层费用字段与显式覆盖,返回最终生效的 :class:`FeeOverrides`。"""
        return FeeOverrides(
            commission_rate=(
                self.fee_overrides.commission_rate
                if self.fee_overrides.commission_rate is not None
                else self.commission_rate
            ),
            commission_min=(
                self.fee_overrides.commission_min
                if self.fee_overrides.commission_min is not None
                else self.commission_min
            ),
            stamp_tax_rate=(
                self.fee_overrides.stamp_tax_rate
                if self.fee_overrides.stamp_tax_rate is not None
                else self.stamp_tax_rate
            ),
            slippage_bps=(
                self.fee_overrides.slippage_bps
                if self.fee_overrides.slippage_bps is not None
                else self.slippage_bps
            ),
        )
