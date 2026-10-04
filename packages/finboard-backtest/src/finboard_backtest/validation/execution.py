"""传统 engine 的真实压力覆盖;未知参数拒绝,绝不静默丢弃 (#503)。"""

from decimal import Decimal

from finboard_backtest.config import FeeOverrides


def registry_stress_overrides(raw: dict[str, object]) -> FeeOverrides:
    if not raw:
        return FeeOverrides()
    if set(raw) - {"cost_multiplier", "slippage_bps", "execution_delay_bars"}:
        raise ValueError("unsupported_config_override")
    if raw.get("execution_delay_bars", 1) != 1 or isinstance(raw.get("execution_delay_bars"), bool):
        raise ValueError("unsupported_execution_delay")
    multiplier = Decimal(str(raw.get("cost_multiplier", 1)))
    slippage = Decimal(str(raw.get("slippage_bps", 0)))
    if not multiplier.is_finite() or not 0 < multiplier <= 10:
        raise ValueError("invalid_cost_multiplier")
    if not slippage.is_finite() or not 0 <= slippage <= 1000:
        raise ValueError("invalid_slippage")
    return FeeOverrides(
        commission_rate=Decimal("0.0003") * multiplier,
        commission_min=Decimal("1") * multiplier,
        stamp_tax_rate=Decimal("0.0005") * multiplier,
        slippage_bps=slippage,
    )
