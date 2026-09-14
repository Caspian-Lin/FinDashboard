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
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import date, timedelta
from datetime import date as parse_date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

# OpenBLAS 单线程界(issue #471 次级活性隐患防线):BLAS 原生调用在
# to_thread / 进程池并发下可被永久卡死(#471 冻结转储:两个 _load_one 任务
# 的 await 停在 ``asyncio.to_thread(_estimate_covariance, ...)``,工作线程
# 卡死在 numpy eigh → ``covariance.py _ensure_positive_definite`` 原生区不
# 返回;OpenBLAS 多线程并发调用死锁是同类已知问题)。研究运行的并行度来自
# chunk 级并发与进程池,BLAS 自身多线程属超额订阅 —— 单线程化即消除该类
# 卡死面,计算并行度不受影响。必须在 numpy 首次 import(**库初始化时读该
# 环境变量,本模块任何 finboard_* 导入都会传递拉起 numpy**)之前生效;
# ``setdefault`` 不剥夺用户显式覆盖。spawn 池子进程(factor_lab
# ProcessPoolExecutor,未显式传 env)经环境继承自动继承本值。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# 注意:不要把 ARROW_DEFAULT_MEMORY_POOL 切到 system —— 2026-09-14 实测
# Windows 上 pyarrow system 池在研究 run 决策 ~120 处原生段错误(exit 139,
# 无 Python 异常;mimalloc 默认池同代码 3h+ 无恙)。mimalloc 缓存已释放页
# 不归还 OS 的 ~3GB 死页问题,改由 factor_series_store 的逐批流式装载
# 根治(瞬态从 ~GB 降到 ~MB,池内无可缓存的大块)。

import typer

from finboard_app.bootstrap import build_kernel_components
from finboard_app.config import Settings, load_settings
from finboard_app.dev_cleanup import sweep_stale_workers
from finboard_app.logging import setup_logging

# ``as`` 同名别名 = 显式再导出(no_implicit_reexport):测试与 #373 REST
# 触发侧经 cli.<名> 引用,与 diagnostics 单一事实源同一,防两入口口径漂移。
from finboard_backtest.background_jobs.diagnostics import (
    FLAMEGRAPH_REPLAYABLE_KINDS as FLAMEGRAPH_REPLAYABLE_KINDS,
)
from finboard_backtest.background_jobs.diagnostics import (
    flamegraph_gate_error as flamegraph_gate_error,
)
from finboard_backtest.background_jobs.diagnostics import (
    resolve_py_spy as resolve_py_spy,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.types import KillSwitchLevel

# Windows CTRL_BREAK_EVENT=1;非 win32 平台取默认值仅服务测试态的 mock 路径求值,
# 真实 os.kill 只在 sys.platform == "win32" 分支内发生。
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_api.schemas import BacktestRunRequest
    from finboard_app.bootstrap import KernelComponents
    from finboard_backtest.background_jobs import JobExecutorRegistry
    from finboard_data import (
        AkShareProvider,
        ResearchDatasetRelease,
        TushareBarProvider,
        YFinanceProvider,
    )
    from finboard_persistence import BackgroundJobModel
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


#: worker 子进程停机兜底窗口秒数(issue #307):settings 宽限为 0 时(worker
#: 收到优雅信号后立即取消 in-flight 并快速退出),父进程(dev / ``--workers N``
#: supervisor)仍给子进程这段窗口自行收敛,超时才 taskkill / kill 强杀。
_WORKER_STOP_FALLBACK_GRACE_SECONDS = 10.0


def _dev_worker_stop_grace_seconds(settings: Settings) -> float:
    """dev 停机给 worker 子进程的优雅宽限(#307):settings 值,0 时用兜底窗口。

    settings 宽限 >0 时 worker 侧会先等 in-flight 任务完成至同一上限,父进程
    等待窗口与之对齐;为 0 时 worker 立即取消并退出,窗口只覆盖「取消 + 退出
    + engine.dispose」的耗时,超时说明 worker 卡死,强杀兜底(任务由 lease
    过期回收,与既有兜底语义一致)。
    """

    grace = float(settings.worker_shutdown_grace_seconds)
    return grace if grace > 0 else _WORKER_STOP_FALLBACK_GRACE_SECONDS


def _stop_dev_process(
    process: subprocess.Popen[bytes],
    *,
    graceful_seconds: float = 5.0,
) -> None:
    """回收 dev 托管子进程;先优雅信号、超时才强杀兜底(issue #307)。

    * Windows —— ``graceful_seconds > 0`` 且子进程仍在运行时,先向子进程自身
      的进程组(经 ``CREATE_NEW_PROCESS_GROUP`` 托管,组根 = 子进程 pid)发
      ``CTRL_BREAK_EVENT``:信号只送达该组,dev 主进程不会被打到;worker 侧
      注册的 SIGBREAK 处理器进入优雅停机。等待 ``graceful_seconds`` 仍未退出
      再 ``taskkill /PID /T /F`` 强杀整树。``graceful_seconds <= 0`` 保持既有
      立即 ``taskkill /T /F``。
    * POSIX —— ``killpg SIGTERM`` 等待 ``graceful_seconds`` 后 ``killpg
      SIGKILL`` 兜底(与既有行为同形,宽限可配)。
    """

    if process.poll() is not None:
        return
    if sys.platform == "win32":
        if graceful_seconds > 0:
            with contextlib.suppress(OSError):
                os.kill(process.pid, _CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=graceful_seconds)
                return
            except subprocess.TimeoutExpired:
                pass
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=max(0.1, graceful_seconds))
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5.0)


