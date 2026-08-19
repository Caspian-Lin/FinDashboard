"""``BacktestBroker`` —— 研究级纸面撮合引擎(issue #56 重构)。

核心约束:

1. **Next-bar 成交**:订单在收单 Bar 不成交;撮合延迟到下一可交易 Bar。
   这消除了"同 Bar 收盘穿越"导致的乐观偏差。
2. **持仓由 fill 驱动**:`available` 仅由成交回报更新,策略乐观修改的内部
   状态不会影响 broker;拒单 / 撤单 / 部分成交不会让仓位漂移。
3. **超卖拒绝**:`allow_short=false` 时,卖出前严格校验可用持仓;不足则
   ``INSUFFICIENT_POSITION`` 拒单,现金 / 持仓均不变。
4. **资产类型化规则**:从 ``AssetRuleTable`` 按 (market, type) 解析,未知
   类型 fail closed。涨跌停 / 停牌 / T+N / 印花税 / 手数 / 价格步长全部
   由规则驱动,不再硬编码 A 股默认。
5. **跳空与限价改善**:开盘跳空仍按限价单方向较优价格成交。
6. **参与率上限与部分成交**:可配置 ``max_participation`` 与
   ``allow_partial_fill``,模拟真实流动性约束。

BacktestBroker 实现 ``BrokerAdapter`` 接口,但**不**走实盘 OrderManager → DB
链路;它由 ``BacktestContext`` 直接调用,所有状态在内存中维护。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import count
from typing import Protocol

import structlog

from finboard_backtest.asset_rules import (
    DEFAULT_TABLE,
    AssetRule,
    AssetRuleResolutionError,
    AssetRuleTable,
    fill_price_for,
    fill_quantity_within_participation,
    price_with_slippage,
    resolve_rule_from_overrides,
)
from finboard_backtest.clock import market_close
from finboard_backtest.config import FillTiming, MatchingModel
from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Bar, Fill, Order, Position, Symbol
from finboard_shared.types import (
    BrokerKind,
    InstrumentType,
    Market,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)

logger = structlog.get_logger(__name__)


class InstrumentResolver(Protocol):
    """把 ``Symbol.code`` 解析到 (市场, 资产类型)。"""

    def resolve(self, code: str) -> tuple[Market, InstrumentType]:
        ...


@dataclass(slots=True)
class _OrderState:
    """内部订单状态;在收单时创建,直到下一 Bar 才进入撮合。"""

    order: Order
    rule: AssetRule
    slippage_bps: Decimal
    submitted_on: date
    remaining: Decimal


@dataclass(slots=True)
class _PositionLot:
    """可用持仓 FIFO 队列;用于 T+N 与可用数量计算。"""

    quantity: Decimal
    available_date: date


@dataclass(slots=True)
class _InternalPosition:
    """broker 内部持仓,只由成交驱动。"""

    symbol: Symbol
    rule: AssetRule
    total: Decimal = Decimal("0")
    available: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    lots: list[_PositionLot] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lots is None:
            self.lots = []

    def apply_sell(self, qty: Decimal, *, trade_date: date) -> Decimal:
        """FIFO 减仓,返回实际可成交数量(可能小于 ``qty``)。"""
        remaining_to_sell = qty
        while remaining_to_sell > 0 and self.lots:
            lot = self.lots[0]
            if lot.quantity <= remaining_to_sell:
                remaining_to_sell -= lot.quantity
                self.lots.pop(0)
            else:
                lot.quantity -= remaining_to_sell
                remaining_to_sell = Decimal("0")
        sold = qty - remaining_to_sell
        self.total = sum((lot.quantity for lot in self.lots), Decimal("0"))
        self.refresh_available(trade_date)
        return sold

    def refresh_available(self, trade_date: date) -> None:
        self.available = sum(
            (lot.quantity for lot in self.lots if lot.available_date <= trade_date),
            Decimal("0"),
        )


class BacktestBroker(BrokerAdapter):
    """纸面撮合 broker — 内存级,无 IO。"""

    def __init__(
        self,
        *,
        initial_capital: Decimal = Decimal("100000"),
        matching_model: MatchingModel | None = None,
        asset_rules: AssetRuleTable | None = None,
        instrument_resolver: InstrumentResolver | None = None,
        # 兼容旧调用(顶层费用字段)
        commission_rate: Decimal | None = None,
        commission_min: Decimal | None = None,
        stamp_tax_rate: Decimal | None = None,
        slippage_bps: Decimal | None = None,
        lot_size: int | None = None,
        allow_short: bool = False,
        enforce_t_plus_1: bool = True,
    ) -> None:
        self._initial_capital = initial_capital
        self._cash: Decimal = initial_capital
        self._matching_model = matching_model or MatchingModel()
        self._asset_rules = asset_rules
        self._instrument_resolver = instrument_resolver or _DefaultInstrumentResolver()
        # 兼容旧 API: 顶层费用字段在每次解析规则时传入
        self._override_commission_rate = commission_rate
        self._override_commission_min = commission_min
        self._override_stamp_tax = stamp_tax_rate
        self._override_slippage = slippage_bps
        # 旧的 lot_size / allow_short / enforce_t_plus_1 仅在没有 asset_rules
        # 时使用,有 asset_rules 时由规则决定。
        self._legacy_lot_size = lot_size
        self._legacy_allow_short = allow_short
        self._legacy_enforce_t1 = enforce_t_plus_1

        self._positions: dict[str, _InternalPosition] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._pending_orders: dict[str, _OrderState] = {}
        self._pending_limit: dict[str, _OrderState] = {}
        self._current_bars: dict[str, Bar] = {}
        self._previous_closes: dict[str, Decimal] = {}
        self._current_date: date | None = None

        self._event_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._id_counter = count(1)
        self._account_id = AccountId("backtest")
        self._connected = False

    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.BACKTEST

    @property
    def matching_model(self) -> MatchingModel:
        return self._matching_model

    @property
    def asset_rules(self) -> AssetRuleTable | None:
        return self._asset_rules

    # ------------------------------------------------------------------ 连接

    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        self._account_id = account_id
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 查询

    async def query_account(self) -> Account:
        return Account(
            account_id=self._account_id,
            broker_kind=BrokerKind.BACKTEST,
            total_asset=self.total_equity(),
            cash=self._cash,
            frozen_cash=Decimal("0"),
        )

    async def query_positions(self) -> list[Position]:
        return [self._snapshot_position(code) for code in self._positions if self._positions[code].total > 0]

    async def query_order(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None:
            return None
        return self._clone_order(order)

    def query_order_sync(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None:
            return None
        return self._clone_order(order)

    async def query_active_orders(self) -> list[Order]:
        return [
            self._clone_order(o)
            for o in self._orders.values()
            if not o.is_terminal
        ]

    # ------------------------------------------------------------------ 交易

    async def place_order(self, order: Order) -> SubmissionResult:
        cid = str(order.client_order_id)
        self._orders[cid] = order
        if self._current_date is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.BROKER_REJECTED
            order.reject_message = "尚未收到任何 Bar,无法下单"
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=RejectReason.BROKER_REJECTED,
            )

        try:
            rule, slippage_bps = self._resolve_rule(order.symbol)
        except AssetRuleResolutionError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.INVALID_SYMBOL
            order.reject_message = str(exc)
            self._push_event(BrokerEventType.ORDER_REJECTED, order)
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=RejectReason.INVALID_SYMBOL,
            )

        # 手数取整(限价单 / 市价单都按资产规则取整)
        if self._matching_model.enforce_lot_rounding:
            rounded = rule.round_to_lot(order.quantity)
            if rounded < rule.lot_size:
                order.status = OrderStatus.REJECTED
                order.reject_reason = RejectReason.INVALID_QUANTITY
                order.reject_message = (
                    f"数量 {order.quantity} 向下取整到 lot_size={rule.lot_size} 后为 0"
                )
                self._push_event(BrokerEventType.ORDER_REJECTED, order)
                return SubmissionResult(
                    client_order_id=order.client_order_id,
                    accepted=False,
                    reject_reason=RejectReason.INVALID_QUANTITY,
                )
            order.quantity = rounded

        state = _OrderState(
            order=order,
            rule=rule,
            slippage_bps=slippage_bps,
            submitted_on=self._current_date,
            remaining=order.quantity,
        )

        # 市价单 / 限价单都不在收单 Bar 成交 —— 进入下一 Bar 处理
        order.status = OrderStatus.SUBMITTED
        order.broker_order_id = str(next(self._id_counter))
        if order.order_type is OrderType.LIMIT:
            self._pending_limit[cid] = state
        else:
            self._pending_orders[cid] = state

        self._push_event(BrokerEventType.ORDER_ACCEPTED, order)
        return SubmissionResult(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            accepted=True,
        )

    async def cancel_order(self, client_order_id: str) -> None:
        state = self._pending_orders.pop(client_order_id, None) or self._pending_limit.pop(
            client_order_id, None
        )
        if state is None:
            return
        order = state.order
        if order.filled_quantity <= 0:
            order.status = OrderStatus.CANCELLED
            self._push_event(BrokerEventType.ORDER_CANCELLED, order)
        else:
            # 已部分成交,把剩余部分取消
            order.status = OrderStatus.CANCELLED

    # ------------------------------------------------------------------ 回报流

    def events(self) -> AsyncIterator[BrokerEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[BrokerEvent]:
        while self._connected or not self._event_queue.empty():
            event = await self._event_queue.get()
            yield event

    async def drain_events(self) -> list[BrokerEvent]:
        events: list[BrokerEvent] = []
        while not self._event_queue.empty():
            events.append(self._event_queue.get_nowait())
        return events

    # ------------------------------------------------------------------ 回测驱动

    def on_new_bar(self, bar: Bar) -> None:
        """每根新 Bar 到达:更新行情 → 推进 T+N 可用性 → 撮合待成交订单。

        **关键不变量**:bar.timestamp.date() 必须严格 > 上一根 Bar 的日期;
        否则视为同一交易日内的多 Bar,在 ``MatchingModel.next_bar_only`` 下
        仍允许成交,但 ``submitted_on == current_date`` 的订单会被跳过
        (同 Bar 收单 → 同 Bar 成交的穿越)。
        """
        code = bar.symbol.code
        previous = self._current_bars.get(code)
        new_day = (
            self._current_date is None
            or bar.timestamp.date() != self._current_date
        )
        self._current_bars[code] = bar
        if new_day:
            self._current_date = bar.timestamp.date()
        # 更新前收盘(用于涨跌停/停牌判断)
        if previous is not None:
            self._previous_closes[code] = previous.close

        # 刷新所有持仓的可用数量(T+N 推进)
        for pos in self._positions.values():
            pos.refresh_available(self._current_date)  # type: ignore[arg-type]

        # 撮合市价单 + 限价单
        self._match_pending_orders(bar)

    def total_equity(self) -> Decimal:
        pos_value = Decimal("0")
        for code, pos in self._positions.items():
            bar = self._current_bars.get(code)
            price = bar.close if bar else pos.average_price
            pos_value += pos.total * price
        return self._cash + pos_value

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    @property
    def all_orders(self) -> list[Order]:
        return [self._clone_order(o) for o in self._orders.values()]

    def available_quantity(self, code: str) -> Decimal:
        """外部查询可用持仓(用于测试 / 内部不变量)。"""
        pos = self._positions.get(code)
        if pos is None:
            return Decimal("0")
        return pos.available

    # ------------------------------------------------------------------ 内部

    def _resolve_rule(self, symbol: Symbol) -> tuple[AssetRule, Decimal]:
        market, instrument_type = self._instrument_resolver.resolve(symbol.code)
        table = self._asset_rules if self._asset_rules is not None else DEFAULT_TABLE
        try:
            return resolve_rule_from_overrides(
                table,
                market=market,
                instrument_type=instrument_type,
                commission_rate=self._override_commission_rate,
                commission_min=self._override_commission_min,
                stamp_tax_rate=self._override_stamp_tax,
                slippage_bps=self._override_slippage,
            )
        except AssetRuleResolutionError:
            # 旧调用路径兼容:即使 resolver 返回了未在 table 中登记的类型,
            # 也允许顶层 lot_size / enforce_t_plus_1 覆盖(便于 ad-hoc 测试)。
            if self._asset_rules is not None:
                raise
            rule = AssetRule(
                instrument_type=instrument_type,
                lot_size=Decimal(self._legacy_lot_size or 100),
                enforce_t_plus_1=self._legacy_enforce_t1,
                stamp_tax_rate=self._override_stamp_tax or Decimal("0.0005"),
                commission_rate=self._override_commission_rate or Decimal("0.0003"),
                commission_min=self._override_commission_min or Decimal("5"),
                price_limit_pct=None,
                price_tick=Decimal("0.01"),
                allow_short=self._legacy_allow_short,
            )
            slippage = self._override_slippage or Decimal("0")
            return rule, slippage

    def _match_pending_orders(self, bar: Bar) -> None:
        """撮合所有待成交订单(市价 + 限价)。

        同 Bar 收单的订单在 ``next_bar_only=True`` 时不参与本次撮合,需等到
        下一根 Bar。这一不变量是消除"同 Bar 收盘穿越"的核心。
        """
        if self._current_date is None:
            return

        # 1) 市价单(只在属于本标的的 Bar 到达时撮合)
        to_remove: list[str] = []
        for cid, state in self._pending_orders.items():
            if state.order.symbol.code != bar.symbol.code:
                continue
            if self._matching_model.next_bar_only and state.submitted_on == self._current_date:
                # 当日新单,跳过(留到下一 Bar)
                continue
            self._try_fill_market(state, bar)
            if state.order.is_terminal or state.remaining <= 0:
                to_remove.append(cid)
        for cid in to_remove:
            self._pending_orders.pop(cid, None)

        # 2) 限价单(每次 Bar 都检查)
        to_remove = []
        for cid, state in self._pending_limit.items():
            if state.order.symbol.code != bar.symbol.code:
                continue
            if self._matching_model.next_bar_only and state.submitted_on == self._current_date:
                continue
            self._try_fill_limit(state, bar)
            if state.order.is_terminal or state.remaining <= 0:
                to_remove.append(cid)
        for cid in to_remove:
            self._pending_limit.pop(cid, None)

    def _try_fill_market(self, state: _OrderState, bar: Bar) -> None:
        order = state.order
        rule = state.rule

        if self._is_suspended(bar, rule):
            self._reject(
                order=order,
                reason=RejectReason.BROKER_REJECTED,
                message=f"{order.symbol.code} 停牌,无法成交",
            )
            return

        if self._is_at_limit_blocked(order.side, bar, rule):
            self._reject(
                order=order,
                reason=RejectReason.BROKER_REJECTED,
                message=f"{order.symbol.code} 触发涨跌停限制",
            )
            return

        raw_price = fill_price_for(
            rule,
            side=order.side,
            bar=bar,
            fill_timing=self._fill_price_timing(),
            limit_price=None,
        )
        self._execute_fill(state, bar, raw_price)

    def _try_fill_limit(self, state: _OrderState, bar: Bar) -> None:
        order = state.order
        rule = state.rule
        limit_price = order.price
        if limit_price is None:
            self._reject(
                order=order,
                reason=RejectReason.INVALID_PRICE,
                message="限价单缺少 price",
            )
            return

        if self._is_suspended(bar, rule):
            self._reject(
                order=order,
                reason=RejectReason.BROKER_REJECTED,
                message=f"{order.symbol.code} 停牌,限价单无法成交",
            )
            return

        # 涨跌停限制下,反方向的限价单仍然不能成交(保护语义)
        if self._is_at_limit_blocked(order.side, bar, rule):
            self._reject(
                order=order,
                reason=RejectReason.BROKER_REJECTED,
                message=f"{order.symbol.code} 涨跌停,限价单无法成交",
            )
            return

        # 判断限价可成交性
        if order.side is Side.BUY:
            if bar.low < limit_price or (self._matching_model.honour_gaps and bar.open <= limit_price):
                # 可成交(open 或 low 任一触发)
                pass
            elif bar.low <= limit_price:
                pass
            else:
                return  # 未触发,继续挂单
        else:  # SELL
            if bar.high > limit_price or (self._matching_model.honour_gaps and bar.open >= limit_price) or bar.high >= limit_price:
                pass
            else:
                return

        raw_price = fill_price_for(
            rule,
            side=order.side,
            bar=bar,
            fill_timing=self._fill_price_timing(),
            limit_price=limit_price,
        )
        self._execute_fill(state, bar, raw_price)

    def _fill_price_timing(self) -> str:
        """把 ``FillTiming`` 转换为 ``fill_price_for`` 期望的简化 timing。"""
        timing = self._matching_model.fill_timing
        if timing is FillTiming.NEXT_BAR_OPEN:
            return "open"
        if timing is FillTiming.NEXT_BAR_VWAP_PROXY:
            return "vwap_proxy"
        return "close"

    def _execute_fill(self, state: _OrderState, bar: Bar, raw_price: Decimal) -> None:
        """执行成交:校验现金/持仓、施加滑点、更新现金/持仓/订单状态。"""
        order = state.order
        rule = state.rule

        # 参与率上限 + 部分成交
        desired_qty = state.remaining
        executed_qty = fill_quantity_within_participation(
            desired_qty,
            bar.volume,
            self._matching_model.max_participation,
        )
        if executed_qty <= 0 and not self._matching_model.allow_partial_fill:
            return
        if executed_qty <= 0:
            # 部分成交允许 0(等下一 Bar);但若整单无法成交则按原数量撮合
            executed_qty = desired_qty if self._matching_model.allow_partial_fill else Decimal("0")
            if executed_qty <= 0:
                return

        # 手数取整(再次校验,防止参与率截断后低于 1 手)
        if self._matching_model.enforce_lot_rounding:
            executed_qty = rule.round_to_lot(executed_qty)
            if executed_qty < rule.lot_size:
                if state.remaining <= rule.lot_size:
                    # 不足一手且不允许,直接拒单
                    self._reject(
                        order=order,
                        reason=RejectReason.INVALID_QUANTITY,
                        message=(
                            f"成交量 {executed_qty} 不足 1 手({rule.lot_size})"
                        ),
                    )
                    return
                # 否则等待下一 Bar
                return
        if executed_qty > state.remaining:
            executed_qty = state.remaining

        # T+N 预检卖出(只检查 broker 内部持仓)
        if order.side is Side.SELL:
            available = self._available_for(state.order.symbol.code, on_date=self._current_date)  # type: ignore[arg-type]
            if executed_qty > available and not rule.allow_short:
                # 超卖:把 executed_qty 截断到 available;若 available 为 0 则拒单
                if available <= 0:
                    self._reject(
                        order=order,
                        reason=RejectReason.INSUFFICIENT_POSITION,
                        message=(
                            f"超卖拒绝: {state.order.symbol.code} "
                            f"可用 {available}, 需 {executed_qty};"
                            f"现金与持仓不变"
                        ),
                    )
                    return
                executed_qty = available
                if self._matching_model.enforce_lot_rounding:
                    executed_qty = rule.round_to_lot(executed_qty)
                    if executed_qty <= 0:
                        return

        # 计算成交价(含滑点)、佣金、印花税
        slip_price = price_with_slippage(raw_price, order.side, state.slippage_bps)
        slip_price = rule.round_to_tick(slip_price)
        turnover = slip_price * executed_qty
        commission = max(turnover * rule.commission_rate, rule.commission_min)
        tax = (
            turnover * rule.stamp_tax_rate
            if order.side is Side.SELL
            else Decimal("0")
        )
        total_cost = commission + tax

        # T+N 检查(卖出):如果卖出后,剩余 T+N 可用 >= 持仓才算合法
        # 这里按 available 拒绝已经保证不会超卖

        # 现金变动
        if order.side is Side.BUY:
            needed = turnover + total_cost
            if self._cash < needed and not rule.allow_short:
                # 资金不足:整单拒单,不修改持仓/现金
                self._reject(
                    order=order,
                    reason=RejectReason.INSUFFICIENT_CASH,
                    message=(
                        f"资金不足: 需 {needed}, 有 {self._cash};"
                        f"持仓与现金不变"
                    ),
                )
                return
            self._cash -= needed
        else:
            received = turnover - total_cost
            self._cash += received

        # 更新持仓(由 fill 驱动)
        trade_date = self._current_date
        assert trade_date is not None  # _match_pending_orders 仅在 on_new_bar 设值后调用
        self._apply_fill_to_position(
            symbol=order.symbol,
            rule=rule,
            side=order.side,
            qty=executed_qty,
            price=slip_price,
            trade_date=trade_date,
            enforce_t_plus_1=rule.enforce_t_plus_1,
        )

        # 记录成交;filled_at = 实际撮合发生的交易日(收盘约定与 engine
        # decision_at 同源),不落 Fill 默认的 _utcnow() 任务运行日(issue #205)
        fill = Fill(
            fill_id=f"BT-{next(self._id_counter)}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=executed_qty,
            price=slip_price,
            commission=commission,
            tax=tax,
            broker_order_id=order.broker_order_id,
            filled_at=market_close(trade_date),
        )
        self._fills.append(fill)

        # 更新订单状态
        if executed_qty >= state.remaining:
            order.status = OrderStatus.FILLED
            order.filled_quantity = (order.filled_quantity or Decimal("0")) + executed_qty
            order.average_fill_price = slip_price
            state.remaining = Decimal("0")
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            order.filled_quantity = (order.filled_quantity or Decimal("0")) + executed_qty
            order.average_fill_price = slip_price
            state.remaining -= executed_qty

        self._push_event(
            BrokerEventType.ORDER_FILLED,
            order,
            fill=fill,
        )

    def _apply_fill_to_position(
        self,
        *,
        symbol: Symbol,
        rule: AssetRule,
        side: Side,
        qty: Decimal,
        price: Decimal,
        trade_date: date,
        enforce_t_plus_1: bool,
    ) -> None:
        pos = self._positions.get(symbol.code)
        if pos is None:
            pos = _InternalPosition(symbol=symbol, rule=rule)
            self._positions[symbol.code] = pos

        if side is Side.BUY:
            available_date = (
                trade_date if not enforce_t_plus_1 else _next_day(trade_date)
            )
            # 加权平均价
            old_value = pos.total * pos.average_price
            new_value = qty * price
            total = pos.total + qty
            if total > 0:
                pos.average_price = (old_value + new_value) / total
            pos.lots.append(_PositionLot(quantity=qty, available_date=available_date))
            pos.total = total
            pos.refresh_available(trade_date)
        else:
            sold = pos.apply_sell(qty, trade_date=trade_date)
            if pos.total <= 0:
                pos.average_price = Decimal("0")
            del sold

    def _available_for(self, code: str, *, on_date: date) -> Decimal:
        pos = self._positions.get(code)
        if pos is None:
            return Decimal("0")
        pos.refresh_available(on_date)
        return pos.available

    def _is_suspended(self, bar: Bar, rule: AssetRule) -> bool:
        if not self._matching_model.enforce_suspension:
            return False
        pre_close = self._previous_closes.get(bar.symbol.code)
        return rule.is_suspended(bar, pre_close=pre_close)

    def _is_at_limit_blocked(self, side: Side, bar: Bar, rule: AssetRule) -> bool:
        if not self._matching_model.enforce_price_limit:
            return False
        pre_close = self._previous_closes.get(bar.symbol.code)
        # BUY 时涨停止买;SELL 时跌停止卖
        if side is Side.BUY:
            return rule.is_at_limit_up(bar, pre_close=pre_close)
        return rule.is_at_limit_down(bar, pre_close=pre_close)

    def _reject(
        self,
        *,
        order: Order,
        reason: RejectReason,
        message: str,
    ) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.reject_message = message
        self._push_event(BrokerEventType.ORDER_REJECTED, order)

    def _snapshot_position(self, code: str) -> Position:
        pos = self._positions[code]
        bar = self._current_bars.get(code)
        mv = Decimal("0")
        unrealized = Decimal("0")
        if bar and pos.total > 0:
            mv = pos.total * bar.close
            unrealized = mv - pos.total * pos.average_price
        return Position(
            account_id=self._account_id,
            symbol=pos.symbol,
            position_side="long",  # type: ignore[arg-type]
            total_quantity=pos.total,
            available_quantity=pos.available,
            frozen_quantity=Decimal("0"),
            average_price=pos.average_price,
            market_value=mv,
            unrealized_pnl=unrealized,
        )

    def _clone_order(self, order: Order) -> Order:
        return replace(order)

    def _push_event(
        self,
        event_type: BrokerEventType,
        order: Order,
        fill: Fill | None = None,
    ) -> None:
        self._event_queue.put_nowait(
            BrokerEvent(
                type=event_type,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                fill=fill,
                reject_reason=order.reject_reason,
                message=order.reject_message or "",
            )
        )


class _DefaultInstrumentResolver:
    """默认解析器:全部按 A 股股票处理(向后兼容)。

    新资产类型(issue #58)必须替换为显式 resolver,避免 fail-closed 被绕过。
    """

    def resolve(self, code: str) -> tuple[Market, InstrumentType]:
        upper = code.upper()
        if ".HK" in upper:
            return Market.HK, InstrumentType.STOCK
        if ".US" in upper:
            return Market.US, InstrumentType.STOCK
        # A 股股票 / ETF 都按 STOCK 解析,避免破坏现有回测
        return Market.A_SHARE, InstrumentType.STOCK


def _next_day(d: date) -> date:
    """T+1:日历日 +1;跨周末 / 跨节假日由交易日历统一处理。"""
    from datetime import timedelta

    return d + timedelta(days=1)


__all__ = ["BacktestBroker", "InstrumentResolver"]
