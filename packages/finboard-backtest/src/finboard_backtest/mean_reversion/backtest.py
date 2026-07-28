"""均值回归策略回测引擎。

核心设计:
1. **下一 Bar 执行**:T 日收盘信号 → T+1 开盘成交,**禁止同 Bar 成交**。
2. **逐 Bar 推进**:每个 Bar 填充昨日挂单 → 更新持仓 → 计算今日信号 → 挂单。
3. **所有约束硬执行**:持仓数 / 持有期 / 冷却 / 换手 / 参与率 不可绕过。
4. **成本跟踪**:佣金 / 印花税 / 滑点 按每笔成交单独归因。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.mean_reversion.config import MeanReversionConfig
from finboard_backtest.mean_reversion.constraints import (
    _CooldownState,
    _OpenPosition,
    check_entry,
    check_max_holding,
)
from finboard_backtest.mean_reversion.regime import (
    classify_regime,
    is_entry_allowed,
)
from finboard_backtest.mean_reversion.signals import (
    _compute_signal_value,
    _signal_direction,
    generate_signals,
)


@dataclass(frozen=True, slots=True)
class Trade:
    """单笔成交记录。"""

    symbol: str
    side: str
    """``"BUY"`` 或 ``"SELL"``。"""
    bar_index: int
    """成交 Bar 的索引。"""
    fill_price: float
    """成交价(已含滑点)。"""
    raw_price: float
    """成交前原始价格。"""
    shares: float
    gross_value: float
    """成交金额(fill_price x shares)。"""
    commission: float
    stamp_tax: float
    slippage_cost: float
    """滑点造成的成本(raw_price 与 fill_price 的差额 x shares)。"""
    total_cost: float
    signal_value: float
    reason: str
    """``"entry"`` / ``"exit_signal"`` / ``"max_holding"``。"""


@dataclass(frozen=True, slots=True)
class MeanReversionResult:
    """均值回归回测完整结果。"""

    config: MeanReversionConfig
    equity_curve: list[float]
    """组合净值时间序列(按 Bar 索引)。"""
    trades: list[Trade]
    position_weights: list[dict[str, float]]
    """每个 Bar 的持仓权重快照。"""
    regime_states: list[dict[str, str]]
    """每个 Bar 的状态分类快照。"""
    bars_processed: int
    skipped_bars: int
    """因数据不足跳过的 Bar 数。"""
    symbols_traded: list[str]
    """实际发生过交易的标的列表。"""
    entry_count: int
    exit_signal_count: int
    exit_max_holding_count: int
    blocked_count: int
    """被风控约束阻止的入场次数。"""

    @property
    def total_return(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        return self.equity_curve[-1] / self.equity_curve[0] - 1.0

    @property
    def total_cost(self) -> float:
        return sum(t.total_cost for t in self.trades)

    @property
    def total_commission(self) -> float:
        return sum(t.commission for t in self.trades)

    @property
    def total_slippage(self) -> float:
        return sum(t.slippage_cost for t in self.trades)

    @property
    def total_stamp_tax(self) -> float:
        return sum(t.stamp_tax for t in self.trades)

    @property
    def turnover_count(self) -> int:
        return len(self.trades)

    @property
    def gross_return(self) -> float:
        """毛收益(含成本的总回报的反面:总回报 + 总成本率)。"""
        if len(self.equity_curve) < 2:
            return 0.0
        gross_final = self.equity_curve[-1] + self.total_cost
        return gross_final / self.equity_curve[0] - 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "bars_processed": self.bars_processed,
            "skipped_bars": self.skipped_bars,
            "total_return": self.total_return,
            "gross_return": self.gross_return,
            "total_cost": self.total_cost,
            "total_commission": self.total_commission,
            "total_slippage": self.total_slippage,
            "total_stamp_tax": self.total_stamp_tax,
            "trade_count": len(self.trades),
            "entry_count": self.entry_count,
            "exit_signal_count": self.exit_signal_count,
            "exit_max_holding_count": self.exit_max_holding_count,
            "blocked_count": self.blocked_count,
            "symbols_traded": self.symbols_traded,
        }


@dataclass(slots=True)
class _PendingOrder:
    """待成交挂单(T 日生成 → T+1 填充)。"""

    symbol: str
    side: str
    target_weight: float
    signal_value: float
    reason: str


class MeanReversionSimulator:
    """均值回归策略回测模拟器。

    使用方式::

        sim = MeanReversionSimulator(config)
        result = sim.run(closes, opens=opens, volumes=volumes)
    """

    def __init__(self, config: MeanReversionConfig) -> None:
        self._config = config

    def run(
        self,
        closes: Mapping[str, Sequence[float]],
        *,
        opens: Mapping[str, Sequence[float]] | None = None,
        volumes: Mapping[str, Sequence[float]] | None = None,
        initial_capital: float = 100_000.0,
    ) -> MeanReversionResult:
        """执行完整回测。

        :param closes: ``{symbol: [close_1, close_2, ...]}``。
        :param opens: 开盘价;``None`` 时用收盘价填充(保守假设)。
        :param volumes: 成交量;``None`` 时跳过参与率检查。
        :param initial_capital: 初始资金。
        :return: 完整回测结果。
        """
        config = self._config
        symbols = sorted(closes.keys())
        if not symbols:
            raise ValueError("closes 不能为空")

        n_bars = max(len(closes[s]) for s in symbols)
        warmup = config.min_data_days

        opens_ = opens if opens is not None else closes
        volumes_ = volumes if volumes is not None else {}

        cash = initial_capital
        positions: list[_OpenPosition] = []
        cooldowns: list[_CooldownState] = []
        pending_orders: list[_PendingOrder] = []

        equity_curve: list[float] = []
        trades: list[Trade] = []
        position_weights: list[dict[str, float]] = []
        regime_states: list[dict[str, str]] = []

        entry_count = 0
        exit_signal_count = 0
        exit_max_holding_count = 0
        blocked_count = 0
        skipped_bars = 0

        for bar_idx in range(n_bars):
            # ── Step 1: Fill pending orders at bar open ──────────────
            daily_bought = 0.0
            daily_sold = 0.0

            for order in pending_orders:
                symbol = order.symbol

                open_prices = opens_.get(symbol, [])
                if bar_idx >= len(open_prices):
                    continue

                raw_price = open_prices[bar_idx]
                if raw_price <= 0:
                    continue

                slippage = config.slippage_bps / 10_000.0
                if order.side == "BUY":
                    fill_price = raw_price * (1.0 + slippage)
                else:
                    fill_price = raw_price * (1.0 - slippage)

                portfolio_value = cash + sum(
                    p.shares * self._close_or_nan(
                        closes.get(p.symbol, []), bar_idx,
                    )
                    for p in positions
                )

                if order.side == "BUY":
                    order_value = min(
                        portfolio_value * order.target_weight,
                        portfolio_value * config.max_weight_per_position,
                    )
                    if order_value <= 0:
                        continue

                    vol_data = volumes_.get(symbol, [])
                    if vol_data and bar_idx < len(vol_data):
                        max_shares_by_vol = vol_data[bar_idx] * config.max_participation
                        max_shares_by_value = order_value / fill_price
                        shares = min(max_shares_by_vol, max_shares_by_value)
                    else:
                        shares = order_value / fill_price

                    if shares <= 0:
                        continue

                    gross = fill_price * shares
                    commission = max(gross * config.commission_rate, config.commission_min)
                    stamp_tax = 0.0
                    slippage_cost = (fill_price - raw_price) * shares
                    total_cost = commission + stamp_tax + slippage_cost

                    if gross + commission > cash:
                        affordable = max(0.0, cash - commission) / fill_price
                        if affordable <= 0:
                            continue
                        shares = affordable
                        gross = fill_price * shares
                        commission = max(gross * config.commission_rate, config.commission_min)
                        slippage_cost = (fill_price - raw_price) * shares
                        total_cost = commission + slippage_cost

                    cash -= gross + commission

                    positions.append(_OpenPosition(
                        symbol=symbol,
                        entry_bar=bar_idx,
                        entry_price=fill_price,
                        shares=shares,
                        weight=order.target_weight,
                    ))

                    trades.append(Trade(
                        symbol=symbol, side="BUY", bar_index=bar_idx,
                        fill_price=fill_price, raw_price=raw_price,
                        shares=shares, gross_value=gross,
                        commission=commission, stamp_tax=0.0,
                        slippage_cost=slippage_cost,
                        total_cost=commission + slippage_cost,
                        signal_value=order.signal_value, reason=order.reason,
                    ))
                    daily_bought += gross
                    entry_count += 1

                elif order.side == "SELL":
                    pos = self._find_position(positions, symbol)
                    if pos is None:
                        continue

                    gross = fill_price * pos.shares
                    commission = max(gross * config.commission_rate, config.commission_min)
                    stamp_tax = gross * config.stamp_tax_rate
                    slippage_cost = (raw_price - fill_price) * pos.shares
                    total_cost = commission + stamp_tax + slippage_cost

                    cash += gross - commission - stamp_tax

                    trades.append(Trade(
                        symbol=symbol, side="SELL", bar_index=bar_idx,
                        fill_price=fill_price, raw_price=raw_price,
                        shares=pos.shares, gross_value=gross,
                        commission=commission, stamp_tax=stamp_tax,
                        slippage_cost=slippage_cost, total_cost=total_cost,
                        signal_value=order.signal_value, reason=order.reason,
                    ))
                    daily_sold += gross

                    if order.reason == "max_holding":
                        exit_max_holding_count += 1
                    else:
                        exit_signal_count += 1

                    positions.remove(pos)
                    cooldowns.append(_CooldownState(
                        symbol=symbol, remaining=config.cooldown_days,
                    ))

            pending_orders = []

            # ── Step 2: Decrement cooldowns ───────────────────────────
            cooldowns = [cd for cd in cooldowns if cd.remaining > 0]
            for cd in cooldowns:
                cd.remaining -= 1

            # ── Step 3: Check if enough data ──────────────────────────
            if bar_idx < warmup:
                equity = cash + sum(
                    p.shares * self._safe_close(closes.get(p.symbol, []), bar_idx)
                    for p in positions
                )
                equity_curve.append(equity)
                position_weights.append(self._position_weights(positions, equity))
                regime_states.append({})
                skipped_bars += 1
                continue

            closes_at_bar = {
                s: list(closes[s][:bar_idx + 1])
                for s in symbols
                if bar_idx < len(closes[s])
            }
            if not closes_at_bar:
                equity = cash
                equity_curve.append(equity)
                position_weights.append({})
                regime_states.append({})
                skipped_bars += 1
                continue

            # ── Step 4: Update holding periods ────────────────────────
            for pos in positions:
                pos.bars_held += 1

            # ── Step 5: Compute regime ────────────────────────────────
            regime = classify_regime(closes_at_bar, config)
            regime_states.append({
                s: r.state.value for s, r in regime.items()
            })

            # ── Step 6: Check exits for open positions ────────────────
            new_orders: list[_PendingOrder] = []

            for pos in list(positions):
                prices = closes_at_bar.get(pos.symbol)
                if prices is None or len(prices) < 2:
                    continue

                signal_value = _compute_signal_value(prices, config)
                direction = _signal_direction(signal_value, config)

                force_exit = check_max_holding(pos, bar_idx, config)

                if direction.exit:
                    new_orders.append(_PendingOrder(
                        symbol=pos.symbol, side="SELL",
                        target_weight=0.0,
                        signal_value=signal_value,
                        reason="exit_signal",
                    ))
                elif force_exit:
                    new_orders.append(_PendingOrder(
                        symbol=pos.symbol, side="SELL",
                        target_weight=0.0,
                        signal_value=signal_value,
                        reason="max_holding",
                    ))

            # ── Step 7: Generate entry signals ────────────────────────
            held_symbols = {p.symbol for p in positions}
            symbols_with_orders = {o.symbol for o in new_orders}

            target_weight = 1.0 / config.max_positions

            portfolio_value = cash + sum(
                p.shares * self._safe_close(closes.get(p.symbol, []), bar_idx)
                for p in positions
            )

            daily_turnover = (daily_bought + daily_sold) / portfolio_value if portfolio_value > 0 else 0.0

            signals = generate_signals(closes_at_bar, config)

            for sig in signals:
                if sig.symbol in held_symbols:
                    continue
                if sig.symbol in symbols_with_orders:
                    continue

                if not sig.should_enter:
                    continue

                reg = regime.get(sig.symbol)
                if reg is None or not is_entry_allowed(reg):
                    blocked_count += 1
                    continue

                check = check_entry(
                    sig.symbol, positions, cooldowns, config,
                    pending_buy_value=portfolio_value * target_weight,
                    portfolio_value=portfolio_value,
                    daily_turnover_so_far=daily_turnover,
                )
                if check.blocked:
                    blocked_count += 1
                    continue

                new_orders.append(_PendingOrder(
                    symbol=sig.symbol, side="BUY",
                    target_weight=target_weight,
                    signal_value=sig.value,
                    reason="entry",
                ))
                held_symbols.add(sig.symbol)
                symbols_with_orders.add(sig.symbol)
                daily_turnover += target_weight

            pending_orders = new_orders

            # ── Step 8: Record equity ─────────────────────────────────
            equity = cash + sum(
                p.shares * self._safe_close(closes.get(p.symbol, []), bar_idx)
                for p in positions
            )
            equity_curve.append(equity)
            position_weights.append(self._position_weights(positions, equity))

        symbols_traded = sorted({t.symbol for t in trades})

        return MeanReversionResult(
            config=config,
            equity_curve=equity_curve,
            trades=trades,
            position_weights=position_weights,
            regime_states=regime_states,
            bars_processed=n_bars - skipped_bars,
            skipped_bars=skipped_bars,
            symbols_traded=symbols_traded,
            entry_count=entry_count,
            exit_signal_count=exit_signal_count,
            exit_max_holding_count=exit_max_holding_count,
            blocked_count=blocked_count,
        )

    @staticmethod
    def _find_position(
        positions: list[_OpenPosition], symbol: str,
    ) -> _OpenPosition | None:
        for p in positions:
            if p.symbol == symbol:
                return p
        return None

    @staticmethod
    def _safe_close(prices: Sequence[float], idx: int) -> float:
        if idx < len(prices):
            return prices[idx]
        if prices:
            return prices[-1]
        return 0.0

    @staticmethod
    def _close_or_nan(prices: Sequence[float], idx: int) -> float:
        if idx < len(prices):
            return prices[idx]
        return 0.0

    @staticmethod
    def _position_weights(
        positions: list[_OpenPosition], equity: float,
    ) -> dict[str, float]:
        if equity <= 0:
            return {}
        return {
            p.symbol: p.shares * p.entry_price / equity
            for p in positions
        }


def run_backtest(
    closes: Mapping[str, Sequence[float]],
    config: MeanReversionConfig,
    *,
    opens: Mapping[str, Sequence[float]] | None = None,
    volumes: Mapping[str, Sequence[float]] | None = None,
    initial_capital: float = 100_000.0,
) -> MeanReversionResult:
    """便捷函数:创建模拟器并运行回测。"""
    sim = MeanReversionSimulator(config)
    return sim.run(
        closes, opens=opens, volumes=volumes,
        initial_capital=initial_capital,
    )
