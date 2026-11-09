"""Issue #517 —— 独立研究 MCP 与按客户端授权。

覆盖验收标准:

* 创建客户端 → 只读工具可用;readonly 预设调写工具 → ``denied`` 且业务零写入;
* 吊销 / 错误密钥 / 过期 token → 401,且不泄露客户端存在性;
* 旧 ``FINBOARD_MCP_AUTH_TOKEN`` 兼容为 ``MC-legacy`` / 通配作用域;
* 重复客户端名被拒绝;数据库不存明文 token;
* ``TOOL_SCOPES`` 覆盖全部注册工具;
* stdio 默认作用域拒绝写操作;
* 启动门禁:``FINBOARD_MCP_ENABLED=false`` 退出码 2;非 loopback 绑定无
  ``FINBOARD_MCP_ALLOW_REMOTE=true`` 具名拒绝;无 OpenCode 环境也能独立启动。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import pytest_asyncio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_mcp.audit import AuditRecorder
from finboard_mcp.auth import BearerTokenMiddleware, DatabaseIdentityResolver
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import run_tool
from finboard_mcp.identity import (
    McpClientIdentity,
    generate_client_id,
    generate_client_token,
)
from finboard_mcp.scopes import (
    SCOPE_PRESETS,
    TOOL_SCOPES,
    VALID_SCOPES,
    arm_scope_enforcement,
    parse_scopes,
    reset_scope_policy,
)
from finboard_mcp.server import build_mcp_server
from finboard_persistence import session_factory
from finboard_persistence.mcp_client_repo import (
    McpClientConflictError,
    McpClientRepository,
)
from finboard_persistence.models import McpClientModel
from tests.integration.conftest import TEST_DB_URL

_REPO_ROOT = Path(__file__).resolve().parents[2]
_READ_TOOL = "finboard.run.list"
_WRITE_TOOL = "finboard.run.queue"


def _mcp_env(**overrides: str) -> dict[str, str]:
    """无 .env / 无 OpenCode 环境的 MCP 子进程环境。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FINBOARD_MCP_", "FINBOARD_OPENCODE_"))
    }
    env.update(overrides)
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, *, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@contextmanager
def _server_process(tmp_path: Path, **env_overrides: str) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "finboard_mcp"],
        cwd=tmp_path,
        env=_mcp_env(**env_overrides),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - 防御性清理
            proc.kill()
            proc.wait(timeout=20)


@asynccontextmanager
async def _http_session(url: str, token: str) -> AsyncIterator[ClientSession]:
    """带 Bearer header 的 Streamable HTTP MCP 会话(真实走 HTTP)。"""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=60.0
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@pytest.fixture
def client_smaker(_engine: Any) -> async_sessionmaker[AsyncSession]:
    return session_factory(_engine)


@pytest_asyncio.fixture(autouse=True)
async def _clean_clients(_engine: Any) -> Any:
    """每个用例前后清空 ``mcp_clients``,避免跨用例的凭据串扰。"""

    async def _clean() -> None:
        async with session_factory(_engine)() as session:
            await session.execute(delete(McpClientModel))
            await session.commit()

    await _clean()
    yield
    await _clean()


