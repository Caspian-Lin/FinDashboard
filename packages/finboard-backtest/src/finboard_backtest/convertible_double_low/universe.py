"""可转债 PIT(point-in-time)候选池过滤(issue #63)。

红线:**不能使用今天仍存续的债券回填历史决策**。
``is_tradable_on`` 严格按上市/退市/余额/成交额/价格状态过滤,
``as_of`` 当日不可知的字段一律不参与判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finboard_backtest.convertible_double_low.config import ConvertibleDoubleLowConfig
from finboard_shared.instruments import ConvertibleMetadata, Instrument
from finboard_shared.types import ListingStatus


@dataclass(frozen=True, slots=True)
class ConvertibleSnapshot:
    """单只可转债在某日的时点化快照(全字段来自 T 日收盘后可知数据)。"""

    instrument: Instrument
    metadata: ConvertibleMetadata
    as_of: date
    close: Decimal
    volume: Decimal
    amount: Decimal
    conversion_premium: Decimal
    conversion_price: Decimal
    conversion_value: Decimal
    ytm: Decimal | None
    days_to_maturity: int | None
    remaining_size: Decimal
    avg_amount_20d: Decimal

    @property
    def code(self) -> str:
        return self.instrument.code

    @property
    def issuer_code(self) -> str:
        return self.metadata.underlying_stock_code

    @property
    def double_low_value(self) -> Decimal:
        """经典双低 = 价格 + 溢价 * 100。"""
        return self.close + self.conversion_premium * Decimal("100")


def is_tradable_on(
    snapshot: ConvertibleSnapshot,
    config: ConvertibleDoubleLowConfig,
    *,
    as_of: date,
) -> bool:
    """判断某转债在 ``as_of`` 当日是否可进入候选池。

    判断维度(全部 PIT 安全):
    * 上市天数 >= ``universe_min_listing_days``
    * 剩余天数 >= ``universe_min_remaining_days``
    * 20 日均成交额 >= ``universe_min_amount_20d``
    * 剩余规模 >= ``universe_min_remaining_size``
    * 价格区间 [price_min, price_max]
    * 溢价 <= ``premium_max``
    * 上市状态为 ACTIVE 或 UNKNOWN
    """
    instr = snapshot.instrument

    if instr.list_date is not None:
        listing_days = (as_of - instr.list_date).days
        if listing_days < config.universe_min_listing_days:
            return False

    if (
        snapshot.days_to_maturity is not None
        and snapshot.days_to_maturity < config.universe_min_remaining_days
    ):
        return False

    if snapshot.avg_amount_20d < config.universe_min_amount_20d:
        return False

    if snapshot.remaining_size < config.universe_min_remaining_size:
        return False

    if snapshot.close < config.price_min or snapshot.close > config.price_max:
        return False

    if snapshot.conversion_premium > config.premium_max:
        return False

    if instr.status is ListingStatus.DELISTED:
        return False

    return instr.was_listed_at(as_of)


def filter_universe(
    snapshots: list[ConvertibleSnapshot],
    config: ConvertibleDoubleLowConfig,
    *,
    as_of: date,
) -> list[ConvertibleSnapshot]:
    """从全量快照中筛出 ``as_of`` 当日可交易的候选池。"""
    return [
        s for s in snapshots if is_tradable_on(s, config, as_of=as_of)
    ]


__all__ = [
    "ConvertibleSnapshot",
    "filter_universe",
    "is_tradable_on",
]
