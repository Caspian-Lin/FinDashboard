"""``finboard`` CLI 入口。

子命令:

* ``run``          —— 启动交易内核,运行直至 Ctrl-C;
* ``reconcile``    —— 执行一次本地 ↔ 券商核对并打印报告;
* ``migrate``      —— 应用 alembic 迁移(``alembic upgrade head``);
* ``kill-switch``  —— 激活 Kill Switch(off / no_new_orders / reduce_only / cancel_all / halt);
* ``scheduler``    —— 列出 / 手动触发定时任务。

注意:进程内的 TradingKernel 在整个生命周期内复用同一个 AsyncSession。
若日后改为多账户/多策略,需要替换为 session-per-operation。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import sys
from datetime import date, timedelta
from datetime import date as parse_date
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

import typer

from finboard_app.bootstrap import build_kernel_components
from finboard_app.config import Settings, load_settings
from finboard_app.logging import setup_logging
from finboard_shared.identifiers import AccountId
from finboard_shared.types import KillSwitchLevel

if TYPE_CHECKING:
    from finboard_app.bootstrap import KernelComponents
    from finboard_reconcile import ReconciliationReport
    from finboard_scheduler import Scheduler

app = typer.Typer(
    name="finboard",
    help="生产级量化交易系统 CLI(P0:打通端到端真实交易链路)",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main(
    ctx: typer.Context,
    env_file: Annotated[
        str | None,
        typer.Option("--env-file", help="指定 .env 文件路径(默认 ./.env)"),
    ] = None,
) -> None:
    settings = load_settings(env_file=env_file)
    ctx.obj = settings


@app.command()
def run(
    ctx: typer.Context,
    strategies_file: Annotated[
        str | None,
        typer.Option(
            "--strategies",
            "-s",
            help="策略配置 YAML 文件路径(不指定则不加载策略)",
        ),
    ] = None,
    timer_interval: Annotated[
        float,
        typer.Option(
            "--timer-interval",
            help="策略定时回调间隔(秒,默认 60)",
        ),
    ] = 60.0,
    no_scheduler: Annotated[
        bool,
        typer.Option(
            "--no-scheduler",
            help="禁用定时任务调度(盘前检查/收盘撤单/日终核对/心跳)",
        ),
    ] = False,
) -> None:
    """启动交易内核。"""
    asyncio.run(_run_kernel(ctx.obj, strategies_file, timer_interval, no_scheduler))


@app.command()
def reconcile(ctx: typer.Context) -> None:
    """执行一次本地 ↔ 券商核对。"""
    rc = asyncio.run(_run_reconcile(ctx.obj))
    typer.echo(rc.summary())
    if not rc.ok:
        sys.exit(1)


@app.command()
def migrate() -> None:
    """应用 alembic 迁移到最新版本。"""
    raise typer.Exit(subprocess.call(["alembic", "upgrade", "head"]))


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", "-h", help="监听地址")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p", help="监听端口")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="热重载(开发)")] = False,
) -> None:
    """启动 FastAPI 交易控制台后端(含 WebSocket)。"""
    import uvicorn

    if reload:
        uvicorn.run(
            "finboard_api.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
        )
    else:
        from finboard_api.app import create_app

        app = create_app(ctx.obj)
        uvicorn.run(app, host=host, port=port)


@app.command(name="kill-switch")
def kill_switch(
    ctx: typer.Context,
    level: Annotated[
        KillSwitchLevel,
        typer.Argument(
            help=(
                "Kill Switch 级别: off / no_new_orders / reduce_only / cancel_all / halt"
            ),
            case_sensitive=False,
        ),
    ],
    reason: Annotated[str, typer.Option("--reason", "-r")] = "manual",
) -> None:
    """激活 Kill Switch。

    说明:P0 阶段交易内核是单进程,本命令仅在内核进程未运行时打印动作建议;
    真正的 Kill Switch 写入由运行中的 ``run`` 进程经信号/API 接收完成。
    """
    typer.echo(
        f"请求激活 Kill Switch: level={level.value} reason={reason}。"
        f" 若交易内核在另一进程运行,请通过信号 / API 转发(P0 暂未实现)。"
        f" 若要下次启动即生效,在 .env 中设置 FINBOARD_KILL_SWITCH_INITIAL={level.value}。"
    )


scheduler_app = typer.Typer(
    name="scheduler",
    help="定时任务管理(列出 / 手动触发)。",
    no_args_is_help=True,
)


@scheduler_app.command(name="list")
def scheduler_list(ctx: typer.Context) -> None:
    """列出已注册的定时任务。"""
    asyncio.run(_scheduler_list(ctx.obj))


async def _scheduler_list(settings: Settings) -> None:
    from finboard_persistence import AuditLogRepository
    from finboard_scheduler import Scheduler, TradingCalendar, create_default_tasks

    components = build_kernel_components(settings)
    async with components.session_maker() as session:
        kernel = components.new_kernel(session)
        reconciler = components.new_reconciler(session)
        audit_repo = AuditLogRepository(session)
        tasks = create_default_tasks(
            kernel=kernel,
            order_manager=kernel.order_manager,
            reconciler=reconciler,
            audit_repo=audit_repo,
            account_id=AccountId(settings.account_id),
        )
        cal = TradingCalendar()
        sched = Scheduler(cal)
        for t in tasks:
            sched.schedule(t)

        for t in sched.list_tasks():
            if t.interval is not None:
                trigger = f"interval={t.interval}s"
            else:
                assert t.time is not None
                trigger = f"time={t.time.strftime('%H:%M')}"
            typer.echo(
                f"  {t.name:<25s} {trigger:<20s} trading_days_only={t.trading_days_only}"
            )


@scheduler_app.command(name="trigger")
def scheduler_trigger(
    ctx: typer.Context,
    task_name: Annotated[
        str,
        typer.Argument(help="任务名称(如 heartbeat / close_cancel / end_of_day_reconcile)"),
    ],
) -> None:
    """手动触发一次定时任务。"""
    settings = ctx.obj
    components = build_kernel_components(settings)
    asyncio.run(_trigger_task(components, settings, task_name))


async def _trigger_task(
    components: KernelComponents,
    settings: Settings,
    task_name: str,
) -> None:
    from finboard_persistence import AuditLogRepository
    from finboard_scheduler import Scheduler, TradingCalendar, create_default_tasks

    async with components.session_maker() as session:
        kernel = components.new_kernel(session)
        reconciler = components.new_reconciler(session)
        audit_repo = AuditLogRepository(session)
        tasks = create_default_tasks(
            kernel=kernel,
            order_manager=kernel.order_manager,
            reconciler=reconciler,
            audit_repo=audit_repo,
            account_id=AccountId(settings.account_id),
        )
        cal = TradingCalendar()
        sched = Scheduler(cal)
        for t in tasks:
            sched.schedule(t)

        task = sched.get_task(task_name)
        if task is None:
            typer.echo(f"未知任务: {task_name}", err=True)
            sys.exit(1)

        typer.echo(f"触发任务: {task_name}")
        await sched.trigger(task_name)
        typer.echo(f"完成: {task_name}")


app.add_typer(scheduler_app, name="scheduler")


# ---------------------------------------------------------------------------
# backtest 子命令
# ---------------------------------------------------------------------------
backtest_app = typer.Typer(
    name="backtest",
    help="回测引擎 — 历史数据回放 + 纸面撮合 + 绩效分析",
    no_args_is_help=True,
)


@backtest_app.command(name="run")
def backtest_run(
    strategy: Annotated[
        str,
        typer.Option("--strategy", "-s", help="策略类型(如 ma_cross)"),
    ],
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="标的代码,逗号分隔(如 510300.SH)"),
    ],
    start: Annotated[
        str,
        typer.Option("--start", help="开始日期 YYYY-MM-DD"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", help="结束日期 YYYY-MM-DD"),
    ],
    capital: Annotated[
        str,
        typer.Option("--capital", help="初始资金(默认 100000)"),
    ] = "100000",
    short_window: Annotated[
        int,
        typer.Option("--short", help="短期均线周期(ma_cross)"),
    ] = 5,
    long_window: Annotated[
        int,
        typer.Option("--long", help="长期均线周期(ma_cross)"),
    ] = 20,
    adjust: Annotated[
        str,
        typer.Option("--adjust", help="复权方式(qfq/hqfq/none)"),
    ] = "qfq",
) -> None:
    """运行回测并打印绩效报告。"""
    from datetime import date as parse_date
    from decimal import Decimal

    asyncio.run(
        _run_backtest(
            strategy=strategy,
            symbols=[s.strip() for s in symbols.split(",")],
            start=parse_date.fromisoformat(start),
            end=parse_date.fromisoformat(end),
            capital=Decimal(capital),
            short_window=short_window,
            long_window=long_window,
            adjust=adjust,
        )
    )


async def _run_backtest(
    *,
    strategy: str,
    symbols: list[str],
    start: date,
    end: date,
    capital: Decimal,
    short_window: int,
    long_window: int,
    adjust: str,
) -> None:
    from finboard_app.strategies import create_strategy
    from finboard_backtest import BacktestConfig, BacktestEngine
    from finboard_data import AkShareProvider

    strat = create_strategy(
        strategy,
        f"{strategy}_backtest",
        symbol_code=symbols[0],
        short_window=short_window,
        long_window=long_window,
    )
    provider = AkShareProvider()
    config = BacktestConfig(
        symbols=symbols,
        start=start,
        end=end,
        initial_capital=capital,
        adjust=adjust,
    )
    engine = BacktestEngine(strategy=strat, data_provider=provider, config=config)
    result = await engine.run()
    typer.echo(result.summary())


app.add_typer(backtest_app, name="backtest")


# ---------------------------------------------------------------------------
# data 子命令
# ---------------------------------------------------------------------------
data_app = typer.Typer(
    name="data",
    help="历史行情数据拉取与管理",
    no_args_is_help=True,
)


@data_app.command(name="fetch")
def data_fetch(
    symbol: Annotated[
        str,
        typer.Argument(help="标的代码(如 510300.SH)"),
    ],
    start: Annotated[
        str,
        typer.Option("--start", help="开始日期 YYYY-MM-DD"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", help="结束日期 YYYY-MM-DD"),
    ],
    adjust: Annotated[
        str,
        typer.Option("--adjust", help="复权方式(qfq/hqfq/none)"),
    ] = "qfq",
) -> None:
    """拉取并缓存历史行情数据。"""
    from datetime import date as parse_date

    asyncio.run(
        _fetch_data(
            symbol=symbol,
            start=parse_date.fromisoformat(start),
            end=parse_date.fromisoformat(end),
            adjust=adjust,
        )
    )


@data_app.command(name="fetch-all")
def data_fetch_all(
    config_file: Annotated[
        str,
        typer.Option(
            "--config",
            help="标的池配置文件路径(YAML/JSON,默认 symbols.yaml)",
        ),
    ] = "symbols.yaml",
) -> None:
    """批量拉取标的池中所有标的的行情数据(带限流)。"""
    asyncio.run(_fetch_all_data(config_file=config_file))


@data_app.command(name="status")
def data_status(
    symbol: Annotated[
        str | None,
        typer.Argument(help="标的代码;省略时列出全部缓存"),
    ] = None,
    cache_dir: Annotated[
        str,
        typer.Option("--cache-dir", help="缓存目录"),
    ] = "data_cache",
) -> None:
    """查看本地缓存状态。"""
    asyncio.run(_data_status(symbol=symbol, cache_dir=cache_dir))


async def _fetch_data(
    *,
    symbol: str,
    start: date,
    end: date,
    adjust: str,
) -> None:
    import os

    from finboard_data import AkShareProvider, YFinanceProvider
    from finboard_shared.models import Symbol as Sym
    from finboard_shared.types import BarPeriod, Market

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider()
    else:
        provider = YFinanceProvider()
    bars = await provider.fetch_bars(
        Sym(code=symbol, market=Market.A_SHARE),
        BarPeriod.D1,
        start,
        end,
        adjust=adjust,
    )
    typer.echo(f"已获取 {len(bars)} 根日线")
    if bars:
        typer.echo(f"  起始: {bars[0].timestamp.date()} close={bars[0].close}")
        typer.echo(f"  结束: {bars[-1].timestamp.date()} close={bars[-1].close}")


async def _fetch_all_data(*, config_file: str) -> None:
    import os

    from finboard_data import AkShareProvider, YFinanceProvider, load_symbol_pool
    from finboard_data.cache import make_symbol
    from finboard_shared.types import BarPeriod

    config = load_symbol_pool(config_file)
    if not config.symbols:
        typer.echo(f"标的池为空: {config_file}", err=True)
        raise typer.Exit(1)

    end = date.today()
    start = end - timedelta(days=config.fetch_lookback_days)
    period = BarPeriod(config.fetch_period)
    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider()
    else:
        provider = YFinanceProvider()
    sym_objs = [make_symbol(s.code) for s in config.symbols]

    typer.echo(
        f"批量拉取 {len(sym_objs)} 个标的 "
        f"({start} ~ {end}) {period.value} {config.fetch_adjust}"
    )

    def on_progress(code: str, done: int, total: int) -> None:
        typer.echo(f"  [{done}/{total}] {code}")

    results = await provider.fetch_bars_batch(
        sym_objs,
        period,
        start,
        end,
        adjust=config.fetch_adjust,
        on_progress=on_progress,
    )

    success = sum(1 for v in results.values() if v)
    typer.echo(
        f"\n完成: {success}/{len(sym_objs)} 成功, "
        f"{len(sym_objs) - success} 失败"
    )


async def _data_status(*, symbol: str | None, cache_dir: str) -> None:
    from pathlib import Path

    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_shared.types import BarPeriod

    cache = ParquetCache(cache_dir)
    cache_path = Path(cache_dir)

    if symbol is not None:
        sym = make_symbol(symbol)
        bars = await cache.read(sym, BarPeriod.D1, "qfq")
        if not bars:
            typer.echo(f"无缓存: {symbol}")
            return
        typer.echo(f"{symbol}: {len(bars)} 根日线")
        typer.echo(f"  范围: {bars[0].timestamp.date()} ~ {bars[-1].timestamp.date()}")
        typer.echo(f"  最新收盘: {bars[-1].close}")
        return

    parquet_files = sorted(await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet"))))
    if not parquet_files:
        typer.echo("缓存为空")
        return

    typer.echo(f"{'标的':<15} {'周期':<6} {'复权':<6} {'bar数':>8}  日期范围")
    typer.echo("-" * 70)
    for f in parquet_files:
        parts = f.stem.rsplit("_", 2)
        if len(parts) != 3:
            continue
        code, period_str, adjust = parts
        from finboard_shared.models import Symbol as Sym
        from finboard_shared.types import Market

        bars = await cache.read(
            Sym(code=code, market=Market.A_SHARE),
            BarPeriod(period_str),
            adjust,
        )
        if bars:
            typer.echo(
                f"{code:<15} {period_str:<6} {adjust:<6} {len(bars):>8}  "
                f"{bars[0].timestamp.date()} ~ {bars[-1].timestamp.date()}"
            )
        else:
            typer.echo(f"{code:<15} {period_str:<6} {adjust:<6} {'(空)':>8}")


@data_app.command(name="sync")
def data_sync() -> None:
    """同步全市场标的元数据到数据库(akshare 自动发现 A 股 + ETF)。"""
    asyncio.run(_sync_universe())


@data_app.command(name="bulk-download")
def data_bulk_download(
    market: Annotated[
        str,
        typer.Option("--market", help="市场过滤(a_share/hk/us,默认 a_share)"),
    ] = "a_share",
    instrument_type: Annotated[
        str,
        typer.Option("--type", help="类型过滤(stock/etf,空=全部)"),
    ] = "",
    start: Annotated[
        str,
        typer.Option("--start", help="起始日期 YYYY-MM-DD(默认 2015-01-01)"),
    ] = "2015-01-01",
) -> None:
    """批量拉取数据库中所有标的的历史数据到 parquet 缓存。"""
    asyncio.run(
        _bulk_download(
            market=market,
            instrument_type=instrument_type or None,
            start_date=parse_date.fromisoformat(start),
        )
    )


app.add_typer(data_app, name="data")


# ---------------------------------------------------------------------------
# data sync / bulk-download 内部实现
# ---------------------------------------------------------------------------
async def _sync_universe() -> None:
    """从 akshare 发现全市场标的,写入 instruments 表。"""
    from finboard_app.config import load_settings
    from finboard_data.discovery import UniverseDiscovery
    from finboard_persistence import InstrumentRepository, create_async_engine, session_factory

    settings = load_settings()
    engine = create_async_engine(settings.db_url)
    discovery = UniverseDiscovery()

    typer.echo("正在发现 A 股 + ETF 标的...")
    instruments = await discovery.discover_all()
    typer.echo(f"发现 {len(instruments)} 个标的")

    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
        }
        for ins in instruments
    ]

    async with session_factory(engine)() as session:
        repo = InstrumentRepository(session)
        count = await repo.upsert_many(dicts)
        await session.commit()

    typer.echo(f"已同步 {count} 条标的到 instruments 表")
    await engine.dispose()


async def _bulk_download(
    *,
    market: str,
    instrument_type: str | None,
    start_date: date,
) -> None:
    """从数据库读取标的列表,批量拉取历史数据到 parquet。"""
    import os

    from finboard_app.config import load_settings
    from finboard_data import AkShareProvider, YFinanceProvider
    from finboard_data.cache import make_symbol
    from finboard_persistence import InstrumentRepository, create_async_engine, session_factory
    from finboard_shared.types import BarPeriod

    settings = load_settings()
    engine = create_async_engine(settings.db_url)

    async with session_factory(engine)() as session:
        repo = InstrumentRepository(session)
        instruments, _total = await repo.list_active(
            market=market,
            instrument_type=instrument_type,
            limit=99999,
        )

    await engine.dispose()

    if not instruments:
        typer.echo("未找到匹配的标的(请先运行 finboard data sync)", err=True)
        raise typer.Exit(1)

    typer.echo(f"开始批量拉取 {len(instruments)} 个标的 ({start_date} ~ today)")

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider(max_concurrency=2, request_interval=0.5)
    else:
        provider = YFinanceProvider(max_concurrency=3, request_interval=0.3)

    end = date.today()
    sym_objs = [make_symbol(ins.code) for ins in instruments]

    done = 0
    success = 0

    def on_progress(code: str, d: int, t: int) -> None:
        nonlocal done, success
        done = d
        if d % 100 == 0 or d == t:
            typer.echo(f"  进度: {d}/{t} ({d * 100 // t}%)")

    results = await provider.fetch_bars_batch(
        sym_objs,
        BarPeriod.D1,
        start_date,
        end,
        on_progress=on_progress,
    )

    success = sum(1 for v in results.values() if v)
    typer.echo(f"\n完成: {success}/{len(sym_objs)} 成功, {len(sym_objs) - success} 失败")


# --------------------------------------------------------------------------- 内部
async def _run_kernel(
    settings: Settings,
    strategies_file: str | None = None,
    timer_interval: float = 60.0,
    no_scheduler: bool = False,
) -> None:
    setup_logging(settings)
    components = build_kernel_components(settings)
    async with components.session_maker() as session:
        kernel = components.new_kernel(session)
        if settings.kill_switch_initial is not KillSwitchLevel.OFF:
            await kernel.activate_kill_switch(
                settings.kill_switch_initial, reason="initial state from config"
            )

        # 加载策略
        runner: StrategyRunner | None = None
        if strategies_file:
            from finboard_app.strategies import create_strategy
            from finboard_app.strategies.config import load_strategy_configs
            from finboard_core import StrategyRunner

            configs = load_strategy_configs(strategies_file)
            runner = StrategyRunner(
                event_bus=kernel.event_bus,
                order_manager=kernel.order_manager,
                position_manager=kernel.position_manager,
                account_manager=kernel.account_manager,
                account_id=AccountId(settings.account_id),
                timer_interval=timer_interval,
            )
            for cfg in configs:
                strategy = create_strategy(cfg.kind, cfg.id, **cfg.params)
                runner.register(strategy)

        # 加载定时任务调度器
        scheduler_obj: Scheduler | None = None
        if not no_scheduler:
            from finboard_persistence import AuditLogRepository
            from finboard_scheduler import Scheduler, TradingCalendar, create_default_tasks

            reconciler = components.new_reconciler(session)
            audit_repo = AuditLogRepository(session)
            scheduler_obj = Scheduler(TradingCalendar())
            for task in create_default_tasks(
                kernel=kernel,
                order_manager=kernel.order_manager,
                reconciler=reconciler,
                audit_repo=audit_repo,
                account_id=AccountId(settings.account_id),
            ):
                scheduler_obj.schedule(task)

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        await kernel.start()
        if runner is not None:
            await runner.start()
        if scheduler_obj is not None:
            await scheduler_obj.start()
        try:
            await stop_event.wait()
        finally:
            if scheduler_obj is not None:
                await scheduler_obj.stop()
            if runner is not None:
                await runner.stop()
            await kernel.stop()
            await session.commit()
            await components.engine.dispose()


async def _run_reconcile(settings: Settings) -> ReconciliationReport:
    setup_logging(settings)
    components = build_kernel_components(settings)
    async with components.session_maker() as session:
        recon = components.new_reconciler(session)
        await components.broker.connect(components.account_id, components.credentials)
        try:
            return await recon.run()
        finally:
            await components.broker.disconnect()
            await session.commit()
            await components.engine.dispose()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