async def _check_dev_database(settings: Settings) -> None:
    """在启动 Vite 前验证数据库,避免前端对未就绪 API 持续代理报错。"""
    from sqlalchemy import text

    from finboard_persistence import create_async_engine

    # #471:经 persistence 工厂创建 —— postgres URL 默认注入 #450 keepalive,
    # connect_timeout 保持 3s 快速失败(同名键覆盖默认 10s),预检不等满窗口。
    engine = create_async_engine(
        settings.db_url,
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


def _ensure_sandbox_image(settings: Settings) -> bool:
    """研究沙箱开关开启时,确保沙箱镜像在本机存在(缺失则自动构建)。

    沙箱没有常驻服务:每次研究代码 run 由 worker 按需 ``docker run`` 一次性
    容器,镜像缺失只会在执行期报 SANDBOX_UNAVAILABLE。dev 预检把这个失败
    提前到启动期(与 CI 同源 Dockerfile,layer 缓存命中时秒级);docker
    不可用或构建失败仅警告,不阻断 dev 启动。返回是否执行了构建。
    """
    if not settings.research_sandbox_enabled:
        return False
    docker_bin = settings.research_sandbox_docker_bin
    image = settings.research_sandbox_image
    try:
        inspected = subprocess.run(
            [docker_bin, "image", "inspect", image],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        typer.echo(f"警告: 无法调用 {docker_bin},跳过沙箱镜像检查({exc})。", err=True)
        return False
    if inspected.returncode == 0:
        return False
    typer.echo(
        f"沙箱镜像 {image} 不在本机,自动构建中(docker/research-sandbox/,"
        "首次约 1-2 分钟,之后走 layer 缓存)..."
    )
    try:
        built = subprocess.run(
            [
                docker_bin,
                "build",
                "-f",
                "docker/research-sandbox/Dockerfile",
                "-t",
                image,
                ".",
            ],
            check=False,
        )
    except OSError as exc:
        typer.echo(
            f"警告: 沙箱镜像自动构建失败({exc});研究代码 run 将报 "
            "SANDBOX_UNAVAILABLE,可手动 docker build -f docker/research-sandbox/Dockerfile。",
            err=True,
        )
        return False
    if built.returncode != 0:
        typer.echo(
            "警告: 沙箱镜像自动构建失败;研究代码 run 将不可用"
            "(可手动 docker build -f docker/research-sandbox/Dockerfile -t "
            f"{image} .)。",
            err=True,
        )
        return False
    typer.echo(f"沙箱镜像 {image} 已就绪。")
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


def _dev_worker_command() -> list[str]:
    """dev 托管 worker 子进程的命令;独立进程让队列消费与 API 互不影响。"""
    return [sys.executable, "-m", "finboard_app.cli", "worker", "run"]


def _spawn_dev_worker() -> subprocess.Popen[bytes]:
    """启动 worker 子进程(与前端一样按进程组托管,退出时整树回收)。"""
    if sys.platform == "win32":
        return subprocess.Popen(
            _dev_worker_command(),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(_dev_worker_command(), start_new_session=True)


@app.command()
def dev(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="后端监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="后端监听端口")] = 8000,
    web_dir: Annotated[Path, typer.Option("--web-dir", help="前端目录")] = Path("web"),
    no_worker: Annotated[
        bool,
        typer.Option(
            "--no-worker",
            help="不随 dev 启动后台任务 worker(默认启动,研究/数据 job 才能被消费)",
        ),
    ] = False,
) -> None:
    """前台运行 API 并托管 Vite 与后台 worker,确保 Ctrl-C 触发 FastAPI shutdown。"""
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
    if _ensure_sandbox_image(settings):
        typer.echo("已自动构建研究沙箱镜像。")
    # issue #339:启动前清扫本工作区残留 worker(上一会话孤儿)——
    # 防止旧代码 worker 与本次 spawn 的新 worker 赛跑抢队列。
    swept_workers = sweep_stale_workers()
    if swept_workers:
        typer.echo(
            "已清理残留 worker 进程: "
            + ", ".join(f"PID {item.pid}" for item in swept_workers)
            + "(其 in-flight 任务由 lease 过期回收后自动重排)。"
        )

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

    worker_process: subprocess.Popen[bytes] | None = None
    if not no_worker:
        # 研究/数据 job 默认由随 dev 托管的 worker 子进程消费;
        # 生产部署用独立 `finboard worker run`(见 README),--no-worker 关闭。
        try:
            worker_process = _spawn_dev_worker()
        except OSError as exc:
            typer.echo(f"警告: 后台 worker 启动失败,任务将停留在 queued: {exc}", err=True)
        else:
            typer.echo(
                "已同步启动后台 worker(消费研究/数据任务队列);"
                "如需关闭请用 --no-worker"
            )

    try:
        # Uvicorn 留在当前前台进程中,Ctrl-C 会进入其优雅关闭和 FastAPI lifespan。
        serve(ctx, host=host, port=port, reload=False)
    finally:
        stop_event.set()
        frontend_thread.join(timeout=1.0)
        for frontend in frontend_holder:
            _stop_dev_process(frontend)
        if worker_process is not None:
            # issue #307:不再 taskkill /F 首杀 —— 先向 worker 进程组发优雅
            # 信号(Windows CTRL_BREAK / POSIX SIGTERM),给 worker 侧宽限
            # (settings.worker_shutdown_grace_seconds,默认 0 时给兜底窗口
            # 让 worker 完成「立即取消 → 退出」),超时才强杀。
            _stop_dev_process(
                worker_process,
                graceful_seconds=_dev_worker_stop_grace_seconds(settings),
            )


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


@app.command(name="factor-eval")
def factor_eval(
    ctx: typer.Context,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="评估数据面:synthetic(合成,全目录冒烟,免 DB)/ release(真实冻结发布小窗口)",
        ),
    ] = "synthetic",
    factors: Annotated[
        str,
        typer.Option("--factors", help="因子清单,逗号分隔(p_ 名或裸名);缺省 = 全目录"),
    ] = "",
    release_id: Annotated[
        str, typer.Option("--release-id", help="[release] bars 主发布 ID(DR-...)")
    ] = "",
    dataset_release_ids: Annotated[
        str,
        typer.Option("--dataset-release-ids", help="[release] 研究发布联合集,逗号分隔"),
    ] = "",
    window_start: Annotated[
        str, typer.Option("--window-start", help="[release] 窗口起点 YYYY-MM-DD")
    ] = "",
    window_end: Annotated[
        str, typer.Option("--window-end", help="[release] 窗口终点 YYYY-MM-DD")
    ] = "",
    horizon: Annotated[
        int, typer.Option("--horizon", help="前向收益持有期(交易日)")
    ] = 5,
    step: Annotated[
        int, typer.Option("--step", help="决策日步长(交易日)")
    ] = 5,
    output: Annotated[
        str, typer.Option("--output", "-o", help="报告 JSON 路径")
    ] = "",
) -> None:
    """因子质量评估(#403):逐因子 IC/RankIC/ICIR、分组单调性、换手衰减、覆盖起点。

    报告落 JSON(缺省 ``data_cache/factor_evals/``),含逐因子结论 flag 与
    signal_eligible 治理清单(#214 规则校验,疑似标注错误人工拍板)。
    合成模式仅证明机制跑通(IC 数值无研究含义);研究结论以 release 模式为准。
    """
    if mode not in ("synthetic", "release"):
        typer.echo(f"未知评估模式: {mode!r}(可用: synthetic / release)", err=True)
        raise typer.Exit(code=2)
    if mode == "release" and (not release_id or not window_start or not window_end):
        typer.echo(
            "release 模式需要 --release-id 与 --window-start/--window-end", err=True
        )
        raise typer.Exit(code=2)
    factor_list = (
        [item.strip() for item in factors.split(",") if item.strip()]
        if factors
        else None
    )
    dataset_ids = (
        tuple(item.strip() for item in dataset_release_ids.split(",") if item.strip())
        if dataset_release_ids
        else ()
    )
    settings = load_settings()
    report = asyncio.run(
        _run_factor_eval(
            settings=settings,
            mode=mode,
            factors=factor_list,
            release_id=release_id or None,
            dataset_release_ids=dataset_ids,
            window_start=window_start or None,
            window_end=window_end or None,
            horizon=horizon,
            step=step,
        )
    )
    out_path = (
        Path(output)
        if output
        else Path(settings.research_sandbox_workspace_root).parent
        / "factor_evals"
        / (
            f"factor-eval-{mode}-"
            f"{date.today().isoformat()}-{report['summary']['factor_count']}.json"
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = report["summary"]
    typer.echo(
        f"评估完成: {summary['factor_count']} 因子"
        f"(ic_available={summary['ic_available']}, "
        f"status={summary['status_counts']}, "
        f"signal_eligible 疑似标注错误={summary['signal_eligible_suspected']})"
    )
    typer.echo(f"最强 |RankIC|: {summary['strongest_abs_rank_ic']}")
    typer.echo(f"报告已写入: {out_path}")


async def _run_factor_eval(
    *,
    settings: Settings,
    mode: str,
    factors: list[str] | None,
    release_id: str | None,
    dataset_release_ids: tuple[str, ...],
    window_start: str | None,
    window_end: str | None,
    horizon: int,
    step: int,
) -> dict[str, Any]:
    from finboard_backtest.factors.eval import (
        FactorEvalConfig,
        evaluate_catalog_on_release,
        evaluate_catalog_synthetic,
    )

    config = FactorEvalConfig(horizon=horizon)
    if mode == "synthetic":
        synthetic: dict[str, Any] = evaluate_catalog_synthetic(
            factors=factors, config=config, step=step
        )
        return synthetic
    # 入口已校验(mode=release 时 release_id/window 非空)
    assert release_id is not None
    assert window_start
    assert window_end
    release_report: dict[str, Any] = await evaluate_catalog_on_release(
        release_id=release_id,
        dataset_release_ids=dataset_release_ids,
        window_start=parse_date.fromisoformat(window_start),
        window_end=parse_date.fromisoformat(window_end),
        factors=factors,
        config=config,
        decision_step=step,
        settings=settings,
    )
    return release_report


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


#: 多 worker 进程下父进程转发给子进程的 CLI 覆盖项(#286):
#: (选项名, 目标 settings 键)。--workers 不转发 —— 子进程固定单 worker
#: 形态,避免递归拉起进程树;--shutdown-grace(#307)显式给出时转发,
#: 保证子进程侧 drain 宽限与 supervisor 侧等待窗口取同一值。
_WORKER_CHILD_OPTIONS: tuple[tuple[str, str], ...] = (
    ("--poll-interval", "worker_poll_interval_seconds"),
    ("--max-concurrent", "worker_max_concurrent"),
    ("--queues", "worker_queues"),
    ("--maintenance-interval", "worker_maintenance_interval_seconds"),
    ("--retry-backoff", "worker_retry_backoff_seconds"),
    ("--shutdown-grace", "worker_shutdown_grace_seconds"),
)


def _worker_child_command(overrides: Mapping[str, str]) -> list[str]:
    """构造单 worker 子进程命令(#286):重新拉起 CLI 单 worker 形态。

    与 ``finboard dev`` 托管 worker(``_dev_worker_command``)同一入口 ——
    spawn 天然安全(无 pickle 约束),子进程各自独立 engine / session_maker /
    worker_id(``default_worker_id()`` 进程内生成,全局唯一)。只转发用户在
    CLI 显式给出的覆盖项(含空串 ``--queues ""`` = 消费全部队列),其余配置
    由子进程自行读 settings / .env(同一 CWD);``--workers`` 永不转发 ——
    子进程固定单 worker 形态,避免递归拉起进程树。
    """

    cmd = [sys.executable, "-m", "finboard_app.cli", "worker", "run"]
    for flag, key in _WORKER_CHILD_OPTIONS:
        if key in overrides:
            cmd.extend([flag, overrides[key]])
    return cmd


def _signal_worker_children_graceful(procs: list[subprocess.Popen[bytes]]) -> None:
    """向 worker 子进程组发优雅停止信号(issue #307)。

    Windows:子进程经 ``CREATE_NEW_PROCESS_GROUP`` 托管(组根 = 子进程
    pid),CTRL_BREAK 只送达该组 —— 子进程内注册的 SIGBREAK 处理器进入优雅
    停机;supervisor 自身不在组内,不会被打到(同控制台 Ctrl-C 对新进程组
    默认禁用,子进程本来就收不到)。POSIX:子进程在独立会话,SIGTERM 由
    ``run_worker`` 注册的信号处理器接管,进入同一优雅停机路径。
    """

    for proc in procs:
        if proc.poll() is not None:
            continue
        if sys.platform == "win32":
            with contextlib.suppress(OSError):
                os.kill(proc.pid, _CTRL_BREAK_EVENT)
        else:
            with contextlib.suppress(Exception):
                proc.terminate()


def _supervise_worker_processes(
    command: list[str],
    workers: int,
    shutdown_grace_seconds: float = 0.0,
) -> int:
    """拉起并收敛 N 个 worker 子进程,返回父进程退出码(#286)。

    * 正常退出:任一子进程非零退出 → 父进程返回 1(子进程崩溃不自动重启 ——
      队列语义下重启安全:未完成任务由 lease 过期回收后重排,重新执行命令即可);
    * Ctrl-C / 终止信号(#307):宽限 >0 时先向各子进程组发优雅停止信号
      (Windows CTRL_BREAK / POSIX SIGTERM),等 in-flight 任务在
      ``shutdown_grace_seconds`` 内自行收尾(与 worker 侧 drain 宽限同读
      settings),超时才强杀兜底;宽限 = 0(默认)保持立即 terminate(worker
      侧收到信号立即取消 in-flight),仅保留 ``_WORKER_STOP_FALLBACK_GRACE_SECONDS``
      兜底窗口供子进程完成取消收尾。用户主动停止不算失败,退出码 0。
    """

    procs: list[subprocess.Popen[bytes]] = []
    if sys.platform != "win32":
        # SIGTERM(systemd / docker stop 的默认信号)走与 Ctrl-C 相同的优雅
        # 收敛路径。没有本处理器时 supervisor 被即刻杀死,子 worker(各自
        # 独立会话)成为孤儿、继续消费队列(issue #316 实测)。
        def _sigterm_to_interrupt(signum: int, frame: object) -> None:
            del signum, frame
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    for _ in range(workers):
        if sys.platform == "win32":
            procs.append(
                subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            )
        else:
            procs.append(subprocess.Popen(command, start_new_session=True))
    typer.echo(f"已启动 {workers} 个 worker 子进程(pid: {[p.pid for p in procs]})")
    interrupted = False
    try:
        while any(p.poll() is None for p in procs):
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        typer.echo("收到停止信号,正在收敛 worker 子进程…")
    failed: list[int] = []
    if interrupted:
        if shutdown_grace_seconds > 0:
            typer.echo(
                f"优雅停机:等待 in-flight 任务收尾(宽限 {shutdown_grace_seconds:g}s)…"
            )
            _signal_worker_children_graceful(procs)
            deadline = time.monotonic() + shutdown_grace_seconds
        else:
            for proc in procs:
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.terminate()
            deadline = time.monotonic() + _WORKER_STOP_FALLBACK_GRACE_SECONDS
        for proc in procs:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
        return 0
    for proc in procs:
        code = proc.wait()
        if code != 0:
            failed.append(code)
    if failed:
        typer.echo(
            f"警告: {len(failed)} 个 worker 子进程异常退出(退出码 {failed});"
            "未完成任务将由 lease 过期回收后重排,请排查后重新执行本命令",
            err=True,
        )
        return 1
    return 0


#: worker per-kind 并发上限(issue #144 claim_next max_per_kind)。
#: 数据源压力敏感的 kind 单并发;dataset_publish / backtest_run 不限。
#: research_code_run 单并发(#216,沙箱容器本机资源受限);factor_series_build
#: 为 2(#375,#360 曾单并发)—— 单因子 build 的 3 个容器(主构建 + 2 审计
#: 截断)本就两两并发,双 job 并行使 4 因子队列墙钟近半;并发受
#: 「并发数 x research_sandbox_memory_mb(默认 4096,#374)<= Docker Desktop
#: WSL2 可用内存」约束,内存不足时经 env/配置回落。validation_experiment
#: 单并发(#233,揭盲一次性门,并发重入只会重复消耗试验预算)。research_run
#: 单并发(2026-09-13 全市场 556 期双 run 并发实测:加载期全量驻留的特征
#: 截面 ~5-6GB/run,双并发把 40GB 宿主推到 98.8% 靠 swap 硬撑,#424 审计
#: 容器并发改串行同理由 —— 代价为 research 队列墙钟串行)。
_KIND_CONCURRENCY: dict[str, int] = {
    "feature_snapshot": 1,
    "bulk_download": 1,
    "data_sync": 1,
    "quality_repair": 1,
    "dataset_sync": 1,
    "research_code_run": 1,
    "factor_series_build": 2,
    "validation_experiment": 1,
    "research_run": 1,
}


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
    maintenance_interval: Annotated[
        float | None,
        typer.Option(
            "--maintenance-interval",
            help="周期性维护(回收过期租约/重试重排)间隔秒数",
        ),
    ] = None,
    retry_backoff: Annotated[
        float | None,
        typer.Option(
            "--retry-backoff",
            help="retry_waiting/interrupted 自动重排前的退避秒数",
        ),
    ] = None,
    shutdown_grace: Annotated[
        float | None,
        typer.Option(
            "--shutdown-grace",
            help=(
                "优雅停机宽限秒数(默认取 settings."
                "worker_shutdown_grace_seconds,0=立即取消 in-flight)。"
                ">0 时收到停止信号先等 in-flight 完成至宽限上限再取消;"
                "等待中第二次 Ctrl-C 立即强退(退出码 130)"
            ),
        ),
    ] = None,
    workers: Annotated[
        int | None,
        typer.Option(
            "--workers",
            help=(
                "worker 进程数(默认取 settings.worker_processes,=1 单进程)。"
                ">1 时本命令作为父进程拉起 N 个独立 worker 子进程,各自"
                "engine/worker_id,多核并行消费;Ctrl-C 收敛全部子进程"
            ),
        ),
    ] = None,
) -> None:
    """启动后台 worker 进程,从 PostgreSQL 队列领取任务直到 Ctrl-C。

    停机语义与退出码(issue #307):第一次停止信号停止领新任务并按
    ``--shutdown-grace``(默认 settings.worker_shutdown_grace_seconds,0)
    收尾 in-flight;等待中第二次 Ctrl-C 立即强退。退出码:优雅完成 0,
    强退 130、异常非 0;未完成任务由 lease 过期回收后重排。
    """

    settings = ctx.obj
    overrides: dict[str, str] = {}
    if poll_interval is not None:
        overrides["worker_poll_interval_seconds"] = str(poll_interval)
        settings = settings.model_copy(
            update={"worker_poll_interval_seconds": poll_interval}
        )
    if max_concurrent is not None:
        overrides["worker_max_concurrent"] = str(max_concurrent)
        settings = settings.model_copy(
            update={"worker_max_concurrent": max_concurrent}
        )
    if queues is not None:
        overrides["worker_queues"] = queues
        settings = settings.model_copy(update={"worker_queues": queues})
    if maintenance_interval is not None:
        overrides["worker_maintenance_interval_seconds"] = str(maintenance_interval)
        settings = settings.model_copy(
            update={"worker_maintenance_interval_seconds": maintenance_interval}
        )
    if retry_backoff is not None:
        overrides["worker_retry_backoff_seconds"] = str(retry_backoff)
        settings = settings.model_copy(
            update={"worker_retry_backoff_seconds": retry_backoff}
        )
    if shutdown_grace is not None:
        overrides["worker_shutdown_grace_seconds"] = str(shutdown_grace)
        settings = settings.model_copy(
            update={"worker_shutdown_grace_seconds": shutdown_grace}
        )
    process_count = workers if workers is not None else settings.worker_processes
    if process_count > 1:
        # 多进程形态:父进程只做监管,不领取任务(issue #286)。
        child_cmd = _worker_child_command(overrides)
        raise typer.Exit(
            _supervise_worker_processes(
                child_cmd,
                process_count,
                # issue #307:supervisor 收敛宽限与 worker 侧 drain 宽限同读
                # settings(显式 --shutdown-grace 已合入上方 model_copy)。
                shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
            )
        )
    asyncio.run(_run_worker(settings))


@worker_app.command(name="recover")
def worker_recover(ctx: typer.Context) -> None:
    """回收过期 lease(running → interrupted),不启动常驻循环。

    适用于:worker 崩溃后只想清理脏状态、不想立刻起常驻进程的场景。
    """

    asyncio.run(_recover_stale(ctx.obj))


def build_executor_registry(
    settings: Settings, session_maker: async_sessionmaker[AsyncSession]
) -> JobExecutorRegistry:
    """装配全部 job kind 的执行器注册表(issue #383 抽取为共享工厂)。

    单一事实源:``worker run`` 主循环(:func:`_run_worker`)与 ``job-flamegraph``
    的诊断重放子进程(``job-replay-exec``)都经此构造,保证两边执行器 wiring
    一致 —— 此前装配内联在 ``_run_worker`` 里,诊断进程无法复用。
    """

    from finboard_backtest.background_jobs import JobExecutorRegistry
    from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
    from finboard_backtest.background_jobs.executors import (
        BacktestRunExecutor,
        BulkDownloadExecutor,
        DatasetPublishExecutor,
        DataSyncExecutor,
        EchoExecutor,
        FactorSeriesBuildExecutor,
        FeatureSnapshotExecutor,
        QualityRepairExecutor,
        ResearchCodeRunExecutor,
        ResearchRunExecutor,
        ValidationExperimentExecutor,
    )
    from finboard_backtest.background_jobs.executors._providers import (
        default_settings_factory,
    )
    from finboard_backtest.background_jobs.executors.research_run import (
        default_store_factory,
    )
    from finboard_backtest.research_run.signal_engine import (
        build_signal_engine_adapter_factory,
    )

    settings_factory = default_settings_factory
    registry = JobExecutorRegistry()
    registry.register("echo", EchoExecutor())
    # issue #143:research_run 执行器接入统一队列;#170:multi_factor 规格接入
    # 真实信号引擎适配器工厂;#218:user_code 规格经同一工厂分发到沙箱
    # decide 适配器(需要 settings 解析镜像/资源限制/代码仓库路径)。
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_maker,
            store_factory=default_store_factory,
            adapter_factory=build_signal_engine_adapter_factory(
                session_maker,
                settings_factory=settings_factory,
            ),
        ),
    )
    # issue #144:7 类数据域任务迁移到统一队列。
    registry.register(
        "bulk_download",
        BulkDownloadExecutor(
            session_maker=session_maker,
            settings_factory=settings_factory,
        ),
    )
    registry.register(
        "feature_snapshot",
        FeatureSnapshotExecutor(
            session_maker=session_maker,
            max_concurrency=getattr(settings, "feature_snapshot_max_concurrency", 8),
            process_workers=getattr(settings, "feature_snapshot_process_workers", 0),
        ),
    )
    registry.register(
        "dataset_publish",
        DatasetPublishExecutor(session_maker=session_maker),
    )
    registry.register(
        "backtest_run",
        BacktestRunExecutor(
            session_maker=session_maker,
            runner=_backtest_runner,
        ),
    )
    registry.register(
        "data_sync",
        DataSyncExecutor(session_maker=session_maker),
    )
    registry.register(
        "quality_repair",
        QualityRepairExecutor(
            session_maker=session_maker,
            settings_factory=settings_factory,
        ),
    )
    # issue #171 → #392:数据集驱动统一同步框架(SyncSpec 注册表,kind 由
    # research_data_sync 改名 dataset_sync)。
    registry.register(
        "dataset_sync",
        DatasetSyncExecutor(
            session_maker=session_maker,
            settings_factory=settings_factory,
        ),
    )
    # issue #216:研究代码沙箱执行(一次性 Docker 容器;settings 工厂沿用
    # executors._providers 默认实现,镜像/超时/限额取 research_sandbox_* 配置)。
    registry.register(
        "research_code_run",
        ResearchCodeRunExecutor(
            session_maker=session_maker,
            settings_factory=settings_factory,
        ),
    )
    # issue #360:内容寻址因子序列构建(单并发,复用 research_code_run 的
    # 沙箱容器槽位约定;容器执行本体由 #359 runner 提供,缓存命中不启动容器)。
    registry.register(
        "factor_series_build",
        FactorSeriesBuildExecutor(
            session_maker=session_maker,
            settings_factory=settings_factory,
        ),
    )
    # issue #233:#57 验证实验执行(walk-forward + 一次性揭盲)。runner 工厂
    # 按实验 selection_config 声明构建注册表策略回测;单并发 —— 揭盲是一次性
    # 门,并发重入只会重复消耗试验预算。
    from finboard_backtest.background_jobs.executors.validation_experiment import (
        default_trial_runner_factory,
    )

    registry.register(
        "validation_experiment",
        ValidationExperimentExecutor(
            session_maker=session_maker,
            runner_factory=default_trial_runner_factory,
        ),
    )
    return registry


