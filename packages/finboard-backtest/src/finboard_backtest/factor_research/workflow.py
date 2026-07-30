"""因子假设审批与研究工作流(Issue #65 / #84)。

状态机::

    proposed ──approve──▶ approved ──register──▶ in_sample
       │                    │                       │
       │                 reject                  reject
       ▼                    ▼                       ▼
    rejected             rejected               rejected
                                                       │
                                                   pass OOS
                                                       ▼
                                                 validated_oos

    任意状态 ──supersede──▶ superseded

核心不变量(AGENTS.md 红线):
- 假设必须通过白名单验证才能提交
- 人工批准是进入实验的显式 gate(approver / 时间 / 版本可审计)
- 试验次数不能超过参数预算
- validated_oos 只能由 #57 持久化的机器验证实验决定(complete_experiment
  绑定 MachineValidationOutcome,不接受 passed_oos: bool)
- 不连接 Broker / OrderManager / 实盘策略配置 / 动态代码执行
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from finboard_backtest.factor_research.audit import (
    AuditEventType,
    AuditTrail,
)
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    HypothesisStatus,
    Reference,
)
from finboard_backtest.factor_research.whitelist import (
    HypothesisValidationResult,
    HypothesisValidator,
)


class WorkflowError(Exception):
    """工作流状态转换错误。"""


@dataclass(frozen=True, slots=True)
class ExperimentRegistration:
    """实验登记记录。

    绑定模型 / 提示词 / 文献 / 数据 / 代码版本,
    用于审计和可复现性。
    """

    experiment_id: str
    hypothesis_id: str
    model_version: str
    prompt_version: str
    dataset_version: str
    code_version: str
    registered_at: datetime
    registered_by: str
    references: tuple[Reference, ...] = ()
    status: str = "registered"
    validation_experiment_id: str | None = None

    @property
    def reference_count(self) -> int:
        return len(self.references)


# #57 机器验证终态(与 validation.contracts.ExperimentStatus 的终态对应)。
_VALID_MACHINE_STATUSES: frozenset[str] = frozenset({"validated_oos", "rejected"})


@dataclass(frozen=True, slots=True)
class MachineValidationOutcome:
    """#57 持久化机器验证的终态结果(只读值对象)。

    由系统从已完成的 #57 ``ResearchExperiment`` 读取后传入
    :meth:`ResearchWorkflow.complete_experiment`。

    红线:
    * ``status`` 必须是终态(validated_oos / rejected),不接受中间态;
    * 该对象只能由持久化 service 从 DB 构造,不能由 API 请求体 / LLM 直接伪造;
    * ``validation_experiment_id`` 必须指向持久化的 #57 实验。
    """

    validation_experiment_id: str
    status: str
    trials_used: int
    rejection_reason: str = ""

    def __post_init__(self) -> None:
        if not self.validation_experiment_id.strip():
            raise ValueError("MachineValidationOutcome.validation_experiment_id 不能为空")
        if self.status not in _VALID_MACHINE_STATUSES:
            raise ValueError(
                f"MachineValidationOutcome.status 必须是终态 "
                f"{sorted(_VALID_MACHINE_STATUSES)}, 得到 '{self.status}'"
            )
        if self.trials_used < 0:
            raise ValueError("MachineValidationOutcome.trials_used 不能为负")

    @property
    def passed_oos(self) -> bool:
        """由 #57 机器结果决定(非人工 / LLM 判断)。"""
        return self.status == "validated_oos"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_experiment_id() -> str:
    return f"exp-{uuid.uuid4().hex[:12]}"


