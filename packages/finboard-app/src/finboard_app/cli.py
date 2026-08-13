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
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from datetime import date, timedelta
from datetime import date as parse_date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from finboard_app.bootstrap import build_kernel_components
from finboard_app.config import Settings, load_settings
from finboard_app.logging import setup_logging
from finboard_shared.identifiers import AccountId
from finboard_shared.types import KillSwitchLevel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_api.schemas import BacktestRunRequest
    from finboard_app.bootstrap import KernelComponents
    from finboard_backtest.research_run import ResearchRunManifest
    from finboard_backtest.research_run.adapters import ResearchStrategyAdapter
    from finboard_data import ResearchDatasetRelease
    from finboard_reconcile import ReconciliationReport
    from finboard_scheduler import Scheduler

app = typer.Typer(
    name="finboard",
    help="生产级量化交易系统 CLI(P0:打通端到端真实交易链路)",
    no_args_is_help=True,
    add_completion=False,
)


def _configure_windows_asyncio() -> None:
    """为 Windows 上的 psycopg 异步连接选择兼容的事件循环。"""
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@app.callback()
def _main(
    ctx: typer.Context,
    env_file: Annotated[
        str | None,
        typer.Option("--env-file", help="指定 .env 文件路径(默认 ./.env)"),
    ] = None,
) -> None:
    _configure_windows_asyncio()
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

    # Uvicorn 0.36+ 在 Windows 默认显式创建 ProactorEventLoop,而 psycopg
    # 的异步连接要求 SelectorEventLoop。reload 子进程必须显式使用
    # ``asyncio``:Uvicorn 会在 use_subprocess=True 时返回 SelectorEventLoop;
    # 非 reload 模式继续用 ``none`` 继承上面设置的 Selector policy。
    if sys.platform == "win32":
        uvicorn_loop = "asyncio" if reload else "none"
    else:
        uvicorn_loop = "auto"
    if reload:
        uvicorn.run(
            "finboard_api.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            loop=uvicorn_loop,
        )
    else:
        from finboard_api.app import create_app

        app = create_app(ctx.obj)
        uvicorn.run(app, host=host, port=port, loop=uvicorn_loop)


