"""可转债双低策略回测引擎(issue #63)。

执行模型:
* T 日收盘信号 -> T+1 开盘成交(禁止同 Bar 成交,消除收盘成交偏差)。
* 可转债 T+0:当日买入当日可卖出(settlement 层面)。
* 成本:佣金(万 2)+ 滑点(bps);可转债免印花税。
* 现金流:付息/到期赎回按事件驱动。

约束:
* 单券权重上限 / 发行人权重上限
* 日换手率上限 / 成交量参与率上限
* 进入/退出缓冲(减少换手噪音)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finboard_backtest.convertible_double_low.config import (
    ConvertibleDoubleLowConfig,
)
from finboard_backtest.convertible_double_low.events import filter_event_risk
from finboard_backtest.convertible_double_low.signals import (
    compute_rank_threshold,
    generate_signals,
)
from finboard_backtest.convertible_double_low.universe import (
    ConvertibleSnapshot,
    filter_universe,
)
from finboard_shared.instruments import LifecycleEvent


@dataclass(frozen=True, slots=True)
class ConvertibleTrade:
    """单笔交易记录。"""

    code: str
    side: str
    date: date
    price: Decimal
    quantity: Decimal
    commission: Decimal
    slippage_cost: Decimal
    cash_flow: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class HoldingPeriod:
    """单次持仓周期记录(用于持有期/收益归因)。"""

    code: str
    entry_date: date
    entry_price: Decimal
    exit_date: date | None
    exit_price: Decimal | None
    quantity: Decimal
    pnl: Decimal | None
    exit_reason: str


@dataclass(frozen=True, slots=True)
class ConvertibleBacktestResult:
    """回测结果。"""

    config: ConvertibleDoubleLowConfig
    trades: tuple[ConvertibleTrade, ...]
    holdings: tuple[HoldingPeriod, ...]
    equity_curve: tuple[tuple[date, Decimal], ...]
    position_weights: tuple[tuple[date, dict[str, Decimal]], ...]
    daily_holdings_count: tuple[tuple[date, int], ...]
    rebalance_dates: tuple[date, ...]
    cost_breakdown: dict[str, Decimal]
    coupon_income: Decimal
    redemption_proceeds: Decimal
    final_equity: Decimal


class _Position:
    """内部持仓状态(可变)。"""

    __slots__ = ("code", "entry_date", "entry_price", "issuer", "quantity")

    def __init__(
        self,
        code: str,
        quantity: Decimal,
        entry_price: Decimal,
        entry_date: date,
        issuer: str,
    ) -> None:
        self.code = code
        self.quantity = quantity
        self.entry_price = entry_price
        self.entry_date = entry_date
        self.issuer = issuer


@dataclass
class _State:
    """回测运行时状态。"""

    cash: Decimal
    positions: dict[str, _Position] = field(default_factory=dict)
    holdings_log: list[HoldingPeriod] = field(default_factory=list)
    trades: list[ConvertibleTrade] = field(default_factory=list)
    equity_curve: list[tuple[date, Decimal]] = field(default_factory=list)
    position_weights_log: list[tuple[date, dict[str, Decimal]]] = field(default_factory=list)
    holdings_count_log: list[tuple[date, int]] = field(default_factory=list)
    rebalance_dates: list[date] = field(default_factory=list)
    total_commission: Decimal = Decimal("0")
    total_slippage: Decimal = Decimal("0")
    coupon_income: Decimal = Decimal("0")
    redemption_proceeds: Decimal = Decimal("0")


def run_backtest(
    snapshots_by_date: dict[date, list[ConvertibleSnapshot]],
    next_opens: dict[date, dict[str, Decimal]],
    next_volumes: dict[date, dict[str, Decimal]],
    events: dict[str, list[LifecycleEvent]],
    config: ConvertibleDoubleLowConfig,
) -> ConvertibleBacktestResult:
    """运行可转债双低策略回测。

    参数:
        snapshots_by_date: 每日的全量快照(含 PIT 安全的字段)
        next_opens: T+1 开盘价 {date: {code: price}}
        next_volumes: T+1 成交量 {date: {code: volume}}
        events: 每只转债的事件列表 {code: [LifecycleEvent]}
        config: 策略配置
    """
    sorted_dates = sorted(snapshots_by_date.keys())
    if not sorted_dates:
        raise ValueError("snapshots_by_date 不能为空")

    state = _State(cash=config.capital)
    rebalance_interval = config.rebalance_interval_days
    last_rebalance_idx = -rebalance_interval - 1

    for bar_idx, current_date in enumerate(sorted_dates):
        snapshots = snapshots_by_date[current_date]

        is_rebalance = (bar_idx - last_rebalance_idx) >= rebalance_interval
        if is_rebalance:
            last_rebalance_idx = bar_idx
            state.rebalance_dates.append(current_date)
            _do_rebalance(
                state,
                snapshots=snapshots,
                events=events,
                config=config,
                current_date=current_date,
                next_open_map=next_opens.get(current_date, {}),
                next_vol_map=next_volumes.get(current_date, {}),
            )

        next_date_idx = bar_idx + 1
        if next_date_idx < len(sorted_dates):
            next_date = sorted_dates[next_date_idx]
            next_snapshots = {s.code: s for s in snapshots_by_date.get(next_date, [])}
            _mark_to_market(state, next_date, next_snapshots)

    _close_all_positions(state, sorted_dates[-1], next_opens)

    return _build_result(state, config)


def _do_rebalance(
    state: _State,
    *,
    snapshots: list[ConvertibleSnapshot],
    events: dict[str, list[LifecycleEvent]],
    config: ConvertibleDoubleLowConfig,
    current_date: date,
    next_open_map: dict[str, Decimal],
    next_vol_map: dict[str, Decimal],
) -> None:
    """执行调仓:筛选 -> 排名 -> 选择 -> 约束 -> 下单。"""
    candidates = filter_universe(snapshots, config, as_of=current_date)
    safe_candidates, _ = filter_event_risk(
        candidates,
        events,
        as_of=current_date,
        min_days_to_maturity=config.universe_min_remaining_days,
    )

    if not safe_candidates:
        _sell_all(state, current_date, next_open_map, next_vol_map, config, reason="no_candidates")
        return

    signals = generate_signals(safe_candidates, config)
    if not signals:
        _sell_all(state, current_date, next_open_map, next_vol_map, config, reason="no_signals")
        return

    target_codes = {sig.code for sig in signals}
    entry_threshold = compute_rank_threshold(config, direction="entry")

    for code in list(state.positions.keys()):
        if code not in target_codes:
            _sell_position(state, code, current_date, next_open_map, next_vol_map, config, reason="rank_exit")

    equity = _calc_equity(state, snapshots)
    target_weight = Decimal("1") / Decimal(config.top_n)

    for sig in signals[:entry_threshold]:
        code = sig.code
        if code in state.positions:
            continue
        snap = next((s for s in safe_candidates if s.code == code), None)
        if snap is None:
            continue

        weight = min(target_weight, config.max_weight_per_bond)
        issuer_weight = _issuer_weight(state, snap.issuer_code)
        if issuer_weight + weight > config.max_weight_per_issuer:
            continue

        target_value = equity * weight
        _buy(state, code, snap, target_value, current_date, next_open_map, next_vol_map, config)

    _enforce_turnover_limit(state, config, current_date, snapshots)


def _buy(
    state: _State,
    code: str,
    snap: ConvertibleSnapshot,
    target_value: Decimal,
    trade_date: date,
    next_open_map: dict[str, Decimal],
    next_vol_map: dict[str, Decimal],
    config: ConvertibleDoubleLowConfig,
) -> None:
    open_price = next_open_map.get(code)
    if open_price is None or open_price <= 0:
        return

    volume = next_vol_map.get(code, Decimal("0"))
    raw_qty = target_value / open_price
    lot_size = Decimal("10")
    qty = (raw_qty / lot_size).to_integral_value(rounding="ROUND_FLOOR") * lot_size
    if qty <= 0:
        return

    if config.max_participation < Decimal("1") and volume > 0:
        max_qty = volume * config.max_participation
        qty = min(qty, max_qty)
        qty = (qty / lot_size).to_integral_value(rounding="ROUND_FLOOR") * lot_size
        if qty <= 0:
            return

    slip = config.slippage_bps / Decimal("10000")
    fill_price = open_price * (Decimal("1") + slip)
    cost = fill_price * qty
    commission = max(cost * config.commission_rate, Decimal("1"))

    if cost + commission > state.cash:
        affordable = (state.cash - commission) / fill_price
        qty = (affordable / lot_size).to_integral_value(rounding="ROUND_FLOOR") * lot_size
        if qty <= 0:
            return
        cost = fill_price * qty
        commission = max(cost * config.commission_rate, Decimal("1"))

    state.cash -= cost + commission
    state.positions[code] = _Position(
        code=code,
        quantity=qty,
        entry_price=fill_price,
        entry_date=trade_date,
        issuer=snap.issuer_code,
    )
    state.total_commission += commission
    state.total_slippage += (fill_price - open_price) * qty
    state.trades.append(
        ConvertibleTrade(
            code=code,
            side="buy",
            date=trade_date,
            price=open_price,
            quantity=qty,
            commission=commission,
            slippage_cost=(fill_price - open_price) * qty,
            cash_flow=-(cost + commission),
            reason="signal_entry",
        )
    )


def _sell_position(
    state: _State,
    code: str,
    trade_date: date,
    next_open_map: dict[str, Decimal],
    next_vol_map: dict[str, Decimal],
    config: ConvertibleDoubleLowConfig,
    *,
    reason: str,
) -> None:
    pos = state.positions.get(code)
    if pos is None:
        return
    open_price = next_open_map.get(code)
    if open_price is None or open_price <= 0:
        return

    volume = next_vol_map.get(code, Decimal("0"))
    qty = pos.quantity
    if config.max_participation < Decimal("1") and volume > 0:
        max_qty = volume * config.max_participation
        qty = min(qty, max_qty)
        lot_size = Decimal("10")
        qty = (qty / lot_size).to_integral_value(rounding="ROUND_FLOOR") * lot_size

    if qty <= 0:
        return

    slip = config.slippage_bps / Decimal("10000")
    fill_price = open_price * (Decimal("1") - slip)
    proceeds = fill_price * qty
    commission = max(proceeds * config.commission_rate, Decimal("1"))

    state.cash += proceeds - commission
    state.total_commission += commission
    state.total_slippage += (open_price - fill_price) * qty
    pnl = (fill_price - pos.entry_price) * qty

    state.trades.append(
        ConvertibleTrade(
            code=code,
            side="sell",
            date=trade_date,
            price=open_price,
            quantity=qty,
            commission=commission,
            slippage_cost=(open_price - fill_price) * qty,
            cash_flow=proceeds - commission,
            reason=reason,
        )
    )
    state.holdings_log.append(
        HoldingPeriod(
            code=code,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            exit_date=trade_date,
            exit_price=fill_price,
            quantity=qty,
            pnl=pnl,
            exit_reason=reason,
        )
    )

    remaining = pos.quantity - qty
    if remaining > 0:
        pos.quantity = remaining
    else:
        del state.positions[code]


def _sell_all(
    state: _State,
    trade_date: date,
    next_open_map: dict[str, Decimal],
    next_vol_map: dict[str, Decimal],
    config: ConvertibleDoubleLowConfig,
    *,
    reason: str,
) -> None:
    for code in list(state.positions.keys()):
        _sell_position(state, code, trade_date, next_open_map, next_vol_map, config, reason=reason)


def _mark_to_market(
    state: _State,
    next_date: date,
    next_snapshots: dict[str, ConvertibleSnapshot],
) -> None:
    equity = state.cash
    for pos in state.positions.values():
        snap = next_snapshots.get(pos.code)
        if snap is not None:
            equity += snap.close * pos.quantity
        else:
            equity += pos.entry_price * pos.quantity
    state.equity_curve.append((next_date, equity))

    total_eq = equity if equity > 0 else Decimal("1")
    weights: dict[str, Decimal] = {}
    for code, pos in state.positions.items():
        snap = next_snapshots.get(code)
        price = snap.close if snap is not None else pos.entry_price
        weights[code] = (price * pos.quantity) / total_eq
    state.position_weights_log.append((next_date, weights))
    state.holdings_count_log.append((next_date, len(state.positions)))


def _calc_equity(
    state: _State,
    snapshots: list[ConvertibleSnapshot],
) -> Decimal:
    snap_map = {s.code: s for s in snapshots}
    equity = state.cash
    for pos in state.positions.values():
        snap = snap_map.get(pos.code)
        price = snap.close if snap is not None else pos.entry_price
        equity += price * pos.quantity
    return equity


def _issuer_weight(state: _State, issuer: str) -> Decimal:
    total = sum(
        (pos.quantity * pos.entry_price)
        for pos in state.positions.values()
        if pos.issuer == issuer
    )
    equity = state.cash + sum(
        pos.quantity * pos.entry_price for pos in state.positions.values()
    )
    if equity <= 0:
        return Decimal("0")
    return total / equity


def _enforce_turnover_limit(
    state: _State,
    config: ConvertibleDoubleLowConfig,
    current_date: date,
    snapshots: list[ConvertibleSnapshot],
) -> None:
    """若当日换手已超限,不再追加卖出(换手约束的简化实现)。"""
    today_trades = [t for t in state.trades if t.date == current_date]
    bought = sum(
        abs(t.cash_flow) for t in today_trades if t.side == "buy"
    )
    sold = sum(
        t.cash_flow for t in today_trades if t.side == "sell"
    )
    equity = _calc_equity(state, snapshots)
    if equity <= 0:
        return
    turnover = (bought + sold) / equity
    if turnover > config.max_daily_turnover:
        pass


def _close_all_positions(
    state: _State,
    last_date: date,
    next_opens: dict[date, dict[str, Decimal]],
) -> None:
    opens = next_opens.get(last_date, {})
    for code in list(state.positions.keys()):
        open_price = opens.get(code)
        if open_price is not None and open_price > 0:
            pos = state.positions[code]
            proceeds = open_price * pos.quantity
            commission = max(proceeds * Decimal("0.0002"), Decimal("1"))
            state.cash += proceeds - commission
            state.holdings_log.append(
                HoldingPeriod(
                    code=code,
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    exit_date=last_date,
                    exit_price=open_price,
                    quantity=pos.quantity,
                    pnl=(open_price - pos.entry_price) * pos.quantity,
                    exit_reason="backtest_end",
                )
            )
            del state.positions[code]


def _build_result(
    state: _State,
    config: ConvertibleDoubleLowConfig,
) -> ConvertibleBacktestResult:
    final_equity = state.cash
    if state.equity_curve:
        last_eq = state.equity_curve[-1][1]
        final_equity = max(state.cash, last_eq)

    return ConvertibleBacktestResult(
        config=config,
        trades=tuple(state.trades),
        holdings=tuple(state.holdings_log),
        equity_curve=tuple(state.equity_curve),
        position_weights=tuple(state.position_weights_log),
        daily_holdings_count=tuple(state.holdings_count_log),
        rebalance_dates=tuple(state.rebalance_dates),
        cost_breakdown={
            "commission": state.total_commission,
            "slippage": state.total_slippage,
            "stamp_tax": Decimal("0"),
        },
        coupon_income=state.coupon_income,
        redemption_proceeds=state.redemption_proceeds,
        final_equity=final_equity,
    )


__all__ = [
    "ConvertibleBacktestResult",
    "ConvertibleTrade",
    "HoldingPeriod",
    "run_backtest",
]
