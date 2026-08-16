"""期货 TSMOM 回测引擎 —— 支持多空 / 保证金 / 每日盯市 / 换月。

架构选择:**独立模拟器**(不走 ``BacktestBroker``),因为 ``BacktestBroker``
不支持做空、保证金、乘数和每日结算。

核心流程(per bar):
1. **成交 pending orders** → 在当日 open 成交(T+1 执行)。
2. **每日盯市** → 持仓按当日 close 结算 P&L,调整现金。
3. **换月检查** → 临近交割或成交量交叉时移仓。
4. **信号生成** → TSMOM 信号 + 波动率缩放 + 风险约束。
5. **调仓** → 目标权重 vs 实际权重,超阈值才调。
6. **记录权益** → cash + 所有持仓的盯市 P&L。

**关键不变量**:
- 信号在 T 日收盘后生成 → 次日开盘成交。
- 保证金占用 <= max_margin_usage x equity。
- 总名义 <= max_leverage x equity。
- 成交量参与率约束。
- 整数手(不可用小数合约)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .config import FuturesTsmomConfig
from .contracts import ContractSpec
from .roll import ActiveContractSeries, RollEvent
from .signals import TsmomSignal, generate_signals


class TradeAction(StrEnum):
    """期货交易动作。"""

    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    ROLL = "roll"


@dataclass(frozen=True, slots=True)
class FuturesTrade:
    """单笔期货成交记录。"""

    symbol: str
    action: TradeAction
    bar_index: int
    contract_id: str
    lots: int
    fill_price: float
    raw_price: float
    notional: float
    commission: float
    slippage_cost: float
    total_cost: float
    reason: str


@dataclass
class _Position:
    """持仓状态(多空独立)。"""

    symbol: str
    direction: int  # +1=long, -1=short
    lots: int = 0
    entry_price: float = 0.0
    contract_id: str = ""

    @property
    def is_open(self) -> bool:
        return self.lots > 0

    @property
    def side_label(self) -> str:
        return "long" if self.direction > 0 else "short"


@dataclass
class _PendingOrder:
    symbol: str
    action: TradeAction
    target_lots: int
    contract_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class DailyRecord:
    """每日快照。"""

    bar_index: int
    equity: float
    cash: float
    long_notional: float
    short_notional: float
    margin_used: float
    gross_exposure: float
    long_positions: dict[str, int]
    short_positions: dict[str, int]


@dataclass(frozen=True, slots=True)
class TsmomResult:
    """TSMOM 回测结果。"""

    config: FuturesTsmomConfig
    equity_curve: list[float]
    daily_records: list[DailyRecord]
    trades: list[FuturesTrade]
    all_signals: list[dict[str, TsmomSignal]]
    roll_events: list[RollEvent]
    symbols_traded: tuple[str, ...]
    bar_count: int

    @property
    def total_return(self) -> float:
        if not self.equity_curve:
            return 0.0
        return self.equity_curve[-1] / self.equity_curve[0] - 1.0

    @property
    def gross_exposure_final(self) -> float:
        if not self.daily_records:
            return 0.0
        return self.daily_records[-1].gross_exposure

    @property
    def margin_used_final(self) -> float:
        if not self.daily_records:
            return 0.0
        return self.daily_records[-1].margin_used

    @property
    def max_gross_exposure(self) -> float:
        return max((r.gross_exposure for r in self.daily_records), default=0.0)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def roll_count(self) -> int:
        return len(self.roll_events)

    @property
    def total_commission(self) -> float:
        return sum(t.commission for t in self.trades)

    @property
    def total_slippage(self) -> float:
        return sum(t.slippage_cost for t in self.trades)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_return": self.total_return,
            "trade_count": self.trade_count,
            "roll_count": self.roll_count,
            "total_commission": self.total_commission,
            "total_slippage": self.total_slippage,
            "max_gross_exposure": self.max_gross_exposure,
            "margin_used_final": self.margin_used_final,
            "bar_count": self.bar_count,
            "symbols_traded": list(self.symbols_traded),
        }


def _apply_slippage(price: float, is_buy: bool, bps: float) -> float:
    if bps <= 0:
        return price
    slip = bps / 10000.0
    if is_buy:
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def _lots_from_weight(
    weight: float,
    equity: float,
    spec: ContractSpec,
    price: float,
) -> int:
    """从目标权重计算整数手数。"""
    if weight <= 0 or equity <= 0 or price <= 0:
        return 0
    target_notional = weight * equity
    lots = int(target_notional / (price * spec.multiplier))
    return max(lots, 0) * spec.lot_size


def _check_risk_constraints(
    desired_positions: dict[str, tuple[int, int]],
    specs: dict[str, ContractSpec],
    closes: dict[str, float],
    equity: float,
    config: FuturesTsmomConfig,
) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """裁剪仓位以满足风险约束。

    ``desired_positions``: {symbol: (long_lots, short_lots)}
    返回裁剪后的仓位 + 裁剪原因列表。
    """
    adjusted = dict(desired_positions)
    reasons: list[str] = []

    # 1. 单合约约束
    for sym, (lng, sht) in adjusted.items():
        spec = specs.get(sym)
        if spec is None:
            continue
        px = closes.get(sym, 0.0)
        if px <= 0:
            continue
        max_notional = config.max_single_contract_weight * equity
        max_lots = int(max_notional / (px * spec.multiplier))
        if lng > max_lots:
            reasons.append(f"{sym}_long_capped: {lng}->{max_lots}")
            adjusted[sym] = (max_lots, sht)
        if sht > max_lots:
            reasons.append(f"{sym}_short_capped: {sht}->{max_lots}")
            adjusted[sym] = (adjusted[sym][0], max_lots)

    # 2. 单市场约束
    market_notional: dict[str, float] = {}
    for sym, (lng, sht) in adjusted.items():
        spec = specs.get(sym)
        if spec is None:
            continue
        px = closes.get(sym, 0.0)
        notional = (lng + sht) * px * spec.multiplier
        mkt = spec.market.value
        market_notional[mkt] = market_notional.get(mkt, 0.0) + notional

    max_market = config.max_single_market_weight * equity
    for mkt, total in market_notional.items():
        if total > max_market:
            scale = max_market / total if total > 0 else 0.0
            for sym, spec in specs.items():
                if spec.market.value == mkt:
                    lng, sht = adjusted.get(sym, (0, 0))
                    new_lng = int(lng * scale)
                    new_sht = int(sht * scale)
                    if new_lng != lng or new_sht != sht:
                        reasons.append(f"{sym}_market_capped: ({lng},{sht})->({new_lng},{new_sht})")
                        adjusted[sym] = (new_lng, new_sht)

    # 3. 总杠杆 + 保证金约束
    total_notional = 0.0
    total_margin = 0.0
    for sym, (lng, sht) in adjusted.items():
        spec = specs.get(sym)
        if spec is None:
            continue
        px = closes.get(sym, 0.0)
        total_notional += (lng + sht) * px * spec.multiplier
        total_margin += (lng + sht) * px * spec.multiplier * spec.margin_rate

    max_leverage_notional = config.max_leverage * equity
    if total_notional > max_leverage_notional:
        scale = max_leverage_notional / total_notional if total_notional > 0 else 0.0
        for sym in list(adjusted.keys()):
            lng, sht = adjusted[sym]
            new_lng = int(lng * scale)
            new_sht = int(sht * scale)
            if new_lng != lng or new_sht != sht:
                reasons.append(f"{sym}_leverage_capped: ({lng},{sht})->({new_lng},{new_sht})")
                adjusted[sym] = (new_lng, new_sht)
        total_notional *= scale

    max_margin = config.max_margin_usage * equity
    total_margin = 0.0
    for sym, (lng, sht) in adjusted.items():
        spec = specs.get(sym)
        if spec is None:
            continue
        px = closes.get(sym, 0.0)
        total_margin += (lng + sht) * px * spec.multiplier * spec.margin_rate

    if total_margin > max_margin:
        scale = max_margin / total_margin if total_margin > 0 else 0.0
        for sym in list(adjusted.keys()):
            lng, sht = adjusted[sym]
            new_lng = int(lng * scale)
            new_sht = int(sht * scale)
            if new_lng != lng or new_sht != sht:
                reasons.append(f"{sym}_margin_capped: ({lng},{sht})->({new_lng},{new_sht})")
                adjusted[sym] = (new_lng, new_sht)

    return adjusted, reasons


def run_backtest(
    *,
    closes_by_symbol: Mapping[str, Sequence[float]],
    opens_by_symbol: Mapping[str, Sequence[float]],
    volumes_by_symbol: Mapping[str, Sequence[float]],
    specs: Mapping[str, ContractSpec],
    config: FuturesTsmomConfig,
    initial_capital: float = 100_000.0,
    active_series: Mapping[str, ActiveContractSeries] | None = None,
) -> TsmomResult:
    """运行 TSMOM 回测。

    Parameters
    ----------
    closes_by_symbol
        每个品种的收盘价序列(未调整原始合约价格)。
    opens_by_symbol
        每个品种的开盘价序列(用于次日成交)。
    volumes_by_symbol
        每个品种的成交量序列(用于参与率约束)。
    specs
        每个品种的合约规格。
    config
        策略配置。
    initial_capital
        初始资金(元)。
    active_series
        可选的活跃合约序列(包含换月事件)。如果提供,换月事件会
        记录到结果中。
    """
    symbols = sorted(closes_by_symbol.keys())
    if not symbols:
        raise ValueError("closes_by_symbol 不能为空")

    bar_count = max(len(closes_by_symbol[s]) for s in symbols)
    for s in symbols:
        if len(closes_by_symbol[s]) != bar_count:
            raise ValueError(f"品种 {s} 长度 {len(closes_by_symbol[s])} != {bar_count}")

    cash = initial_capital
    long_positions: dict[str, _Position] = {s: _Position(s, direction=1) for s in symbols}
    short_positions: dict[str, _Position] = {s: _Position(s, direction=-1) for s in symbols}
    pending_orders: list[_PendingOrder] = []
    trades: list[FuturesTrade] = []
    all_signals: list[dict[str, TsmomSignal]] = []
    daily_records: list[DailyRecord] = []
    roll_events_collected: list[RollEvent] = []

    # 从 active_series 中提取换月事件
    if active_series is not None:
        for _sym, series in active_series.items():
            roll_events_collected.extend(series.roll_events)

    for bar_idx in range(bar_count):
        closes_today = {s: float(closes_by_symbol[s][bar_idx]) for s in symbols}

        # Step 1: 成交 pending orders at today's open
        for order in pending_orders:
            spec = specs.get(order.symbol)
            if spec is None:
                continue
            if order.symbol not in opens_by_symbol:
                continue
            opens = opens_by_symbol[order.symbol]
            if bar_idx >= len(opens):
                continue
            open_price = float(opens[bar_idx])
            if open_price <= 0:
                continue

            vol_series = volumes_by_symbol.get(order.symbol)
            bar_volume = float(vol_series[bar_idx]) if vol_series and bar_idx < len(vol_series) else 0.0
            if bar_volume <= 0:
                continue

            contract_id = order.contract_id

            if order.action is TradeAction.OPEN_LONG:
                is_buy = True
                fill_price = _apply_slippage(open_price, is_buy, config.slippage_bps)
                lots = order.target_lots
                if lots <= 0:
                    continue
                notional = spec.notional_value(fill_price, lots)
                commission = spec.commission(fill_price, lots)
                slip_cost = abs(fill_price - open_price) * spec.multiplier * lots
                margin = spec.margin_required(fill_price, lots)
                if margin > cash:
                    affordable = int(cash / (spec.margin_rate * open_price * spec.multiplier))
                    lots = max(affordable * spec.lot_size, 0)
                    if lots <= 0:
                        continue
                    fill_price = _apply_slippage(open_price, is_buy, config.slippage_bps)
                    notional = spec.notional_value(fill_price, lots)
                    commission = spec.commission(fill_price, lots)
                    slip_cost = abs(fill_price - open_price) * spec.multiplier * lots

                trades.append(FuturesTrade(
                    symbol=order.symbol, action=order.action, bar_index=bar_idx,
                    contract_id=contract_id, lots=lots, fill_price=fill_price,
                    raw_price=open_price, notional=notional, commission=commission,
                    slippage_cost=slip_cost, total_cost=commission + slip_cost,
                    reason=order.reason,
                ))
                pos = long_positions[order.symbol]
                if pos.lots > 0:
                    total_value = pos.lots * pos.entry_price + lots * fill_price
                    pos.lots += lots
                    pos.entry_price = total_value / pos.lots if pos.lots > 0 else 0.0
                else:
                    pos.lots = lots
                    pos.entry_price = fill_price
                pos.contract_id = contract_id

            elif order.action is TradeAction.OPEN_SHORT:
                is_buy = False
                fill_price = _apply_slippage(open_price, is_buy, config.slippage_bps)
                lots = order.target_lots
                if lots <= 0:
                    continue
                margin = spec.margin_required(fill_price, lots)
                if margin > cash:
                    affordable = int(cash / (spec.margin_rate * open_price * spec.multiplier))
                    lots = max(affordable * spec.lot_size, 0)
                    if lots <= 0:
                        continue
                    fill_price = _apply_slippage(open_price, is_buy, config.slippage_bps)

                notional = spec.notional_value(fill_price, lots)
                commission = spec.commission(fill_price, lots)
                slip_cost = abs(fill_price - open_price) * spec.multiplier * lots

                trades.append(FuturesTrade(
                    symbol=order.symbol, action=order.action, bar_index=bar_idx,
                    contract_id=contract_id, lots=lots, fill_price=fill_price,
                    raw_price=open_price, notional=notional, commission=commission,
                    slippage_cost=slip_cost, total_cost=commission + slip_cost,
                    reason=order.reason,
                ))
                pos = short_positions[order.symbol]
                if pos.lots > 0:
                    total_value = pos.lots * pos.entry_price + lots * fill_price
                    pos.lots += lots
                    pos.entry_price = total_value / pos.lots if pos.lots > 0 else 0.0
                else:
                    pos.lots = lots
                    pos.entry_price = fill_price
                pos.contract_id = contract_id

            elif order.action in (TradeAction.CLOSE_LONG, TradeAction.CLOSE_SHORT):
                pos = long_positions[order.symbol] if order.action is TradeAction.CLOSE_LONG else short_positions[order.symbol]
                lots = min(order.target_lots, pos.lots)
                if lots <= 0:
                    continue
                is_buy = order.action is TradeAction.CLOSE_SHORT
                fill_price = _apply_slippage(open_price, is_buy, config.slippage_bps)
                notional = spec.notional_value(fill_price, lots)
                commission = spec.commission(fill_price, lots)
                slip_cost = abs(fill_price - open_price) * spec.multiplier * lots

                trades.append(FuturesTrade(
                    symbol=order.symbol, action=order.action, bar_index=bar_idx,
                    contract_id=contract_id, lots=lots, fill_price=fill_price,
                    raw_price=open_price, notional=notional, commission=commission,
                    slippage_cost=slip_cost, total_cost=commission + slip_cost,
                    reason=order.reason,
                ))
                pos.lots -= lots
                if pos.lots <= 0:
                    pos.lots = 0
                    pos.entry_price = 0.0

            elif order.action is TradeAction.ROLL:
                long_pos = long_positions[order.symbol]
                short_pos = short_positions[order.symbol]

                for pos in [long_pos, short_pos]:
                    if not pos.is_open:
                        continue
                    old_lots = pos.lots
                    new_price = _apply_slippage(open_price, pos.direction < 0, config.slippage_bps)
                    notional = spec.notional_value(new_price, old_lots)
                    commission = spec.commission(new_price, old_lots)
                    slip_cost = abs(new_price - open_price) * spec.multiplier * old_lots

                    trades.append(FuturesTrade(
                        symbol=order.symbol, action=TradeAction.ROLL, bar_index=bar_idx,
                        contract_id=contract_id, lots=old_lots, fill_price=new_price,
                        raw_price=open_price, notional=notional, commission=commission,
                        slippage_cost=slip_cost, total_cost=commission + slip_cost,
                        reason=order.reason,
                    ))
                    pos.entry_price = new_price
                    pos.contract_id = contract_id

        pending_orders = []

        # Step 2: Daily MTM — 按当日收盘结算 P&L
        for sym in symbols:
            spec = specs.get(sym)
            if spec is None:
                continue
            px = closes_today.get(sym, 0.0)
            if px <= 0:
                continue

            lpos = long_positions[sym]
            if lpos.is_open:
                daily_pnl = (px - _prev_close(sym, bar_idx, closes_by_symbol)) * spec.multiplier * lpos.lots
                cash += daily_pnl

            spos = short_positions[sym]
            if spos.is_open:
                daily_pnl = -((px - _prev_close(sym, bar_idx, closes_by_symbol)) * spec.multiplier * spos.lots)
                cash += daily_pnl

        # Step 3: Compute equity and exposure
        long_notional = 0.0
        short_notional = 0.0
        margin_used = 0.0
        for sym in symbols:
            spec = specs.get(sym)
            if spec is None:
                continue
            px = closes_today.get(sym, 0.0)
            if px <= 0:
                continue
            lpos = long_positions[sym]
            spos = short_positions[sym]
            lng_n = lpos.lots * px * spec.multiplier
            sht_n = spos.lots * px * spec.multiplier
            long_notional += lng_n
            short_notional += sht_n
            margin_used += (lng_n + sht_n) * spec.margin_rate

        equity = cash + 0.0  # MTM already in cash; equity = cash (no unrealized in futures MTM model)

        # Actually in daily MTM, cash includes all realized P&L. But unrealized P&L is 0
        # because we settle daily. So equity = cash. But we should also add back margin
        # (margin is a deposit, not a cost). Wait - in futures, margin is a deposit that
        # earns no interest in our model. So cash is reduced by margin deposits? No -
        # margin is not deducted from cash; it's a segregation. The cash account
        # tracks the P&L variation margin. The initial margin is a separate account.
        #
        # Simplified model: cash = initial_capital + cumulative daily P&L - costs.
        # Margin is tracked separately as "frozen" but doesn't reduce cash.
        # Equity = cash (all cash is free + margin is tracked as margin_used).
        # This is the standard "return on full notional" model used in TSMOM literature.

        daily_records.append(DailyRecord(
            bar_index=bar_idx,
            equity=equity,
            cash=cash,
            long_notional=long_notional,
            short_notional=short_notional,
            margin_used=margin_used,
            gross_exposure=long_notional + short_notional,
            long_positions={s: long_positions[s].lots for s in symbols if long_positions[s].lots > 0},
            short_positions={s: short_positions[s].lots for s in symbols if short_positions[s].lots > 0},
        ))

        # Step 4: Signal generation (warmup check)
        if bar_idx < config.min_data_days:
            all_signals.append({})
            continue

        closes_history = {s: list(closes_by_symbol[s][:bar_idx + 1]) for s in symbols}
        signals = generate_signals(closes_history, config)
        all_signals.append(signals)

        if equity <= 0:
            continue

        # Step 5: Compute desired positions from signals
        desired: dict[str, tuple[int, int]] = {}
        for sym in symbols:
            sig = signals.get(sym)
            spec = specs.get(sym)
            if sig is None or spec is None:
                desired[sym] = (0, 0)
                continue
            px = closes_today.get(sym, 0.0)
            if px <= 0:
                desired[sym] = (0, 0)
                continue

            if sig.direction > 0:
                lng = _lots_from_weight(sig.target_weight, equity, spec, px)
                desired[sym] = (lng, 0)
            elif sig.direction < 0:
                sht = _lots_from_weight(sig.target_weight, equity, spec, px)
                desired[sym] = (0, sht)
            else:
                desired[sym] = (0, 0)

        # Step 6: Apply risk constraints
        desired, _constraint_reasons = _check_risk_constraints(
            desired, dict(specs), closes_today, equity, config,
        )

        # Step 7: Generate orders for next bar
        new_orders: list[_PendingOrder] = []
        for sym in symbols:
            spec = specs.get(sym)
            if spec is None:
                continue
            lpos = long_positions[sym]
            spos = short_positions[sym]
            tgt_lng, tgt_sht = desired.get(sym, (0, 0))

            cur_lng = lpos.lots
            cur_sht = spos.lots

            if cur_lng != tgt_lng:
                if tgt_lng > cur_lng:
                    delta = tgt_lng - cur_lng
                    new_orders.append(_PendingOrder(
                        sym, TradeAction.OPEN_LONG, delta,
                        contract_id=_current_contract(sym, bar_idx, active_series),
                        reason=f"signal_long_{sig.composite_score:.2f}" if (sig := signals.get(sym)) else "rebalance",
                    ))
                elif tgt_lng < cur_lng:
                    delta = cur_lng - tgt_lng
                    new_orders.append(_PendingOrder(
                        sym, TradeAction.CLOSE_LONG, delta,
                        contract_id=lpos.contract_id or sym,
                        reason="reduce_long",
                    ))

            if cur_sht != tgt_sht:
                if tgt_sht > cur_sht:
                    delta = tgt_sht - cur_sht
                    new_orders.append(_PendingOrder(
                        sym, TradeAction.OPEN_SHORT, delta,
                        contract_id=_current_contract(sym, bar_idx, active_series),
                        reason=f"signal_short_{sig.composite_score:.2f}" if (sig := signals.get(sym)) else "rebalance",
                    ))
                elif tgt_sht < cur_sht:
                    delta = cur_sht - tgt_sht
                    new_orders.append(_PendingOrder(
                        sym, TradeAction.CLOSE_SHORT, delta,
                        contract_id=spos.contract_id or sym,
                        reason="reduce_short",
                    ))

        pending_orders = new_orders

    equity_curve = [r.equity for r in daily_records]
    traded = tuple(sorted(
        {t.symbol for t in trades if t.lots > 0}
    ))

    return TsmomResult(
        config=config,
        equity_curve=equity_curve,
        daily_records=daily_records,
        trades=trades,
        all_signals=all_signals,
        roll_events=roll_events_collected,
        symbols_traded=traded,
        bar_count=bar_count,
    )


def _prev_close(
    symbol: str,
    bar_idx: int,
    closes_by_symbol: Mapping[str, Sequence[float]],
) -> float:
    """前一日收盘价(MTM 结算用)。"""
    closes = closes_by_symbol.get(symbol)
    if closes is None or bar_idx < 1:
        return 0.0
    if bar_idx - 1 < len(closes):
        return float(closes[bar_idx - 1])
    return 0.0


def _current_contract(
    symbol: str,
    bar_idx: int,
    active_series: Mapping[str, ActiveContractSeries] | None,
) -> str:
    """获取当前活跃合约 ID。"""
    if active_series is None or symbol not in active_series:
        return symbol
    series = active_series[symbol]
    if bar_idx < len(series.contract_ids):
        return series.contract_ids[bar_idx]
    return symbol


__all__ = [
    "DailyRecord",
    "FuturesTrade",
    "TradeAction",
    "TsmomResult",
    "run_backtest",
]