@dataclass
class ResearchWorkflow:
    """因子假设审批与研究工作流。

    使用方式::

        wf = ResearchWorkflow()
        h = FactorHypothesis(...)
        wf.submit(h, actor="llm@gpt-4")
        wf.approve(h.hypothesis_id, approver="alice")
        reg = wf.register_experiment(
            h.hypothesis_id,
            model_version="gpt-4-0613",
            prompt_version="v3",
            dataset_version="akshare-2024-01",
            code_version="abc1234",
            registered_by="alice",
        )
        wf.complete_experiment(
            reg.experiment_id,
            validation=MachineValidationOutcome(
                validation_experiment_id="exp-57-xxxx",
                status="validated_oos",
                trials_used=3,
            ),
            completed_by="alice",
        )
    """

    validator: HypothesisValidator = field(default_factory=HypothesisValidator)
    audit: AuditTrail = field(default_factory=AuditTrail)
    _hypotheses: dict[str, FactorHypothesis] = field(default_factory=dict)
    _experiments: dict[str, ExperimentRegistration] = field(default_factory=dict)
    _validations: dict[str, HypothesisValidationResult] = field(default_factory=dict)

    @classmethod
    def restore(
        cls,
        hypotheses: list[FactorHypothesis],
        experiments: list[ExperimentRegistration],
        *,
        validator: HypothesisValidator | None = None,
    ) -> ResearchWorkflow:
        """从持久化存储重建内存工作流(用于重启恢复)。

        重新对每个假设跑白名单验证,重建 ``_validations`` 快照。
        审计轨迹(:class:`AuditTrail`)不在此重建 —— 审计事件由持久化层
        独立保存(``ai_audit_events`` 表),重启后从 DB 读取。
        """
        wf = cls(validator=validator or HypothesisValidator())
        for h in hypotheses:
            wf._hypotheses[h.hypothesis_id] = h
            wf._validations[h.hypothesis_id] = wf.validator.validate(h)
        for e in experiments:
            wf._experiments[e.experiment_id] = e
        return wf

    # ------------------------------------------------------------------
    # 提交 / 验证
    # ------------------------------------------------------------------

    def submit(
        self,
        hypothesis: FactorHypothesis,
        actor: str = "system",
    ) -> FactorHypothesis:
        """提交新假设并自动验证。

        如果验证失败,假设直接进入 ``REJECTED`` 状态但仍然保留在记录中
        (失败实验同样保留,用于审计)。
        """
        result = self.validator.validate(hypothesis)
        self._validations[hypothesis.hypothesis_id] = result

        if not result.is_valid:
            rejected = hypothesis.with_status(
                HypothesisStatus.REJECTED,
                rejection_reason="; ".join(result.errors),
            )
            self._hypotheses[hypothesis.hypothesis_id] = rejected
            self.audit.log(
                AuditEventType.VALIDATION_FAILED,
                hypothesis.hypothesis_id,
                actor,
                errors="; ".join(result.errors),
            )
            return rejected

        self._hypotheses[hypothesis.hypothesis_id] = hypothesis
        self.audit.log(
            AuditEventType.VALIDATION_PASSED,
            hypothesis.hypothesis_id,
            actor,
            name=hypothesis.name,
            version=hypothesis.version,
            warnings="; ".join(result.warnings) if result.warnings else "",
        )
        return hypothesis

    # ------------------------------------------------------------------
    # 人工审批 gate
    # ------------------------------------------------------------------

    def approve(
        self,
        hypothesis_id: str,
        approver: str,
    ) -> FactorHypothesis:
        """人工批准假设 —— 进入实验的显式 gate。"""
        current = self._require(hypothesis_id)
        if current.status != HypothesisStatus.PROPOSED:
            raise WorkflowError(
                f"假设 {hypothesis_id} 当前状态为 {current.status.value}, "
                f"只有 PROPOSED 才能审批"
            )
        approved = current.with_status(
            HypothesisStatus.APPROVED,
            approved_by=approver,
            approved_at=_utcnow(),
        )
        self._hypotheses[hypothesis_id] = approved
        self.audit.log(
            AuditEventType.APPROVED,
            hypothesis_id,
            approver,
        )
        return approved

    def reject(
        self,
        hypothesis_id: str,
        approver: str,
        reason: str,
    ) -> FactorHypothesis:
        """人工拒绝假设。"""
        current = self._require(hypothesis_id)
        if current.status not in (HypothesisStatus.PROPOSED, HypothesisStatus.APPROVED):
            raise WorkflowError(
                f"假设 {hypothesis_id} 当前状态为 {current.status.value}, "
                f"只有 PROPOSED / APPROVED 才能拒绝"
            )
        rejected = current.with_status(
            HypothesisStatus.REJECTED,
            rejection_reason=reason,
        )
        self._hypotheses[hypothesis_id] = rejected
        self.audit.log(
            AuditEventType.REJECTED,
            hypothesis_id,
            approver,
            reason=reason,
        )
        return rejected

    # ------------------------------------------------------------------
    # 实验登记 / 完成
    # ------------------------------------------------------------------

    def register_experiment(
        self,
        hypothesis_id: str,
        *,
        model_version: str,
        prompt_version: str,
        dataset_version: str,
        code_version: str,
        registered_by: str,
        validation_experiment_id: str | None = None,
    ) -> ExperimentRegistration:
        """登记实验(假设必须已 APPROVED 或已在 IN_SAMPLE 中)。

        检查试验预算:试验次数不能超过 ``parameter_budget``。

        ``validation_experiment_id`` 可选,用于预绑定关联的 #57 机器验证实验;
        最终结论由 :meth:`complete_experiment` 的
        :class:`MachineValidationOutcome` 决定。
        """
        current = self._require(hypothesis_id)
        if current.status not in (
            HypothesisStatus.APPROVED,
            HypothesisStatus.IN_SAMPLE,
        ):
            raise WorkflowError(
                f"假设 {hypothesis_id} 状态为 {current.status.value}, "
                f"必须先 APPROVED 才能登记实验"
            )

        if current.experiment_count >= current.parameter_budget:
            raise WorkflowError(
                f"假设 {hypothesis_id} 试验次数 "
                f"({current.experiment_count}) 已达预算上限 "
                f"({current.parameter_budget})"
            )

        reg = ExperimentRegistration(
            experiment_id=_new_experiment_id(),
            hypothesis_id=hypothesis_id,
            model_version=model_version,
            prompt_version=prompt_version,
            dataset_version=dataset_version,
            code_version=code_version,
            registered_at=_utcnow(),
            registered_by=registered_by,
            references=current.references,
            validation_experiment_id=validation_experiment_id,
        )
        self._experiments[reg.experiment_id] = reg

        new_count = current.experiment_count + 1
        if current.status == HypothesisStatus.APPROVED:
            updated = current.with_status(
                HypothesisStatus.IN_SAMPLE,
                experiment_count=new_count,
            )
        else:
            updated = dataclasses.replace(
                current,
                experiment_count=new_count,
            )
        self._hypotheses[hypothesis_id] = updated

        self.audit.log(
            AuditEventType.EXPERIMENT_REGISTERED,
            hypothesis_id,
            registered_by,
            experiment_id=reg.experiment_id,
            model_version=model_version,
            dataset_version=dataset_version,
            code_version=code_version,
        )
        return reg

    def complete_experiment(
        self,
        experiment_id: str,
        *,
        validation: MachineValidationOutcome,
        completed_by: str,
    ) -> FactorHypothesis:
        """完成实验并更新假设状态(Issue #84 重构)。

        不再接受 ``passed_oos: bool`` —— 必须传入
        :class:`MachineValidationOutcome`(由系统从已完成的 #57
        持久化机器验证实验读取):

        * ``validation.status == "validated_oos"`` → 假设进入 ``VALIDATED_OOS``;
        * ``validation.status == "rejected"`` → 假设进入 ``REJECTED``。

        红线: ``validation`` 只能由持久化 service 从 DB 构造,
        不能由 LLM / 人工主观判断或 API 请求体直接伪造。
        """
        reg = self._experiments.get(experiment_id)
        if reg is None:
            raise WorkflowError(f"未知实验 ID: {experiment_id}")

        current = self._require(reg.hypothesis_id)

        completed_reg = dataclasses.replace(
            reg,
            status="completed",
            validation_experiment_id=validation.validation_experiment_id,
        )
        self._experiments[experiment_id] = completed_reg

        if validation.passed_oos:
            validated = current.with_status(HypothesisStatus.VALIDATED_OOS)
            self._hypotheses[reg.hypothesis_id] = validated
            self.audit.log(
                AuditEventType.VALIDATED_OOS,
                reg.hypothesis_id,
                completed_by,
                experiment_id=experiment_id,
                validation_experiment_id=validation.validation_experiment_id,
                trials_used=str(validation.trials_used),
            )
            return validated

        reason = validation.rejection_reason or "机器 OOS 验证未通过"
        rejected = current.with_status(
            HypothesisStatus.REJECTED,
            rejection_reason=reason,
        )
        self._hypotheses[reg.hypothesis_id] = rejected
        self.audit.log(
            AuditEventType.EXPERIMENT_COMPLETED,
            reg.hypothesis_id,
            completed_by,
            experiment_id=experiment_id,
            validation_experiment_id=validation.validation_experiment_id,
            result="failed_oos",
            reason=reason,
        )
        return rejected

    def interrupt_experiment(
        self,
        experiment_id: str,
        reason: str,
        actor: str,
    ) -> ExperimentRegistration:
        """标记实验中断(故障注入 / 系统崩溃后恢复)。"""
        reg = self._experiments.get(experiment_id)
        if reg is None:
            raise WorkflowError(f"未知实验 ID: {experiment_id}")
        if reg.status not in ("registered", "running"):
            raise WorkflowError(
                f"实验 {experiment_id} 状态为 {reg.status}, 不能中断"
            )
        interrupted = dataclasses.replace(reg, status="interrupted")
        self._experiments[experiment_id] = interrupted
        self.audit.log(
            AuditEventType.EXPERIMENT_INTERRUPTED,
            reg.hypothesis_id,
            actor,
            experiment_id=experiment_id,
            reason=reason,
        )
        return interrupted

    # ------------------------------------------------------------------
    # 版本取代
    # ------------------------------------------------------------------

    def supersede(
        self,
        old_id: str,
        new_hypothesis: FactorHypothesis,
        actor: str = "system",
    ) -> FactorHypothesis:
        """标记新假设取代旧假设。

        旧假设进入 ``SUPERSEDED``,新假设的 ``supersedes_id`` 指向旧 ID。
        新假设需要独立提交和验证。
        """
        old = self._require(old_id)
        self._hypotheses[old_id] = old.with_status(HypothesisStatus.SUPERSEDED)

        linked = dataclasses.replace(
            new_hypothesis,
            supersedes_id=old_id,
        )
        result = self.validator.validate(linked)
        self._validations[linked.hypothesis_id] = result
        if not result.is_valid:
            rejected = linked.with_status(
                HypothesisStatus.REJECTED,
                rejection_reason="; ".join(result.errors),
            )
            self._hypotheses[linked.hypothesis_id] = rejected
        else:
            self._hypotheses[linked.hypothesis_id] = linked

        self.audit.log(
            AuditEventType.SUPERSEDED,
            old_id,
            actor,
            superseded_by=linked.hypothesis_id,
        )
        return self._hypotheses[linked.hypothesis_id]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_hypothesis(self, hypothesis_id: str) -> FactorHypothesis | None:
        return self._hypotheses.get(hypothesis_id)

    def get_validation(
        self,
        hypothesis_id: str,
    ) -> HypothesisValidationResult | None:
        return self._validations.get(hypothesis_id)

    def get_experiment(self, experiment_id: str) -> ExperimentRegistration | None:
        return self._experiments.get(experiment_id)

    def list_hypotheses(
        self,
        status: HypothesisStatus | None = None,
    ) -> list[FactorHypothesis]:
        if status is None:
            return list(self._hypotheses.values())
        return [
            h for h in self._hypotheses.values()
            if h.status == status
        ]

    def list_experiments(
        self,
        hypothesis_id: str | None = None,
    ) -> list[ExperimentRegistration]:
        if hypothesis_id is None:
            return list(self._experiments.values())
        return [
            e for e in self._experiments.values()
            if e.hypothesis_id == hypothesis_id
        ]

    @property
    def hypothesis_count(self) -> int:
        return len(self._hypotheses)

    @property
    def experiment_count(self) -> int:
        return len(self._experiments)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require(self, hypothesis_id: str) -> FactorHypothesis:
        h = self._hypotheses.get(hypothesis_id)
        if h is None:
            raise WorkflowError(f"未知假设 ID: {hypothesis_id}")
        return h
