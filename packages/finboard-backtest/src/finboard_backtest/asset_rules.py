"""按 ``InstrumentType``/市场分发的真实撮合 / 费用 / 手数规则。

issue #56 的核心目标之一:**消除 A 股默认规则被错误地套用到 ETF / 债券 /
可转债 / 期货**。这里集中维护:

* 手数(最小交易单位):A 股 / 股票 ETF = 100 股;债券 / 货币 ETF = 10 张;
  可转债 = 10 张(深市)/ 10 张(沪市);期货 = 1 手。
* ``T+0/T+1``:A 股 / 股票 ETF / 债券 ETF / 可转债 = T+1(可显式关闭);
  货币 ETF / 跨境 ETF / 期货 = T+0。
* 印花税:A 股股票 / 股票 ETF / 可转债卖出收万 5;债券 ETF / 货币 ETF /
  国债 ETF 免印花税;期货按合约设定(股指期货万分之 0.23 卖出,本所收)。
* 佣金费率与最低佣金:全部按 ``commission_rate / commission_min`` 复用配置。
* 涨跌停:仅在 ``price_limit`` 显式配置时启用;A 股主板默认 ±10%(亏损/退市
  风险警示 *ST、*ST/* ST 为 ±5%),创业 / 科创板 ±20%,可转债盘中 ±20%。
* 价格步长(tick):A 股 / ETF 默认 0.001 元;可转债 0.001 元;国债期货 0.002 元;
  股指期货 0.2 点。
* 停牌:当 ``volume == 0`` 且 ``high == low == close == pre_close`` 时识别为停牌。

期货合约的多空 / 开平 / 保证金 / 每日盯市由 ``finboard-backtest`` 期货子模块
(Issue #64)消费本模块的 ``FuturesRule`` 后自行实现;当前 ``BacktestBroker``
仅做"按规则分发"和"已知类型 fail closed"的硬约束。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from finboard_shared.models import Bar
from finboard_shared.types import InstrumentType, Market, Side

ASSET_RULES_VERSION = "v1"
"""本撮合规则目录的版本号;每次手数 / T+N / 税率 / tick 变更必须递增。