async def _run_worker(settings: Settings) -> None:
    setup_logging(settings)
    components = build_kernel_components(settings)
    from finboard_backtest.background_jobs.worker import (
        WorkerConfig,
        default_worker_id,
    )
    from finboard_backtest.background_jobs.worker import (
        run_worker as run_bg_worker,
    )

    # 启动恢复:把崩溃前 research_runs 残留的 RUNNING 收敛成可续跑的 INTERRUPTED
    # (background_jobs 的过期 lease 由 worker.run() 内部 _recover_stale 处理)。
    # issue #306:属主检查 —— 兄弟 worker 活跃持有的 run 跳过误标;lease 活跃
    # 判定阈值与 worker lease 超时同源。
    await _recover_research_runs(
        components.session_maker,
        lease_active_seconds=settings.worker_lease_timeout_seconds,
    )

    registry = build_executor_registry(settings, components.session_maker)
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
        maintenance_interval_seconds=settings.worker_maintenance_interval_seconds,
        retry_backoff_seconds=settings.worker_retry_backoff_seconds,
        # issue #307:优雅停机宽限 —— 0(默认)= 停止信号立即取消 in-flight
        # (现状语义);>0 = 先等 in-flight 完成至宽限上限再取消。等待中第二
        # 次停止信号立即强退(退出码 130)。
        shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
        # issue #306:僵尸无进展检测阈值(0 = 关闭;默认 3600s 见 settings 注释)。
        zombie_no_progress_seconds=settings.worker_zombie_no_progress_seconds,
        # issue #471:执行段停滞看门狗阈值(0 = 关闭;默认 900s 见 settings
        # 注释)—— 心跳线程侧独立看门狗,无 progress 回调且未返回即取消
        # 执行任务并具名转 retry_waiting。
        stall_timeout_seconds=settings.worker_stall_timeout_seconds,
        # per-kind 全局并发上限见模块级 _KIND_CONCURRENCY(#144/#375)。
        kind_concurrency=dict(_KIND_CONCURRENCY),
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
    *,
    lease_active_seconds: float = 600.0,
) -> None:
    """worker 启动时把残留 RUNNING 研究运行收敛为 INTERRUPTED(issue #143)。

    issue #306:注入属主探针 —— 经 ``run.job_id`` join background_jobs,job 仍
    running/cancel_requested 且 lease/heartbeat 活跃的 run 视为兄弟 worker 正在
    活跃执行,跳过误标(多 worker 并发启动互不误伤);job 不存在 / 已终态 /
    lease 过期才是真孤儿,照旧标 interrupted(#305 replay 通道)。
    ``lease_active_seconds`` 与 worker lease 超时同源(settings
    ``worker_lease_timeout_seconds``):lease 未到期即 reclaim_stale 也不会回收
    该任务,口径一致。
    """

    from datetime import UTC, datetime

    from finboard_app.research_run_store import SqlAlchemyResearchRunStore
    from finboard_backtest.research_run import JobOwnershipProbe, ResearchRunCoordinator
    from finboard_persistence import BackgroundJobRepository, ResearchRunRepository
    from finboard_shared.background_jobs import (
        TERMINAL_STATUSES,
        BackgroundJobStatus,
    )

    active_window = timedelta(seconds=max(lease_active_seconds, 0.0))

    def _job_ownership_probe() -> JobOwnershipProbe:
        async def probe(job_id: str) -> bool:
            async with session_maker() as session:
                row = await BackgroundJobRepository(session).get(job_id)
            if row is None or row.status in TERMINAL_STATUSES:
                return False
            if row.status not in (
                BackgroundJobStatus.RUNNING.value,
                BackgroundJobStatus.CANCEL_REQUESTED.value,
            ):
                # queued / retry_waiting:没有 worker 在活跃推进该 run。
                return False
            # 「活跃」口径与 reclaim_stale 一致:lease 未过期即不会被回收,
            # run 也不该被误标。heartbeat 仅在 lease 缺失(旧行)时兜底,
            # 窗口与 lease 超时同源 —— 不然过期 lease 会被旧心跳长期续命。
            now = datetime.now(UTC)
            if row.lease_until is not None:
                return row.lease_until >= now
            return (
                row.heartbeat_at is not None
                and row.heartbeat_at >= now - active_window
            )

        return probe

    async with session_maker() as session:
        store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
        await ResearchRunCoordinator(store).mark_stale_running_as_interrupted(
            job_ownership=_job_ownership_probe()
        )
        await store.checkpoint()


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


