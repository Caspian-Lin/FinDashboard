"""MCP 客户端身份契约与 Bearer 凭据原语(issue #517)。

``McpClientIdentity`` 是 #518(统一研究操作审计)依赖的跨议题共享契约:

* ``client_id`` —— ``MC-<12hex>``(CLI 创建)或 ``MC-legacy`` / ``local:stdio``;
* ``name`` —— 人类可读客户端名;
* ``scopes`` —— 授权集合,取值见 :mod:`finboard_mcp.scopes`;``"*"`` 是
  仅用于 ``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 的兼容通配。

身份如何到达工具(设计选择):**HTTP 由 ASGI 中间件把解析出的身份写进
``scope["state"]["mcp_identity"]``,工具入口 ``app_context(ctx)`` 再把它写入
``contextvars.ContextVar``,``run_tool`` 在唯一漏斗读取**。不采用「每个工具
签名多一个 identity 参数」:148 个工具、135 处 ``run_tool`` 调用点会全面
改签名且容易漏;也不采用纯 contextvar:实测 MCP SDK 的 in-memory / stdio
任务上下文不会继承外层 ``ContextVar``,必须由工具调用自身所在的
``request_context`` 注入。stdio 无 HTTP 请求时回退到进程级
``FINBOARD_MCP_STDIO_SCOPES`` 默认身份。

Token 只存 ``sha256`` 摘要;明文仅在创建时打印一次。``token_prefix`` 是
secret 前 16 字符,只用于人工辨认,不能用于认证。
"""

from __future__ import annotations

import hashlib
import secrets
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Final

#: 服务端签发 token 的固定前缀(便于日志/配置中辨认,不含秘密)。
TOKEN_PREFIX: Final[str] = "fbmc"

#: ``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 合成的客户端 ID(下一版本移除)。
LEGACY_CLIENT_ID: Final[str] = "MC-legacy"

#: stdio 本机子进程的客户端 ID(无 HTTP 请求上下文)。
STDIO_CLIENT_ID: Final[str] = "local:stdio"


@dataclass(frozen=True, slots=True)
class McpClientIdentity:
    """一次 MCP 调用解析出的客户端身份(#517 契约,#518 复用)。"""

    client_id: str
    name: str
    #: 授权集合;元素是 ``Scope`` 字面量或 ``"*"``(仅 legacy 兼容)。
    scopes: frozenset[str]

    def has_wildcard(self) -> bool:
        return "*" in self.scopes


@dataclass(frozen=True, slots=True)
class GeneratedClientToken:
    """创建客户端时一次性返回的凭据材料(明文只打印一次,不落库)。"""

    token: str
    token_hash: str
    token_prefix: str


def generate_client_id() -> str:
    """生成 ``MC-<12hex>`` 客户端 ID。"""
    return f"MC-{secrets.token_hex(6)}"


def generate_client_token(client_id: str) -> GeneratedClientToken:
    """生成 ``fbmc_<client_id>_<32B urlsafe>`` token 及其摘要/前缀。"""
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}_{client_id}_{secret}"
    return GeneratedClientToken(
        token=token,
        token_hash=hash_client_token(token),
        # 人工辨认用;16 字符不足以重建 32 字节 secret。
        token_prefix=secret[:16],
    )


def hash_client_token(token: str) -> str:
    """``sha256(token)`` 十六进制(64 字符),与 ``mcp_clients.token_hash`` 同构。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: 当前调用上下文中的客户端身份。由 ``app_context(ctx)`` 在工具调用所在任务
#: 写入;``run_tool`` 读取。缺省 None = 未解析(scope 强制开启时按拒绝处理)。
_current_identity: ContextVar[McpClientIdentity | None] = ContextVar(
    "finboard_mcp_identity", default=None
)


def current_identity() -> McpClientIdentity | None:
    """读取当前工具调用的客户端身份(未解析返回 ``None``)。"""
    return _current_identity.get()


def set_current_identity(identity: McpClientIdentity | None) -> Token[McpClientIdentity | None]:
    """在工具调用任务内绑定身份;返回可用于恢复的 token。"""
    return _current_identity.set(identity)


def reset_current_identity(token: Token[McpClientIdentity | None]) -> None:
    _current_identity.reset(token)


__all__ = [
    "LEGACY_CLIENT_ID",
    "STDIO_CLIENT_ID",
    "TOKEN_PREFIX",
    "GeneratedClientToken",
    "McpClientIdentity",
    "current_identity",
    "generate_client_id",
    "generate_client_token",
    "hash_client_token",
    "reset_current_identity",
    "set_current_identity",
]
