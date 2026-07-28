"""结构化因子假设(Issue #65)。

LLM 只能输出 ``FactorHypothesis``, 不允许生成可执行代码。
所有假设必须通过白名单验证 + 人工批准后才能进入研究实验。

设计约束(AGENTS.md 红线):
* LLM 不连接实盘账户 / 不发送订单 / 不修改持仓 / 不绕过风控
* 公式只映射到白名单字段与算子,禁止任意 Python / SQL / shell
* 人工批准是进入实验的显式 gate
* validated_oos 只能由 #57 walk-forward 的机器可验证结果产生
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class HypothesisStatus(StrEnum):
    """因子假设生命周期状态。

    状态转换图::

        proposed ──approve──▶ approved ──register──▶ in_sample
           │                     │                      │
           │                  reject                 reject
           ▼                     ▼                      ▼
        rejected              rejected              rejected
                                                        │
                                                    pass OOS
                                                        ▼
                                                  validated_oos

        任意状态 ──supersede──▶ superseded
    """

    PROPOSED = "proposed"
    APPROVED = "approved_for_research"
    IN_SAMPLE = "in_sample"
    VALIDATED_OOS = "validated_oos"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


_VALID_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    HypothesisStatus.PROPOSED: frozenset({
        HypothesisStatus.APPROVED,
        HypothesisStatus.REJECTED,
        HypothesisStatus.SUPERSEDED,
    }),
    HypothesisStatus.APPROVED: frozenset({
        HypothesisStatus.IN_SAMPLE,
        HypothesisStatus.REJECTED,
        HypothesisStatus.SUPERSEDED,
    }),
    HypothesisStatus.IN_SAMPLE: frozenset({
        HypothesisStatus.VALIDATED_OOS,
        HypothesisStatus.REJECTED,
        HypothesisStatus.SUPERSEDED,
    }),
    HypothesisStatus.VALIDATED_OOS: frozenset({
        HypothesisStatus.SUPERSEDED,
    }),
    HypothesisStatus.REJECTED: frozenset({
        HypothesisStatus.SUPERSEDED,
    }),
    HypothesisStatus.SUPERSEDED: frozenset(),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return f"fh-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class Reference:
    """外部文献 / 数据源引用。

    必须记录出处和访问时间,用于审计和可复现性。
    """

    title: str
    authors: str = ""
    year: int | None = None
    url: str | None = None
    doi: str | None = None
    accessed_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Reference.title 不能为空")


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """参数预算规格。

    限制 LLM 提出的参数搜索空间,防止数据挖掘(multiple testing)。
    ``grid_size`` 表示该参数在搜索网格中的取值数量;
    假设的总组合数 = 所有参数 ``grid_size`` 的乘积。
    """

    name: str
    min_value: float
    max_value: float
    grid_size: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ParameterSpec.name 不能为空")
        if self.grid_size < 1:
            raise ValueError(
                f"ParameterSpec '{self.name}' grid_size 必须 >= 1, "
                f"得到 {self.grid_size}"
            )
        if self.min_value > self.max_value:
            raise ValueError(
                f"ParameterSpec '{self.name}' min_value({self.min_value}) "
                f"不能大于 max_value({self.max_value})"
            )


@dataclass(frozen=True, slots=True)
class FactorHypothesis:
    """结构化因子假设。

    LLM 的唯一合法输出格式 —— 不包含可执行代码。
    必须通过白名单验证(:class:`HypothesisValidator`)和人工批准
    (:meth:`ResearchWorkflow.approve`)后才能进入研究实验。

    Attributes:
        name: 因子名称(人类可读)。
        economic_mechanism: 经济机制描述(为什么这个因子应该有效)。
        input_fields: 输入字段名(必须在 ``ALLOWED_FIELDS`` 白名单中)。
        decision_timing: 决策时点(close / open / vwap / twap)。
        formula: 公式描述(人类可读,引用白名单算子,不含可执行代码)。
        direction: 因子方向(long_high = 高分做多 / long_low = 低分做多 /
            neutral = 中性)。
        applicable_assets: 适用资产描述(如 "沪深300 成分股")。
        expected_failure_scenarios: 预期失效场景(如 "牛市末期动量反转")。
        parameters: 参数预算(有限搜索空间)。
        references: 参考文献 / 数据出处。
    """

    name: str
    economic_mechanism: str
    input_fields: tuple[str, ...]
    decision_timing: str
    formula: str
    direction: str
    applicable_assets: tuple[str, ...] = ()
    expected_failure_scenarios: tuple[str, ...] = ()
    parameters: tuple[ParameterSpec, ...] = ()
    references: tuple[Reference, ...] = ()
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    hypothesis_id: str = field(default_factory=_new_id)
    version: str = "v1"
    supersedes_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    experiment_count: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("FactorHypothesis.name 不能为空")
        if not self.economic_mechanism.strip():
            raise ValueError("FactorHypothesis.economic_mechanism 不能为空")
        if len(self.input_fields) == 0:
            raise ValueError("FactorHypothesis.input_fields 不能为空")
        if not self.formula.strip():
            raise ValueError("FactorHypothesis.formula 不能为空")
        valid_directions = ("long_high", "long_low", "neutral")
        if self.direction not in valid_directions:
            raise ValueError(
                f"direction 必须是 {valid_directions}, 得到 '{self.direction}'"
            )

    @property
    def parameter_budget(self) -> int:
        """总参数组合数 = 所有参数 grid_size 的乘积。"""
        budget = 1
        for p in self.parameters:
            budget *= p.grid_size
        return budget

    @property
    def experiments_remaining(self) -> int:
        """剩余可用试验次数。"""
        return max(0, self.parameter_budget - self.experiment_count)

    @property
    def is_terminal(self) -> bool:
        """是否处于终态(不可再转换)。"""
        return self.status in (
            HypothesisStatus.VALIDATED_OOS,
            HypothesisStatus.SUPERSEDED,
        )

    def with_status(self, new_status: HypothesisStatus, **kwargs: Any) -> FactorHypothesis:
        """返回带新状态的不可变副本。

        Raises:
            ValueError: 如果状态转换不合法。
        """
        allowed = _VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"非法状态转换: {self.status.value} -> {new_status.value}. "
                f"合法目标: {[s.value for s in allowed] or '(终态)'}"
            )
        return dataclasses.replace(self, status=new_status, **kwargs)

    def to_text(self) -> str:
        """序列化为纯文本,用于注入 ``ResearchExperiment.hypothesis: str``。

        保持与 #57 现有 ``ResearchExperiment`` 的兼容性 ——
        结构化假设最终仍以文本形式注入实验记录。
        """
        lines = [
            f"[FactorHypothesis] {self.name} ({self.hypothesis_id})",
            f"  economic_mechanism: {self.economic_mechanism}",
            f"  input_fields: {', '.join(self.input_fields)}",
            f"  decision_timing: {self.decision_timing}",
            f"  formula: {self.formula}",
            f"  direction: {self.direction}",
            f"  applicable_assets: {', '.join(self.applicable_assets) or '(未指定)'}",
            f"  expected_failure_scenarios: {'; '.join(self.expected_failure_scenarios) or '(未指定)'}",
            f"  parameter_budget: {self.parameter_budget} combinations",
            f"  references: {len(self.references)} entries",
            f"  status: {self.status.value}",
            f"  version: {self.version}",
        ]
        return "\n".join(lines)
