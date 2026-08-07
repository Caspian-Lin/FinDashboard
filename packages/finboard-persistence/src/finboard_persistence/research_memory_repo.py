"""研究长期记忆 / 研究笔记持久化仓储(issue #110)。

让 OpenCode 研究 Agent 跨会话积累结构化研究上下文。记忆通过 ``source_refs``
关联数据集 / 策略 / 实验 / ResearchRun / Simulation 产物(只引用,不修改产物)。

操作语义:
* ``remember`` —— 记住一条 ``active`` 记忆;
* ``forget`` —— 软删除(``status=forgotten``),保留审计;
* ``correct`` —— 新建 ``active`` 记忆,经 ``supersedes_id`` 链接被纠正的旧记忆,
  旧记忆标记 ``forgotten``;
* ``confirm`` —— 标记确认(``confirmed_by`` / ``confirmed_at``);
* ``archive`` —— 归档(``status=archived``)。

红线:不写入实盘 orders/fills/positions/audit_logs。
Repository 不控制事务边界,commit 由调用方决定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchMemoryModel

MemoryType = str  # note / insight / correction / confirmation
MemoryStatus = str  # active / forgotten / archived

ACTIVE = "active"
FORGOTTEN = "forgotten"
ARCHIVED = "archived"

_VALID_TYPES = {"note", "insight", "correction", "confirmation"}
_VALID_STATUSES = {ACTIVE, FORGOTTEN, ARCHIVED}


@dataclass(frozen=True)
class SourceRef:
    """记忆关联的研究产物引用(只读引用,不修改产物本身)。

    ``kind`` 常见值:research_run / strategy / simulation / dataset /
    experiment / hypothesis / factor / backtest。
    """

    kind: str
    ref_id: str
    label: str | None = None


@dataclass(frozen=True)
class ResearchMemory:
    """研究记忆领域记录。"""

    memory_id: str
    memory_type: MemoryType
    content: str
    source_refs: list[SourceRef] = field(default_factory=list)
    status: MemoryStatus = ACTIVE
    tags: list[str] = field(default_factory=list)
    created_by: str = ""
    conversation_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    supersedes_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def generate_memory_id() -> str:
    """生成全局唯一记忆 ID(前缀 ``RM-``,便于审计检索)。"""
    return f"RM-{uuid.uuid4().hex[:24]}"


class ResearchMemoryRepository:
    """研究记忆持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def remember(
        self,
        *,
        memory_type: MemoryType,
        content: str,
        source_refs: list[SourceRef] | None = None,
        tags: list[str] | None = None,
        created_by: str,
        conversation_id: str | None = None,
    ) -> ResearchMemory:
        """记住一条 ``active`` 记忆。"""
        _require_type(memory_type)
        if not content.strip():
            raise ValueError("记忆内容不能为空")
        model = ResearchMemoryModel(
            memory_id=generate_memory_id(),
            memory_type=memory_type,
            content=content,
            source_refs=[_ref_to_dict(r) for r in (source_refs or [])],
            status=ACTIVE,
            tags=list(tags or []),
            created_by=created_by,
            conversation_id=conversation_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._session.add(model)
        await self._session.flush()
        return _model_to_record(model)

    async def get(self, memory_id: str) -> ResearchMemory | None:
        """按 ``memory_id`` 查询单条记忆(含 forgotten / archived)。"""
        model = await self._get_model(memory_id)
        return _model_to_record(model) if model else None

    async def list_memories(
        self,
        *,
        status: MemoryStatus | None = None,
        memory_type: MemoryType | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        tag: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[ResearchMemory]:
        """列表查询。``source_kind`` / ``source_ref`` / ``tag`` 在应用层过滤。"""
        stmt = select(ResearchMemoryModel)
        if status is not None:
            _require_status(status)
            stmt = stmt.where(ResearchMemoryModel.status == status)
        if memory_type is not None:
            _require_type(memory_type)
            stmt = stmt.where(ResearchMemoryModel.memory_type == memory_type)
        if conversation_id is not None:
            stmt = stmt.where(
                ResearchMemoryModel.conversation_id == conversation_id
            )
        stmt = stmt.order_by(ResearchMemoryModel.created_at.desc()).limit(
            max(1, min(limit, 500))
        )
        result = await self._session.execute(stmt)
        rows = list(result.scalars())
        records = [_model_to_record(m) for m in rows]
        if source_kind is not None:
            records = [
                r for r in records if any(s.kind == source_kind for s in r.source_refs)
            ]
        if source_ref is not None:
            records = [
                r
                for r in records
                if any(s.ref_id == source_ref for s in r.source_refs)
            ]
        if tag is not None:
            records = [r for r in records if tag in r.tags]
        return records

    async def forget(self, memory_id: str) -> ResearchMemory:
        """软删除:标记 ``forgotten``(保留审计)。"""
        model = await self._require_active(memory_id)
        model.status = FORGOTTEN
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _model_to_record(model)

    async def correct(
        self,
        *,
        memory_id: str,
        content: str,
        source_refs: list[SourceRef] | None = None,
        tags: list[str] | None = None,
        created_by: str,
        conversation_id: str | None = None,
    ) -> ResearchMemory:
        """纠正:新建 ``active`` 记忆,旧记忆标记 ``forgotten``。

        新记忆继承旧记忆的 ``memory_type``,经 ``supersedes_id`` 链接旧记忆,
        形成纠正链。``source_refs`` / ``tags`` 不传则继承旧记忆。
        """
        old = await self._require_active(memory_id)
        if not content.strip():
            raise ValueError("纠正内容不能为空")
        new_model = ResearchMemoryModel(
            memory_id=generate_memory_id(),
            memory_type=old.memory_type,
            content=content,
            source_refs=[
                _ref_to_dict(r) for r in (source_refs or _model_refs(old))
            ],
            status=ACTIVE,
            tags=list(tags if tags is not None else old.tags),
            created_by=created_by,
            conversation_id=conversation_id or old.conversation_id,
            supersedes_id=old.memory_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        old.status = FORGOTTEN
        old.updated_at = datetime.now(UTC)
        self._session.add(new_model)
        await self._session.flush()
        return _model_to_record(new_model)

    async def confirm(self, memory_id: str, *, actor: str) -> ResearchMemory:
        """确认:标记 ``confirmed_by`` / ``confirmed_at``。"""
        model = await self._require_active(memory_id)
        model.confirmed_by = actor
        model.confirmed_at = datetime.now(UTC)
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _model_to_record(model)

    async def archive(self, memory_id: str) -> ResearchMemory:
        """归档:标记 ``archived``。"""
        model = await self._require_active(memory_id)
        model.status = ARCHIVED
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _model_to_record(model)

    async def _get_model(self, memory_id: str) -> ResearchMemoryModel | None:
        stmt = select(ResearchMemoryModel).where(
            ResearchMemoryModel.memory_id == memory_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _require_active(self, memory_id: str) -> ResearchMemoryModel:
        model = await self._get_model(memory_id)
        if model is None:
            raise LookupError(f"研究记忆不存在: {memory_id}")
        if model.status != ACTIVE:
            raise ValueError(f"记忆 {memory_id} 当前状态为 {model.status},无法操作")
        return model


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def _ref_to_dict(ref: SourceRef) -> dict[str, object]:
    d: dict[str, object] = {"kind": ref.kind, "ref_id": ref.ref_id}
    if ref.label is not None:
        d["label"] = ref.label
    return d


def _dict_to_ref(d: Any) -> SourceRef:
    if not isinstance(d, dict):
        return SourceRef(kind="unknown", ref_id=str(d))
    return SourceRef(
        kind=str(d.get("kind", "unknown")),
        ref_id=str(d.get("ref_id", "")),
        label=cast(str | None, d.get("label")),
    )


def _model_refs(model: ResearchMemoryModel) -> list[SourceRef]:
    return [_dict_to_ref(d) for d in model.source_refs]


def _model_to_record(model: ResearchMemoryModel) -> ResearchMemory:
    return ResearchMemory(
        memory_id=model.memory_id,
        memory_type=model.memory_type,
        content=model.content,
        source_refs=[_dict_to_ref(d) for d in model.source_refs],
        status=model.status,
        tags=list(model.tags),
        created_by=model.created_by,
        conversation_id=model.conversation_id,
        confirmed_by=model.confirmed_by,
        confirmed_at=model.confirmed_at,
        supersedes_id=model.supersedes_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _require_type(value: str) -> None:
    if value not in _VALID_TYPES:
        raise ValueError(
            f"非法记忆类型 {value!r},允许: {sorted(_VALID_TYPES)}"
        )


def _require_status(value: str) -> None:
    if value not in _VALID_STATUSES:
        raise ValueError(
            f"非法记忆状态 {value!r},允许: {sorted(_VALID_STATUSES)}"
        )


__all__ = [
    "ACTIVE",
    "ARCHIVED",
    "FORGOTTEN",
    "MemoryStatus",
    "MemoryType",
    "ResearchMemory",
    "ResearchMemoryRepository",
    "SourceRef",
    "generate_memory_id",
]
