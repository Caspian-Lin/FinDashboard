"""ResearchRun API 契约(issue #80)。API 只排队,不执行策略。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from finboard_backtest.research_run.contracts import (
    DECISION_SCHEDULE_KINDS,
    REBALANCE_FREQUENCIES,
    parse_decision_schedule,
)


class ResearchRunQueueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=64)
    strategy_version: int = Field(ge=1)
    dataset_release_ids: list[str] = Field(min_length=1)
    factor_snapshot_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    validation_config: dict[str, Any] = Field(default_factory=dict)
    portfolio_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] = Field(default_factory=dict)
    execution_config: dict[str, Any] = Field(default_factory=dict)
    fee_config: dict[str, Any] = Field(default_factory=dict)
    benchmark_config: dict[str, Any] = Field(default_factory=dict)
    code_version: str = Field(min_length=7, max_length=64)
    initial_capital: Decimal = Field(ge=Decimal("100000"), le=Decimal("500000"))
    requested_by: str = Field(min_length=1, max_length=128)
    # issue #312:REST 默认 human 不变;放开 agent(网关/自动化通道显式声明),
    # llm 仍被拒绝(契约层 fail-closed,红线不变)。
    actor_type: Literal["human", "agent"] = "human"

    @field_validator("parameters")
    @classmethod
    def _validate_decision_schedule(cls, value: dict[str, Any]) -> dict[str, Any]:
        """决策日历声明参数门(issue #361,#183 legacy 扩展)。

        * ``decision_schedule``:kind ∈ {daily, weekly, monthly, quarterly,
          custom};custom 必须声明非空 ``dates``(YYYY-MM-DD 字符串,严格
          升序去重);形状规则与执行期 ``parse_decision_schedule`` 同源
          (单一事实来源,不双份漂移);
        * legacy ``rebalance_frequency`` 兼容扩展接受 daily/weekly(旧值
          monthly/quarterly 零变化);与 ``decision_schedule`` 不可同时声明;
        * ``dates ⊆ 发布交易日`` 不是形状规则(需要发布日历),由入队路由
          的日历门控校验(REST+MCP 共用)。

        非法值提前在入队时拒绝(研究运行执行时也会 fail-closed,这里给 agent
        更早、更清晰的错误)。
        """
        if "decision_schedule" in value:
            if value.get("rebalance_frequency") is not None:
                raise ValueError(
                    "parameters.decision_schedule 与 legacy rebalance_frequency"
                    " 不可同时声明(声明 decision_schedule 即 multi_period)"
                )
            try:
                parse_decision_schedule(value["decision_schedule"])
            except ValueError as exc:
                raise ValueError(f"decision_schedule 非法: {exc}") from exc
        frequency = value.get("rebalance_frequency")
        if frequency is not None and (
            not isinstance(frequency, str) or frequency not in REBALANCE_FREQUENCIES
        ):
            raise ValueError(
                f"rebalance_frequency 仅支持 {sorted(REBALANCE_FREQUENCIES)}"
                f"(decision_schedule.kind 支持 {sorted(DECISION_SCHEDULE_KINDS)}),"
                f"收到 {frequency!r}"
            )
        return value


class ResearchRunReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    # issue #312:同 queue —— 默认 human 不变,放开 agent,llm 仍拒绝。
    actor_type: Literal["human", "agent"] = "human"


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
