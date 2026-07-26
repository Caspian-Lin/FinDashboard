"""回测配置。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """回测引擎配置。

    费用参数均按 A 股现行标准:

    * 佣金:万 3(0.03%),最低 ¥5/笔
    * 印花税:万 5(0.05%),仅卖出收取(2023.08 降税后)
    * 过户费:万 0.1(0.001%),买卖均收(金额较小,可忽略)
    """

    symbols: list[str]
    start: date
    end: date
    initial_capital: Decimal = Decimal("100000")

    # 费用
    commission_rate: Decimal = Decimal("0.0003")    # 万 3
    commission_min: Decimal = Decimal("1")           # ¥1/笔(线上券商)
    stamp_tax_rate: Decimal = Decimal("0.0005")      # 万 5(卖出)
    slippage_bps: Decimal = Decimal("0")             # 滑点(bps,1bp=0.01%)

    # 交易规则
    lot_size: int = 100              # A 股最小交易单位
    allow_market_order: bool = True
    allow_short: bool = False
    enforce_t_plus_1: bool = True    # T+1:当日买入次日方可卖出

    # 数据
    adjust: str = "qfq"              # 复权方式

    # 策略参数(传入 strategy kwargs)
    strategy_params: dict[str, object] | None = None
