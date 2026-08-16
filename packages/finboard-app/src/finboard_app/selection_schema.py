"""因子选股配置的统一 Pydantic 契约。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finboard_data.factors import (
    FACTOR_VERSION,
    FactorName,
    FactorSelectionConfig,
    InputsMode,
    RankingScope,
)


class FactorSelectionParams(BaseModel):
    """API、预设与回测引擎共享的按日选股规则。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    source: str = Field(default="tushare", min_length=1, max_length=32)
    inputs_mode: InputsMode = InputsMode.RESEARCH_DB
    snapshot_ids: list[str] = Field(default_factory=list, max_length=64)
    factor_version: str = FACTOR_VERSION
    max_symbols: int = Field(default=20, gt=0, le=5000)
    ranking_factor: FactorName = FactorName.MARKET_CAP
    ranking_scope: RankingScope = RankingScope.GLOBAL
    ranking_ascending: bool = False
    max_per_industry: int | None = Field(default=None, gt=0, le=5000)
    min_listing_days: int = Field(default=60, ge=0)
    exclude_st: bool = True
    exclude_suspended: bool = True
    momentum_lookback: int = Field(default=20, gt=0, le=1000)
    min_market_cap: Decimal | None = None
    max_market_cap: Decimal | None = None
    min_pb: Decimal | None = None
    max_pb: Decimal | None = None
    min_turnover_rate: Decimal | None = None
    max_turnover_rate: Decimal | None = None
    min_momentum: Decimal | None = None
    min_roe: Decimal | None = None
    min_gross_profit_margin: Decimal | None = None
    min_revenue_yoy: Decimal | None = None
    dataset_versions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_domain_rules(self) -> FactorSelectionParams:
        self.to_domain()
        return self

    def to_domain(self) -> FactorSelectionConfig:
        """转换为回测引擎使用的不可变领域配置。"""
        data = self.model_dump()
        data["snapshot_ids"] = tuple(data["snapshot_ids"])
        return FactorSelectionConfig(**data)
