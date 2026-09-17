"""#29/#60-#64 到统一 ResearchRun 的适配器端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    DecisionLedgerView,
    ResearchRunManifest,
    ResearchRunReport,
    UnsupportedResearchCapabilityError,
)
from finboard_backtest.strategy_spec.registry import get_strategy_capability

SUPPORTED_RESEARCH_STRATEGIES = frozenset(
    {
        "ma_cross",
        "multi_factor",
        "etf_rotation",
        "mean_reversion",
        "convertible_double_low",
        "futures_tsmom",
        # issue #218:沙箱策略代码(strategy.decide → 目标权重)。
        "user_code",
    }
)
_CAPABILITY_ALTERNATIVES: dict[str, tuple[tuple[str, ...], ...]] = {
    "ma_cross": (("stock", "etf:*"),),
    "multi_factor": (("stock",),),
    "etf_rotation": (("etf:*",),),
    "mean_reversion": (("etf:*",),),
    "convertible_double_low": (("convertible",),),
    "futures_tsmom": (("futures",),),
    # issue #218:与 ma_cross 同面 —— A 股权益行情(bars 主发布)。
    "user_code": (("stock", "etf:*"),),
}


class ResearchStrategyAdapter(Protocol):
    """统一策略适配器。

    适配器负责调用既有策略/回测实现并把结果归一为 ``DecisionBundle``。编排器
    只消费此契约,不感知 #60-#64 的内部模拟器,也不会把研究订单送往 Broker。
    """

    @property
    def strategy_kind(self) -> str: ...

    def validate_manifest(self, manifest: ResearchRunManifest) -> None: ...

    def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]: ...

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionLedgerView],
    ) -> ResearchRunReport: ...
    # issue #473:``decisions`` 收账本视图 —— run 主链路传 ``DecisionLedgerRecord``
    # (落库后驻留形态,candidates/features 不再钉住),测试 / 对照路径传完整
    # ``DecisionBundle`` 亦结构性满足(消费字段集见 DecisionLedgerView 审计)。


@dataclass(frozen=True, slots=True)
class DecisionSequenceAdapter:
    """预计算决策的兼容/测试适配器。

    仅用于回归样本和通用流水线尚不能表达的专用模拟器迁移。每个
    ``DecisionBundle`` 仍必须携带可校验的正式流水线证据, 否则 Coordinator 会
    失败关闭。long-only 生产研究应使用 ``PortfolioPipelineAdapter``, 不能由
    调用方预拼装目标、订单、成交或持仓。
    """

    strategy_kind: str
    decision_sequence: tuple[DecisionBundle, ...]
    report: ResearchRunReport
    required_capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        strategy_kind: str,
        decisions: Iterable[DecisionBundle],
        report: ResearchRunReport,
        required_capabilities: Iterable[str] = (),
    ) -> None:
        object.__setattr__(self, "strategy_kind", strategy_kind)
        object.__setattr__(self, "decision_sequence", tuple(decisions))
        object.__setattr__(self, "report", report)
        object.__setattr__(
            self, "required_capabilities", tuple(sorted(set(required_capabilities)))
        )
        if strategy_kind not in SUPPORTED_RESEARCH_STRATEGIES:
            raise UnsupportedResearchCapabilityError(
                f"未注册策略适配器: {strategy_kind}"
            )
        get_strategy_capability(strategy_kind)

    def validate_manifest(self, manifest: ResearchRunManifest) -> None:
        if manifest.strategy_kind != self.strategy_kind:
            raise UnsupportedResearchCapabilityError(
                f"manifest={manifest.strategy_kind} 与 adapter={self.strategy_kind} 不一致"
            )
        available = {
            capability
            for release in manifest.dataset_releases
            for capability in release.capabilities
        }
        missing = sorted(set(self.required_capabilities) - available)
        if missing:
            raise UnsupportedResearchCapabilityError(
                f"冻结数据缺少策略能力: {missing}"
            )
        validate_strategy_dataset_capabilities(self.strategy_kind, available)
        if self.report.strategy_kind != self.strategy_kind:
            raise UnsupportedResearchCapabilityError(
                "报告策略类型与适配器不一致"
            )

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        del manifest
        for decision in self.decision_sequence:
            yield decision

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionLedgerView],
    ) -> ResearchRunReport:
        del manifest, decisions
        return self.report


def registered_adapter_kinds() -> tuple[str, ...]:
    return tuple(sorted(SUPPORTED_RESEARCH_STRATEGIES))


def validate_strategy_dataset_capabilities(
    strategy_kind: str,
    available: set[str],
) -> None:
    if strategy_kind not in _CAPABILITY_ALTERNATIVES:
        raise UnsupportedResearchCapabilityError(
            f"未注册策略适配器: {strategy_kind}"
        )
    missing_groups = [
        alternatives
        for alternatives in _CAPABILITY_ALTERNATIVES[strategy_kind]
        if not any(_capability_matches(item, available) for item in alternatives)
    ]
    if missing_groups:
        raise UnsupportedResearchCapabilityError(
            f"冻结数据不满足资产能力组: {missing_groups}"
        )


def _capability_matches(required: str, available: set[str]) -> bool:
    if required.endswith("*"):
        return any(item.startswith(required[:-1]) for item in available)
    return required in available


__all__ = [
    "SUPPORTED_RESEARCH_STRATEGIES",
    "DecisionSequenceAdapter",
    "ResearchStrategyAdapter",
    "registered_adapter_kinds",
    "validate_strategy_dataset_capabilities",
]