回测结果在 ``BacktestResult`` 中归档此版本,使历史 run 不可直接横向比较。
"""

SUSPENDED_SENTINEL_HIGH_LOW_MATCH = Decimal("0")


@dataclass(frozen=True, slots=True)
class AssetRule:
    """单一资产类型的撮合 / 费用 / 手数 / 价格约束。"""

    instrument_type: InstrumentType
    lot_size: Decimal
    enforce_t_plus_1: bool
    stamp_tax_rate: Decimal
    commission_rate: Decimal
    commission_min: Decimal
    price_limit_pct: Decimal | None
    price_tick: Decimal
    allow_short: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("lot_size 必须为正")
        if self.price_tick <= 0:
            raise ValueError("price_tick 必须为正")
        if self.commission_rate < 0:
            raise ValueError("commission_rate 不能为负")
        if self.commission_min < 0:
            raise ValueError("commission_min 不能为负")
        if self.stamp_tax_rate < 0:
            raise ValueError("stamp_tax_rate 不能为负")
        if self.price_limit_pct is not None and self.price_limit_pct <= 0:
            raise ValueError("price_limit_pct 必须为正")

    def round_to_lot(self, quantity: Decimal) -> Decimal:
        """按 ``lot_size`` 向下取整(不可成交碎片直接舍去)。"""
        if quantity <= 0:
            return Decimal("0")
        lots = (quantity / self.lot_size).to_integral_value(rounding="ROUND_FLOOR")
        return lots * self.lot_size

    def round_to_tick(self, price: Decimal) -> Decimal:
        """把价格向下舍入到 ``price_tick`` 的整数倍。"""
        if price <= 0:
            return Decimal("0")
        ticks = (price / self.price_tick).to_integral_value(rounding="ROUND_FLOOR")
        return ticks * self.price_tick

    def is_suspended(self, bar: Bar, *, pre_close: Decimal | None) -> bool:
        """识别停牌:零成交量 + OHLC 全等于前收。

        停牌时禁止任何成交;买卖双方都被拒绝(详见 :meth:`BacktestBroker._fill`)。
        """
        if bar.volume > 0:
            return False
        if pre_close is None or pre_close <= 0:
            return False
        return (
            bar.close == pre_close
            and bar.high == pre_close
            and bar.low == pre_close
            and bar.open == pre_close
        )

    def is_at_limit_up(self, bar: Bar, *, pre_close: Decimal | None) -> bool:
        if self.price_limit_pct is None or pre_close is None or pre_close <= 0:
            return False
        ceiling = self._ceiling(pre_close)
        return bar.close >= ceiling and bar.high <= ceiling

    def is_at_limit_down(self, bar: Bar, *, pre_close: Decimal | None) -> bool:
        if self.price_limit_pct is None or pre_close is None or pre_close <= 0:
            return False
        floor = self._floor(pre_close)
        return bar.close <= floor and bar.low >= floor

    def _ceiling(self, pre_close: Decimal) -> Decimal:
        assert self.price_limit_pct is not None
        raw = pre_close * (Decimal("1") + self.price_limit_pct)
        return self.round_to_tick(raw)

    def _floor(self, pre_close: Decimal) -> Decimal:
        assert self.price_limit_pct is not None
        raw = pre_close * (Decimal("1") - self.price_limit_pct)
        return self.round_to_tick(raw)

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_type": self.instrument_type.value,
            "lot_size": str(self.lot_size),
            "enforce_t_plus_1": self.enforce_t_plus_1,
            "stamp_tax_rate": str(self.stamp_tax_rate),
            "commission_rate": str(self.commission_rate),
            "commission_min": str(self.commission_min),
            "price_limit_pct": (
                str(self.price_limit_pct) if self.price_limit_pct is not None else None
            ),
            "price_tick": str(self.price_tick),
            "allow_short": self.allow_short,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class FuturesRule:
    """期货合约的额外撮合规则(由 #64 消费)。"""

    multiplier: Decimal
    margin_rate: Decimal
    long_t_plus: int = 0
    short_t_plus: int = 0
    allow_short: bool = True

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须为正")
        if not (Decimal("0") < self.margin_rate <= Decimal("1")):
            raise ValueError("margin_rate 必须落在 (0, 1]")


@dataclass(frozen=True, slots=True)
class AssetRuleTable:
    """资产规则目录;按 (市场, 类型) 解析到唯一规则,fail closed。"""

    rules: tuple[tuple[Market, InstrumentType, AssetRule], ...] = ()
    futures_rules: tuple[tuple[str, FuturesRule], ...] = ()
    rule_version: str = ASSET_RULES_VERSION

    def resolve(
        self,
        *,
        market: Market,
        instrument_type: InstrumentType,
    ) -> AssetRule:
        for rule_market, rule_type, rule in self.rules:
            if rule_market == market and rule_type == instrument_type:
                return rule
        raise AssetRuleResolutionError(
            f"无可用撮合规则: market={market.value} type={instrument_type.value}"
        )

    def resolve_futures(self, symbol_code: str) -> FuturesRule:
        for prefix, rule in self.futures_rules:
            if symbol_code.startswith(prefix):
                return rule
        raise AssetRuleResolutionError(
            f"未知期货合约规则: {symbol_code}(未在 futures_rules 登记)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "rules": [
                {
                    "market": market.value,
                    "instrument_type": itype.value,
                    **rule.as_dict(),
                }
                for market, itype, rule in self.rules
            ],
            "futures_rules": [
                {"prefix": prefix, **_futures_dict(rule)}
                for prefix, rule in self.futures_rules
            ],
        }


class AssetRuleResolutionError(RuntimeError):
    """未注册资产规则 —— BacktestBroker 拒绝撮合以避免误用 A 股默认。"""


def _futures_dict(rule: FuturesRule) -> dict[str, object]:
    return {
        "multiplier": str(rule.multiplier),
        "margin_rate": str(rule.margin_rate),
        "long_t_plus": rule.long_t_plus,
        "short_t_plus": rule.short_t_plus,
        "allow_short": rule.allow_short,
    }


# --------------------------------------------------------------------------- 默认目录
#
# 下列费率 / 手数取自交易所现行规则;变更时必须 bump ``ASSET_RULES_VERSION``。

