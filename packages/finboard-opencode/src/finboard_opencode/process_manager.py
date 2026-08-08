"""OpenCode 进程管理器(issue #118)。

由 FinBoard 网关层托管一个**进程级隔离**的 ``opencode web`` 子进程:独立工作目录、
仅必要环境变量(绝不继承 FinBoard 的 DB 密码 / broker 凭证 / API Key)、网络绑定
127.0.0.1。提供启动 / 停止 / 健康探测 / 就绪轮询。

OpenCode v1.18.15 不支持 ``--base-path`` 子路径部署(上游 PR #28326 未合并),因此
FinBoard 前端通过 **iframe 跨源嵌入** OpenCode Web 根 URL,FinBoard 网关只做控制面
(会话授权 + 凭证签发 + 进程生命周期),不透传 OpenCode 流量。

红线:本模块只管理研究运行时进程,不连接实盘 broker / 账户 / 订单 / 持仓 / 风控。
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
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
_DEFAULT_READY_TIMEOUT = 20.0
_DEFAULT_READY_INTERVAL = 0.5


@dataclass(frozen=True, slots=True)
class OpenCodeProcessConfig:
    """OpenCode 子进程启动配置。"""

    #: 可执行文件路径(版本锁定;默认从 PATH 解析 ``opencode``)。
    binary: str = "opencode"
    #: 监听端口(默认 4097,区别于 ``opencode serve`` 的 4096)。
    port: int = 4097
    #: 监听地址(进程级隔离:强制 127.0.0.1,不暴露公网)。
    hostname: str = "127.0.0.1"
    #: 允许跨源访问的浏览器源(iframe 跨源嵌入必须显式允许 FinBoard 源)。
    cors_origins: list[str] = field(default_factory=list)
    #: basic auth 用户名(OpenCode 默认 ``opencode``)。
    username: str = "opencode"
    #: basic auth 密码;留空则启动时自动生成强随机密码并回填到 :attr:`resolved_password`。
    password: str = ""
    #: 研究沙箱工作目录(OpenCode 在此目录运行,与 FinBoard 仓库隔离)。
    workdir: str = ".opencode/workspace"
    #: 子进程日志文件路径(stdout + stderr 合并)。
    log_path: str = ".opencode/logs/opencode-web.log"
    #: 额外注入的环境变量(LLM provider Key 等);优先级高于继承白名单。
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://{self.hostname}:{self.port}"

    def build_command(self, *, subcommand: str = "web") -> list[str]:
        """构造 ``opencode <subcommand>`` 启动参数。"""
        cmd = [self.binary, subcommand]
        cmd += ["--port", str(self.port)]
        cmd += ["--hostname", self.hostname]
        if self.cors_origins:
            cmd += ["--cors", *self.cors_origins]
        return cmd

    def build_environment(
        self, *, parent_env: dict[str, str] | None = None
    ) -> dict[str, str]:
        """构造子进程环境变量(严格白名单 + 显式覆盖)。

        绝不继承 FinBoard 自身的 DB 密码 / broker 凭证 / API Key —— 只保留系统必需
        变量,再叠加 OpenCode 运行所需(basic auth / LLM provider Key)。
        """
        parent = parent_env if parent_env is not None else dict(os.environ)
        env: dict[str, str] = {}
        for key in _INHERITED_ENV_KEYS:
            value = parent.get(key)
            if value:
                env[key] = value
        if self.username:
            env["OPENCODE_SERVER_USERNAME"] = self.username
        env["OPENCODE_SERVER_PASSWORD"] = self.resolved_password
        env.update(self.env_overrides)
        return env

    @property
    def resolved_password(self) -> str:
        """返回实际生效的密码(配置为空时生成强随机密码)。"""
        return self.password or _BOOTSTRAP_PASSWORD_PLACEHOLDER


#: 配置为空、尚未 :meth:`OpenCodeProcessManager.start` 时的占位符。
#: ``start`` 会用真实随机密码替换它。
_BOOTSTRAP_PASSWORD_PLACEHOLDER = "__AUTO_GENERATE__"


@dataclass(slots=True)
class ProcessStatus:
    """进程运行状态快照(用于网关 ``/status`` 端点)。"""

    running: bool
    managed: bool
    pid: int | None
    base_url: str
    healthy: bool | None
    version: str | None
    started_at: datetime | None
    last_error: str | None = None


class OpenCodeProcessError(RuntimeError):
    """OpenCode 进程管理失败。"""


def _generate_password() -> str:
    """生成 24 字节强随机密码(basic auth)。"""
    return secrets.token_urlsafe(18)


class OpenCodeProcessManager:
    """托管 ``opencode web`` 子进程的生命周期。

    单实例 + 进程级隔离:全局一个 OpenCode 进程服务所有研究会话,会话级隔离由
    OpenCode session + FinBoard conversation 授权共同保证。

    生命周期由 FinBoard API lifespan 管理(见 ``app.py``);``stop`` 必须在 shutdown
    时调用以回收子进程。
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
        self._proc: asyncio.subprocess.Process | None = None
        self._log_file: Any = None
        self._started_at: datetime | None = None
        self._effective_password: str = config.password or _generate_password()
        self._health_client: httpx.AsyncClient | None = None
        self._version: str | None = None

    @property
    def config(self) -> OpenCodeProcessConfig:
        return self._config

    @property
    def effective_password(self) -> str:
        """实际生效的 basic auth 密码(配置空则启动时生成的随机值)。"""
        return self._effective_password

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def is_managed(self) -> bool:
        """是否由本管理器托管进程(False = 外部已启动,仅连接)。"""
        return self._manage_process

    def is_running(self) -> bool:
        if not self._manage_process:
            return True
        return self._proc is not None and self._proc.returncode is None

    def _close_log_file(self) -> None:
        """同步关闭日志文件句柄(由 ``asyncio.to_thread`` 调用)。"""
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 ``opencode web`` 子进程并等待健康就绪。

        非托管模式(``manage_process=False``)下不做任何事 —— 调用方负责外部启动。
        """
        if not self._manage_process:
            self._log.info(
                "opencode.process.unmanaged", base_url=self._config.base_url
            )
            return
        if self.is_running():
            raise OpenCodeProcessError("opencode process already running")
        workdir = Path(self._config.workdir)
        log_path = Path(self._config.log_path)
        # 同步文件系统操作搬到线程,避免阻塞事件循环(ASYNC240/230)。
        self._log_file = await asyncio.to_thread(
            _prepare_runtime_dirs, workdir, log_path
        )
        cmd = self._config.build_command()
        env = self._config.build_environment()
        # 用生效密码覆盖占位符。
        env["OPENCODE_SERVER_PASSWORD"] = self._effective_password
        self._log.info(
            "opencode.process.starting",
            cmd=cmd,
            workdir=str(workdir),
            port=self._config.port,
            hostname=self._config.hostname,
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workdir),
                env=env,
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise OpenCodeProcessError(
                f"opencode binary not found: {self._config.binary}"
            ) from exc
        self._started_at = datetime.now(UTC)
        self._log.info(
            "opencode.process.started", pid=self._proc.pid, log_path=str(log_path)
        )

    async def wait_ready(
        self,
        *,
        timeout: float = _DEFAULT_READY_TIMEOUT,  # noqa: ASYNC109
        interval: float = _DEFAULT_READY_INTERVAL,
    ) -> None:
        """轮询健康端点直到就绪或超时。

        非托管模式下同样探测外部实例是否在线。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last_error: str | None = None
        client = self._ensure_health_client()
        url = f"{self._config.base_url}{_HEALTH_PATH}"
        while asyncio.get_event_loop().time() < deadline:
            if self._manage_process and not self.is_running():
                last_error = "process exited before becoming ready"
                break
            try:
                resp = await client.get(url, timeout=5.0)
                if resp.status_code < 400:
                    self._version = _extract_version(resp.json())
                    self._log.info(
                        "opencode.process.ready",
                        pid=self._proc.pid
                        if (self._manage_process and self._proc is not None)
                        else None,
                        version=self._version,
                    )
                    return
                last_error = f"health -> {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            await asyncio.sleep(interval)
        raise OpenCodeProcessError(
            f"opencode process not ready within {timeout}s: {last_error}"
        )

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        """优雅停止子进程(SIGTERM → 超时 SIGKILL)。

        非托管模式不终止外部进程,只关闭本地健康探测 client。
        """
        await self._close_health_client()
        if not self._manage_process or self._proc is None:
            return
        proc = self._proc
        if proc.returncode is not None:
            self._proc = None
            return
        self._log.info("opencode.process.stopping", pid=proc.pid)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except TimeoutError:
            self._log.warning("opencode.process.force_kill", pid=proc.pid)
            proc.kill()
            await proc.wait()
        finally:
            self._proc = None
            self._started_at = None
            await asyncio.to_thread(self._close_log_file)
            self._log.info("opencode.process.stopped")

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
        """返回进程运行状态快照。"""
        healthy: bool | None = None
        if self.is_running():
            data = await self.health()
            healthy = data is not None
        # health() 内部会回写 self._version,这里在调用之后读取。
        return ProcessStatus(
            running=self.is_running(),
            managed=self._manage_process,
            pid=self._proc.pid if (self._manage_process and self._proc) else None,
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


def which_opencode(binary: str = "opencode") -> str | None:
    """解析 OpenCode 可执行文件路径;不存在返回 ``None``。"""
    return shutil.which(binary)
