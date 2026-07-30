"""模拟撮合可用资产规则白名单。未知规则一律失败关闭。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finboard_backtest.asset_rules import (
    A_SHARE_STOCK,
    BOND_ETF,
    CONVERTIBLE_BOND,
    CROSS_BORDER_ETF,
    DEFAULT_TABLE,
    EQUITY_ETF,
    MONEY_MARKET_ETF,
    AssetRule,
    AssetRuleResolutionError,
)
from finboard_shared.types import InstrumentType, Market


@dataclass(frozen=True, slots=True)
class SimulationAssetRule:
    key: str
    market: Market
    instrument_type: InstrumentType
    rule: AssetRule
    multiplier: Decimal = Decimal("1")
    margin_rate: Decimal = Decimal("0")

    @property
    def is_futures(self) -> bool:
        return self.instrument_type is InstrumentType.FUTURES

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "market": self.market.value,
            "instrument_type": self.instrument_type.value,
            "multiplier": str(self.multiplier),
            "margin_rate": str(self.margin_rate),
            "rule": self.rule.as_dict(),
        }


_CASH_RULES: dict[str, SimulationAssetRule] = {
    "a_share_stock": SimulationAssetRule(
        "a_share_stock", Market.A_SHARE, InstrumentType.STOCK, A_SHARE_STOCK
    ),
    "equity_etf": SimulationAssetRule("equity_etf", Market.A_SHARE, InstrumentType.ETF, EQUITY_ETF),
    "cross_border_etf": SimulationAssetRule(
        "cross_border_etf",
        Market.A_SHARE,
        InstrumentType.ETF,
        CROSS_BORDER_ETF,
    ),
    "bond_etf": SimulationAssetRule("bond_etf", Market.A_SHARE, InstrumentType.ETF, BOND_ETF),
    "money_market_etf": SimulationAssetRule(
        "money_market_etf",
        Market.A_SHARE,
        InstrumentType.ETF,
        MONEY_MARKET_ETF,
    ),
    "convertible": SimulationAssetRule(
        "convertible",
        Market.A_SHARE,
        InstrumentType.CONVERTIBLE,
        CONVERTIBLE_BOND,
    ),
}


def resolve_simulation_rule(
    *,
    key: str,
    market: Market,
    instrument_type: InstrumentType,
    symbol: str,
) -> SimulationAssetRule:
    if key == "futures":
        if market is not Market.FUTURE or instrument_type is not InstrumentType.FUTURES:
            raise AssetRuleResolutionError("futures 规则只能用于 future/futures")
        future = DEFAULT_TABLE.resolve_futures(symbol)
        rule = AssetRule(
            instrument_type=InstrumentType.FUTURES,
            lot_size=Decimal("1"),
            enforce_t_plus_1=False,
            stamp_tax_rate=Decimal("0"),
            commission_rate=Decimal("0.000023"),
            commission_min=Decimal("0"),
            price_limit_pct=None,
            price_tick=(
                Decimal("0.002") if symbol.startswith(("T", "TF", "TS")) else Decimal("0.2")
            ),
            allow_short=True,
            description="期货模拟:整数手、双向、保证金和每日盯市。",
        )
        return SimulationAssetRule(
            key=key,
            market=market,
            instrument_type=instrument_type,
            rule=rule,
            multiplier=future.multiplier,
            margin_rate=future.margin_rate,
        )
    result = _CASH_RULES.get(key)
    if result is None:
        raise AssetRuleResolutionError(f"未注册的模拟资产规则: {key}")
    if result.market is not market or result.instrument_type is not instrument_type:
        raise AssetRuleResolutionError(
            f"规则 {key} 与 {market.value}/{instrument_type.value} 不一致"
        )
    return result


def simulation_rule_keys() -> tuple[str, ...]:
    return (*sorted(_CASH_RULES), "futures")


__all__ = [
    "SimulationAssetRule",
    "resolve_simulation_rule",
    "simulation_rule_keys",
]