def _cli_bar_provider(
    provider_name: str,
    *,
    max_concurrency: int | None = None,
    request_interval: float | None = None,
) -> AkShareProvider | TushareBarProvider | YFinanceProvider:
    """CLI 数据命令的行情 provider 构造(issue #393:默认主源 tushare)。

    复用 executor 侧 ``build_bar_provider`` 作为单一事实源:三源路由、
    tushare token / 预算参数读 settings,与 REST / MCP / worker 三入口
    保持同一构造口径。``provider_name`` 解析沿用 CLI 既有 env 直读语义
    (缺省 tushare,#393)。
    """
    from finboard_backtest.background_jobs.executors._providers import (
        build_bar_provider,
        default_settings_factory,
    )

    return build_bar_provider(
        provider_name.strip().lower(),
        default_settings_factory,
        max_concurrency=max_concurrency,
        request_interval=request_interval,
    )


async def _fetch_data(
    *,
    symbol: str,
    start: date,
    end: date,
    adjust: str,
) -> None:
    import os

    from finboard_shared.models import Symbol as Sym
    from finboard_shared.types import BarPeriod, Market

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "tushare")
    provider = _cli_bar_provider(provider_name)
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
    from finboard_app.config import load_settings, postgres_connect_args
    from finboard_data import DatasetReleaseSpec
    from finboard_persistence import (
        ResearchDatasetReleaseService,
        create_async_engine,
        session_factory,
    )

    settings = load_settings()
    engine = create_async_engine(
        settings.db_url,
        connect_args=postgres_connect_args(settings.db_url),
    )
    try:
        # 分段短事务(元数据 prep / 物化 / 登记):物化是分钟级纯文件 I/O,
        # 不能在打开的 PG 事务内进行,否则全市场规模发布会被
        # idle_in_transaction_session_timeout 杀连接。
        service = ResearchDatasetReleaseService(
            None,
            session_factory=session_factory(engine),
            cache_dir=cache_dir,
            release_root=release_root,
        )
        return await service.publish(
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
        # 后置 enrichment(issue #185):akshare 发现链路不携带 list_date/industry,
        # 按本次发现范围从最近一次已发布的 research_instrument_profiles 回填。
        backfill = await repo.backfill_metadata_from_profiles(
            symbols=[ins.code for ins in instruments]
        )
        # issue #394:指数登记携带 index_basic base_date,回填
        # instruments.list_date(只补 null,#185/#265 同语义)。
        # issue #395:期货合约登记携带 fut_basic list_date / delist_date,
        # 同通道回填(只补 null)。
        from finboard_shared.types import InstrumentType

        listing_records: dict[str, tuple[date | None, date | None]] = {}
        for ins in instruments:
            if ins.instrument_type is InstrumentType.INDEX:
                if ins.list_date is not None:
                    listing_records[ins.code] = (ins.list_date, None)
            elif (
                ins.instrument_type is InstrumentType.FUTURES
                and (ins.list_date is not None or ins.delist_date is not None)
            ):
                listing_records[ins.code] = (ins.list_date, ins.delist_date)
        listing_backfill = await repo.backfill_listing_dates(listing_records)
        await session.commit()

    typer.echo(
        f"已同步 {result.total} 条标的"
        f"(新增 {result.new}, 更新 {result.updated},"
        f" 改名 {len(result.renamed)},"
        f" 待退市确认 {len(result.pending_delist)},"
        f" 退市 {len(result.delisted)})"
    )
    if backfill.profile_batch_available:
        typer.echo(
            f"已回填 list_date={backfill.backfilled_list_date} "
            f"industry={backfill.backfilled_industry};"
            f" 仍缺失 list_date={backfill.missing_list_date} "
            f"industry={backfill.missing_industry}"
        )
    else:
        typer.echo("无已发布研究档案批次,跳过 list_date/industry 回填")
    typer.echo(
        f"指数 list_date 回填 {listing_backfill['backfilled_list_date']} 只"
        f"(仍缺失 {listing_backfill['missing_list_date']} 只,#394)"
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

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "tushare")
    provider = _cli_bar_provider(provider_name, max_concurrency=2, request_interval=0.5)

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


# ---- 诊断重放 + py-spy 火焰图(issue #383) ----
# 门控纯函数 / 放行-拒绝 kind 集合 / py-spy 解析在
# finboard_backtest.background_jobs.diagnostics(#373 起与 REST 触发侧共用)。


async def _load_job_row(settings: Settings, job_id: str) -> BackgroundJobModel | None:
    """读一行 background_jobs(诊断进程独立 engine,用完即弃)。"""

    from finboard_persistence import (
        BackgroundJobRepository,
        create_async_engine,
        session_factory,
    )

    engine = create_async_engine(settings.db_url)
    try:
        async with session_factory(engine)() as session:
            return await BackgroundJobRepository(session).get(job_id)
    finally:
        await engine.dispose()


@app.command(name="job-flamegraph")
def job_flamegraph(
    ctx: typer.Context,
    job_id: str = typer.Argument(help="background_jobs.job_id(须为终态)"),
    fmt: str = typer.Option(
        "flamegraph", "--format", help="输出格式:flamegraph(svg)| speedscope(json)"
    ),
    rate: int = typer.Option(50, "--rate", min=10, max=500, help="采样频率 Hz"),
    out: Path | None = typer.Option(
        None, "--out", help="输出目录(默认 data_cache/job_profiles/<job_id>-<时间戳>)"
    ),
) -> None:
    """对一个终态 job 起独立诊断进程重放,并用 py-spy 生成火焰图(issue #383)。

    诊断进程完全独立于 dev / worker —— 不占队列、不写 background_jobs。
    重放副作用因 kind 而异(启动时明示)。读图口径:火焰图宽帧 = CPU 采样
    占比高;IO 等待在本图上「看不见」,IO/CPU 归因请配合 job 行 timing 列的
    parquet_reads 占比(``finboard_job_get`` / GET /api/jobs/{id})。
    """

    settings: Settings = ctx.obj
    fmt_normalized = fmt.strip().lower()
    if fmt_normalized not in {"flamegraph", "speedscope"}:
        raise typer.BadParameter("--format 只接受 flamegraph|speedscope")
    from datetime import UTC, datetime

    pyspy = resolve_py_spy()
    if pyspy is None:
        typer.echo(
            "未找到 py-spy(dev 依赖组已包含)。请在仓库根执行:\n"
            "  uv sync\n"
            "或确认 venv 完整(.venv/Scripts/py-spy.exe)。",
            err=True,
        )
        raise typer.Exit(code=1)

    import asyncio as _asyncio

    row = _asyncio.run(_load_job_row(settings, job_id))
    if row is None:
        typer.echo(f"job {job_id} 不存在", err=True)
        raise typer.Exit(code=1)
    kind = row.kind
    status = row.status
    gate_error = flamegraph_gate_error(kind, status)
    if gate_error is not None:
        typer.echo(gate_error, err=True)
        raise typer.Exit(code=1)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = out or Path("data_cache/job_profiles") / f"{job_id}-{stamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    outfile = session_dir / (
        "flamegraph.svg" if fmt_normalized == "flamegraph" else "profile.json"
    )
    meta = {
        "job_id": job_id,
        "kind": kind,
        "source_status": status,
        "replay_side_effect": FLAMEGRAPH_REPLAYABLE_KINDS[kind],
        "format": fmt_normalized,
        "rate_hz": rate,
        "started_at": datetime.now(UTC).isoformat(),
    }
    (session_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    child_cmd = [
        sys.executable,
        "-m",
        "finboard_app.cli",
        "job-replay-exec",
        job_id,
        "--out-dir",
        str(session_dir),
    ]
    cmd = [
        pyspy,
        "record",
        "--format",
        fmt_normalized,
        "--rate",
        str(rate),
        "--subprocesses",
        "-o",
        str(outfile),
        "--",
        *child_cmd,
    ]
    typer.echo(f"诊断重放 kind={kind}:{FLAMEGRAPH_REPLAYABLE_KINDS[kind]}")
    typer.echo("采样中 —— Ctrl-C 可提前结束(py-spy 仍会写出已采集部分)。")
    completed = subprocess.run(cmd, check=False)

    timing_path = session_dir / "timing.json"
    if timing_path.exists():
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        reads = timing.get("parquet_reads", {})
        typer.echo(
            f"重放耗时 {timing.get('execute_elapsed_seconds')}s;"
            f"parquet 读 {reads.get('read_ops', 0)} 次 /"
            f" {reads.get('read_elapsed_ms', 0.0):.0f}ms /"
            f" {(reads.get('read_bytes', 0) or 0) / 1e6:.1f}MB"
            "(读耗时占 wall-clock 比例高 ≈ IO 瓶颈,低 ≈ CPU 瓶颈;"
            "配合火焰图定位 CPU 热点)"
        )
    result_path = session_dir / "result.json"
    if result_path.exists():
        replay = json.loads(result_path.read_text(encoding="utf-8"))
        typer.echo(f"重放终态: {replay.get('status')}(result_ref={replay.get('result_ref')})")
    if completed.returncode != 0:
        typer.echo(
            f"诊断子进程退出码 {completed.returncode}(py-spy 退出码);"
            "详情见 replay.log / result.json",
            err=True,
        )
    typer.echo(f"火焰图: {outfile}")
    typer.echo(f"会话目录: {session_dir}")


async def _job_replay_async(settings: Settings, job_id: str, out_dir: Path) -> int:
    """诊断重放子进程主体(不经 worker 领取,直接执行终态 job)。

    边界:Path/文件 IO 只允许经同步助手调用(ASYNC240),目录创建在同步
    包装器完成。
    """

    from finboard_backtest.background_jobs.contracts import JobRecord, JobResult
    from finboard_backtest.background_jobs.executors.research_run import (
        ResearchRunExecutor,
    )
    from finboard_backtest.background_jobs.registry import UnknownJobKindError
    from finboard_backtest.background_jobs.worker import build_job_timing
    from finboard_data.cache import collect_parquet_read_stats
    from finboard_persistence import BackgroundJobRepository

    components = build_kernel_components(settings)
    scratch: Path | None = None
    try:
        async with components.session_maker() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        if row is None:
            typer.echo(f"job {job_id} 不存在", err=True)
            return 1
        registry = build_executor_registry(settings, components.session_maker)
        try:
            executor = registry.get(row.kind)
        except UnknownJobKindError:
            typer.echo(f"未知 job kind: {row.kind}", err=True)
            return 1
        job_record = JobRecord(
            job_id=row.job_id,
            kind=row.kind,
            queue=row.queue,
            payload=dict(row.payload),
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            requested_by=row.requested_by,
            progress_total=row.progress_total,
            progress_done=row.progress_done,
            phase=row.phase,
        )

        async def _noop_progress(
            done: int, total: int | None, phase: str | None
        ) -> None:
            return None

        if row.kind == "dataset_publish":
            # scratch release_root(issue #383):强制重新冻结(计算全量执行);
            # release_root() 在 execute 内逐次读 env,执行前设置即可生效。
            # DB 层身份命中返回已有行,不插新行;会话结束删除 scratch。
            scratch = _make_scratch_release_root(out_dir)

        started = time.monotonic()
        result: JobResult | None = None
        crash: BaseException | None = None
        with collect_parquet_read_stats() as stats:
            try:
                if isinstance(executor, ResearchRunExecutor):
                    # COMPLETED 源走普通 execute 是瞬时 no-op(终态短路),
                    # 采样不到计算 —— 必须走 #305 replay 语义新建 run。
                    run_id = job_record.payload.get("run_id")
                    if not isinstance(run_id, str) or not run_id.startswith("RR-"):
                        typer.echo("payload.run_id 缺失或非法(RR- 前缀)", err=True)
                        return 1
                    result = await executor.execute_replay(run_id)
                else:
                    result = await executor.execute(job_record, _noop_progress)
            except Exception as exc:
                # 重放失败也照常落 timing / result(火焰图 + 失败现场同为
                # 诊断产物);不吞退出码。
                crash = exc
        timing = build_job_timing(started, stats)

        if crash is not None or result is None:
            result_payload: dict[str, object] = {
                "status": "crashed",
                "result_ref": None,
                "error_code": type(crash).__name__ if crash else "unknown",
                "error_summary": str(crash) if crash else None,
            }
        else:
            result_payload = {
                "status": result.status,
                "result_ref": result.result_ref,
                "error_code": result.error_code,
                "error_summary": result.error_summary,
            }
        (out_dir / "timing.json").write_text(
            json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "result.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from typing import cast

        reads = cast("dict[str, object]", timing["parquet_reads"])
        typer.echo(
            f"重放终态: {result_payload['status']};"
            f"耗时 {timing['execute_elapsed_seconds']}s,"
            f" parquet 读 {reads.get('read_ops', 0)} 次"
        )
        if crash is not None:
            return 2
        return 0 if result is not None and result.status == "succeeded" else 2
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        await components.engine.dispose()


def _make_scratch_release_root(out_dir: Path) -> Path:
    """为 dataset_publish 重放准备一次性 scratch release_root 并设 env。

    同步助手(ASYNC240):重算发布冻结文件落到 scratch,真实 data_releases
    不被触碰;DB 层身份命中返回已有行,scratch 会话结束即删。
    """

    scratch = out_dir / "scratch-release-root"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["FINBOARD_DATA_RELEASE_ROOT"] = str(scratch)
    return scratch


@app.command(name="job-replay-exec", hidden=True)
def job_replay_exec(
    ctx: typer.Context,
    job_id: str = typer.Argument(),
    out_dir: Path = typer.Option(..., "--out-dir"),
) -> None:
    """诊断重放子进程(issue #383):不经 worker 领取,直接执行一个终态 job。

    由 ``job-flamegraph`` 在 py-spy launch 模式下作为子进程拉起,一般不
    直接调用。全程套 parquet 读取聚合 + wall-clock 计时,timing.json /
    result.json 写入 --out-dir 供父进程展示;退出码 0=重放 succeeded,
    2=重放终态非 succeeded(诊断数据仍有效),1=基础设施错误。
    """

    settings: Settings = ctx.obj
    out_dir.mkdir(parents=True, exist_ok=True)
    exit_code = asyncio.run(_job_replay_async(settings, job_id, out_dir))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