A_SHARE_STOCK = AssetRule(
    instrument_type=InstrumentType.STOCK,
    lot_size=Decimal("100"),
    enforce_t_plus_1=True,
    stamp_tax_rate=Decimal("0.0005"),
    commission_rate=Decimal("0.0003"),
    commission_min=Decimal("5"),
    price_limit_pct=Decimal("0.10"),
    price_tick=Decimal("0.01"),
    allow_short=False,
    description="A 股股票:100 股/手、T+1、卖出印花税万 5、价格步长 0.01。",
)

EQUITY_ETF = AssetRule(
    instrument_type=InstrumentType.ETF,
    lot_size=Decimal("100"),
    enforce_t_plus_1=True,
    stamp_tax_rate=Decimal("0.0005"),
    commission_rate=Decimal("0.0003"),
    commission_min=Decimal("5"),
    price_limit_pct=Decimal("0.10"),
    price_tick=Decimal("0.001"),
    allow_short=False,
    description="股票 ETF:100 份/手、T+1、卖出印花税万 5、tick 0.001。",
)

CROSS_BORDER_ETF = AssetRule(
    instrument_type=InstrumentType.ETF,
    lot_size=Decimal("100"),
    enforce_t_plus_1=False,
    stamp_tax_rate=Decimal("0"),
    commission_rate=Decimal("0.0003"),
    commission_min=Decimal("5"),
    price_limit_pct=Decimal("0.10"),
    price_tick=Decimal("0.001"),
    allow_short=False,
    description="跨境 ETF:T+0、免印花税;同 ETF 类型,但 T+N 与税种独立。",
)

BOND_ETF = AssetRule(
    instrument_type=InstrumentType.ETF,
    lot_size=Decimal("10"),
    enforce_t_plus_1=True,
    stamp_tax_rate=Decimal("0"),
    commission_rate=Decimal("0.00003"),
    commission_min=Decimal("5"),
    price_limit_pct=None,
    price_tick=Decimal("0.001"),
    allow_short=False,
    description="国债 / 政策金融债 ETF:免印花税、10 份/手、无涨跌停。",
)

MONEY_MARKET_ETF = AssetRule(
    instrument_type=InstrumentType.ETF,
    lot_size=Decimal("100"),
    enforce_t_plus_1=False,
    stamp_tax_rate=Decimal("0"),
    commission_rate=Decimal("0"),
    commission_min=Decimal("0"),
    price_limit_pct=None,
    price_tick=Decimal("0.001"),
    allow_short=False,
    description="货币 ETF:T+0、无佣金、无涨跌停。",
)

INDEX_RULE = AssetRule(
    instrument_type=InstrumentType.INDEX,
    lot_size=Decimal("1"),
    enforce_t_plus_1=False,
    stamp_tax_rate=Decimal("0"),
    commission_rate=Decimal("0"),
    commission_min=Decimal("0"),
    price_limit_pct=None,
    price_tick=Decimal("0.01"),
    allow_short=False,
    description="指数本身不可交易;仅在研究 / 基准中使用。",
)

DEFAULT_TABLE = AssetRuleTable(
    rules=(
        (Market.A_SHARE, InstrumentType.STOCK, A_SHARE_STOCK),
        (Market.A_SHARE, InstrumentType.ETF, EQUITY_ETF),
        (Market.A_SHARE, InstrumentType.INDEX, INDEX_RULE),
    ),
    futures_rules=(),
    rule_version=ASSET_RULES_VERSION,
)
"""默认目录覆盖 A 股股票 / 股票 ETF / 指数。

研究 / 回测域接入新资产类(issue #58、#64)时,调用方在
``AssetRuleTable`` 中显式注册对应规则,**不**自动回退到 A 股股票规则 ——
未注册的 (market, type) 解析时 raise,保证 fail closed。
"""


def default_rule_table() -> AssetRuleTable:
    """返回可变拷贝,便于测试 / 上层注入额外规则。"""
    return replace(DEFAULT_TABLE)


