"""OpenCode 进程管理器(issue #xxx Docker 隔离,基于 #118;#157 移除 basic auth)。

由 FinBoard 网关层托管一个**容器级隔离**的 ``opencode web`` Docker 容器:独立工作目录、
独立 ``~/.local/share/opencode``(auth.json / 会话 DB,与宿主机全局 opencode 彻底隔离)、
版本锁定镜像、仅必要环境变量(绝不继承 FinBoard 的 DB 密码 / broker 凭证 / API Key)、
宿主机侧网络绑定 127.0.0.1。

鉴权决策(#157,用户确认):当前为**单一用户模型**,OpenCode Web 不启用 basic auth,
直接使用明文 ``http://127.0.0.1:{port}`` URL;宿主机侧 127.0.0.1 绑定是唯一网络边界。
后续多用户时需恢复网关鉴权层。

OpenCode v1.18.15 不支持 ``--base-path`` 子路径部署(上游 PR #28326 未合并),因此
FinBoard 前端通过 **iframe 跨源嵌入** OpenCode Web 根 URL,FinBoard 网关只做控制面
(容器生命周期 + 访问信息签发),不透传 OpenCode 流量。

MCP 接线(#157):``mcp_remote_url`` 生效方式 —— ``start()`` 前把仓库 ``.opencode/opencode.json``
渲染为 ``.opencode/runtime/opencode.json``(仅替换 ``mcp.finboard.url``),再以单文件
bind mount 覆盖容器内 ``/workspace/.opencode/opencode.json``。仓库文件保持事实来源,
运行时产物不入库(.gitignore)。

红线:本模块只管理研究运行时容器,不连接实盘 broker / 账户 / 订单 / 持仓 / 风控。

Windows 事件循环冲突
---------------------
FinBoard 主事件循环是 ``WindowsSelectorEventLoopPolicy`` —— psycopg 异步连接在
Windows 上硬性拒绝 ``ProactorEventLoop``。但 ``asyncio.create_subprocess_exec`` /
``Process.terminate()`` / ``Process.wait()`` 只在 ``ProactorEventLoop`` 上可用,
``SelectorEventLoop`` 会抛 ``NotImplementedError``。

``start`` / ``stop`` 会 spawn ``docker`` CLI 子进程(``docker run`` / ``docker stop``),
故仍需要派发到专用守护线程里的 ``ProactorEventLoop``(:class:`_SubprocessExecutor`)。
主循环仍是 selector(psycopg 用),HTTP 健康探测(httpx)也在主循环上跑。容器化后不再
spawn node 子进程,孤儿进程 / 端口残留问题被 ``docker stop`` 自动解决,但 ``docker`` CLI
本身仍是子进程,Windows 事件循环冲突仍在。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

#: 子进程默认继承的系统必需环境变量白名单。
#: 严格白名单 —— 避免把 FinBoard 的 DB 密码 / broker 凭证 / API Key 泄露给 OpenCode。
_INHERITED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
    }
)

#: OpenCode 健康端点(``opencode serve`` / ``opencode web`` 暴露在根路径,不带 ``/api`` 前缀)。
_HEALTH_PATH = "/global/health"

#: 默认就绪探测参数。
_DEFAULT_READY_TIMEOUT = 30.0
_DEFAULT_READY_INTERVAL = 0.5

#: OpenCode Web 官方 Docker 镜像(组织迁移:旧 ``ghcr.io/sst/opencode`` 已废弃)。
_DEFAULT_IMAGE = "ghcr.io/anomalyco/opencode:latest"

#: 容器内 opencode 工作目录(镜像约定)。
_CONTAINER_WORKDIR = "/workspace"

#: 容器内需要 bind mount 的项目级配置目录(相对宿主机 workdir)。
_CONTAINER_MOUNTS: tuple[tuple[str, str], ...] = (
    (".opencode", f"{_CONTAINER_WORKDIR}/.opencode"),
    (".agents", f"{_CONTAINER_WORKDIR}/.agents"),
)

#: 容器内 OpenCode 运行时数据(会话 DB / auth),用 named volume 持久化。
#: 容器删除(``docker rm -f``)不影响 named volume,重启后会话历史保留。
#: 容器内 opencode 以 root 运行,数据目录在 ``/root/.local/share/opencode``
#: (XDG_DATA_HOME)和 ``/root/.config/opencode``(XDG_CONFIG_HOME)。
_RUNTIME_VOLUME_MOUNTS: tuple[tuple[str, str], ...] = (
    ("opencode-data", "/root/.local/share/opencode"),
    ("opencode-config", "/root/.config/opencode"),
)

#: 容器内 opencode 的环境目录语义(HOME / XDG),与 volume 挂载并列的硬性约束。
#:
#: - ``HOME=/workspace``:opencode web 的文件选择器 / homedir 默认从 HOME 开始。
#:   容器默认 HOME=/root,文件选择器从 /root 开始搜索 —— 而 /workspace 是独立
#:   挂载点(不在 /root 下),用户因此「搜不到 workspace」。改为 /workspace 后,
#:   打开项目对话框直接落在工作目录,历史会话目录立即可见。
#: - ``XDG_*_HOME`` 的取值是**数据根**,opencode 会在其下追加 ``opencode/``
#:   子目录(实测 v1.18.15:``XDG_DATA_HOME=/tmp/x`` → 数据在 ``/tmp/x/opencode/``)。
#:   因此必须指向 volume 挂载点的**父目录**,让最终落点(``.../opencode``)正好是
#:   named volume 挂载点 —— 否则 HOME 变化会把会话 DB / auth 带到容器层(重启即丢)
#:   或写进嵌套目录(旧数据读不到)。
_CONTAINER_ENV_DIRS: tuple[tuple[str, str], ...] = (
    ("HOME", "/workspace"),
    ("XDG_DATA_HOME", "/root/.local/share"),
    ("XDG_CONFIG_HOME", "/root/.config"),
    ("XDG_STATE_HOME", "/root/.local/state"),
)


@dataclass(frozen=True, slots=True)
class OpenCodeProcessConfig:
    """OpenCode 容器启动配置。"""

    #: Docker 镜像(版本锁定;官方 ``ghcr.io/anomalyco/opencode``)。
    image: str = _DEFAULT_IMAGE
    #: 固定容器名(便于 stop / logs / inspect)。
    container_name: str = "finboard-opencode-web"
    #: 宿主机侧监听端口(默认 4097,区别于 ``opencode serve`` 的 4096)。
    port: int = 4097
    #: 宿主机侧监听地址(隔离:强制 127.0.0.1,不暴露公网);容器内 opencode 绑 0.0.0.0。
    #: #157 移除 basic auth 后,这是 OpenCode Web 的唯一网络边界(单用户模型)。
    hostname: str = "127.0.0.1"
    #: 允许跨源访问的浏览器源(iframe 跨源嵌入必须显式允许 FinBoard 源)。
    cors_origins: list[str] = field(default_factory=list)
    #: 宿主机侧工作目录(包含 ``.opencode`` / ``.agents``,bind mount 进容器)。
    workdir: str = "."
    #: docker CLI 日志文件路径(``docker run`` / ``docker stop`` 的 stdout/stderr)。
    log_path: str = ".opencode/logs/opencode-web.log"
    #: 额外注入容器的环境变量(LLM provider Key / MCP token 等;KEY=VAL 转 ``-e``)。
    #: 严格语义:这些变量进容器,FinBoard 自身凭证仍不进容器(见 :meth:`build_environment`)。
    env_overrides: dict[str, str] = field(default_factory=dict)
    #: MCP Bearer token:容器内 opencode 用它访问宿主机 finboard_mcp(跨容器鉴权)。
    mcp_auth_token: str = ""
    #: 容器内 opencode 连接宿主机 finboard_mcp 的 URL(#157:由本配置渲染进容器
    #: opencode.json,使 ``opencode_mcp_remote_url`` 设置实际生效)。
    mcp_remote_url: str = "http://host.docker.internal:8765/mcp"

    @property
    def base_url(self) -> str:
        return f"http://{self.hostname}:{self.port}"

    def build_docker_run_command(
        self, *, runtime_config_path: Path | None = None
    ) -> list[str]:
        """构造 ``docker run -d`` 参数列表(detach 模式,stdout 输出容器 ID)。

        关键决策:
        - ``-p 127.0.0.1:{port}:{port}`` —— 宿主机侧锁 loopback,不暴露公网。
        - ``--add-host=host.docker.internal:host-gateway`` —— 容器内可访问宿主机
          finboard_mcp(Windows Docker Desktop 默认支持,Linux 需此 flag)。
        - 容器内 opencode 绑 ``0.0.0.0``(否则端口映射进不来),由 ``--hostname`` 指定。
        - bind mount ``.opencode`` / ``.agents`` —— agent 定义 / skill / opencode.json
          持久化在宿主机仓库内,容器只读这些项目级配置。
        - named volume ``opencode-data`` / ``opencode-config`` —— 会话 DB(opencode.db)
          和 auth 持久化,容器删除后保留,重启可恢复历史。
        - ``runtime_config_path`` —— 渲染后的 opencode.json(mcp.finboard.url 已替换为
          ``mcp_remote_url``)以单文件 bind mount 覆盖容器内同名文件(#157)。单文件
          mount 必须排在目录 mount 之后(Docker 按精确路径优先)。
        - ``-e`` 注入:MCP token + env_overrides(LLM key 等)。FinBoard 自身
          DB 密码 / broker 凭证**永不**进入 ``-e`` 列表。HOME / XDG 目录语义
          也以 ``-e`` 注入(见 ``_CONTAINER_ENV_DIRS``),钉死数据落点。
        """
        cmd: list[str] = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "--add-host=host.docker.internal:host-gateway",
            "-p", f"{self.hostname}:{self.port}:{self.port}",
        ]
        # bind mount 项目级配置目录(.opencode / .agents)。
        workdir = Path(self.workdir).resolve()
        for host_rel, container_abs in _CONTAINER_MOUNTS:
            host_abs = workdir / host_rel
            cmd += ["-v", f"{host_abs}:{container_abs}"]
        # 渲染后的运行时配置覆盖容器内 opencode.json(MCP 地址可配置,#157)。
        if runtime_config_path is not None:
            cmd += ["-v", f"{runtime_config_path}:{_CONTAINER_WORKDIR}/.opencode/opencode.json"]
        # named volume 持久化会话 DB / auth:容器删除后数据保留,重启可恢复历史。
        for volume_name, container_abs in _RUNTIME_VOLUME_MOUNTS:
            cmd += ["-v", f"{volume_name}:{container_abs}"]
        # HOME / XDG 钉死(与 volume 挂载配套):文件选择器从 /workspace 开始,
        # 会话 DB / auth 仍落 named volume(不随 HOME 漂移到容器层)。
        for key, value in _CONTAINER_ENV_DIRS:
            cmd += ["-e", f"{key}={value}"]
        # MCP token:容器内 opencode 通过 finboard MCP remote type 访问宿主机。
        if self.mcp_auth_token:
            cmd += ["-e", f"FINBOARD_MCP_TOKEN={self.mcp_auth_token}"]
        # 显式 env_overrides(LLM provider Key 等)。
        for key, value in self.env_overrides.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["-w", _CONTAINER_WORKDIR, self.image]
        # opencode 子命令:容器内绑 0.0.0.0(端口映射要求),宿主机侧由 -p 锁 loopback。
        cmd += ["web", "--hostname", "0.0.0.0", "--port", str(self.port)]
        if self.cors_origins:
            cmd += ["--cors", *self.cors_origins]
        return cmd

    def build_docker_stop_command(self) -> list[str]:
        """构造 ``docker stop`` + ``rm`` 序列(容器名固定,幂等)。"""
        return ["docker", "rm", "-f", self.container_name]

    def render_runtime_config(self) -> Path:
        """渲染容器用的运行时 opencode.json(#157,同步 IO,调用方搬到线程)。

        读取 ``{workdir}/.opencode/opencode.json``(仓库事实来源),把
        ``mcp.finboard.url`` 替换为 ``mcp_remote_url``,写入
        ``{workdir}/.opencode/runtime/opencode.json``(gitignore 的运行时产物)。
        其余键原样保留(provider / agent / permission 等)。
        """
        source = Path(self.workdir).resolve() / ".opencode" / "opencode.json"
        target = source.parent / "runtime" / "opencode.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        mcp = data.get("mcp")
        if isinstance(mcp, dict) and isinstance(mcp.get("finboard"), dict):
            mcp["finboard"]["url"] = self.mcp_remote_url
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target

    def build_environment(
        self, *, parent_env: dict[str, str] | None = None
    ) -> dict[str, str]:
        """构造 ``docker`` CLI 子进程的环境变量(严格白名单 + 显式覆盖)。

        这是 ``docker run`` / ``docker stop`` 子进程自身的环境,不是容器的环境
        (容器环境由 ``-e`` 参数控制,见 :meth:`build_docker_run_command`)。``docker``
        CLI 需要找到 docker daemon socket,继承的系统变量应最小化。
        """
        parent = parent_env if parent_env is not None else dict(os.environ)
        env: dict[str, str] = {}
        for key in _INHERITED_ENV_KEYS:
            value = parent.get(key)
            if value:
                env[key] = value
        # docker CLI 在 Windows 上需要 COMSPEC / ProgramFiles(Docker Desktop 路径)。
        for extra in ("COMSPEC", "ProgramFiles", "ProgramData"):
            value = parent.get(extra)
            if value:
                env[extra] = value
        env.update(self.env_overrides)
        return env


@dataclass(slots=True)
class ProcessStatus:
    """容器运行状态快照(用于网关 ``/status`` 端点)。"""

    running: bool
    managed: bool
    #: 容器短 ID(前 12 位);非托管模式或未启动时为 None。
    container_id: str | None
    base_url: str
    healthy: bool | None
    version: str | None
    started_at: datetime | None
    last_error: str | None = None


class OpenCodeProcessError(RuntimeError):
    """OpenCode 进程管理失败。"""


class _SubprocessExecutor:
    """在专用 ``ProactorEventLoop`` 守护线程上执行子进程生命周期操作。

    Windows 上 ``asyncio`` 子进程(spawn / terminate / wait / kill)只支持
    ``ProactorEventLoop``,而 FinBoard 主循环必须用 ``SelectorEventLoop``(psycopg
    硬性要求)。本类启动一个独立守护线程跑 ``ProactorEventLoop``,通过
    :func:`asyncio.run_coroutine_threadsafe` 把子进程协程派发过去,在调用方循环侧
    透明地 ``await`` 结果。

    非 Windows 平台没有此限制,所有方法直接在调用方事件循环上执行,不额外开线程。
    生命周期跟随 :class:`OpenCodeProcessManager`,在 :meth:`shutdown` 时关闭。
    """

    def __init__(self) -> None:
        self._needs_thread = sys.platform == "win32"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop | None:
        """惰性启动专用 proactor 线程;非 Windows 返回 None(调用方循环直接跑)。"""
        if not self._needs_thread:
            return None
        # 双检锁:已启动直接返回。线程一旦启动就活到 shutdown,无需重复创建。
        if self._loop is not None:
            return self._loop
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()
            loop_holder: list[asyncio.AbstractEventLoop] = []

            def _runner() -> None:
                # Windows 专用:ProactorEventLoop 支持子进程。用 getattr 动态获取,
                # 避免 mypy 在非 Windows 平台报 ``WindowsProactorEventLoopPolicy``
                # 未定义(该符号仅 win32 存在);运行时本函数也只在 win32 被调用。
                policy_factory = getattr(
                    asyncio, "WindowsProactorEventLoopPolicy", None
                )
                if policy_factory is not None:
                    asyncio.set_event_loop_policy(policy_factory())
                loop = asyncio.new_event_loop()
                loop_holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    with contextlib.suppress(Exception):
                        loop.close()

            thread = threading.Thread(
                target=_runner, name="finboard-opencode-subprocess", daemon=True
            )
            thread.start()
            ready.wait(timeout=10.0)
            if not loop_holder:
                raise OpenCodeProcessError(
                    "子进程专用事件循环启动超时(ProactorEventLoop 守护线程未就绪)"
                )
            self._loop = loop_holder[0]
            self._thread = thread
            return self._loop

    async def run(self, coro_factory: Any) -> Any:
        """在专用循环上执行协程工厂,返回结果。

        ``coro_factory`` 是一个零参数可调用,在被派发的循环里调用以构造协程。
        这样 ``create_subprocess_exec`` 真正在 proactor 循环上创建,其 transport
        也注册在该循环上(后续 terminate/wait/kill 也必须回到同一循环)。
        """
        loop = self._ensure_loop()
        if loop is None:
            # 非 Windows:直接在当前循环执行。
            return await coro_factory()
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return await asyncio.wrap_future(future)

    def shutdown(self) -> None:
        """停止专用循环并回收线程(幂等)。"""
        loop = self._loop
        if loop is None:
            return
        # 循环可能已关闭,忽略 call_soon_threadsafe 的 RuntimeError。
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._loop = None
        self._thread = None


class OpenCodeProcessManager:
    """托管 ``opencode web`` Docker 容器的生命周期。

    单实例 + 容器级隔离:全局一个 OpenCode 容器服务所有研究会话,会话级隔离由
    OpenCode session 管理(#121 重构后 FinBoard 不再维护独立 conversation 授权层)。

    生命周期由 FinBoard API lifespan 管理(见 ``app.py``);``stop`` 必须在 shutdown
    时调用以回收容器(``docker rm -f``)。``start`` 执行 ``docker run -d``,从 stdout
    读取容器 ID;``stop`` 执行 ``docker rm -f``(自带 10s grace + SIGKILL)。
    """

    def __init__(
        self,
        config: OpenCodeProcessConfig,
        *,
        manage_process: bool = True,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._config = config
        self._manage_process = manage_process
        self._log = logger or structlog.get_logger("finboard_opencode.process")
        self._container_id: str | None = None
        self._log_file: Any = None
        self._started_at: datetime | None = None
        self._health_client: httpx.AsyncClient | None = None
        self._version: str | None = None
        # docker CLI 子进程派发到专用 ProactorEventLoop(Windows;见类文档)。
        self._executor = _SubprocessExecutor()

    @property
    def config(self) -> OpenCodeProcessConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def is_managed(self) -> bool:
        """是否由本管理器托管容器(False = 外部已启动,仅连接)。"""
        return self._manage_process

    @property
    def container_id(self) -> str | None:
        """当前容器短 ID(未启动 / 非托管为 None)。"""
        return self._container_id

    def is_running(self) -> bool:
        """托管模式:查 ``docker inspect`` 容器是否 Running;非托管模式:恒 True。"""
        if not self._manage_process:
            return True
        if self._container_id is None:
            return False
        return _docker_container_running(self._config.container_name)

    def _close_log_file(self) -> None:
        """同步关闭日志文件句柄(由 ``asyncio.to_thread`` 调用)。"""
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 ``opencode web`` Docker 容器(detach 模式)。

        非托管模式(``manage_process=False``)下不做任何事 —— 调用方负责外部启动。
        启动失败(无 docker / 镜像拉取失败 / 端口占用)抛 ``OpenCodeProcessError``,
        由 API lifespan 捕获降级为 503(#118 边界:启动失败应降级,不炸 lifespan)。
        """
        if not self._manage_process:
            self._log.info(
                "opencode.process.unmanaged", base_url=self._config.base_url
            )
            return
        if self.is_running():
            raise OpenCodeProcessError("opencode container already running")
        log_path = Path(self._config.log_path)
        workdir = Path(self._config.workdir)
        # 同步文件系统操作搬到线程,避免阻塞事件循环(ASYNC240/230)。
        self._log_file = await asyncio.to_thread(
            _prepare_runtime_dirs, workdir, log_path
        )
        # 清理同名残留容器:上次 FinBoard 进程异常退出(SIGKILL / 崩溃 / Ctrl-C)
        # 时 docker 不知道宿主进程已死,容器仍 Up,新 ``docker run`` 会 exit 125
        # (name conflict)。``docker rm -f`` 幂等 —— 不存在时 no-op;会话数据由
        # named volume(``opencode-data``)保护,不受容器删除影响。
        await self._remove_stale_container()
        # 解析 docker CLI 完整路径(Windows 上是 docker.exe,同样需要 which)。
        docker_bin = _resolve_docker_binary()
        if docker_bin is None:
            raise OpenCodeProcessError(
                "docker CLI not found on PATH;请确认 Docker Desktop 已安装并启动"
            )
        # 渲染运行时 opencode.json(mcp.finboard.url ← mcp_remote_url,#157),
        # 单文件 bind mount 覆盖容器内同名文件。同步 IO 搬到线程;渲染失败
        # (仓库缺 .opencode/opencode.json 等)视为启动配置错误,fail-fast。
        try:
            runtime_config_path = await asyncio.to_thread(
                self._config.render_runtime_config
            )
        except (OSError, ValueError) as exc:
            raise OpenCodeProcessError(
                f"failed to render runtime opencode.json: {exc}"
            ) from exc
        cmd = self._config.build_docker_run_command(
            runtime_config_path=runtime_config_path
        )
        cmd[0] = docker_bin  # 用解析出的完整路径替换 "docker"
        env = self._config.build_environment()
        self._log.info(
            "opencode.container.starting",
            image=self._config.image,
            container_name=self._config.container_name,
            port=self._config.port,
            hostname=self._config.hostname,
        )
        try:
            # ``docker run -d`` 立即返回,stdout 输出容器 ID(64 hex,取前 12 位短 ID)。
            proc = await self._executor.run(
                lambda: asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )
        except FileNotFoundError as exc:
            raise OpenCodeProcessError(
                "docker CLI not found on PATH;请确认 Docker Desktop 已安装并启动"
            ) from exc
        except OpenCodeProcessError:
            raise
        except Exception as exc:
            raise OpenCodeProcessError(
                f"failed to spawn docker run: {exc}"
            ) from exc
        # 读取 ``docker run -d`` 的完整输出(stdout = 容器 ID;失败时 = 错误信息)。
        stdout_data = await self._read_docker_output(proc)
        returncode = proc.returncode
        if returncode is not None and returncode != 0:
            # docker run 失败(端口占用 / 镜像不存在 / 权限等)。
            await self._append_log(stdout_data)
            await asyncio.to_thread(self._close_log_file)
            raise OpenCodeProcessError(
                f"docker run failed (exit {returncode}): {stdout_data.strip()[:500]}"
            )
        # 容器 ID 是 stdout 第一行(64 hex 字符);取前 12 位作为短 ID。
        first_line = stdout_data.splitlines()[0].strip() if stdout_data else ""
        self._container_id = first_line[:12] or None
        self._started_at = datetime.now(UTC)
        await self._append_log(stdout_data)
        self._log.info(
            "opencode.container.started",
            container_id=self._container_id,
            container_name=self._config.container_name,
            log_path=str(log_path),
        )

    async def _read_docker_output(self, proc: asyncio.subprocess.Process) -> str:
        """读取 ``docker run`` 子进程的 stdout/stderr(detach 模式输出量小)。"""
        async def _read() -> str:
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace") if stdout else ""
        try:
            result = await self._executor.run(_read)
            return str(result) if result else ""
        except OpenCodeProcessError:
            return ""

    async def _append_log(self, text: str) -> None:
        """把 docker CLI 输出追加到日志文件(同步 IO 搬到线程)。"""
        if not text or self._log_file is None:
            return

        def _write() -> None:
            assert self._log_file is not None
            self._log_file.write(text)
            if not text.endswith("\n"):
                self._log_file.write("\n")
            self._log_file.flush()

        await asyncio.to_thread(_write)

    async def stop(self) -> None:
        """停止并移除容器(``docker rm -f``;自带 grace + SIGKILL)。

        非托管模式不终止外部容器,只关闭本地健康探测 client。
        ``docker rm -f`` 是幂等的:对已不存在的容器是 no-op。
        """
        await self._close_health_client()
        if not self._manage_process or self._container_id is None:
            return
        container_name = self._config.container_name
        self._log.info("opencode.container.stopping", container_name=container_name)
        docker_bin = _resolve_docker_binary()
        if docker_bin is None:
            # docker 不可用就清空状态;容器可能已经被外部回收。
            self._container_id = None
            self._started_at = None
            await asyncio.to_thread(self._close_log_file)
            return
        cmd = self._config.build_docker_stop_command()
        cmd[0] = docker_bin
        env = self._config.build_environment()
        try:
            proc = await self._executor.run(
                lambda: asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )
            stdout_data = await self._read_docker_output(proc)
            await self._append_log(stdout_data)
        except Exception as exc:  # stop 失败不抛,只记日志(容器可能已被外部回收)
            self._log.warning(
                "opencode.container.stop_failed", container_name=container_name, error=str(exc)
            )
        finally:
            self._container_id = None
            self._started_at = None
            await asyncio.to_thread(self._close_log_file)
            self._executor.shutdown()
            self._log.info("opencode.container.stopped", container_name=container_name)

    async def _remove_stale_container(self) -> None:
        """``start()`` 前清理同名残留容器(``docker rm -f``,幂等)。

        FinBoard 进程异常退出(SIGKILL / 崩溃 / Ctrl-C)时,``stop()`` 不会被调用,
        docker 不知道宿主进程已死,容器仍 Up。下次 ``docker run`` 遇同名容器 exit 125
        (name conflict)。本方法在 ``start()`` 内无条件执行 ``docker rm -f``:

        - 容器不存在 → no-op(stdout 空,exit 0)
        - 容器存在(Up/Exited) → 强制移除(会话 DB / auth 由 named volume 保护,
          不随容器删除丢失)

        失败不抛(只记 warning)—— 容器清理失败会在后续 ``docker run`` 报 exit 125,
        那里有完整的错误处理。
        """
        docker_bin = _resolve_docker_binary()
        if docker_bin is None:
            return  # docker 不可用,start() 稍后会抛完整错误
        cmd = self._config.build_docker_stop_command()
        cmd[0] = docker_bin
        env = self._config.build_environment()
        try:
            proc = await self._executor.run(
                lambda: asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )
            stdout_data = await self._read_docker_output(proc)
            if stdout_data.strip():
                self._log.info(
                    "opencode.container.stale_removed",
                    container_name=self._config.container_name,
                    output=stdout_data.strip()[:200],
                )
        except Exception as exc:
            self._log.warning(
                "opencode.container.stale_remove_failed",
                container_name=self._config.container_name,
                error=str(exc),
            )

    async def wait_ready(
        self,
        *,
        timeout: float = _DEFAULT_READY_TIMEOUT,  # noqa: ASYNC109
        interval: float = _DEFAULT_READY_INTERVAL,
    ) -> None:
        """轮询健康端点直到就绪或超时。

        非托管模式下同样探测外部实例是否在线。容器化后探测路径不变
        (httpx → 宿主机侧 ``127.0.0.1:{port}/global/health``,经端口映射进容器)。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last_error: str | None = None
        client = self._ensure_health_client()
        url = f"{self._config.base_url}{_HEALTH_PATH}"
        while asyncio.get_event_loop().time() < deadline:
            if self._manage_process and not self.is_running():
                last_error = "container exited before becoming ready"
                break
            try:
                resp = await client.get(url, timeout=5.0)
                if resp.status_code < 400:
                    self._version = _extract_version(resp.json())
                    self._log.info(
                        "opencode.container.ready",
                        container_id=self._container_id,
                        version=self._version,
                    )
                    return
                last_error = f"health -> {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            await asyncio.sleep(interval)
        raise OpenCodeProcessError(
            f"opencode container not ready within {timeout}s: {last_error}"
        )

    # ------------------------------------------------------------------
    # 健康探测
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any] | None:
        """探测 OpenCode 健康端点;不可达返回 ``None``。"""
        client = self._ensure_health_client()
        url = f"{self._config.base_url}{_HEALTH_PATH}"
        try:
            resp = await client.get(url, timeout=5.0)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if self._version is None:
            self._version = _extract_version(data)
        return data if isinstance(data, dict) else None

    async def status(self) -> ProcessStatus:
        """返回容器运行状态快照。"""
        healthy: bool | None = None
        if self.is_running():
            data = await self.health()
            healthy = data is not None
        # health() 内部会回写 self._version,这里在调用之后读取。
        return ProcessStatus(
            running=self.is_running(),
            managed=self._manage_process,
            container_id=self._container_id,
            base_url=self._config.base_url,
            healthy=healthy,
            version=self._version,
            started_at=self._started_at,
        )

    async def aclose(self) -> None:
        """关闭所有底层资源(等价于 :meth:`stop`)。"""
        await self.stop()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure_health_client(self) -> httpx.AsyncClient:
        if self._health_client is None:
            # #157 移除 basic auth(单用户明文 URL 决策):OpenCode Web 无鉴权,
            # 健康探测直接 GET;宿主机侧 127.0.0.1 绑定是唯一网络边界。
            self._health_client = httpx.AsyncClient(
                base_url=self._config.base_url, timeout=10.0
            )
        return self._health_client

    async def _close_health_client(self) -> None:
        client = self._health_client
        self._health_client = None
        if client is not None:
            await client.aclose()


def _extract_version(data: Any) -> str | None:
    """从健康响应里提取版本号(容错多种字段名)。"""
    if not isinstance(data, dict):
        return None
    for key in ("version", "Version", "server_version"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _prepare_runtime_dirs(workdir: Path, log_path: Path) -> Any:
    """同步创建工作目录 + 打开日志文件(由 ``asyncio.to_thread`` 调用)。

    返回已打开的日志文件句柄(append 模式)。调用方负责在停止时关闭。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path.open("a", encoding="utf-8")


#: Windows Docker Desktop 常见安装路径模板(按 ``%VAR%`` 展开)。
#: 探测顺序:标准全用户安装 → 当前用户安装 → 版本绑定 bin。Docker Desktop 安装器
#: 通常会把 ``resources\bin`` 加进系统 PATH,但从 IDE / 服务 / 非交互 shell 启动
#: FinBoard 时该目录可能不在 PATH 中(``shutil.which`` 返回 None),需要回退探测。
_WINDOWS_DOCKER_PATHS: tuple[str, ...] = (
    r"${ProgramFiles}\Docker\Docker\resources\bin\docker.exe",
    r"${ProgramFiles(x86)}\Docker\Docker\resources\bin\docker.exe",
    r"${LOCALAPPDATA}\Docker\Docker\resources\bin\docker.exe",
    r"${ProgramData}\DockerDesktop\version-bin\docker.exe",
)


def _windows_docker_binary_candidates() -> list[str]:
    """展开 Windows Docker Desktop 安装路径模板为具体候选路径。

    模板中的 ``${VAR}`` 用 ``os.environ`` 展开;未设置的环境变量展开为空串,
    对应候选被跳过。重复路径去重(不同变量可能指向同一目录)。
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for template in _WINDOWS_DOCKER_PATHS:
        # 简单的 ``${VAR}`` 展开(避免引入 string.Template 对 ``$VAR`` 的歧义)。
        path = os.path.expandvars(template)
        if path and path not in seen:
            seen.add(path)
            candidates.append(path)
    return candidates


def _resolve_docker_binary() -> str | None:
    """解析 docker CLI 完整路径;不存在返回 ``None``。

    优先 ``shutil.which("docker")``(走 PATH);Windows 上 docker 安装为
    ``docker.exe``,``create_subprocess_exec`` 不走 shell、不按 PATHEXT 自动补
    扩展名,故 ``which`` 会返回含扩展名的完整路径。

    当 ``which`` 失败时(IDE / 服务 / 非交互 shell 启动 FinBoard,Docker Desktop
    的 bin 目录不在 PATH 中),回退探测 Windows Docker Desktop 标准安装路径。
    """
    found = shutil.which("docker")
    if found:
        return found
    if sys.platform == "win32":
        for path in _windows_docker_binary_candidates():
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


def _docker_container_running(container_name: str) -> bool:
    """查 ``docker inspect`` 容器是否 Running(同步瞬时命令,超时 5s)。

    容器不存在 / docker 不可用 / inspect 失败一律视为「未运行」(保守判定)。
    """
    docker_bin = _resolve_docker_binary()
    if docker_bin is None:
        return False
    try:
        result = subprocess.run(
            [
                docker_bin, "inspect",
                "--type=container",
                "-f", "{{.State.Running}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip().lower() == "true"


def which_opencode(binary: str = "opencode") -> str | None:
    """解析 OpenCode 可执行文件路径;不存在返回 ``None``。

    .. deprecated:: Docker 隔离后不再 spawn 宿主机 opencode,本函数仅保留为
       向后兼容导出(旧测试 / 外部调用可能引用)。容器化路径用 ``_resolve_docker_binary``。
    """
    return shutil.which(binary)
