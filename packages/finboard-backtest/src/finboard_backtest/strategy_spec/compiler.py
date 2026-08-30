"""研究策略规格的确定性解析器。

解析只生成不可执行的研究计划。不实例化策略类、不创建订单、不访问 Broker。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from finboard_backtest.factors.catalog import RESEARCH_FACTOR_CATALOG
from finboard_backtest.strategy_spec.contracts import (
    FeatureKind,
    ResearchStrategySpec,
    StrategySpecError,
    migrate_strategy_payload,
)
from finboard_backtest.strategy_spec.universe_precheck import UniversePoolPreview
from finboard_data.factor_lab import is_user_factor_name
from finboard_data.factors import FACTOR_CATALOG

LIFECYCLE_STAGES = (
    "raw_data",
    "features",
    "signals",
    "target_positions",
    "risk_constraints",
    "order_intents",
    "paper_execution",
    "positions_and_pnl",
)


@dataclass(frozen=True, slots=True)
class FeatureSourceDefinition:
    name: str
    kind: FeatureKind
    required_datasets: tuple[str, ...]
    description: str


def _factor_sources() -> dict[str, FeatureSourceDefinition]:
    result: dict[str, FeatureSourceDefinition] = {}
    for definition in FACTOR_CATALOG.values():
        datasets = tuple(sorted({item.split(".", maxsplit=1)[0] for item in definition.dependencies}))
        result[definition.name.value] = FeatureSourceDefinition(
            name=definition.name.value,
            kind=FeatureKind.FACTOR,
            required_datasets=datasets,
            description=definition.description,
        )
    for name, meta in RESEARCH_FACTOR_CATALOG.items():
        result.setdefault(
            name,
            FeatureSourceDefinition(
                name=name,
                kind=FeatureKind.FACTOR,
                required_datasets=(_dataset_from_source(meta.source_field),),
                description=meta.economic_hypothesis,
            ),
        )
    return result


def _dataset_from_source(source: str) -> str:
    prefix = source.split(":", maxsplit=1)[-1].split(".", maxsplit=1)[0]
    aliases = {
        "daily": "daily_metrics",
        "financial": "financial_indicators",
        "daily_returns_std_20d": "bars",
        "daily_returns_std_60d": "bars",
        "daily_returns_std_120d": "bars",
        "downside_returns_std_60d": "bars",
        "return_excl_latest_20d": "bars",
        "1/pe_ttm": "daily_metrics",
    }
    return aliases.get(prefix, prefix)


FEATURE_SOURCE_CATALOG: dict[str, FeatureSourceDefinition] = {
    **_factor_sources(),
    "open": FeatureSourceDefinition("open", FeatureKind.MARKET_INPUT, ("bars",), "开盘价"),
    "high": FeatureSourceDefinition("high", FeatureKind.MARKET_INPUT, ("bars",), "最高价"),
    "low": FeatureSourceDefinition("low", FeatureKind.MARKET_INPUT, ("bars",), "最低价"),
    "close": FeatureSourceDefinition("close", FeatureKind.MARKET_INPUT, ("bars",), "收盘价"),
    "volume": FeatureSourceDefinition("volume", FeatureKind.MARKET_INPUT, ("bars",), "成交量"),
    "amount": FeatureSourceDefinition("amount", FeatureKind.MARKET_INPUT, ("bars",), "成交额"),
    "premium_rate": FeatureSourceDefinition(
        "premium_rate",
        FeatureKind.FACTOR,
        ("convertible_metadata", "bars"),
        "可转债转股溢价率",
    ),
    "ytm": FeatureSourceDefinition(
        "ytm",
        FeatureKind.FACTOR,
        ("convertible_metadata", "bars"),
        "可转债到期收益率",
    ),
    "remaining_size": FeatureSourceDefinition(
        "remaining_size",
        FeatureKind.FACTOR,
        ("convertible_metadata",),
        "可转债剩余规模",
    ),
    "market_return": FeatureSourceDefinition(
        "market_return",
        FeatureKind.MARKET_INPUT,
        ("benchmark_bars",),
        "市场基准收益",
    ),
    "risk_free_rate": FeatureSourceDefinition(
        "risk_free_rate",
        FeatureKind.MARKET_INPUT,
        ("macro_inputs",),
        "无风险利率",
    ),
    "term_spread": FeatureSourceDefinition(
        "term_spread",
        FeatureKind.MARKET_INPUT,
        ("macro_inputs",),
        "期限利差",
    ),
    "fx_return": FeatureSourceDefinition(
        "fx_return",
        FeatureKind.MARKET_INPUT,
        ("cross_market_inputs",),
        "汇率收益",
    ),
    "commodity_return": FeatureSourceDefinition(
        "commodity_return",
        FeatureKind.MARKET_INPUT,
        ("cross_market_inputs",),
        "商品收益",
    ),
    "beta": FeatureSourceDefinition(
        "beta",
        FeatureKind.RISK_FACTOR,
        ("bars", "benchmark_bars"),
        "市场 beta",
    ),
    "industry": FeatureSourceDefinition(
        "industry",
        FeatureKind.RISK_FACTOR,
        ("industry_memberships",),
        "行业暴露",
    ),
    "size": FeatureSourceDefinition(
        "size",
        FeatureKind.RISK_FACTOR,
        ("daily_metrics",),
        "规模风险暴露",
    ),
}


@dataclass(frozen=True, slots=True)
class ResolvedStrategyPlan:
    """可归档但不可直接运行的策略解析结果。"""

    spec: ResearchStrategySpec
    checksum: str
    feature_order: tuple[str, ...]
    required_factor_sources: tuple[str, ...]
    required_datasets: tuple[str, ...]
    dataset_release_ids: tuple[str, ...]
    lifecycle_stages: tuple[str, ...] = LIFECYCLE_STAGES
    can_execute: bool = False
    # issue #186:universe 预检结果(字段依赖 warning + 候选池空池预览)。
    # 由 API/MCP 的 ``_compile_with_releases`` 在拿到发布 instruments 后
    # 经 ``dataclasses.replace`` 附加;纯 ``compile_strategy_spec`` 路径为 None。
    universe_precheck: UniversePoolPreview | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.canonical_payload(),
            "checksum": self.checksum,
            "feature_order": list(self.feature_order),
            "required_factor_sources": list(self.required_factor_sources),
            "required_datasets": list(self.required_datasets),
            "dataset_release_ids": list(self.dataset_release_ids),
            "lifecycle_stages": list(self.lifecycle_stages),
            "can_execute": self.can_execute,
            "universe_precheck": (
                self.universe_precheck.as_dict() if self.universe_precheck is not None else None
            ),
        }


def strategy_spec_checksum(spec: ResearchStrategySpec) -> str:
    encoded = json.dumps(
        spec.canonical_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compile_strategy_spec(
    raw: ResearchStrategySpec | dict[str, Any],
    *,
    disabled_factors: frozenset[str] = frozenset(),
    available_dataset_release_ids: frozenset[str] | None = None,
    user_factor_sources: Collection[str] = frozenset(),
) -> ResolvedStrategyPlan:
    """校验并解析策略规格。所有依赖均 fail closed。

    ``user_factor_sources`` 是当前 ``status=active`` 的沙箱用户因子名
    集合(u_ 前缀,#217);引用非集合内的用户因子直接报错(retired /
    不存在的 artifact fail-visible,由调用方从 DB 注入名单)。
    """

    payload = raw.canonical_payload() if isinstance(raw, ResearchStrategySpec) else raw
    spec = ResearchStrategySpec.model_validate(migrate_strategy_payload(payload))

    required_sources: set[str] = set()
    required_datasets: set[str] = set()
    for node in spec.feature_graph.nodes:
        if node.source is None:
            continue
        if is_user_factor_name(node.source):
            if node.source not in user_factor_sources:
                raise StrategySpecError(
                    f"用户因子不可引用(artifact 不存在或非 active): {node.source};"
                    "先 finboard_research_code_submit 提交因子代码并保持 "
                    "status=active,沙箱执行产出快照后才能被规格引用"
                )
            if node.kind is not FeatureKind.FACTOR:
                raise StrategySpecError(
                    f"用户因子节点 {node.node_id} kind 须为 factor,"
                    f"实际 {node.kind.value}"
                )
            if node.source in disabled_factors:
                raise StrategySpecError(f"策略依赖已停用因子: {node.source}")
            required_sources.add(node.source)
            continue
        definition = FEATURE_SOURCE_CATALOG.get(node.source)
        if definition is None:
            raise StrategySpecError(f"未注册的因子/市场输入: {node.source}")
        if node.kind is not definition.kind:
            raise StrategySpecError(
                f"节点 {node.node_id} kind={node.kind.value} 与源 {node.source} "
                f"kind={definition.kind.value} 不一致"
            )
        if node.source in disabled_factors:
            raise StrategySpecError(f"策略依赖已停用因子: {node.source}")
        required_sources.add(node.source)
        required_datasets.update(definition.required_datasets)

    missing_releases: set[str] = set()
    if available_dataset_release_ids is not None:
        missing_releases = (
            set(spec.validation_plan.dataset_release_ids)
            - available_dataset_release_ids
        )
    if missing_releases:
        raise StrategySpecError(f"历史数据发布不存在或不可用: {sorted(missing_releases)}")

    return ResolvedStrategyPlan(
        spec=spec,
        checksum=strategy_spec_checksum(spec),
        feature_order=_topological_order(spec),
        required_factor_sources=tuple(sorted(required_sources)),
        required_datasets=tuple(sorted(required_datasets)),
        dataset_release_ids=spec.validation_plan.dataset_release_ids,
    )


def _topological_order(spec: ResearchStrategySpec) -> tuple[str, ...]:
    nodes = {node.node_id: node for node in spec.feature_graph.nodes}
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        for dependency in nodes[node_id].inputs:
            visit(dependency)
        visited.add(node_id)
        ordered.append(node_id)

    for node in spec.feature_graph.nodes:
        visit(node.node_id)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class StructuredChange:
    path: str
    before: object
    after: object

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "before": self.before, "after": self.after}


def structured_diff(
    before: ResearchStrategySpec | dict[str, Any],
    after: ResearchStrategySpec | dict[str, Any],
) -> tuple[StructuredChange, ...]:
    """生成稳定路径的结构化差异。供版本历史审计。"""

    left = before.canonical_payload() if isinstance(before, ResearchStrategySpec) else before
    right = after.canonical_payload() if isinstance(after, ResearchStrategySpec) else after
    changes: list[StructuredChange] = []

    def compare(path: str, old: object, new: object) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                compare(f"{path}.{key}", old.get(key), new.get(key))
            return
        if isinstance(old, list) and isinstance(new, list):
            for index in range(max(len(old), len(new))):
                old_item = old[index] if index < len(old) else None
                new_item = new[index] if index < len(new) else None
                compare(f"{path}[{index}]", old_item, new_item)
            return
        if old != new:
            changes.append(StructuredChange(path=path, before=old, after=new))

    compare("$", left, right)
    return tuple(changes)


__all__ = [
    "FEATURE_SOURCE_CATALOG",
    "LIFECYCLE_STAGES",
    "FeatureSourceDefinition",
    "ResolvedStrategyPlan",
    "StructuredChange",
    "compile_strategy_spec",
    "strategy_spec_checksum",
    "structured_diff",
]
