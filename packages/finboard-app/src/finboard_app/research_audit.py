"""研究操作审计(issue #518):事件契约、脱敏与写入器。

本模块是 Web / MCP / worker 三处研究写操作的**唯一**审计契约:

* :class:`ResearchAuditEvent` —— 冻结事件,由服务端构造的
  :class:`~finboard_app.research_operation.ResearchOperationContext` 提供
  ``actor`` / ``actor_type`` / ``source`` / ``client_id``;任何请求载荷都不能
  覆盖这些字段。
* :class:`ResearchAuditRecorder.record` —— 在**调用方 session** 插入(不
  commit),与业务写入同事务:审计持久化失败 → 业务事务整体回滚。
* :class:`ResearchAuditRecorder.record_detached` —— 拒绝/失败路径业务事务
  已回滚,用独立会话补记;自身失败只记 structlog,**绝不**掩盖原始错误。
* 脱敏复用 :func:`finboard_backtest.factor_research.sanitizer.sanitize_prompt`,
  追加 Bearer / token / api_key / password / secret / 凭据前缀正则;单值
  ≤120 字、整包 ≤4 KiB、全量 sha256 摘要。代码正文、记忆正文、未脱敏对话
  **永不入库**。

事务边界:``record`` 只 ``flush``;``record_detached`` 自己 ``commit``。
实盘 ``audit_logs`` 边界不变(研究域独立账本)。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from sqlalchemy.exc import IntegrityError

from finboard_app.research_operation import ResearchOperationContext
from finboard_backtest.background_jobs.audit import WorkerAuditEvent
from finboard_backtest.factor_research.sanitizer import sanitize_prompt
from finboard_persistence.research_audit_repo import ResearchAuditRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finboard_persistence.models import ResearchAuditEventModel
    from finboard_persistence.session import AsyncSessionMaker

logger = structlog.get_logger("finboard.research.audit")

#: 审计结果分类:成功 / 执行失败 / 前置拒绝(业务未执行)/ 权限拒绝。
AuditOutcome = Literal["succeeded", "failed", "rejected", "denied"]
#: 事实来源:服务端观察到 / 客户端声明。
AuditTrust = Literal["server_observed", "client_declared"]

SERVER_OBSERVED: AuditTrust = "server_observed"
CLIENT_DECLARED: AuditTrust = "client_declared"

#: 单个摘要值上限(字符)。
MAX_VALUE_CHARS = 120
#: 整包 ``params_summary`` 上限(字节,序列化后)。
MAX_SUMMARY_BYTES = 4096
#: ``error_summary`` 上限(字符)。
MAX_ERROR_CHARS = 1000

#: 属于研究域的后台任务 kind(其入队/取消进研究审计;数据同步类不进)。
RESEARCH_JOB_KINDS: frozenset[str] = frozenset(
    {
        "research_run",
        "validation_experiment",
        "research_code_run",
        "factor_series_build",
        "feature_snapshot",
        "backtest_run",
    }
)


def is_research_job_kind(kind: str) -> bool:
    """任务 kind 是否属于研究域(is True 才写研究审计)。"""

    return kind in RESEARCH_JOB_KINDS

_REDACTED = "***REDACTED***"
#: 正文类值(代码 / 记忆 / 对话)整值丢弃,只留占位符。
_OMITTED = "***OMITTED***"

#: 敏感**键名**:命中即整值替换(比文本正则更可靠,``{"token": "…"}`` 也覆盖)。
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|"
    r"credential|private[_-]?key|client[_-]?secret)"
)

#: 正文**键名**:代码正文 / 记忆正文 / 对话与推理,整值丢弃(不截断后入库)。
_BODY_KEY = re.compile(
    r"(?i)^(content|code|source_code|files?|file_contents?|body|conversation|"
    r"messages?|prompt|prompts|reasoning|transcript|memory|memory_content|"
    r"text_body|raw|diff|patch)$"
)

#: 正文**前缀**:即使键名中性,明显代码/记忆正文也不入库。
_BODY_PREFIXES: tuple[str, ...] = (
    "import ",
    "from ",
    "def ",
    "class ",
    "async def ",
    "#!/",
    '"""',
    "'''",
    "MEMORY",
    "memory:",
    "# -*- coding",
)

