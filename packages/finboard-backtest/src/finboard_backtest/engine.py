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

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock, market_close
from finboard_backtest.config import BacktestConfig
from finboard_backtest.context import BacktestContext
from finboard_backtest.metrics import (
    annualized_return,
    buy_and_hold_return,
    equal_weight_universe_return,
    max_drawdown,
    sharpe_ratio,
    sharpe_ratio_rf0,
    total_commission,
    total_return,
    total_tax,
    turnover_ratio,
    win_rate,
)
from finboard_backtest.result import BacktestResult
from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_core.strategy import OrderEvent, Strategy, UniverseSelectionEvent
from finboard_data.base import HistoricalDataProvider
from finboard_data.factors import FactorSnapshot, FactorSnapshotStatus
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

logger = structlog.get_logger(__name__)

_LOAD_CHUNK = 500  # 每批并发加载的标的数

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
        factor_selector: PointInTimeFactorSelector | None = None,
    ) -> None:
        self._strategy = strategy
        self._provider = data_provider
        self._config = config
        self._factor_selector = factor_selector

    async def run(self) -> BacktestResult:
        """执行回测,返回绩效报告。"""
        # 1. 加载历史数据
        bars_by_symbol = await self._load_data()
        # 1b. 显式基准不在回测 universe 时单独拉取(issue #184)
        benchmark_bars = await self._load_benchmark_bars(bars_by_symbol)

        # 2. 合并所有 Bar,按交易日批处理
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
            matching_model=cfg.matching_model,
            asset_rules=cfg.asset_rules,
            commission_rate=cfg.resolved_fees().commission_rate,
            commission_min=cfg.resolved_fees().commission_min,
            stamp_tax_rate=cfg.resolved_fees().stamp_tax_rate,
            slippage_bps=cfg.resolved_fees().slippage_bps,
            allow_short=cfg.allow_short,
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

        if cfg.selection.enabled and self._factor_selector is None:
            raise ValueError("启用因子选股时必须提供 factor_selector")

        # 5. 按日回放:先一次性更新全部标的行情,再派发策略回调。
        equity_by_date: dict[date, Decimal] = {}
        history_by_symbol: dict[str, list[Bar]] = defaultdict(list)
        selection_snapshots: list[FactorSnapshot] = []
        active_symbols: set[str] = set(cfg.symbols) if not cfg.selection.enabled else set()
        pending_snapshot: FactorSnapshot | None = None
        daily_bars = self._group_bars_by_date(all_bars)

        for index, (business_date, bars) in enumerate(daily_bars):
            clock.advance_to(max(bar.timestamp for bar in bars))
            for bar in bars:
                broker.on_new_bar(bar)
                history_by_symbol[bar.symbol.code].append(bar)

            if pending_snapshot is not None and pending_snapshot.effective_date == business_date:
                if pending_snapshot.status is FactorSnapshotStatus.PUBLISHED:
                    active_symbols = set(pending_snapshot.selected_symbols)
                await self._notify_selection(
                    pending_snapshot,
                    active_symbols=active_symbols,
                    ctx=ctx,
                )

            held_symbols = {position.symbol.code for position in await broker.query_positions()}
            dispatch_symbols = (
                active_symbols | held_symbols
                if cfg.selection.enabled
                else {bar.symbol.code for bar in bars}
            )
            for bar in bars:
                if bar.symbol.code not in dispatch_symbols:
                    continue
                market_event = MarketDataEvent(
                    type=MarketDataEventType.BAR,
                    bar=bar,
                )
                try:
                    await self._strategy.on_market_data(
                        market_event,
                        ctx,  # type: ignore[arg-type]
                    )
                except Exception:
                    logger.exception(
                        "backtest.strategy_error",
                        bar_date=str(business_date),
                        symbol=bar.symbol.code,
                    )
                await self._drain_order_events(broker, ctx)

            # 没有入选标的时也必须投递由挂单撮合产生的订单回报。
            await self._drain_order_events(broker, ctx)
            equity_by_date[business_date] = broker.total_equity()

            if cfg.selection.enabled and index + 1 < len(daily_bars):
                assert self._factor_selector is not None
                next_date = daily_bars[index + 1][0]
                pending_snapshot = await self._factor_selector.select(
                    config=cfg.selection,
                    static_universe=cfg.symbols,
                    business_date=business_date,
                    decision_at=market_close(business_date),
                    effective_date=next_date,
                    price_history=history_by_symbol,
                )
                selection_snapshots.append(pending_snapshot)

        # dict → 按日期排序的 list
        equity_curve = sorted(equity_by_date.items())

        logger.info("backtest.completed", bars=len(all_bars))

        # 6. 计算绩效
        return self._build_result(
            equity_curve=equity_curve,
            bars_by_symbol=bars_by_symbol,
            benchmark_bars=benchmark_bars,
            broker=broker,
            selection_snapshots=selection_snapshots,
        )

    async def _load_benchmark_bars(
        self,
        bars_by_symbol: dict[str, list[Bar]],
    ) -> dict[str, list[Bar]]:
        """显式基准标的单独拉取(不在回测 universe 时,issue #184)。

        基准曲线用买入持有口径,不进入策略 / 撮合;universe 已含该标的时
        直接复用,避免重复拉取;拉取失败或空数据返回空 dict,由
        ``_build_result`` 落 null + warning。
        """
        bench_cfg = self._config.benchmark
        if bench_cfg.symbol is None:
            return {}
        if bench_cfg.symbol in bars_by_symbol:
            return {bench_cfg.symbol: bars_by_symbol[bench_cfg.symbol]}
        symbol = self._parse_symbol(bench_cfg.symbol)
        try:
            bars = await self._provider.fetch_bars(
                symbol,
                BarPeriod.D1,
                self._config.start,
                self._config.end,
                adjust=self._config.adjust,
            )
        except Exception as exc:
            logger.error(
                "backtest.benchmark_fetch_failed",
                symbol=bench_cfg.symbol,
                error=str(exc),
            )
            return {}
        return {bench_cfg.symbol: bars} if bars else {}

    async def _load_data(self) -> dict[str, list[Bar]]:
        """并发加载所有标的的历史 Bar 数据。

        分 chunk 并发拉取(每 chunk 最多 ``_LOAD_CHUNK`` 个标的),
        AkShareProvider 内部的信号量负责网络限流,缓存命中时全并发。
        """
        cfg = self._config
        symbols = [self._parse_symbol(code) for code in cfg.symbols]
        result: dict[str, list[Bar]] = {}

        chunk_size = _LOAD_CHUNK
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            tasks = [
                self._provider.fetch_bars(
                    sym,
                    BarPeriod.D1,
                    cfg.start,
                    cfg.end,
                    adjust=cfg.adjust,
                )
                for sym in chunk
            ]
            batch = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, bars in zip(chunk, batch, strict=True):
                if isinstance(bars, BaseException):
                    logger.error(
                        "backtest.data_load_failed",
                        symbol=sym.code,
                        error=str(bars),
                    )
                    result[sym.code] = []
                else:
                    result[sym.code] = bars
            loaded = sum(1 for v in result.values() if v)
            logger.info(
                "backtest.data_progress",
                loaded=loaded,
                total=len(symbols),
            )

        return result

    @staticmethod
    def _merge_bars(bars_by_symbol: dict[str, list[Bar]]) -> list[Bar]:
        """合并多标的 Bar,按 timestamp 排序。"""
        all_bars: list[Bar] = []
        for bars in bars_by_symbol.values():
            all_bars.extend(bars)
        all_bars.sort(key=lambda bar: (bar.timestamp, bar.symbol.code))
        return all_bars

    @staticmethod
    def _group_bars_by_date(
        all_bars: list[Bar],
    ) -> list[tuple[date, list[Bar]]]:
        grouped: dict[date, list[Bar]] = defaultdict(list)
        for bar in all_bars:
            grouped[bar.timestamp.date()].append(bar)
        return [
            (
                business_date,
                sorted(grouped[business_date], key=lambda bar: bar.symbol.code),
            )
            for business_date in sorted(grouped)
        ]

    @staticmethod
    def _parse_symbol(code: str) -> Symbol:
        return Symbol(code=code.upper(), market=Market.A_SHARE)

    @staticmethod
    def _to_order_event(broker_event: BrokerEvent, broker: BacktestBroker) -> OrderEvent | None:
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

    async def _drain_order_events(
        self,
        broker: BacktestBroker,
        ctx: BacktestContext,
    ) -> None:
        for broker_event in await broker.drain_events():
            order_event = self._to_order_event(broker_event, broker)
            if order_event is None:
                continue
            try:
                await self._strategy.on_order_update(
                    order_event,
                    ctx,  # type: ignore[arg-type]
                )
            except Exception:
                logger.exception("backtest.on_order_update_error")

    async def _notify_selection(
        self,
        snapshot: FactorSnapshot,
        *,
        active_symbols: set[str],
        ctx: BacktestContext,
    ) -> None:
        event = UniverseSelectionEvent(
            decision_at=snapshot.decision_at,
            effective_date=snapshot.effective_date,
            static_universe=snapshot.static_universe,
            selected_symbols=tuple(sorted(active_symbols)),
            dataset_versions=snapshot.dataset_versions,
            factor_version=snapshot.factor_version,
            status=snapshot.status.value,
            skip_reason=snapshot.skip_reason,
            snapshot_id=snapshot.snapshot_id,
        )
        try:
            await self._strategy.on_universe_selection(
                event,
                ctx,  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception(
                "backtest.on_universe_selection_error",
                effective_date=str(snapshot.effective_date),
            )

    def _build_result(
        self,
        *,
        equity_curve: list[tuple[date, Decimal]],
        bars_by_symbol: dict[str, list[Bar]],
        benchmark_bars: dict[str, list[Bar]],
        broker: BacktestBroker,
        selection_snapshots: list[FactorSnapshot],
    ) -> BacktestResult:
        fills = broker.fills
        orders = broker.all_orders

        # 基准:显式选择 > 等权候选池 > 第一个标的(向后兼容)
        benchmark_curve: list[tuple[date, Decimal]] = []
        bench_cfg = self._config.benchmark
        if bench_cfg.symbol is not None:
            bench_bars = benchmark_bars.get(bench_cfg.symbol, [])
            if bench_bars:
                bar_prices = [(b.timestamp.date(), b.close) for b in bench_bars]
                benchmark_curve = buy_and_hold_return(
                    bar_prices, self._config.initial_capital
                )
        elif bench_cfg.equal_weight_universe and len(bars_by_symbol) > 1:
            benchmark_curve = equal_weight_universe_return(
                bars_by_symbol, self._config.initial_capital
            )
        else:
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
        # issue #262:rf=0 对照口径随报告序列化,与 research_run 报告同屏可比。
        sharpe_rf0 = sharpe_ratio_rf0(equity_curve)
        mdd = max_drawdown(equity_curve)
        wr = win_rate(fills)
        comm = total_commission(fills)
        tax = total_tax(fills)
        turn = turnover_ratio(fills, equity_curve)
        # 基准缺失 → null + warning(issue #184),禁止静默 0.0
        if len(benchmark_curve) >= 2:
            bench_ret: float | None = total_return(benchmark_curve)
        else:
            bench_ret = None
            reason = (
                "no_bars_in_universe_or_fetch"
                if bench_cfg.symbol is not None
                else "empty_universe"
            )
            logger.warning(
                "backtest.benchmark_missing",
                symbol=bench_cfg.symbol,
                reason=reason,
            )
        excess: float | None = ret - bench_ret if bench_ret is not None else None
        dataset_versions: dict[str, set[str]] = defaultdict(set)
        for snapshot in selection_snapshots:
            for dataset, version in snapshot.dataset_versions.items():
                dataset_versions[dataset].add(version)

        return BacktestResult(
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            fills=fills,
            orders=orders,
            selection_snapshots=selection_snapshots,
            total_return=ret,
            annualized_return=ann_ret,
            sharpe_ratio=sharpe,
            sharpe_rf0=sharpe_rf0,
            max_drawdown=mdd,
            win_rate=wr,
            trade_count=len(fills),
            turnover=turn,
            commission_paid=comm,
            stamp_tax_paid=tax,
            benchmark_return=bench_ret,
            excess_return=excess,
            start_date=equity_curve[0][0] if equity_curve else None,
            end_date=equity_curve[-1][0] if equity_curve else None,
            initial_capital=self._config.initial_capital,
            final_equity=equity_curve[-1][1] if equity_curve else self._config.initial_capital,
            dataset_versions={
                dataset: sorted(versions)
                for dataset, versions in sorted(dataset_versions.items())
            },
            factor_version=self._config.selection.factor_version
            if self._config.selection.enabled
            else None,
            matching_model=self._config.matching_model.as_dict(),
            asset_rules=(
                self._config.asset_rules.as_dict()
                if self._config.asset_rules is not None
                else None
            ),
            fee_assumptions=self._config.resolved_fees().as_dict(),
            benchmark_config=self._config.benchmark.as_dict(),
        )