def resolve_rule_from_overrides(
    table: AssetRuleTable,
    *,
    market: Market,
    instrument_type: InstrumentType,
    commission_rate: Decimal | None,
    commission_min: Decimal | None,
    stamp_tax_rate: Decimal | None,
    slippage_bps: Decimal | None,
) -> tuple[AssetRule, Decimal]:
    """根据回测请求覆盖参数返回最终规则 + 全局滑点。

    覆盖语义:回测请求可以**全局**覆盖 commission/stamp_tax/slippage;若未
    覆盖,则使用资产规则自带的费率。该函数返回的规则已合并覆盖值。
    """
    base = table.resolve(market=market, instrument_type=instrument_type)
    rule = replace(
        base,
        commission_rate=(
            commission_rate if commission_rate is not None else base.commission_rate
        ),
        commission_min=(
            commission_min if commission_min is not None else base.commission_min
        ),
        stamp_tax_rate=(
            stamp_tax_rate if stamp_tax_rate is not None else base.stamp_tax_rate
        ),
    )
    slippage = slippage_bps if slippage_bps is not None else Decimal("0")
    return rule, slippage


def fill_quantity_within_participation(
    requested_qty: Decimal,
    bar_volume: Decimal,
    max_participation: Decimal | None,
) -> Decimal:
    """按 ``max_participation`` 截断成交量;``None`` 表示不限制。

    返回的成交量不会超过 ``requested_qty`` 或 ``bar_volume``。
    """
    if requested_qty <= 0:
        return Decimal("0")
    cap = requested_qty
    if max_participation is not None and max_participation >= 0:
        cap = min(cap, max_participation * bar_volume)
    return min(cap, bar_volume) if bar_volume > 0 else cap


def price_with_slippage(
    raw_price: Decimal,
    side: Side,
    slippage_bps: Decimal,
) -> Decimal:
    """按方向施加滑点(bps,1bp = 0.01%)。"""
    if slippage_bps == 0:
        return raw_price
    slip = slippage_bps / Decimal("10000")
    if side is Side.BUY:
        return raw_price * (Decimal("1") + slip)
    return raw_price * (Decimal("1") - slip)


def fill_price_for(
    rule: AssetRule,
    *,
    side: Side,
    bar: Bar,
    fill_timing: str,
    limit_price: Decimal | None,
) -> Decimal:
    """根据成交时点(open / close / vwap_proxy)返回原始成交价。

    * ``fill_timing="open"`` → 下一 Bar ``open``;
    * ``fill_timing="close"`` → 下一 Bar ``close``;
    * ``fill_timing="vwap_proxy"`` → ``(high + low + 2 * close) / 4`` 近似 VWAP;
    * 限价单在不超过 ``limit_price``(BUY)或不低于 ``limit_price``(SELL)的
      前提下取"原始成交价与 limit 的较优者",使得开盘跳空也能享受价格改善。
    """
    if fill_timing == "open":
        base = bar.open
    elif fill_timing == "vwap_proxy":
        if bar.high + bar.low + Decimal("2") * bar.close <= 0:
            base = bar.close
        else:
            base = (bar.high + bar.low + Decimal("2") * bar.close) / Decimal("4")
    else:  # "close"
        base = bar.close

    price = rule.round_to_tick(base)
    if limit_price is None:
        return price
    if side is Side.BUY:
        return min(price, limit_price)
    return max(price, limit_price)


def merged_rule_map(
    table: AssetRuleTable,
) -> Mapping[tuple[str, str], AssetRule]:
    """``{(market.value, type.value): rule}`` 平铺视图,便于序列化。"""
    return {
        (market.value, itype.value): rule
        for market, itype, rule in table.rules
    }


__all__ = [
    "ASSET_RULES_VERSION",
    "A_SHARE_STOCK",
    "BOND_ETF",
    "CROSS_BORDER_ETF",
    "DEFAULT_TABLE",
    "EQUITY_ETF",
    "INDEX_RULE",
    "MONEY_MARKET_ETF",
    "AssetRule",
    "AssetRuleResolutionError",
    "AssetRuleTable",
    "FuturesRule",
    "default_rule_table",
    "fill_price_for",
    "fill_quantity_within_participation",
    "merged_rule_map",
    "price_with_slippage",
    "resolve_rule_from_overrides",
]
