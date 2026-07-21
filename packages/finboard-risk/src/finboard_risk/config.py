"""风控配置。

默认值与 ``phase1_doc.md`` §6.2 / ``.env.example`` 中的 ``FINBOARD_RISK_*`` 一致。
生产部署前必须重新校准,默认值偏严格 —— 宁可慢,不可错。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_order_value: Decimal = Decimal("10000")
    max_symbol_position_value: Decimal = Decimal("30000")
    max_daily_buy_value: Decimal = Decimal("50000")
    max_active_orders: int = 10
    max_orders_per_minute: int = 5
    allow_short: bool = False
    allow_market_order: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str]) -> RiskConfig:
        """从环境变量(已去除 ``FINBOARD_RISK_`` 前缀的字典)构造。"""

        def _dec(key: str, default: Decimal) -> Decimal:
            value = env.get(key)
            return Decimal(value) if value else default

        def _int(key: str, default: int) -> int:
            value = env.get(key)
            return int(value) if value else default

        def _bool(key: str, default: bool) -> bool:
            value = env.get(key)
            if value is None:
                return default
            return value.lower() in {"1", "true", "yes", "on"}

        return cls(
            max_order_value=_dec("MAX_ORDER_VALUE", Decimal("10000")),
            max_symbol_position_value=_dec(
                "MAX_SYMBOL_POSITION_VALUE", Decimal("30000")
            ),
            max_daily_buy_value=_dec("MAX_DAILY_BUY_VALUE", Decimal("50000")),
            max_active_orders=_int("MAX_ACTIVE_ORDERS", 10),
            max_orders_per_minute=_int("MAX_ORDERS_PER_MINUTE", 5),
            allow_short=_bool("ALLOW_SHORT", False),
            allow_market_order=_bool("ALLOW_MARKET_ORDER", False),
        )
