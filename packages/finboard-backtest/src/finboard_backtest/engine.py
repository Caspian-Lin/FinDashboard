"""``BacktestEngine`` —— 回测主引擎。

职责:

1. 从 ``HistoricalDataProvider`` 加载历史 Bar 数据;
2. 初始化 ``BacktestBroker`` + ``SimulatedClock`` + ``BacktestContext``;
3. 按时间顺序逐根 Bar 回放:
   a. 更新 broker 当前行情 + 撮合挂起的限价单;
   b. 推送 ``MarketDataEvent`` 到策略 ``on_market_data``;
   c. 策略下单 → broker 即时 / 延迟成交;
   d. 推送成交事件到策略 ``on_order_update``;
   e. 记录每日权益;
4. 计算绩效指标,返回 ``BacktestResult``。

策略代码在回测和实盘中**完全一致** —— 同样的 ``on_market_data`` / ``on_order_update`` 回调。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock
from finboard_backtest.config import BacktestConfig
from finboard_backtest.context import BacktestContext
from finboard_backtest.metrics import (
    annualized_return,
    buy_and_hold_return,
    max_drawdown,
    sharpe_ratio,
    total_commission,
    total_return,
    total_tax,
    turnover_ratio,
    win_rate,
)
from finboard_backtest.result import BacktestResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_core.strategy import OrderEvent, Strategy
from finboard_data.base import HistoricalDataProvider
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

logger = structlog.get_logger(__name__)

_KIND_MAP: dict[BrokerEventType, str] = {
    BrokerEventType.ORDER_ACCEPTED: "accepted",
    BrokerEventType.ORDER_FILLED: "filled",
    BrokerEventType.ORDER_CANCELLED: "cancelled",
    BrokerEventType.ORDER_REJECTED: "rejected",
}


class BacktestEngine:
    """回测引擎 —— 驱动策略在历史数据上运行。

    用法::

        engine = BacktestEngine(
            strategy=my_strategy,
            data_provider=akshare_provider,
            config=BacktestConfig(symbols=["510300.SH"], ...),
        )
        result = await engine.run()
        print(result.summary())
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        data_provider: HistoricalDataProvider,
        config: BacktestConfig,
    ) -> None:
        self._strategy = strategy
        self._provider = data_provider
        self._config = config

    async def run(self) -> BacktestResult:
        """执行回测,返回绩效报告。"""
        # 1. 加载历史数据
        bars_by_symbol = await self._load_data()

        # 2. 合并所有 Bar,按 timestamp 排序
        all_bars = self._merge_bars(bars_by_symbol)
        if not all_bars:
            logger.warning("backtest.no_data")
            return BacktestResult()

        logger.info(
            "backtest.starting",
            bars=len(all_bars),
            symbols=list(bars_by_symbol.keys()),
            start=str(all_bars[0].timestamp.date()),
            end=str(all_bars[-1].timestamp.date()),
        )

        # 3. 初始化组件
        cfg = self._config
        broker = BacktestBroker(
            initial_capital=cfg.initial_capital,
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            slippage_bps=cfg.slippage_bps,
            lot_size=cfg.lot_size,
            allow_short=cfg.allow_short,
            enforce_t_plus_1=cfg.enforce_t_plus_1,
        )
        await broker.connect(AccountId("backtest"), {})

        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=self._strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("backtest"),
        )

        # 4. 策略启动
        await self._strategy.on_start(ctx)  # type: ignore[arg-type]

        # 5. 逐 Bar 回放
        equity_curve: list[tuple[date, Decimal]] = []
        for bar in all_bars:
            clock.advance_to(bar.timestamp)
            broker.on_new_bar(bar)

            # 推送行情到策略
            market_event = MarketDataEvent(
                type=MarketDataEventType.BAR,
                bar=bar,
            )
            try:
                await self._strategy.on_market_data(market_event, ctx)  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "backtest.strategy_error",
                    bar_date=str(bar.timestamp.date()),
                )

            # 消费成交事件 → 通知策略
            for broker_event in await broker.drain_events():
                order_event = self._to_order_event(broker_event, broker)
                if order_event is not None:
                    try:
                        await self._strategy.on_order_update(
                            order_event, ctx  # type: ignore[arg-type]
                        )
                    except Exception:
                        logger.exception("backtest.on_order_update_error")

            # 记录每日权益
            equity_curve.append((bar.timestamp.date(), broker.total_equity()))

        logger.info("backtest.completed", bars=len(all_bars))

        # 6. 计算绩效
        return self._build_result(
            equity_curve=equity_curve,
            bars_by_symbol=bars_by_symbol,
            broker=broker,
        )

    async def _load_data(self) -> dict[str, list[Bar]]:
        """加载所有标的的历史 Bar 数据。"""
        cfg = self._config
        result: dict[str, list[Bar]] = {}
        for code in cfg.symbols:
            symbol = self._parse_symbol(code)
            bars = await self._provider.fetch_bars(
                symbol,
                BarPeriod.D1,
                cfg.start,
                cfg.end,
                adjust=cfg.adjust,
            )
            result[code] = bars
            logger.info("backtest.data_loaded", symbol=code, bars=len(bars))
        return result

    @staticmethod
    def _merge_bars(bars_by_symbol: dict[str, list[Bar]]) -> list[Bar]:
        """合并多标的 Bar,按 timestamp 排序。"""
        all_bars: list[Bar] = []
        for bars in bars_by_symbol.values():
            all_bars.extend(bars)
        all_bars.sort(key=lambda b: b.timestamp)
        return all_bars

    @staticmethod
    def _parse_symbol(code: str) -> Symbol:
        return Symbol(code=code.upper(), market=Market.A_SHARE)

    @staticmethod
    def _to_order_event(
        broker_event: BrokerEvent, broker: BacktestBroker
    ) -> OrderEvent | None:
        """将 BrokerEvent 转换为策略可见的 OrderEvent。"""
        kind = _KIND_MAP.get(broker_event.type)
        if kind is None or broker_event.client_order_id is None:
            return None

        order = broker.query_order_sync(str(broker_event.client_order_id))
        if order is None:
            return None

        return OrderEvent(
            order=order,
            kind=kind,
            fill=broker_event.fill,
            timestamp=datetime.now(UTC),
        )

    def _build_result(
        self,
        *,
        equity_curve: list[tuple[date, Decimal]],
        bars_by_symbol: dict[str, list[Bar]],
        broker: BacktestBroker,
    ) -> BacktestResult:
        fills = broker.fills
        orders = broker.all_orders

        # 买入持有基准(用第一个标的)
        benchmark_curve: list[tuple[date, Decimal]] = []
        first_symbol_bars = next(iter(bars_by_symbol.values()), [])
        if first_symbol_bars:
            bar_prices = [(b.timestamp.date(), b.close) for b in first_symbol_bars]
            benchmark_curve = buy_and_hold_return(
                bar_prices, self._config.initial_capital
            )

        # 绩效指标
        ret = total_return(equity_curve)
        ann_ret = annualized_return(equity_curve)
        sharpe = sharpe_ratio(equity_curve)
        mdd = max_drawdown(equity_curve)
        wr = win_rate(fills)
        comm = total_commission(fills)
        tax = total_tax(fills)
        turn = turnover_ratio(fills, equity_curve)
        bench_ret = total_return(benchmark_curve) if benchmark_curve else 0.0

        return BacktestResult(
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            fills=fills,
            orders=orders,
            total_return=ret,
            annualized_return=ann_ret,
            sharpe_ratio=sharpe,
            max_drawdown=mdd,
            win_rate=wr,
            trade_count=len(fills),
            turnover=turn,
            commission_paid=comm,
            stamp_tax_paid=tax,
            benchmark_return=bench_ret,
            excess_return=ret - bench_ret,
            start_date=equity_curve[0][0] if equity_curve else None,
            end_date=equity_curve[-1][0] if equity_curve else None,
            initial_capital=self._config.initial_capital,
            final_equity=equity_curve[-1][1]
            if equity_curve
            else self._config.initial_capital,
        )