async def _create_client(
    smaker: async_sessionmaker[AsyncSession],
    *,
    name: str | None = None,
    scopes: tuple[str, ...] = ("research.read",),
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    """创建客户端,返回 ``(client_id, plaintext_token)``。"""
    client_id = generate_client_id()
    material = generate_client_token(client_id)
    async with smaker() as session:
        await McpClientRepository(session).append(
            client_id=client_id,
            name=name or f"client-{client_id[-6:]}",
            token_hash=material.token_hash,
            token_prefix=material.token_prefix,
            scopes=list(scopes),
            created_by="pytest",
            expires_at=expires_at,
        )
        await session.commit()
    return client_id, material.token


async def _authenticate(
    middleware: BearerTokenMiddleware, token: str
) -> tuple[int, McpClientIdentity | None]:
    """驱动一次 ASGI HTTP 请求,返回 (status, 中间件解析出的身份)。"""
    captured: dict[str, McpClientIdentity | None] = {}

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["identity"] = (scope.get("state") or {}).get("mcp_identity")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware._app = inner
    responses: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        responses.append(message)

    async def receive() -> None:
        return None

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await middleware(scope, receive, send)
    return int(responses[0]["status"]), captured.get("identity")


def _middleware(smaker: async_sessionmaker[AsyncSession]) -> BearerTokenMiddleware:
    async def _passthrough(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("未认证请求不应进入 inner app")

    return BearerTokenMiddleware(
        _passthrough, resolver=DatabaseIdentityResolver(smaker)
    )


def _spy() -> tuple[Any, dict[str, bool]]:
    hit = {"called": False}

    async def handler() -> dict[str, bool]:
        hit["called"] = True
        return {"written": True}

    return handler, hit


async def _run(
    tool_name: str,
    *,
    identity: McpClientIdentity | None,
    handler: Any,
) -> ToolEnvelope:
    return await run_tool(
        audit=AuditRecorder(),
        tool_name=tool_name,
        arguments={"limit": 1},
        handler=handler,
        identity=identity,
    )


class TestClientCredentials:
    async def test_read_client_can_call_read_tool(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, token = await _create_client(client_smaker)
        status, identity = await _authenticate(_middleware(client_smaker), token)
        assert status == 200
        assert identity is not None
        assert identity.scopes == frozenset({"research.read"})

        arm_scope_enforcement(readonly_only=False)
        try:
            handler, hit = _spy()
            env = await _run(_READ_TOOL, identity=identity, handler=handler)
        finally:
            reset_scope_policy()
        assert env.status == "ok"
        assert hit["called"] is True

    async def test_readonly_client_write_tool_denied_with_zero_writes(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, token = await _create_client(client_smaker)
        _, identity = await _authenticate(_middleware(client_smaker), token)
        assert identity is not None

        arm_scope_enforcement(readonly_only=False)
        try:
            handler, hit = _spy()
            env = await _run(_WRITE_TOOL, identity=identity, handler=handler)
        finally:
            reset_scope_policy()
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"
        assert hit["called"] is False

    async def test_revoked_token_401_and_last_used_unchanged(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        client_id, token = await _create_client(client_smaker)
        middleware = _middleware(client_smaker)
        status, _ = await _authenticate(middleware, token)
        assert status == 200

        async with client_smaker() as session:
            repo = McpClientRepository(session)
            row = await repo.revoke(client_id)
            assert row is not None
            assert row.revoked_at is not None
            await session.commit()
            before = row.last_used_at

        status, identity = await _authenticate(middleware, token)
        assert status == 401
        assert identity is None
        async with client_smaker() as session:
            row = await McpClientRepository(session).get_by_client_id(client_id)
        assert row is not None
        assert row.last_used_at == before

    async def test_wrong_secret_and_unknown_client_share_401(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, token = await _create_client(client_smaker)
        middleware = _middleware(client_smaker)
        wrong = token[:-4] + "AAAA"
        wrong_status, _ = await _authenticate(middleware, wrong)
        unknown_status, _ = await _authenticate(middleware, "fbmc_MC-000000000000_unknown")
        assert wrong_status == 401
        assert unknown_status == 401

    async def test_expired_token_401(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, token = await _create_client(
            client_smaker, expires_at=datetime.now(UTC) - timedelta(days=1)
        )
        status, identity = await _authenticate(_middleware(client_smaker), token)
        assert status == 401
        assert identity is None

    async def test_legacy_env_token_has_wildcard_and_can_write(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async def _unused_app(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("legacy token 请求应通过中间件")

        middleware = BearerTokenMiddleware(
            _unused_app, token="legacy-shared-token"
        )
        status, identity = await _authenticate(middleware, "legacy-shared-token")
        assert status == 200
        assert identity is not None
        assert identity.client_id == "MC-legacy"
        assert identity.scopes == frozenset({"*"})

        arm_scope_enforcement(readonly_only=False)
        try:
            handler, hit = _spy()
            env = await _run(_WRITE_TOOL, identity=identity, handler=handler)
        finally:
            reset_scope_policy()
        assert env.status == "ok"
        assert hit["called"] is True

    async def test_duplicate_client_name_rejected(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _create_client(client_smaker, name="codex")
        with pytest.raises(McpClientConflictError):
            await _create_client(client_smaker, name="codex")

    async def test_no_plaintext_secret_stored(
        self, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        client_id, token = await _create_client(client_smaker)
        async with client_smaker() as session:
            row = await McpClientRepository(session).get_by_client_id(client_id)
        assert row is not None
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert row.token_hash != token
        # 明文 token / secret 不得出现在任何持久化字段。
        for value in (row.token_hash, row.token_prefix, row.name, row.client_id):
            assert token not in value
            assert token.rsplit("_", 1)[-1] not in value


class TestCli:
    async def test_list_shows_token_prefix_but_never_plaintext(
        self, tmp_path: Path, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, token = await _create_client(
            client_smaker, name="cli-list-smoke", scopes=("research.read",)
        )
        # token 格式 fbmc_<client_id>_<secret>;secret 本身可能含 "_"。
        secret = token.split("_", 2)[2]
        prefix = secret[:16]
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "finboard_app.cli", "mcp", "client", "list"],
            cwd=tmp_path,
            env=_mcp_env(FINBOARD_DB_URL=TEST_DB_URL),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "PREFIX" in result.stdout
        assert prefix in result.stdout
        assert token not in result.stdout
        assert secret not in result.stdout


class TestScopeModel:
    async def test_tool_scopes_cover_all_registered_tools(self) -> None:
        names = {tool.name for tool in await build_mcp_server().list_tools()}
        # 不写死数量:``TOOL_SCOPES`` 与注册表的双向集合相等才是契约本体;
        # 新增/删除工具必须同步改 scopes 表,而不是改这个数字。
        assert set(TOOL_SCOPES) == names
        for tool_name, scopes in TOOL_SCOPES.items():
            assert scopes, tool_name
            assert scopes <= VALID_SCOPES, tool_name
            assert "*" not in scopes, tool_name

    async def test_stdio_default_scope_denies_writes(self) -> None:
        from finboard_mcp.context import build_stdio_identity

        stdio = build_stdio_identity("research.read")
        arm_scope_enforcement(readonly_only=False, default_identity=stdio)
        try:
            write_handler, write_hit = _spy()
            denied_env = await run_tool(
                audit=AuditRecorder(),
                tool_name=_WRITE_TOOL,
                arguments={},
                handler=write_handler,
            )
            read_handler, read_hit = _spy()
            read_env = await run_tool(
                audit=AuditRecorder(),
                tool_name=_READ_TOOL,
                arguments={},
                handler=read_handler,
            )
        finally:
            reset_scope_policy()
        assert denied_env.status == "denied"
        assert write_hit["called"] is False
        assert read_env.status == "ok"
        assert read_hit["called"] is True

    async def test_parse_scopes_rejects_unknown_and_wildcard(self) -> None:
        assert parse_scopes("research.read,research.write") == frozenset(
            {"research.read", "research.write"}
        )
        with pytest.raises(ValueError, match="未知 MCP 作用域"):
            parse_scopes("research.read,not_a_scope")
        with pytest.raises(ValueError, match="通配作用域"):
            parse_scopes("*")
        assert SCOPE_PRESETS["data-operator"] >= SCOPE_PRESETS["topic-writer"]

    async def test_side_effect_tool_scope_classification(self) -> None:
        """有落盘 / 启动运行副作用的工具不得归入只读作用域。"""
        assert TOOL_SCOPES["finboard_report_export"] == frozenset({"research.write"})
        assert TOOL_SCOPES["finboard_factor_series_build"] == frozenset(
            {"research.run"}
        )


class TestStartupGate:
    def _run_mcp(
        self, tmp_path: Path, **env_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        """在无 .env / 无 OpenCode 环境的空目录启动 ``python -m finboard_mcp``。"""
        return subprocess.run(
            [sys.executable, "-m", "finboard_mcp"],
            cwd=tmp_path,
            env=_mcp_env(**env_overrides),
            input="",
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    async def _run_mcp_async(
        self, tmp_path: Path, **env_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(self._run_mcp, tmp_path, **env_overrides)

    async def test_disabled_exits_2_with_named_reason(self, tmp_path: Path) -> None:
        result = await self._run_mcp_async(
            tmp_path,
            FINBOARD_MCP_ENABLED="false",
        )
        assert result.returncode == 2
        assert "FINBOARD_MCP_ENABLED=false" in result.stderr

    async def test_non_loopback_without_allow_remote_exits_named(
        self, tmp_path: Path
    ) -> None:
        result = await self._run_mcp_async(
            tmp_path,
            FINBOARD_MCP_ENABLED="true",
            FINBOARD_MCP_TRANSPORT="streamable-http",
            FINBOARD_MCP_HOST="0.0.0.0",
            FINBOARD_MCP_ALLOW_REMOTE="false",
            FINBOARD_MCP_AUTH_TOKEN="legacy-token",
        )
        assert result.returncode == 2
        assert "FINBOARD_MCP_ALLOW_REMOTE" in result.stderr

    async def test_standalone_stdio_starts_without_opencode_env(
        self, tmp_path: Path
    ) -> None:
        """无 OpenCode 环境、无 .env 时 stdio 可独立启动并干净退出。"""
        result = await self._run_mcp_async(
            tmp_path,
            FINBOARD_MCP_ENABLED="true",
            FINBOARD_MCP_TRANSPORT="stdio",
            FINBOARD_MCP_STDIO_SCOPES="research.read",
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "mcp.stdio_scopes" in combined
        assert "Traceback" not in combined

    @pytest.mark.usefixtures("_engine")
    async def test_http_without_any_credential_exits_named(
        self, tmp_path: Path
    ) -> None:
        """无 env token 且 mcp_clients 无有效客户端 → HTTP 具名拒绝启动。"""
        result = await self._run_mcp_async(
            tmp_path,
            FINBOARD_MCP_ENABLED="true",
            FINBOARD_MCP_TRANSPORT="streamable-http",
            FINBOARD_MCP_HOST="127.0.0.1",
            FINBOARD_MCP_AUTH_TOKEN="",
            FINBOARD_DB_URL=TEST_DB_URL,
        )
        assert result.returncode == 2
        assert "没有可用凭据" in result.stderr


class TestHttpEndToEnd:
    """真实 HTTP 子进程(非 in-process):Windows SelectorEventLoop + psycopg 路径。"""

    async def test_client_token_read_ok_write_denied_over_http(
        self, tmp_path: Path, client_smaker: async_sessionmaker[AsyncSession]
    ) -> None:
        port = _free_port()
        _, token = await _create_client(client_smaker, scopes=("research.read",))
        env = {
            "FINBOARD_MCP_ENABLED": "true",
            "FINBOARD_MCP_TRANSPORT": "streamable-http",
            "FINBOARD_MCP_HOST": "127.0.0.1",
            "FINBOARD_MCP_PORT": str(port),
            "FINBOARD_MCP_AUTH_TOKEN": "",
            "FINBOARD_DB_URL": TEST_DB_URL,
        }
        with _server_process(tmp_path, **env) as proc:
            assert await asyncio.to_thread(_wait_port, port), "MCP HTTP server 未就绪"
            assert proc.poll() is None, "MCP HTTP server 提前退出"
            async with _http_session(
                f"http://127.0.0.1:{port}/mcp", token
            ) as session:
                read = await session.call_tool("finboard_run_list", {"limit": 1})
                read_payload = read.structured_content
                assert read_payload is not None
                assert read_payload["status"] == "ok", read_payload

                write = await session.call_tool(
                    "finboard_run_queue", {"payload": {}}
                )
                write_payload = write.structured_content
                assert write_payload is not None
                assert write_payload["status"] == "denied", write_payload
                assert write_payload["error"]["kind"] == "permission_denied"

    async def test_duplicate_start_gives_named_error(self, tmp_path: Path) -> None:
        """同端口重复启动 → FinBoard 具名错误(不是 uvicorn 原始 traceback)。"""
        port = _free_port()
        env = {
            "FINBOARD_MCP_ENABLED": "true",
            "FINBOARD_MCP_TRANSPORT": "streamable-http",
            "FINBOARD_MCP_HOST": "127.0.0.1",
            "FINBOARD_MCP_PORT": str(port),
            "FINBOARD_MCP_AUTH_TOKEN": "legacy-token",
        }
        with _server_process(tmp_path, **env):
            assert await asyncio.to_thread(_wait_port, port), "首个 MCP server 未就绪"
            second = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "finboard_mcp"],
                cwd=tmp_path,
                env=_mcp_env(**env),
                input="",
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        assert second.returncode == 2
        combined = second.stdout + second.stderr
        assert "绑定失败" in combined
        assert "FINBOARD_MCP_PORT" in combined

    async def test_allow_remote_true_starts_non_loopback(self, tmp_path: Path) -> None:
        """ALLOW_REMOTE=true 正向用例:非 loopback 绑定可启动并监听。"""
        port = _free_port()
        env = {
            "FINBOARD_MCP_ENABLED": "true",
            "FINBOARD_MCP_TRANSPORT": "streamable-http",
            "FINBOARD_MCP_HOST": "0.0.0.0",
            "FINBOARD_MCP_ALLOW_REMOTE": "true",
            "FINBOARD_MCP_PORT": str(port),
            "FINBOARD_MCP_AUTH_TOKEN": "legacy-token",
        }
        with _server_process(tmp_path, **env) as proc:
            assert await asyncio.to_thread(_wait_port, port), "非 loopback MCP server 未就绪"
            assert proc.poll() is None


async def test_database_resolver_touches_last_used(
    client_smaker: async_sessionmaker[AsyncSession],
) -> None:
    """认证成功会记录 last_used_at;同 token 再认证不重复写(60s 节流)。"""
    client_id, token = await _create_client(client_smaker)
    resolver = DatabaseIdentityResolver(client_smaker)
    identity = await resolver(token)
    assert identity is not None
    async with client_smaker() as session:
        row = await McpClientRepository(session).get_by_client_id(client_id)
    assert row is not None
    assert row.last_used_at is not None

    first = row.last_used_at
    await asyncio.sleep(0.01)
    assert await resolver(token) is not None
    async with client_smaker() as session:
        row = await McpClientRepository(session).get_by_client_id(client_id)
    assert row is not None
    assert row.last_used_at == first
