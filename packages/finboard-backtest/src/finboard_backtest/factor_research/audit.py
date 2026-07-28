"""审计追踪(Issue #65)。

记录所有假设状态变更、审批事件和实验登记。
审计日志是不可变的 —— 只追加,不修改,不删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class AuditEventType(StrEnum):
    SUBMITTED = "submitted"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPERIMENT_REGISTERED = "experiment_registered"
    EXPERIMENT_COMPLETED = "experiment_completed"
    VALIDATED_OOS = "validated_oos"
    SUPERSEDED = "superseded"
    MODEL_UNAVAILABLE = "model_unavailable"
    REFERENCE_MISSING = "reference_missing"
    EXPERIMENT_INTERRUPTED = "experiment_interrupted"
    VERSION_DRIFT = "version_drift"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """审计日志条目(不可变)。"""

    timestamp: datetime
    event_type: AuditEventType
    hypothesis_id: str
    actor: str
    details: tuple[tuple[str, str], ...] = ()


class AuditTrail:
    """内存审计追踪。

    离线研究工具,不需要持久化到 PostgreSQL。
    若需持久化,可在流程结束时将 ``get_all()`` 序列化为 JSON。
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def log(
        self,
        event_type: AuditEventType,
        hypothesis_id: str,
        actor: str,
        **details: str,
    ) -> AuditEntry:
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            event_type=event_type,
            hypothesis_id=hypothesis_id,
            actor=actor,
            details=tuple(sorted(details.items())),
        )
        self._entries.append(entry)
        return entry

    def get_history(self, hypothesis_id: str) -> list[AuditEntry]:
        """返回某假设的全部审计记录(按时间顺序)。"""
        return [e for e in self._entries if e.hypothesis_id == hypothesis_id]

    def get_by_type(self, event_type: AuditEventType) -> list[AuditEntry]:
        """返回某类型的全部审计记录。"""
        return [e for e in self._entries if e.event_type == event_type]

    def get_all(self) -> list[AuditEntry]:
        """返回全部审计记录。"""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