#: 追加的凭据文本形态(sanitize_prompt 之外的平台前缀)。
_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bfbmc[._-][A-Za-z0-9._-]{8,}"), _REDACTED),
    (re.compile(r"(?i)\b(?:sk|pk|ghp|ghs|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}"), _REDACTED),
)


def _is_body_like(key: str, value: Any) -> bool:
    """键名或值是否明显是代码/记忆/对话正文(正文永不入库)。"""

    if _BODY_KEY.search(str(key)):
        return True
    if isinstance(value, str):
        stripped = value.lstrip()
        return any(stripped.startswith(prefix) for prefix in _BODY_PREFIXES)
    return False


def generate_audit_id() -> str:
    """``RA-<16hex>`` 审计 ID(不可猜测、不含时间信息)。"""

    return f"RA-{secrets.token_hex(8)}"


def redact_secrets(text: str) -> str:
    """脱敏文本中的凭据/令牌(在 ``sanitize_prompt`` 之上追加平台前缀规则)。"""

    result = sanitize_prompt(text)
    for pattern, replacement in _EXTRA_SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _sanitize_structure(value: Any) -> Any:
    """递归脱敏任意结构(敏感键整值替换,正文键整值丢弃,字符串截断)。"""

    if isinstance(value, Mapping):
        return {
            str(key)[:MAX_VALUE_CHARS]: (
                _REDACTED
                if _SENSITIVE_KEY.search(str(key))
                else (
                    _OMITTED
                    if _is_body_like(str(key), item)
                    else _sanitize_structure(item)
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_structure(item) for item in value]
    if isinstance(value, str):
        return _OMITTED if _is_body_like("", value) else redact_secrets(value)[:MAX_VALUE_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return redact_secrets(str(value))[:MAX_VALUE_CHARS]


def _coerce_text(value: Any) -> str:
    """把任意值转成脱敏后的短文本(嵌套结构递归脱敏后再序列化截断)。"""

    if isinstance(value, str):
        if _is_body_like("", value):
            return _OMITTED
        text = redact_secrets(value)
    elif isinstance(value, bool | int | float) or value is None:
        text = "" if value is None else str(value)
    else:
        try:
            text = json.dumps(
                _sanitize_structure(value),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):  # pragma: no cover - 防御性
            text = str(value)
    return text[:MAX_VALUE_CHARS]


def sanitize_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """把参数映射脱敏成 ``{key: ≤120字文本}``(不截断整包)。

    三层规则:敏感键整值 ``***REDACTED***``;代码/记忆/对话正文键或正文
    形态整值 ``***OMITTED***``(正文永不入库);其余脱敏后截断。
    """

    summary: dict[str, str] = {}
    for key, value in (params or {}).items():
        raw_key = str(key)
        name = redact_secrets(raw_key)[:MAX_VALUE_CHARS]
        if _SENSITIVE_KEY.search(raw_key):
            summary[name] = _REDACTED
        elif _is_body_like(raw_key, value):
            summary[name] = _OMITTED
        else:
            summary[name] = _coerce_text(value)
    return summary


def digest_params(summary: Mapping[str, Any]) -> str:
    """参数摘要的 sha256(对**脱敏后、截断前**内容取 canonical JSON)。"""

    payload = json.dumps(
        dict(summary), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encoded_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(dict(value), ensure_ascii=True, sort_keys=True, default=str))


def bound_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """把摘要收敛到 ≤4 KiB:按插入顺序保留,超限的尾部键丢弃并标记。

    预算检查**包含** ``_truncated`` / ``_omitted_keys`` 元信息(先在预留
    元信息的前提下逐键判定,再按实际元信息复核),避免元信息把上限顶破。
    """

    if _encoded_size(summary) <= MAX_SUMMARY_BYTES:
        return dict(summary)

    # 预留最坏元信息长度(`_omitted_keys` 最多 6 位),逐键判定是否放得下。
    reserved = {"_truncated": True, "_omitted_keys": 999_999}
    bounded: dict[str, Any] = {}
    omitted = 0
    for key, value in summary.items():
        if _encoded_size({**bounded, key: value, **reserved}) > MAX_SUMMARY_BYTES:
            omitted += 1
            continue
        bounded[key] = value
    # 用真实 omitted 数字复核;仍超限则继续丢尾部键。
    while bounded and _encoded_size(
        {**bounded, "_truncated": True, "_omitted_keys": omitted}
    ) > MAX_SUMMARY_BYTES:
        bounded.popitem()
        omitted += 1
    if omitted:
        bounded["_truncated"] = True
        bounded["_omitted_keys"] = omitted
    return bounded


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


@dataclass(frozen=True, slots=True)
class ResearchAuditEvent:
    """一条研究操作审计事件(不可变;字段语义见数据字典)。

    ``context`` 必须是服务端构造的
    :class:`~finboard_app.research_operation.ResearchOperationContext` ——
    ``actor`` / ``actor_type`` / ``source`` / ``client_id`` 都由它派生,
    调用方不得用请求载荷覆盖。
    """

    operation: str
    outcome: AuditOutcome
    context: ResearchOperationContext
    topic_id: str | None = None
    entry_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    correlation: Mapping[str, str] = field(default_factory=dict)
    result_code: str | None = None
    params_summary: Mapping[str, Any] = field(default_factory=dict)
    params_digest: str = ""
    error_summary: str | None = None
    idempotency_key: str | None = None
    trust: AuditTrust = SERVER_OBSERVED
    occurred_at: datetime | None = None


class ResearchAuditRecorder:
    """审计写入器(成功同事务 / 拒绝独立会话)。"""

    def __init__(self, *, session_maker: AsyncSessionMaker | None = None) -> None:
        self._session_maker = session_maker

    # -- 内部 -------------------------------------------------------------- #
    @staticmethod
    def _fields(event: ResearchAuditEvent) -> dict[str, Any]:
        """把事件规约成仓储层字段(脱敏 + 截断 + digest)。"""

        summary = sanitize_params(event.params_summary)
        digest = event.params_digest or digest_params(summary)
        context = event.context
        return {
            "audit_id": generate_audit_id(),
            "occurred_at": event.occurred_at or datetime.now(UTC),
            "actor": context.actor[:128],
            "actor_type": str(context.actor_type.value)[:16],
            "source": str(context.source.value)[:16],
            "client_id": _clip(context.client_id, 32),
            "topic_id": _clip(event.topic_id, 32),
            "entry_id": _clip(event.entry_id, 32),
            "operation": event.operation[:64],
            "entity_type": _clip(event.entity_type, 32),
            "entity_id": _clip(event.entity_id, 64),
            "outcome": event.outcome,
            "result_code": _clip(event.result_code, 64),
            "correlation": {
                str(key)[:64]: redact_secrets(str(value))[:256]
                for key, value in event.correlation.items()
                if value is not None
            },
            "params_summary": bound_summary(summary),
            "params_digest": digest,
            "error_summary": _clip(
                redact_secrets(event.error_summary) if event.error_summary else None,
                MAX_ERROR_CHARS,
            ),
            "idempotency_key": _clip(event.idempotency_key, 128),
            "trust": event.trust,
        }

    # -- 写入 -------------------------------------------------------------- #
    async def record(self, session: AsyncSession, event: ResearchAuditEvent) -> None:
        """在调用方 session 插入(不 commit),失败向上抛(业务事务回滚)。"""

        await ResearchAuditRepository(session).append(**self._fields(event))

    async def record_detached(self, event: ResearchAuditEvent) -> None:
        """用独立会话写入并 commit;失败只记 structlog,不掩盖原始错误。"""

        if self._session_maker is None:
            logger.warning(
                "research.audit.detached_skipped",
                operation=event.operation,
                outcome=event.outcome,
                detail="未配置 session_maker,拒绝/失败审计未落库",
            )
            return
        try:
            async with self._session_maker() as session:
                await ResearchAuditRepository(session).append(**self._fields(event))
                await session.commit()
        except Exception:
            logger.exception(
                "research.audit.detached_failed",
                operation=event.operation,
                outcome=event.outcome,
            )

    async def record_detached_safe(self, event: ResearchAuditEvent) -> None:
        """:meth:`record_detached` 的别名,用于强调「绝不抛出」。"""

        await self.record_detached(event)

    async def record_client_declared(
        self,
        session: AsyncSession,
        *,
        context: ResearchOperationContext,
        client_event_id: str,
        operation: str,
        outcome: AuditOutcome,
        params_summary: Mapping[str, Any],
        correlation: Mapping[str, str] | None = None,
        result_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[ResearchAuditEventModel | None, bool]:
        """插入一条客户端声明事件,按 ``(client_id, client_event_id)`` 去重。

        返回 ``(row, created)``;``created=False`` 表示同键事件已上报。
        本方法不 commit(路由持有唯一一次 commit)。
        """

        if context.client_id is None:
            raise ValueError("客户端声明事件必须有 client_id")
        repo = ResearchAuditRepository(session)
        existing = await repo.find_client_event(
            client_id=context.client_id, client_event_id=client_event_id
        )
        if existing is not None:
            return existing, False
        row = None
        try:
            row = await repo.append(
                **self._fields(
                    ResearchAuditEvent(
                        operation=operation,
                        outcome=outcome,
                        context=context,
                        correlation=dict(correlation or {}),
                        result_code=result_code,
                        params_summary=params_summary,
                        idempotency_key=client_event_id,
                        trust=CLIENT_DECLARED,
                        occurred_at=occurred_at,
                    )
                )
            )
        except IntegrityError:
            # 并发重复上报:部分唯一索引兜底;回滚本次插入后读既有行,
            # 返回同一 audit_id(不 500、不产生第二行)。
            await session.rollback()
            existing = await repo.find_client_event(
                client_id=context.client_id, client_event_id=client_event_id
            )
            if existing is not None:
                return existing, False
            raise
        return row, True


# --------------------------------------------------------------------------- #
# 进程级默认 recorder
# --------------------------------------------------------------------------- #

_default_recorder = ResearchAuditRecorder()


class ResearchAuditWorkerSink:
    """把 worker 执行器的中立事件适配成研究审计(``source=worker``)。

    满足 ``finboard_backtest.background_jobs.audit.WorkerAuditSink`` 协议:
    独立会话写入、commit、失败只记日志(``record_detached`` 已保证不抛)。
    """

    def __init__(
        self,
        recorder: ResearchAuditRecorder,
        *,
        worker_id: str,
    ) -> None:
        self._recorder = recorder
        self._worker_id = worker_id

    async def record(self, event: WorkerAuditEvent) -> None:
        # 身份用执行器 kind(worker:research_run / worker:validation_experiment),
        # 进程级 worker id 只进 correlation,避免把宿主信息写进 actor。
        actor_id = event.worker_id or self._worker_id
        context = ResearchOperationContext.for_worker(actor_id)
        correlation = dict(event.correlation)
        correlation.setdefault("worker_process_id", self._worker_id)
        await self._recorder.record_detached(
            ResearchAuditEvent(
                operation=event.operation,
                outcome=cast(AuditOutcome, event.outcome),
                context=context,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                correlation=correlation,
                result_code=event.result_code,
                params_summary=dict(event.params_summary),
                error_summary=event.error_summary,
            )
        )


def configure_research_audit_recorder(
    recorder: ResearchAuditRecorder | None = None,
) -> ResearchAuditRecorder:
    """装配进程级 recorder(REST / MCP lifespan、worker 启动时调用)。"""

    global _default_recorder
    _default_recorder = recorder or ResearchAuditRecorder()
    return _default_recorder


def research_audit_recorder() -> ResearchAuditRecorder:
    """读取进程级 recorder(默认无 session_maker:``record`` 可用,detached 跳过)。"""

    return _default_recorder


def reset_research_audit_recorder() -> None:
    """恢复默认 recorder(测试隔离用)。"""

    global _default_recorder
    _default_recorder = ResearchAuditRecorder()


__all__ = [
    "CLIENT_DECLARED",
    "MAX_ERROR_CHARS",
    "MAX_SUMMARY_BYTES",
    "MAX_VALUE_CHARS",
    "RESEARCH_JOB_KINDS",
    "SERVER_OBSERVED",
    "AuditOutcome",
    "AuditTrust",
    "ResearchAuditEvent",
    "ResearchAuditRecorder",
    "ResearchAuditWorkerSink",
    "WorkerAuditEvent",
    "bound_summary",
    "configure_research_audit_recorder",
    "digest_params",
    "generate_audit_id",
    "is_research_job_kind",
    "redact_secrets",
    "research_audit_recorder",
    "reset_research_audit_recorder",
    "sanitize_params",
]
