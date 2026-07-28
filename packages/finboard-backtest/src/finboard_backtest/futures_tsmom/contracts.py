"""期货合约规格 —— 乘数 / 保证金 / tick / 涨跌停 / 手续费。

issue #64 的核心:回测引擎需要知道每个合约的乘数和保证金率才能正确
计算名义价值、保证金占用和每日盯市。这些规格是**静态元数据**,
不随时间变化(交易所调规时 bump 版本)。

真实参数取自中金所(CFFEX)现行规则(2024):
- 股指期货(IF/IC/IH):乘数 200-300,保证金 ~12-14%,tick 0.2
- 国债期货(T/TF/TS):面值乘数 10000-20000,保证金 ~0.5-2%,tick 0.005

国债期货 T (10 年):合约乘数 10000 元/点,tick 0.005,手续费 3 元/手。
股指期货 IF (沪深 300):乘数 300 元/点,tick 0.2,手续费率万分之 0.23。

**免责声明**:这些参数仅用于离线研究回测。实盘交易前必须从交易所
获取最新规则。不同期货公司可能加收保证金。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FuturesMarket(StrEnum):
    """期货品种大类(用于单市场风险约束)。"""

    EQUITY_INDEX = "equity_index"
    """股指期货(IF / IC / IH / IM)。"""
    TREASURY_BOND = "treasury_bond"
    """国债期货(T / TF / TS)。"""


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """单一期货合约系列的静态规格。

    所有字段都是合约级别的不变属性;品种的乘数 / 保证金率在合约存续期
    内不变(交易所调规除外)。
    """

    symbol: str
    """品种代码,如 ``"IF"`` / ``"T"`` / ``"TF"``。"""
    name: str
    """中文名称。"""
    market: FuturesMarket
    """品种大类。"""
    multiplier: float
    """合约乘数(每点价值)。IF=300, IC=200, T=10000。"""
    margin_rate: float
    """保证金比例(小数)。IF ~ 0.12, T ~ 0.02。"""
    tick_size: float
    """最小价格变动。IF=0.2, T=0.005。"""
    commission_rate: float
    """手续费率(按成交额)。股指期货万分之 0.23。"""
    commission_per_lot: float
    """每手固定手续费。国债期货 3 元/手。"""
    price_limit_pct: float
    """涨跌停比例(小数)。股指 ±10%, 国债 ±2%。"""
    lot_size: int = 1
    """最小交易手数(期货 = 1)。"""

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须 > 0")
        if not (0 < self.margin_rate <= 1.0):
            raise ValueError("margin_rate 必须落在 (0, 1]")
        if self.tick_size <= 0:
            raise ValueError("tick_size 必须 > 0")
        if self.commission_rate < 0:
            raise ValueError("commission_rate 不能为负")
        if self.commission_per_lot < 0:
            raise ValueError("commission_per_lot 不能为负")
        if self.price_limit_pct <= 0:
            raise ValueError("price_limit_pct 必须 > 0")
        if self.lot_size < 1:
            raise ValueError("lot_size 必须 >= 1")

    def notional_value(self, price: float, lots: int) -> float:
        """名义价值 = 价格 x 乘数 x 手数。"""
        return price * self.multiplier * lots

    def margin_required(self, price: float, lots: int) -> float:
        """保证金 = 名义价值 x 保证金率。"""
        return self.notional_value(price, lots) * self.margin_rate

    def commission(self, price: float, lots: int) -> float:
        """手续费 = max(成交额 x 费率, 每手固定 x 手数)。

        股指期货按成交额收;国债期货按手数收。
        """
        by_rate = self.notional_value(price, lots) * self.commission_rate
        by_lot = self.commission_per_lot * lots
        return max(by_rate, by_lot)

    def round_to_tick(self, price: float) -> float:
        """把价格舍入到 tick_size 整数倍。"""
        if price <= 0:
            return 0.0
        ticks = round(price / self.tick_size)
        return ticks * self.tick_size

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market.value,
            "multiplier": self.multiplier,
            "margin_rate": self.margin_rate,
            "tick_size": self.tick_size,
            "commission_rate": self.commission_rate,
            "commission_per_lot": self.commission_per_lot,
            "price_limit_pct": self.price_limit_pct,
            "lot_size": self.lot_size,
        }


# --------------------------------------------------------------------------- 默认合约规格
#
# 数据来源:中金所(CFFEX)官网合约规则页面。
# 最后核实:2024-06;交易所调规时必须 bump 版本号。

IF_SPEC = ContractSpec(
    symbol="IF",
    name="沪深 300 股指期货",
    market=FuturesMarket.EQUITY_INDEX,
    multiplier=300.0,
    margin_rate=0.12,
    tick_size=0.2,
    commission_rate=0.000023,
    commission_per_lot=0.0,
    price_limit_pct=0.10,
)

IC_SPEC = ContractSpec(
    symbol="IC",
    name="中证 500 股指期货",
    market=FuturesMarket.EQUITY_INDEX,
    multiplier=200.0,
    margin_rate=0.14,
    tick_size=0.2,
    commission_rate=0.000023,
    commission_per_lot=0.0,
    price_limit_pct=0.10,
)

IH_SPEC = ContractSpec(
    symbol="IH",
    name="上证 50 股指期货",
    market=FuturesMarket.EQUITY_INDEX,
    multiplier=300.0,
    margin_rate=0.12,
    tick_size=0.2,
    commission_rate=0.000023,
    commission_per_lot=0.0,
    price_limit_pct=0.10,
)

T_SPEC = ContractSpec(
    symbol="T",
    name="10 年期国债期货",
    market=FuturesMarket.TREASURY_BOND,
    multiplier=10000.0,
    margin_rate=0.02,
    tick_size=0.005,
    commission_rate=0.0,
    commission_per_lot=3.0,
    price_limit_pct=0.02,
)

TF_SPEC = ContractSpec(
    symbol="TF",
    name="5 年期国债期货",
    market=FuturesMarket.TREASURY_BOND,
    multiplier=10000.0,
    margin_rate=0.012,
    tick_size=0.005,
    commission_rate=0.0,
    commission_per_lot=3.0,
    price_limit_pct=0.012,
)

TS_SPEC = ContractSpec(
    symbol="TS",
    name="2 年期国债期货",
    market=FuturesMarket.TREASURY_BOND,
    multiplier=20000.0,
    margin_rate=0.005,
    tick_size=0.005,
    commission_rate=0.0,
    commission_per_lot=3.0,
    price_limit_pct=0.005,
)

DEFAULT_CONTRACT_SPECS: dict[str, ContractSpec] = {
    spec.symbol: spec for spec in [IF_SPEC, IC_SPEC, IH_SPEC, T_SPEC, TF_SPEC, TS_SPEC]
}
"""默认合约规格目录 —— 覆盖 3 个股指期货 + 3 个国债期货。"""


def resolve_contract_spec(symbol: str) -> ContractSpec:
    """按品种代码解析合约规格,fail closed。"""
    spec = DEFAULT_CONTRACT_SPECS.get(symbol)
    if spec is None:
        raise KeyError(f"未知期货合约: {symbol}(未在 DEFAULT_CONTRACT_SPECS 登记)")
    return spec


__all__ = [
    "DEFAULT_CONTRACT_SPECS",
    "IC_SPEC",
    "IF_SPEC",
    "IH_SPEC",
    "TF_SPEC",
    "TS_SPEC",
    "T_SPEC",
    "ContractSpec",
    "FuturesMarket",
    "resolve_contract_spec",
]
