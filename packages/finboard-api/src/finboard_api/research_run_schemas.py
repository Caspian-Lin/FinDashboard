"""ResearchRun API 契约(issue #80)。API 只排队,不执行策略。

issue #513:入队 / 重放输入 DTO 下沉到 ``finboard_app.research_run_submission``
(REST 与 MCP 共用同一份字段、默认值与校验器,消除 MCP 对 ``finboard_api`` 的
懒加载依赖);本模块保留同名 wire schema,既有 import 路径、构造方式与 **OpenAPI
组件名**都不破。

wire schema 用同名子类而非别名:FastAPI/pydantic 的 components key 取类名,
若直接 re-export 基类,组件名会变成 ``ResearchRunSubmitInput`` /
``ResearchRunReplayInput``,按 dev 生成 stub 的客户端会断。子类零新增字段 /
校验器,只承载对外组件名。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from finboard_app.research_run_submission import (
    ResearchRunReplayInput,
    ResearchRunSubmitInput,
)


class ResearchRunQueueIn(ResearchRunSubmitInput):
    """``POST /api/research/runs`` 请求体(OpenAPI 组件名保持 #513 之前的值)。"""


class ResearchRunReplayIn(ResearchRunReplayInput):
    """``POST /api/research/runs/{run_id}/replay`` 请求体(组件名同上)。"""


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
