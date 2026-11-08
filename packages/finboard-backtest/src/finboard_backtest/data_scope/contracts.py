"""DataScope 契约(issue #510)。

统一范围对象描述:资产/标的组、决策区间、预热与尾部估值、基准、所需
字段/数据集、bars 复权与发布选择模式。解析结果(:class:`ScopeResolution`)
是提交 run 时复验与冻结的候选集合,不是新的持久化实体。

执行窗口直接映射 dev #502 的 ``research_window``:
``warmup_start / decision_start / decision_end / valuation_end``。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finboard_data.datasets import CatalogDataset
from finboard_shared.types import AssetClass, InstrumentType, Market

SCOPE_VERSION: Literal["v1"] = "v1"
RESOLVER_VERSION = "v1"

ReleaseRole = Literal["bars_primary", "research"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopeFilters(_Frozen):
    """标的池过滤(只做 PIT 可判定的字段;ST 判定留待后续)。"""

    min_listing_days: int = Field(default=60, ge=0, le=5000)
    exclude_suspended: bool = True
    exclude_delisted: bool = True
    exclude_benchmark_only: bool = True


class ScopeUniverse(_Frozen):
    markets: tuple[Market, ...] = ()
    asset_classes: tuple[AssetClass, ...] = ()
    instrument_types: tuple[InstrumentType, ...] = ()
    explicit_symbols: tuple[str, ...] = ()
    filters: ScopeFilters = Field(default_factory=ScopeFilters)
    selection_limit: int | None = Field(default=None, ge=1, le=5000)


class ScopeDecisionWindow(_Frozen):
    start: date
    end: date
    schedule: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate(self) -> ScopeDecisionWindow:
        if self.start > self.end:
            raise ValueError("decision.start 不能晚于 decision.end")
        return self


class ScopeDatasets(_Frozen):
    required: tuple[CatalogDataset, ...]
    optional: tuple[CatalogDataset, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> ScopeDatasets:
        if CatalogDataset.BARS not in self.required:
            raise ValueError("datasets.required 必须包含 bars")
        if len(set(self.required)) != len(self.required):
            raise ValueError("datasets.required 不能重复")
        if len(set(self.optional)) != len(self.optional):
            raise ValueError("datasets.optional 不能重复")
        if set(self.required) & set(self.optional):
            raise ValueError("required 与 optional 不能重叠")
        return self


class DataScopeSpec(_Frozen):
    """一次课题/运行的统一数据范围声明。"""

    scope_version: Literal["v1"] = SCOPE_VERSION
    universe: ScopeUniverse
    decision: ScopeDecisionWindow
    warmup_sessions: int = Field(default=0, ge=0, le=5000)
    tail_sessions: int = Field(default=0, ge=0, le=250)
    benchmarks: tuple[str, ...] = ()
    datasets: ScopeDatasets
    fields: tuple[str, ...] = ()
    bars_adjustment: Literal["qfq", "hqfq", "none"] = "qfq"
    release_selection: Literal["auto", "locked"] = "auto"
    locked_release_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> DataScopeSpec:
        if self.release_selection == "locked" and not self.locked_release_ids:
            raise ValueError("locked 模式必须声明 locked_release_ids")
        if len(set(self.locked_release_ids)) != len(self.locked_release_ids):
            raise ValueError("locked_release_ids 不能重复")
        if len(set(self.benchmarks)) != len(self.benchmarks):
            raise ValueError("benchmarks 不能重复")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """稳定 JSON 负载(checksum 用;不排除空集合,形状固定)。

        语义上是「集合」的字段在这里统一排序,保证调用方声明顺序不影响
        checksum(例如 ``datasets.required`` 写成 bars 在前还是 daily_metrics
        在前,是同一份范围声明)。
        """

        payload = self.model_dump(mode="json", exclude_none=True)
        unordered_top_level = ("benchmarks", "fields", "locked_release_ids")
        for key in unordered_top_level:
            value = payload.get(key)
            if isinstance(value, list):
                payload[key] = sorted(value)
        universe = payload.get("universe")
        if isinstance(universe, dict):
            for key in (
                "markets",
                "asset_classes",
                "instrument_types",
                "explicit_symbols",
            ):
                value = universe.get(key)
                if isinstance(value, list):
                    universe[key] = sorted(value)
        datasets = payload.get("datasets")
        if isinstance(datasets, dict):
            for key in ("required", "optional"):
                value = datasets.get(key)
                if isinstance(value, list):
                    datasets[key] = sorted(value)
        return payload


def scope_checksum(spec: DataScopeSpec) -> str:
    """范围声明的内容 checksum(与发布集合无关)。"""

    payload = json.dumps(
        spec.canonical_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------


class ScopeWindow(_Frozen):
    """解析后的可执行窗口(交易日口径)。"""

    warmup_start: date
    decision_start: date
    decision_end: date
    valuation_end: date
    warmup_sessions: int
    decision_sessions: int
    tail_sessions: int
    calendar_markets: tuple[str, ...]
    calendar_source: str

    def research_window(self) -> dict[str, str]:
        """写进 run ``parameters.research_window`` 的四日期。"""

        return {
            "warmup_start": self.warmup_start.isoformat(),
            "decision_start": self.decision_start.isoformat(),
            "decision_end": self.decision_end.isoformat(),
            "valuation_end": self.valuation_end.isoformat(),
        }


class ResolvedRelease(_Frozen):
    artifact_id: str
    dataset: CatalogDataset
    role: ReleaseRole
    version: str
    checksum: str
    published_at: datetime
    start_date: date
    end_date: date
    coverage_pct: Decimal
    symbol_count: int
    reason: str


class ScopeUniverseResult(_Frozen):
    requested_count: int
    resolved_count: int
    truncated: bool
    symbols_preview: tuple[str, ...]
    excluded: dict[str, int] = Field(default_factory=dict)


class ScopeGaps(_Frozen):
    missing_in_bars: tuple[str, ...] = ()
    missing_in_research: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    window_shortfall: dict[str, str] = Field(default_factory=dict)


class ScopeBlocker(_Frozen):
    code: str
    message: str
    dataset: CatalogDataset | None = None


class ScopeAlternative(_Frozen):
    dataset: CatalogDataset
    release_id: str
    version: str
    reason: str


class AffectedStrategy(_Frozen):
    strategy_id: str
    version: int
    name: str
    reason: str


class ScopeResolution(_Frozen):
    resolution_id: str
    scope_checksum: str
    as_of: datetime
    selection_mode: Literal["auto", "locked"]
    window: ScopeWindow
    releases: tuple[ResolvedRelease, ...]
    universe: ScopeUniverseResult
    gaps: ScopeGaps
    warnings: tuple[str, ...] = ()
    blockers: tuple[ScopeBlocker, ...] = ()
    alternatives: tuple[ScopeAlternative, ...] = ()
    affected_strategies: tuple[AffectedStrategy, ...] = ()
    resolver_version: str = RESOLVER_VERSION

    @property
    def release_ids(self) -> tuple[str, ...]:
        return tuple(item.artifact_id for item in self.releases)

    def run_parameters(self) -> dict[str, str]:
        """提交 run 时需要合并进 ``parameters`` 的 ``research_window``。"""

        return self.window.research_window()

    def manifest_payload(self, scope: DataScopeSpec) -> dict[str, Any]:
        """冻结进 run manifest 的 ``data_scope`` 区块(issue #510)。

        只放解析结论与它依赖的输入(``scope`` 原样 + 窗口 + 精确 release
        版本/checksum),不放候选替代与受影响策略——那些是预览信息,冻结后
        只会误导读者。``resolution_id`` 可复算校验,不需要重跑解析。
        """

        return {
            "resolution_id": self.resolution_id,
            "scope_checksum": self.scope_checksum,
            "resolver_version": self.resolver_version,
            "scope": scope.canonical_payload(),
            "window": self.window.model_dump(mode="json"),
            "releases": [
                {
                    "artifact_id": item.artifact_id,
                    "dataset": item.dataset.value,
                    "role": item.role,
                    "version": item.version,
                    "checksum": item.checksum,
                }
                for item in self.releases
            ],
            "warnings": list(self.warnings),
        }


class ScopePreviewRequest(_Frozen):
    scope: DataScopeSpec
    mode: Literal["new_spec", "existing_spec"] = "new_spec"
    strategy_id: str | None = None
    strategy_version: int | None = None
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> ScopePreviewRequest:
        if self.mode == "existing_spec" and (
            self.strategy_id is None or self.strategy_version is None
        ):
            raise ValueError("existing_spec 模式必须提供 strategy_id/version")
        return self


__all__ = [
    "RESOLVER_VERSION",
    "SCOPE_VERSION",
    "AffectedStrategy",
    "DataScopeSpec",
    "ResolvedRelease",
    "ScopeAlternative",
    "ScopeBlocker",
    "ScopeDatasets",
    "ScopeDecisionWindow",
    "ScopeFilters",
    "ScopeGaps",
    "ScopePreviewRequest",
    "ScopeResolution",
    "ScopeUniverse",
    "ScopeUniverseResult",
    "ScopeWindow",
    "scope_checksum",
]
