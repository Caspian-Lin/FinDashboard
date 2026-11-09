"""ResearchRun API 契约(issue #80)。API 只排队,不执行策略。

issue #513:入队 / 重放输入 DTO 下沉到 ``finboard_app.research_run_submission``
(REST 与 MCP 共用同一份字段、默认值与校验器,消除 MCP 对 ``finboard_api`` 的
懒加载依赖);本模块保留同名 re-export,既有 import 路径与构造方式不破。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from finboard_app.research_run_submission import (
    ResearchRunReplayInput as ResearchRunReplayIn,
)
from finboard_app.research_run_submission import (
    ResearchRunSubmitInput as ResearchRunQueueIn,
)


class ResearchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    idempotency_key: str
    replay_of_run_id: str | None
    strategy_id: str
    strategy_kind: str
    status: str
    manifest_checksum: str
    manifest: dict[str, Any]
    result: dict[str, Any] | None
    result_checksum: str | None
    error_code: str | None
    error_summary: str | None
    requested_by: str
    job_id: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class ResearchArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    run_id: str
    decision_id: str | None
    sequence: int
    stage: str
    trace_id: str
    parent_trace_ids: list[str]
    payload: dict[str, Any]
    checksum: str
    created_at: datetime


class ResearchLineageOut(BaseModel):
    run_id: str
    leaf_trace_id: str
    artifacts: list[ResearchArtifactOut]


__all__ = [
    "ResearchArtifactOut",
    "ResearchLineageOut",
    "ResearchRunOut",
    "ResearchRunQueueIn",
    "ResearchRunReplayIn",
]
