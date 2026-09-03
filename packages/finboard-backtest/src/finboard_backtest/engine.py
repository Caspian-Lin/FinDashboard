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
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from typing import Any

import structlog

from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock, market_close
from finboard_backtest.config import BacktestConfig
from finboard_backtest.context import BacktestContext
from finboard_backtest.metrics import (
    annualized_return,
    buy_and_hold_return,
    equal_weight_selection_pool_return,
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
from finboard_data.cache import ParquetReadJobStats, collect_parquet_read_stats
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


@dataclass(slots=True)
class _ReplayState:
    """逐日回放的跨日可变状态(抽取自 ``run`` 本地变量,issue #286)。

    回放段经 ``asyncio.to_thread`` 在工作线程执行;这些容器由该线程独占读写,
    事件循环线程在 ``to_thread`` 返回后才读取,无并发竞争。
    """

    equity_by_date: dict[date, Decimal]
    history_by_symbol: dict[str, list[Bar]]
    selection_snapshots: list[FactorSnapshot]
    active_symbols: set[str]
    selection_pool_ever_active: bool
    pending_snapshot: FactorSnapshot | None


class _EventLoopBridge:
    """工作线程同步等待事件循环 awaitable 的桥(issue #286)。

    回放段经 ``asyncio.to_thread`` 卸载后,段内的 async 调用分两类:

    * **快路径** —— 「同步实现被当协程 await」的纯计算协程(策略回调 /
      BacktestBroker 查询 / 成交事件排水):在当前工作线程 ``send`` 内联驱动到
      完成,零跨线程往返;99% 的调用走这条路径。
    * **慢路径** —— 发生真实 await 的协程(如 factor_selector 的 AsyncSession
      数据库读取):放弃半执行的副本,把完整协程投递回事件循环线程执行并阻塞
      等待结果。事件循环线程因此始终空闲可跑心跳 / 维护 / 其他 job 的 IO 段,
      且 session 只被事件循环线程触碰。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def call(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """在调用方线程同步执行 ``factory()`` 产出的协程,返回其结果。

        入参 / 返回值用具体化 ``Any``:调用点遍布策略回调(签名含 ``# type:
        ignore``)与 broker 查询,泛型版本会让 mypy 无法推断 lambda 形参类型。
        """

        coro = factory()
        try:
            coro.send(None)
        except StopIteration as stop:
            # 协程体内部抛出的 StopIteration 已被 PEP 479 转为 RuntimeError,
            # 这里捕获到的只能是协程正常返回。
            return stop.value
        except RuntimeError:
            # 协程体依赖「运行中的事件循环」(get_running_loop / create_task 等):
            # 不让线程内联执行变成任务失败,回事件循环线程重跑完整协程。
            coro.close()
            return asyncio.run_coroutine_threadsafe(factory(), self._loop).result()
        except BaseException:
            coro.close()
            raise
        # send 返回说明协程让出了控制权(真实 await):丢弃半执行的副本,
        # 在事件循环线程重跑。纯计算副本无外部副作用,重跑安全。
        coro.close()
        return asyncio.run_coroutine_threadsafe(factory(), self._loop).result()


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
        run_started = time.monotonic()
        load_started = time.monotonic()
        # parquet 读取聚合只包裹加载段(issue #285):回放/撮合段无缓存读取,
        # 段内聚合即数据加载 IO 的画像,回答「慢在 IO 还是计算」。
        with collect_parquet_read_stats() as parquet_stats:
            # 1. 加载历史数据
            bars_by_symbol = await self._load_data()
            # 1b. 显式基准不在回测 universe 时单独拉取(issue #184)
            benchmark_bars = await self._load_benchmark_bars(bars_by_symbol)
        data_load_elapsed = time.monotonic() - load_started

        # 2. 合并所有 Bar,按交易日批处理
        all_bars = self._merge_bars(bars_by_symbol)
        if not all_bars:
            no_data_timing = self._build_timing(
                run_started=run_started,
                data_load_elapsed=data_load_elapsed,
                parquet_stats=parquet_stats,
            )
            logger.warning("backtest.no_data", **no_data_timing)
            return BacktestResult(timing=no_data_timing)

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
        # CPU 密集段逐日经 asyncio.to_thread 卸载(issue #286):逐日纯计算
        # 不再阻塞事件循环线程 —— worker 心跳续约 / 维护循环 / 多 job 的 IO 段
        # 可交错,单段长计算不再把 lease 拖到被误回收。跨日状态收拢进
        # _ReplayState,由工作线程独占读写;段内 async 调用经 _EventLoopBridge
        # (纯计算快路径内联,数据库慢路径回事件循环线程)。
        daily_bars = self._group_bars_by_date(all_bars)
        bridge = _EventLoopBridge(asyncio.get_running_loop())
        state = _ReplayState(
            equity_by_date={},
            history_by_symbol=defaultdict(list),
            selection_snapshots=[],
            active_symbols=set(cfg.symbols) if not cfg.selection.enabled else set(),
            selection_pool_ever_active=bool(cfg.symbols) and not cfg.selection.enabled,
            pending_snapshot=None,
        )

        for index, (business_date, bars) in enumerate(daily_bars):
            next_business_date = (
                daily_bars[index + 1][0] if index + 1 < len(daily_bars) else None
            )
            await asyncio.to_thread(
                self._replay_one_day,
                bridge=bridge,
                state=state,
                cfg=cfg,
                broker=broker,
                clock=clock,
                ctx=ctx,
                business_date=business_date,
                bars=bars,
                next_business_date=next_business_date,
            )

        # dict → 按日期排序的 list
        equity_curve = sorted(state.equity_by_date.items())

        logger.info("backtest.completed", bars=len(all_bars))

        # issue #255:选股启用时归档逐期选股诊断,杜绝「整期 SKIPPED →
        # 0 交易成功」的假象(runs 273-275)。
        selection_diagnostics = self._build_selection_diagnostics(
            state.selection_snapshots, state.selection_pool_ever_active
        )

        # job 级分段耗时(issue #285):进 result payload 与 structlog。
        timing = self._build_timing(
            run_started=run_started,
            data_load_elapsed=data_load_elapsed,
            parquet_stats=parquet_stats,
        )

        # 6. 计算绩效
        result = self._build_result(
            equity_curve=equity_curve,
            bars_by_symbol=bars_by_symbol,
            benchmark_bars=benchmark_bars,
            broker=broker,
            selection_snapshots=state.selection_snapshots,
            selection_diagnostics=selection_diagnostics,
            timing=timing,
        )
        logger.info("backtest.timing", bars=len(all_bars), **timing)
        return result

    @staticmethod
    def _build_timing(
        *,
        run_started: float,
        data_load_elapsed: float,
        parquet_stats: ParquetReadJobStats,
    ) -> dict[str, object]:
        """聚合 job 级分段耗时(issue #285,纯可观测性,不参与任何 checksum)。"""

        return {
            "total_elapsed_seconds": round(time.monotonic() - run_started, 3),
            "data_load_elapsed_seconds": round(data_load_elapsed, 3),
            "parquet_reads": parquet_stats.as_dict(),
        }

    def _replay_one_day(
        self,
        *,
        bridge: _EventLoopBridge,
        state: _ReplayState,
        cfg: BacktestConfig,
        broker: BacktestBroker,
        clock: SimulatedClock,
        ctx: BacktestContext,
        business_date: date,
        bars: list[Bar],
        next_business_date: date | None,
    ) -> None:
        """单个交易日的回放(纯 CPU 密集段,在 to_thread 工作线程内执行,#286)。

        策略回调 / broker 查询 / 成交事件排水是「同步实现被当协程 await」,
        经 bridge 快路径在本线程内联驱动;factor_selector 的数据库读取经慢路径
        投递回事件循环线程 —— AsyncSession 始终只被事件循环线程触碰。
        """

        clock.advance_to(max(bar.timestamp for bar in bars))
        for bar in bars:
            broker.on_new_bar(bar)
            state.history_by_symbol[bar.symbol.code].append(bar)

        pending_snapshot = state.pending_snapshot
        if pending_snapshot is not None and pending_snapshot.effective_date == business_date:
            if pending_snapshot.status is FactorSnapshotStatus.PUBLISHED:
                state.active_symbols = set(pending_snapshot.selected_symbols)
                if state.active_symbols:
                    state.selection_pool_ever_active = True
            # mypy 不把外层 None 收窄传播进闭包:用收窄后的新名字供 lambda 捕获。
            snapshot: FactorSnapshot = pending_snapshot
            bridge.call(
                lambda: self._notify_selection(
                    snapshot,
                    active_symbols=state.active_symbols,
                    ctx=ctx,
                )
            )

        held_symbols = {
            position.symbol.code for position in bridge.call(broker.query_positions)
        }
        dispatch_symbols = (
            state.active_symbols | held_symbols
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
                # partial 而非闭包:market_event 是循环变量,B023 零延迟绑定
                # 噪音没有意义 —— bridge.call 立即同步执行该工厂。ctx 与实盘
                # StrategyContext 同形(回测桩),沿用原有 ignore。
                bridge.call(
                    partial(self._strategy.on_market_data, market_event, ctx)  # type: ignore[arg-type]
                )
            except Exception:
                logger.exception(
                    "backtest.strategy_error",
                    bar_date=str(business_date),
                    symbol=bar.symbol.code,
                )
            bridge.call(lambda: self._drain_order_events(broker, ctx))

        # 没有入选标的时也必须投递由挂单撮合产生的订单回报。
        bridge.call(lambda: self._drain_order_events(broker, ctx))
        state.equity_by_date[business_date] = broker.total_equity()

        if cfg.selection.enabled and next_business_date is not None:
            assert self._factor_selector is not None
            selector = self._factor_selector
            pending_snapshot = bridge.call(
                lambda: selector.select(
                    config=cfg.selection,
                    static_universe=cfg.symbols,
                    business_date=business_date,
                    decision_at=market_close(business_date),
                    effective_date=next_business_date,
                    price_history=state.history_by_symbol,
                )
            )
            state.pending_snapshot = pending_snapshot
            state.selection_snapshots.append(pending_snapshot)

    def _build_selection_diagnostics(
        self,
        selection_snapshots: list[FactorSnapshot],
        selection_pool_ever_active: bool,
    ) -> dict[str, object] | None:
        """选股启用的 run 附带逐期诊断;整期无候选时打具名 warning(#255)。"""
        if not self._config.selection.enabled:
            return None
        skip_reasons: Counter[str] = Counter(
            snapshot.skip_reason or "unknown"
            for snapshot in selection_snapshots
            if snapshot.status is FactorSnapshotStatus.SKIPPED
        )
        published = sum(
            1
            for snapshot in selection_snapshots
            if snapshot.status is FactorSnapshotStatus.PUBLISHED
        )
        diagnostics: dict[str, object] = {
            "total_snapshots": len(selection_snapshots),
            "published_snapshots": published,
            "skipped_snapshots": len(selection_snapshots) - published,
            "skip_reasons": dict(
                sorted(skip_reasons.items(), key=lambda item: (-item[1], item[0]))
            ),
            "selection_pool_ever_active": selection_pool_ever_active,
        }
        if not selection_pool_ever_active:
            diagnostics["zero_trading_suspected"] = True
            reasons = (
                "、".join(f"{reason}={count}" for reason, count in sorted(skip_reasons.items()))
                or "无(无快照)"
            )
            logger.warning(
                "backtest.selection_pool_never_active",
                total_snapshots=len(selection_snapshots),
                published_snapshots=published,
                skip_reasons=reasons,
                message=(
                    "选股启用但整期无任何候选生效:本 run 大概率 0 交易;"
                    "skip 原因统计见 selection_diagnostics"
                ),
            )
        return diagnostics

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
        selection_diagnostics: dict[str, object] | None = None,
        timing: dict[str, object] | None = None,
    ) -> BacktestResult:
        fills = broker.fills
        orders = broker.all_orders

        # 基准回退链(issue #254):显式标的 > 每期选股池等权(选股启用时) >
        # 静态候选池等权 > 首个标的。选股启用时旧逻辑用静态 cfg.symbols 全池
        # 或首标的兜底,基准几乎必然不在当日候选池内、口径失真;现在优先跟随
        # 每期选股结果(动态等权),回退来源记录在 result.benchmark_source 并
        # 打日志,report 可见。
        benchmark_curve: list[tuple[date, Decimal]] = []
        benchmark_source: str | None = None
        bench_cfg = self._config.benchmark
        if bench_cfg.symbol is not None:
            bench_bars = benchmark_bars.get(bench_cfg.symbol, [])
            if bench_bars:
                bar_prices = [(b.timestamp.date(), b.close) for b in bench_bars]
                benchmark_curve = buy_and_hold_return(
                    bar_prices, self._config.initial_capital
                )
                benchmark_source = f"explicit_symbol:{bench_cfg.symbol}"
        else:
            published_periods = [
                (snapshot.effective_date, snapshot.selected_symbols)
                for snapshot in selection_snapshots
                if snapshot.status is FactorSnapshotStatus.PUBLISHED
                and snapshot.selected_symbols
            ]
            if published_periods:
                benchmark_curve = equal_weight_selection_pool_return(
                    bars_by_symbol,
                    published_periods,
                    self._config.initial_capital,
                )
                if benchmark_curve:
                    benchmark_source = "equal_weight_selection_pool"
            if not benchmark_curve and bench_cfg.equal_weight_universe and len(bars_by_symbol) > 1:
                benchmark_curve = equal_weight_universe_return(
                    bars_by_symbol, self._config.initial_capital
                )
                if benchmark_curve:
                    benchmark_source = "equal_weight_static_pool"
            if not benchmark_curve:
                first_symbol_bars = next(iter(bars_by_symbol.values()), [])
                if first_symbol_bars:
                    bar_prices = [
                        (b.timestamp.date(), b.close) for b in first_symbol_bars
                    ]
                    benchmark_curve = buy_and_hold_return(
                        bar_prices, self._config.initial_capital
                    )
                    benchmark_source = "first_symbol"
        if benchmark_source is not None:
            logger.info(
                "backtest.benchmark_source",
                source=benchmark_source,
                points=len(benchmark_curve),
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
            benchmark_source=benchmark_source,
            selection_diagnostics=selection_diagnostics,
            timing=timing,
        )
