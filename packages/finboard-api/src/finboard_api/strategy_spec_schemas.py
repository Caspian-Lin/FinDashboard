"""无代码研究策略 API 的严格请求/响应 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from finboard_backtest.strategy_spec import ResearchStrategySpec


class StrategySpecApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class StrategySpecDraftIn(StrategySpecApiModel):
    spec: ResearchStrategySpec
    expected_version: int | None = Field(default=None, ge=1)


class StrategySpecSupersedeIn(StrategySpecApiModel):
    spec: ResearchStrategySpec
    expected_version: int = Field(ge=1)


class StrategySpecPublishIn(StrategySpecApiModel):
    version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class StrategySpecRollbackIn(StrategySpecApiModel):
    target_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class StrategySpecValidateIn(StrategySpecApiModel):
    spec: ResearchStrategySpec
    disabled_factors: frozenset[str] = frozenset()


class StrategySpecVersionOut(StrategySpecApiModel):
    strategy_id: str
    version: int
    schema_version: str
    name: str
    strategy_kind: str
    status: str
    change_type: str
    checksum: str
    spec: ResearchStrategySpec
    validation_errors: list[dict[str, object]]
    parent_version: int | None
    rollback_of_version: int | None
    created_at: datetime
    published_at: datetime | None


class StrategySpecDiffOut(StrategySpecApiModel):
    strategy_id: str
    from_version: int
    to_version: int
    changes: list[dict[str, object]]


class StrategySpecValidationOut(StrategySpecApiModel):
    valid: bool = True
    checksum: str
    feature_order: list[str]
    required_factor_sources: list[str]
    required_datasets: list[str]
    dataset_release_ids: list[str]
    lifecycle_stages: list[str]
    can_execute: bool


class StrategySpecRegistryOut(StrategySpecApiModel):
    strategies: list[dict[str, object]]
    feature_sources: list[dict[str, object]]
    operators: list[dict[str, Any]]
    lifecycle_stages: list[str]
    publication_starts_run: bool = False
    accepts_python: bool = False


__all__ = [
    "StrategySpecDiffOut",
    "StrategySpecDraftIn",
    "StrategySpecPublishIn",
    "StrategySpecRegistryOut",
    "StrategySpecRollbackIn",
    "StrategySpecSupersedeIn",
    "StrategySpecValidateIn",
    "StrategySpecValidationOut",
    "StrategySpecVersionOut",
]
