"""研究操作上下文与统一业务错误分类(issue #513,跨议题共享契约)。

本模块是 #513-#520 的第一份共享契约,被 REST 适配层、MCP 适配层与未来的课题
动作(#515)共同消费:

* :class:`ResearchOperationContext` —— 一次研究写操作的**服务端身份**;
* :class:`ResearchOperationError` 家族 —— 研究域业务错误的**唯一**分类来源,
  HTTP 状态码与 MCP ``error.kind`` 的映射只发生在两个薄适配层。

不变量:

* 上下文**只能由服务端构造**(``for_web`` / ``for_mcp`` / ``for_worker``)。
  任何 payload / query 字段都不能覆盖 ``actor`` / ``actor_type`` /
  ``client_id``;``ResearchRunManifest.requested_by`` 仍是调用方声明的字段
  (写进 manifest 并参与 checksum),与 ``actor`` 分离 —— 历史 manifest 字节
  不受本模块影响。
* 业务层只抛 :class:`ResearchOperationError`;``routes/*`` 与 ``tools/*`` 各自
  映射成 ``HTTPException`` / ``McpToolError``,业务层不得依赖 FastAPI 或 MCP SDK。
* 本模块不读写任何表,不触实盘 ``orders`` / ``fills`` / ``positions`` /
  ``audit_logs``(研究域隔离)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal

from finboard_backtest.research_run import ResearchActorType

#: MCP 信封允许的 ``error.kind`` 子集(见 ``finboard_mcp.envelope.ErrorKind``)。
#: 这里用本地 Literal 而非 import MCP 类型:``finboard-mcp`` 依赖
#: ``finboard-app``,反向依赖会造成循环。
McpErrorKind = Literal[
    "conflict",
    "invalid_argument",
    "not_found",
    "payload_too_large",
    "permission_denied",
    "unavailable",
]


class OperationSource(StrEnum):
    """研究写操作的来源通道。"""

    WEB = "web"
    MCP = "mcp"
    WORKER = "worker"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ResearchOperationContext:
    """一次研究写操作的服务端身份(不可由请求载荷构造)。

    ``actor`` 是审计与血缘归属(``user:api`` / ``agent:MC-xxx`` /
    ``worker:wk-1``);``actor_type`` 决定 ``ResearchRunManifest.actor_type``
    —— REST 默认 ``human``(可显式 ``agent``),MCP 恒为 ``agent``(#312),
    worker 走 ``system``(#514/#518 引入 worker 枚举后再切换,本 issue 不改
    ``ResearchActorType``,避免 manifest 字节漂移)。
    """

    actor: str
    actor_type: ResearchActorType
    source: OperationSource
    client_id: str | None = None
    #: MCP 的 ``OP-``;REST 由中间件/请求头生成。
    operation_id: str | None = None
    request_id: str | None = None
    #: MCP 客户端作用域(#517)。``None`` 表示服务端可信通道(Web loopback 人类
    #: 操作 / worker);MCP 适配层必须传入 ``identity.scopes``,否则按 fail-closed
    #: 拒绝需要 scope 的动作(#516 per-action 强制)。
    scopes: frozenset[str] | None = None

    def allows_scope(self, scope: str) -> bool:
        """是否具备某作用域;``None`` 仅对服务端可信通道放行。"""

        if self.scopes is None:
            return self.source in {OperationSource.WEB, OperationSource.WORKER}
        return "*" in self.scopes or scope in self.scopes

    @classmethod
    def for_web(
        cls,
        request_id: str | None = None,
        *,
        actor: str = "user:api",
        actor_type: ResearchActorType = ResearchActorType.HUMAN,
        client_id: str | None = None,
        operation_id: str | None = None,
    ) -> ResearchOperationContext:
        """Web / REST 通道上下文(默认 ``human``,可显式声明 ``agent``)。"""

        return cls(
            actor=actor,
            actor_type=actor_type,
            source=OperationSource.WEB,
            client_id=client_id,
            operation_id=operation_id,
            request_id=request_id,
        )

    @classmethod
    def for_mcp(
        cls,
        *,
        client_id: str | None = None,
        operation_id: str | None = None,
        actor: str | None = None,
        request_id: str | None = None,
        scopes: frozenset[str] | None = None,
    ) -> ResearchOperationContext:
        """MCP 通道上下文(恒 ``agent``,与 ``requested_by=agent:mcp`` 对齐)。

        ``scopes`` 传入客户端已授予作用域(#517);缺省 ``None`` 时 per-action
        强制(#516)按 fail-closed 处理,需要作用域的动作一律拒绝。
        """

        return cls(
            actor=actor or (f"agent:{client_id}" if client_id else "agent:mcp"),
            actor_type=ResearchActorType.AGENT,
            source=OperationSource.MCP,
            client_id=client_id,
            operation_id=operation_id,
            request_id=request_id,
            scopes=scopes,
        )

    @classmethod
    def for_worker(
        cls,
        worker_id: str,
        *,
        operation_id: str | None = None,
        client_id: str | None = None,
        request_id: str | None = None,
    ) -> ResearchOperationContext:
        """worker 通道上下文(归属 ``worker:<id>``)。"""

        return cls(
            actor=f"worker:{worker_id}",
            actor_type=ResearchActorType.WORKER,
            source=OperationSource.WORKER,
            client_id=client_id,
            operation_id=operation_id,
            request_id=request_id,
        )


class ResearchOperationError(Exception):
    """研究域业务错误的统一基类。

    ``category`` 是 §3.2 的粗分类(``invalid_input`` / ``not_found`` /
    ``conflict`` / ``precondition_failed`` / ``permission_denied`` /
    ``payload_too_large`` / ``unavailable``);``code`` 是更细的业务码
    (如 ``scope_release_drift``),默认等于 ``category``。

    ``details`` 是可选结构化补充(``actual_release_ids`` / ``field_errors``
    等),随 ``to_detail()`` 一起进入 REST 的 ``detail`` 与 MCP 的消息文本。
    """

    category: ClassVar[str] = "invalid_input"
    http_status: ClassVar[int] = 422
    mcp_kind: ClassVar[McpErrorKind] = "invalid_argument"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.category
        self.details: dict[str, Any] = dict(details or {})

    def to_detail(self) -> dict[str, Any]:
        """REST ``HTTPException.detail`` 的统一体(``code`` + ``message`` + details)。"""

        return {"code": self.code, "message": self.message, **self.details}

    def __repr__(self) -> str:  # pragma: no cover - 诊断辅助
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"http_status={self.http_status}, message={self.message!r})"
        )


class ResearchInvalidInputError(ResearchOperationError):
    """参数/门控失败(422 / ``invalid_argument``)。"""

    category = "invalid_input"
    http_status = 422
    mcp_kind = "invalid_argument"


class ResearchNotFoundError(ResearchOperationError):
    """引用的对象不存在(404 / ``not_found``)。"""

    category = "not_found"
    http_status = 404
    mcp_kind = "not_found"


class ResearchConflictError(ResearchOperationError):
    """幂等键内容冲突 / 状态机非法转移(409 / ``conflict``)。"""

    category = "conflict"
    http_status = 409
    mcp_kind = "conflict"


class ResearchPreconditionFailedError(ResearchOperationError):
    """前置条件漂移(409 / ``conflict``,如 ``data_scope_resolution_id`` 过期)。"""

    category = "precondition_failed"
    http_status = 409
    mcp_kind = "conflict"


class ResearchPermissionDeniedError(ResearchOperationError):
    """权限不足(403 / ``permission_denied``)。"""

    category = "permission_denied"
    http_status = 403
    mcp_kind = "permission_denied"


class ResearchPayloadTooLargeError(ResearchOperationError):
    """载荷超限(413 / ``invalid_argument``)。"""

    category = "payload_too_large"
    http_status = 413
    mcp_kind = "invalid_argument"


class ResearchUnavailableError(ResearchOperationError):
    """依赖暂不可用(503 / ``unavailable``,可重试)。"""

    category = "unavailable"
    http_status = 503
    mcp_kind = "unavailable"
    retryable = True


__all__ = [
    "McpErrorKind",
    "OperationSource",
    "ResearchConflictError",
    "ResearchInvalidInputError",
    "ResearchNotFoundError",
    "ResearchOperationContext",
    "ResearchOperationError",
    "ResearchPayloadTooLargeError",
    "ResearchPermissionDeniedError",
    "ResearchPreconditionFailedError",
    "ResearchUnavailableError",
]
