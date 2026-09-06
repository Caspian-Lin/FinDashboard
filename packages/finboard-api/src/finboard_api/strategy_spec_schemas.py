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


class UniversePrecheckWarningOut(StrategySpecApiModel):
    """一条具名 universe 预检 warning(issue #186)。"""

    code: str
    condition: str
    field: str
    message: str


class UniversePoolPreviewOut(StrategySpecApiModel):
    """候选池静态预览:空池判定 + 排除统计 + 具名 warnings(issue #186)。"""

    total_candidates: int
    included: int
    excluded: int
    is_empty: bool
    excluded_by_condition: dict[str, int]
    missing_fields: list[str]
    warnings: list[UniversePrecheckWarningOut]


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


class UserFactorAnchorWarningOut(StrategySpecApiModel):
    """一条用户因子快照锚定失配提示(issue #355,validate 通道)。"""

    code: str
    factor_name: str
    run_id: str
    snapshot_id: str
    anchored_release_ids: list[str]
    missing_release_ids: list[str]
    message: str


class StrategySpecValidationOut(StrategySpecApiModel):
    valid: bool = True
    checksum: str
    feature_order: list[str]
    required_factor_sources: list[str]
    required_datasets: list[str]
    dataset_release_ids: list[str]
    lifecycle_stages: list[str]
    can_execute: bool
    universe_precheck: UniversePoolPreviewOut | None = None
    # issue #355:引用 u_ 因子的既有沙箱快照锚定发布 ⊄ 本次 dataset_release_ids
    # 时的具名提示(不阻断;全匹配为空列表,零噪音)。直接引用将被入队秒拒。
    user_factor_anchor_warnings: list[UserFactorAnchorWarningOut] = []


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
    "UniversePoolPreviewOut",
    "UniversePrecheckWarningOut",
    "UserFactorAnchorWarningOut",
]
