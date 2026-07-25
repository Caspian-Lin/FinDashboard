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
from datetime import date
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


async def _fetch_data(
    *,
    symbol: str,
    start: date,
    end: date,
    adjust: str,
) -> None:
    from finboard_data import AkShareProvider
    from finboard_shared.models import Symbol as Sym
    from finboard_shared.types import BarPeriod, Market

    provider = AkShareProvider()
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


app.add_typer(data_app, name="data")


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