def _stop_dev_process(process: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    """回收 Vite 进程组;Windows 按精确根 PID 清理整棵子进程树。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return

    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=timeout)


async def _check_dev_database(settings: Settings) -> None:
    """在启动 Vite 前验证数据库,避免前端对未就绪 API 持续代理报错。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        settings.db_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _wake_wsl_postgresql(settings: Settings) -> bool:
    """Windows 本机数据库不可达时,唤醒默认 WSL 发行版的 PostgreSQL。"""
    if sys.platform != "win32":
        return False

    from sqlalchemy.engine import make_url

    database_url = make_url(settings.db_url)
    if database_url.host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    port = database_url.port or 5432
    command = (
        "if command -v systemctl >/dev/null 2>&1; then "
        "systemctl start postgresql; "
        "else service postgresql start; fi && "
        f"pg_isready -h 127.0.0.1 -p {port}"
    )
    try:
        result = subprocess.run(
            ["wsl.exe", "-u", "root", "-e", "sh", "-lc", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ensure_dev_database(settings: Settings) -> bool:
    """检查数据库;必要时唤醒 WSL PostgreSQL 并重试。"""
    try:
        asyncio.run(_check_dev_database(settings))
        return False
    except KeyboardInterrupt:
        raise
    except Exception:
        if not _wake_wsl_postgresql(settings):
            raise
    asyncio.run(_check_dev_database(settings))
    return True


def _start_dev_frontend(npm_executable: str, web_dir: Path) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        return subprocess.Popen(
            [npm_executable, "run", "dev"],
            cwd=web_dir,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        [npm_executable, "run", "dev"],
        cwd=web_dir,
        start_new_session=True,
    )


def _start_frontend_when_api_ready(
    *,
    npm_executable: str,
    web_dir: Path,
    host: str,
    port: int,
    stop_event: threading.Event,
    frontend_holder: list[subprocess.Popen[bytes]],
) -> None:
    """等待 Uvicorn 完成 lifespan 并开始监听后再启动 Vite。"""
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    while not stop_event.wait(0.1):
        try:
            with socket.create_connection((connect_host, port), timeout=0.2):
                break
        except OSError:
            continue
    if stop_event.is_set():
        return
    frontend_holder.append(_start_dev_frontend(npm_executable, web_dir))


@app.command()
def dev(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="后端监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="后端监听端口")] = 8000,
    web_dir: Annotated[Path, typer.Option("--web-dir", help="前端目录")] = Path("web"),
) -> None:
    """前台运行 API 并托管 Vite,确保 Ctrl-C 触发 FastAPI shutdown。"""
    resolved_web_dir = web_dir.resolve()
    if not resolved_web_dir.is_dir():
        raise typer.BadParameter(f"前端目录不存在: {resolved_web_dir}", param_hint="--web-dir")

    npm_name = "npm.cmd" if sys.platform == "win32" else "npm"
    npm_executable = shutil.which(npm_name)
    if npm_executable is None:
        raise typer.BadParameter(f"找不到 {npm_name},请先安装 Node.js/npm")

    settings: Settings = ctx.obj
    try:
        wsl_started = _ensure_dev_database(settings)
    except KeyboardInterrupt as exc:
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        typer.echo(
            "PostgreSQL 连接失败,已尝试唤醒 WSL 服务但仍不可用;"
            "请检查 WSL PostgreSQL 和 .env。",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    if wsl_started:
        typer.echo("已自动唤醒 WSL PostgreSQL。")

    stop_event = threading.Event()
    frontend_holder: list[subprocess.Popen[bytes]] = []
    frontend_thread = threading.Thread(
        target=_start_frontend_when_api_ready,
        kwargs={
            "npm_executable": npm_executable,
            "web_dir": resolved_web_dir,
            "host": host,
            "port": port,
            "stop_event": stop_event,
            "frontend_holder": frontend_holder,
        },
        name="finboard-vite-launcher",
        daemon=True,
    )
    frontend_thread.start()
    try:
        # Uvicorn 留在当前前台进程中,Ctrl-C 会进入其优雅关闭和 FastAPI lifespan。
        serve(ctx, host=host, port=port, reload=False)
    finally:
        stop_event.set()
        frontend_thread.join(timeout=1.0)
        for frontend in frontend_holder:
            _stop_dev_process(frontend)


@app.command(name="kill-switch")
def kill_switch(
    ctx: typer.Context,
    level: Annotated[
        KillSwitchLevel,
        typer.Argument(
            help=("Kill Switch 级别: off / no_new_orders / reduce_only / cancel_all / halt"),
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
            typer.echo(f"  {t.name:<25s} {trigger:<20s} trading_days_only={t.trading_days_only}")


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
# worker 子命令(统一后台任务队列,issue #117 / #142)
# ---------------------------------------------------------------------------
worker_app = typer.Typer(
    name="worker",
    help="统一后台任务队列 worker(研究/数据域耗时任务,不触及实盘交易)",
    no_args_is_help=True,
)


@worker_app.command(name="run")
def worker_run(
    ctx: typer.Context,
    poll_interval: Annotated[
        float | None,
        typer.Option("--poll-interval", help="轮询队列的间隔秒数"),
    ] = None,
    max_concurrent: Annotated[
        int | None,
        typer.Option("--max-concurrent", help="最大并发任务数"),
    ] = None,
    queues: Annotated[
        str | None,
        typer.Option(
            "--queues",
            help="逗号分隔的逻辑队列白名单;留空表示消费全部队列",
        ),
    ] = None,
) -> None:
    """启动后台 worker 进程,从 PostgreSQL 队列领取任务直到 Ctrl-C。"""

    settings = ctx.obj
    if poll_interval is not None:
        settings = settings.model_copy(
            update={"worker_poll_interval_seconds": poll_interval}
        )
    if max_concurrent is not None:
        settings = settings.model_copy(
            update={"worker_max_concurrent": max_concurrent}
        )
    if queues is not None:
        settings = settings.model_copy(update={"worker_queues": queues})
    asyncio.run(_run_worker(settings))


@worker_app.command(name="recover")
def worker_recover(ctx: typer.Context) -> None:
    """回收过期 lease(running → interrupted),不启动常驻循环。

    适用于:worker 崩溃后只想清理脏状态、不想立刻起常驻进程的场景。
    """

    asyncio.run(_recover_stale(ctx.obj))


async def _run_worker(settings: Settings) -> None:
    setup_logging(settings)
    components = build_kernel_components(settings)
    from finboard_backtest.background_jobs import JobExecutorRegistry
    from finboard_backtest.background_jobs.executors import (
        BacktestRunExecutor,
        BulkDownloadExecutor,
        DataFetchAllExecutor,
        DatasetPublishExecutor,
        DataSyncExecutor,
        EchoExecutor,
        FeatureSnapshotExecutor,
        QualityRepairExecutor,
        ResearchRunExecutor,
    )
    from finboard_backtest.background_jobs.executors._providers import (
        default_settings_factory,
    )
    from finboard_backtest.background_jobs.executors.research_run import (
        default_store_factory,
    )
    from finboard_backtest.background_jobs.worker import (
        WorkerConfig,
        default_worker_id,
    )
    from finboard_backtest.background_jobs.worker import (
        run_worker as run_bg_worker,
    )

    # 启动恢复:把崩溃前 research_runs 残留的 RUNNING 收敛成可续跑的 INTERRUPTED
    # (background_jobs 的过期 lease 由 worker.run() 内部 _recover_stale 处理)。
    await _recover_research_runs(components.session_maker)

    settings_factory = default_settings_factory
    registry = JobExecutorRegistry()
    registry.register("echo", EchoExecutor())
    # issue #143:research_run 执行器接入统一队列。adapter_factory 用占位实现
    # (真实「冻结产物 → PortfolioPipelineAdapter」信号引擎留后续 issue);
    # 端到端验证通过注入 DecisionSequenceAdapter 的测试覆盖(见集成测试)。
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=components.session_maker,
            store_factory=default_store_factory,
            adapter_factory=_placeholder_adapter_factory,
        ),
    )
    # issue #144:7 类数据域任务迁移到统一队列。
    registry.register(
        "bulk_download",
        BulkDownloadExecutor(
            session_maker=components.session_maker,
            settings_factory=settings_factory,
        ),
    )
    registry.register(
        "feature_snapshot",
        FeatureSnapshotExecutor(
            session_maker=components.session_maker,
            max_concurrency=getattr(settings, "feature_snapshot_max_concurrency", 8),
            process_workers=getattr(settings, "feature_snapshot_process_workers", 0),
        ),
    )
    registry.register(
        "dataset_publish",
        DatasetPublishExecutor(session_maker=components.session_maker),
    )
    registry.register(
        "backtest_run",
        BacktestRunExecutor(
            session_maker=components.session_maker,
            runner=_backtest_runner,
        ),
    )
    registry.register(
        "data_sync",
        DataSyncExecutor(session_maker=components.session_maker),
    )
    registry.register(
        "fetch_all",
        DataFetchAllExecutor(
            session_maker=components.session_maker,
            settings_factory=settings_factory,
        ),
    )
    registry.register(
        "quality_repair",
        QualityRepairExecutor(
            session_maker=components.session_maker,
            settings_factory=settings_factory,
        ),
    )
    queue_list = [
        q.strip() for q in settings.worker_queues.split(",") if q.strip()
    ] or None
    config = WorkerConfig(
        worker_id=default_worker_id(),
        poll_interval_seconds=settings.worker_poll_interval_seconds,
        max_concurrent=settings.worker_max_concurrent,
        lease_timeout_seconds=settings.worker_lease_timeout_seconds,
        heartbeat_interval_seconds=settings.worker_heartbeat_interval_seconds,
        queues=queue_list,
        # issue #144:per-kind 全局并发上限(SQL 层 claim_next max_per_kind 实现)。
        # 数据源压力敏感的 kind 限制为单并发;dataset_publish / backtest_run 不限。
        kind_concurrency={
            "feature_snapshot": 1,
            "bulk_download": 1,
            "data_sync": 1,
            "fetch_all": 1,
            "quality_repair": 1,
        },
    )
    await run_bg_worker(
        engine=components.engine,
        session_maker=components.session_maker,
        registry=registry,
        config=config,
    )


async def _backtest_runner(
    session: AsyncSession,
    request: BacktestRunRequest,
    provider_name: str,
) -> int:
    """注入给 ``BacktestRunExecutor`` 的回测编排(延迟导入 ``finboard_api``)。"""
    from finboard_api.backtest_service import run_backtest_and_persist

    return await run_backtest_and_persist(session, request, provider_name=provider_name)


async def _recover_research_runs(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """worker 启动时把残留 RUNNING 研究运行收敛为 INTERRUPTED(issue #143)。"""
    from finboard_app.research_run_store import SqlAlchemyResearchRunStore
    from finboard_backtest.research_run import ResearchRunCoordinator
    from finboard_persistence import ResearchRunRepository

    async with session_maker() as session:
        store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
        await ResearchRunCoordinator(store).mark_stale_running_as_interrupted()
        await store.checkpoint()


def _placeholder_adapter_factory(
    manifest: ResearchRunManifest,
) -> ResearchStrategyAdapter:
    """占位适配器工厂:真实策略信号引擎尚未接入时,任务以不可重试失败收口。

    真实工厂应:用 ``FrozenInputLoader`` 加载机械字段 → 调策略信号引擎生成
    ``NormalizedSignal`` → 构造 ``PortfolioPipelineAdapter``。端到端验证通过
    集成测试注入 ``DecisionSequenceAdapter`` 固定样本覆盖。
    """
    from finboard_backtest.background_jobs.contracts import ExecutorError

    del manifest
    raise ExecutorError(
        code="signal_engine_not_implemented",
        summary=(
            "真实策略信号引擎尚未接入;research_run 执行器当前只支持注入固定样本"
            "适配器的测试路径。请通过后续 issue 实现「冻结产物 → 信号」加载器。"
        ),
        retryable=False,
    )


async def _recover_stale(settings: Settings) -> None:
    from datetime import UTC, datetime

    from finboard_persistence import BackgroundJobRepository

    setup_logging(settings)
    components = build_kernel_components(settings)
    async with components.session_maker() as session:
        repo = BackgroundJobRepository(session)
        rows = await repo.reclaim_stale(datetime.now(UTC))
        await repo.checkpoint()
        await components.engine.dispose()
    typer.echo(f"回收过期 lease 任务: {len(rows)}")
    for row in rows:
        typer.echo(f"  {row.job_id} kind={row.kind} -> interrupted")


app.add_typer(worker_app, name="worker")


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

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
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
    period = (
        BarPeriod[config.fetch_period]
        if config.fetch_period in BarPeriod.__members__
        else BarPeriod(config.fetch_period)
    )
    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider()
    else:
        provider = YFinanceProvider()
    sym_objs = [make_symbol(s.code) for s in config.symbols]

    typer.echo(
        f"批量拉取 {len(sym_objs)} 个标的 ({start} ~ {end}) {period.value} {config.fetch_adjust}"
    )

    def on_progress(code: str, done: int, total: int) -> None:
        typer.echo(f"  [{done}/{total}] {code}")

    results = await provider.update_cache_batch(
        sym_objs,
        period,
        start,
        end,
        adjust=config.fetch_adjust,
        on_progress=on_progress,
    )

    success = sum(results.values())
    typer.echo(f"\n完成: {success}/{len(sym_objs)} 成功, {len(sym_objs) - success} 失败")


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
        metadata = await cache.metadata(f)
        if metadata.bar_count:
            typer.echo(
                f"{code:<15} {period_str:<6} {adjust:<6} {metadata.bar_count:>8}  "
                f"{metadata.first_date} ~ {metadata.last_date}"
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


@data_app.command(name="release")
def data_release(
    release_id: Annotated[
        str,
        typer.Option("--release-id", help="不可变发布 ID(已存在时只允许相同规格幂等读取)"),
    ],
    version: Annotated[
        str,
        typer.Option("--version", help="数据版本(同 dataset/source 下唯一)"),
    ],
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="发布标的代码,逗号分隔"),
    ],
    start: Annotated[
        str,
        typer.Option("--start", help="发布开始日期 YYYY-MM-DD"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", help="发布结束日期 YYYY-MM-DD"),
    ],
    dataset_name: Annotated[
        str,
        typer.Option("--dataset-name", help="数据集名称"),
    ] = "multi_asset_daily_bars",
    source: Annotated[
        str,
        typer.Option("--source", help="原始行情来源"),
    ] = "akshare",
    cache_dir: Annotated[
        str,
        typer.Option("--cache-dir", help="可变 Parquet 缓存目录"),
    ] = "data_cache",
    release_root: Annotated[
        str,
        typer.Option("--release-root", help="不可变发布根目录"),
    ] = "data_releases",
    adjust: Annotated[
        str,
        typer.Option("--adjust", help="复权方式(qfq/hqfq/none)"),
    ] = "qfq",
    required_capabilities: Annotated[
        str,
        typer.Option(
            "--required-capabilities",
            help="质量门必需能力,逗号分隔",
        ),
    ] = "stock,etf:index,etf:cross_border,etf:commodity,etf:bond",
    code_version: Annotated[
        str | None,
        typer.Option("--code-version", help="生成代码版本;默认当前 git commit"),
    ] = None,
) -> None:
    """冻结、校验并登记一个可复现的多资产研究数据发布。"""

    normalized_symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    capabilities = tuple(
        item.strip() for item in required_capabilities.split(",") if item.strip()
    )
    try:
        release = asyncio.run(
            _publish_dataset_release(
                release_id=release_id,
                version=version,
                dataset_name=dataset_name,
                source=source,
                symbols=normalized_symbols,
                start_date=parse_date.fromisoformat(start),
                end_date=parse_date.fromisoformat(end),
                cache_dir=cache_dir,
                release_root=release_root,
                adjust=adjust,
                required_capabilities=capabilities,
                code_version=code_version or _current_code_version(),
            )
        )
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"发布失败: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"发布成功: {release.release_id} quality={release.quality_status.value} "
        f"symbols={release.symbol_count} rows={release.row_count} "
        f"coverage={release.coverage_pct} checksum={release.release_checksum}"
    )


@data_app.command(name="release-verify")
def data_release_verify(
    release_id: Annotated[
        str,
        typer.Argument(help="待校验的发布 ID"),
    ],
    release_root: Annotated[
        str,
        typer.Option("--release-root", help="不可变发布根目录"),
    ] = "data_releases",
) -> None:
    """离线校验发布 manifest 与全部 Parquet 文件 checksum。"""

    from pathlib import Path

    from finboard_data import DatasetReleaseError, verify_dataset_release

    try:
        release = verify_dataset_release(Path(release_root) / release_id)
    except DatasetReleaseError as exc:
        typer.echo(f"校验失败: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"校验通过: {release.release_id} symbols={release.symbol_count} "
        f"rows={release.row_count} checksum={release.release_checksum}"
    )


app.add_typer(data_app, name="data")


# ---------------------------------------------------------------------------
# data sync / bulk-download 内部实现
# ---------------------------------------------------------------------------
def _current_code_version() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f"{value}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else value


async def _publish_dataset_release(
    *,
    release_id: str,
    version: str,
    dataset_name: str,
    source: str,
    symbols: list[str],
    start_date: date,
    end_date: date,
    cache_dir: str,
    release_root: str,
    adjust: str,
    required_capabilities: tuple[str, ...],
    code_version: str,
) -> ResearchDatasetRelease:
    from finboard_app.config import load_settings
    from finboard_data import DatasetReleaseSpec
    from finboard_persistence import (
        ResearchDatasetReleaseService,
        create_async_engine,
        session_factory,
    )

    settings = load_settings()
    engine = create_async_engine(settings.db_url)
    try:
        async with session_factory(engine)() as session:
            service = ResearchDatasetReleaseService(
                session,
                cache_dir=cache_dir,
                release_root=release_root,
            )
            release = await service.publish(
                DatasetReleaseSpec(
                    release_id=release_id,
                    dataset_name=dataset_name,
                    source=source,
                    version=version,
                    start_date=start_date,
                    end_date=end_date,
                    code_version=code_version,
                    adjustment=adjust,
                    required_capabilities=required_capabilities,
                    known_limitations=(
                        "交易日覆盖使用 akshare/exchange_calendars 真实 A 股交易日历",
                        "停牌优先使用停复牌生命周期事件;缺少事件时按本地缓存的已查询区间(covered_ranges)对齐批量拉取口径",
                        "首期仅发布本地缓存已有字段,不回退到联网数据源",
                    ),
                ),
                symbols,
            )
            await session.commit()
            return release
    finally:
        await engine.dispose()


async def _sync_universe() -> None:
    """从 akshare 发现全市场标的,写入 instruments 表(带生命周期 diff)。"""
    from datetime import date

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
        result = await repo.sync_with_diff(dicts, as_of=date.today())
        await session.commit()

    typer.echo(
        f"已同步 {result.total} 条标的"
        f"(新增 {result.new}, 更新 {result.updated},"
        f" 改名 {len(result.renamed)},"
        f" 待退市确认 {len(result.pending_delist)},"
        f" 退市 {len(result.delisted)})"
    )
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

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider(
            max_concurrency=2, request_interval=0.5
        )
    else:
        provider = YFinanceProvider(max_concurrency=3, request_interval=0.3)

    end = date.today()
    sym_objs = [make_symbol(ins.code) for ins in instruments]

    done = 0
    success = 0

    def on_progress(code: str, d: int, t: int) -> None:
        nonlocal done, success
        done = d
        typer.echo(f"\r  进度: {d}/{t} ({d * 100 // t}%) {code:<16}", nl=False)

    results = await provider.update_cache_batch(
        sym_objs,
        BarPeriod.D1,
        start_date,
        end,
        on_progress=on_progress,
    )

    success = sum(results.values())
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
