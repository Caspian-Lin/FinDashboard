"""样本外验证流水线的数据契约(issue #57)。

核心原则
========

1. **假设先行冻结**:``ResearchExperiment.hypothesis`` 在创建后不可修改;
   修改假设必须新版本(supersedes)。
2. **试验预算预先声明**:`trial_budget` 限制允许的候选试验总数,
   ``trials_used`` 必须单调递增,超出预算即拒绝。
3. **最终揭盲只允许一次**:`final_test_unsealed` 一旦 True 不可撤销,
   揭盲后修改假设必须新建 experiment(supersedes 旧版本)。
4. **失败也算试验**:``TrialRecord`` 保存全部候选(包括 REJECTED/FAILED),
   多重试验修正需要真实试验总数,不只保存赢家。
5. **门在实验前冻结**:`acceptance_thresholds` 在实验创建时确定,
   ``runner`` 仅判定 pass/fail,不能事后调整阈值。
6. **不触及交易红线**:本模块只消费历史数据 / 离线计算,
   不连接券商、不发订单、不修改持仓 / 风控。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ExperimentStatus(StrEnum):
    """实验生命周期状态(单向流)。"""

    HYPOTHESIS = "hypothesis"  # 假设已登记,尚未开始训练
    IN_SAMPLE = "in_sample"  # 正在训练集上做参数搜索
    VALIDATED_OOS = "validated_oos"  # 通过样本外验证(包含揭盲)
    REJECTED = "rejected"  # 假设被样本内 / 样本外证据拒绝
    SUPERSEDED = "superseded"  # 被新版本取代


class ValidationMode(StrEnum):
    """Walk-forward 切分模式。"""

    ROLLING = "rolling"  # 固定窗口大小向前滑动
    EXPANDING = "expanding"  # 训练集起点固定,终点随时间推进


class TrialStatus(StrEnum):
    """单次试验状态。"""

    CANDIDATE = "candidate"  # 已配置参数,待运行
    RUNNING = "running"
    SELECTED = "selected"  # 通过 IS,候选进入 OOS / 最终揭盲
    REJECTED = "rejected"  # 在 IS 或 OOS 被门拒绝
    FAILED = "failed"  # 异常 / 数据问题(失败也算试验)
    SKIPPED = "skipped"  # 因预算 / 配置问题被跳过


class WindowRole(StrEnum):
    """单窗口在 walk-forward 中的角色。"""

    TRAIN = "train"  # 训练集
    VALIDATION = "validation"  # 调参 / 早停验证集
    TEST = "test"  # 冻结测试集(揭盲)


@dataclass(frozen=True, slots=True)
class VersionStamp:
    """实验所基于的代码 / 数据 / 模型版本,用于复现与审计。

    核心模型 / 数据 / 选择字段必填 —— 任何版本漂移都会让"样本外通过"的
    结论失效。``user_code`` 的四个代码 artifact 字段在旧实验中可为空,
    新的代码晋级实验必须全部填写并由晋级门逐项绑定。
    """

    matching_model_version: str
    asset_rules_version: str
    factor_version: str | None
    dataset_versions: dict[str, str]
    selection_config: dict[str, object]
    strategy_kind: str
    # issue #219:user_code 晋级时把 OOS 实验绑定到精确的代码产物与 commit,
    # 防止把另一条策略/因子的 validated_oos 结论挪用给当前代码。
    code_artifact_id: str | None = None
    code_artifact_name: str | None = None
    code_kind: str | None = None
    code_commit: str | None = None

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        return d

    def checksum(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    """实验前冻结的统计门。

    未通过任一门 → REJECTED。已通过的候选才能进入最终揭盲。
    所有阈值的语义见 README §研究流程。
    """

    min_in_sample_sharpe: float = 1.0
    min_oos_sharpe: float = 0.5
    max_oos_drawdown: float = 0.25  # 绝对值,0.25 = -25%
    min_oos_calmar: float = 0.5
    min_oos_information_ratio: float = 0.0
    max_param_sensitivity_sharpe_drop: float = 0.5  # 邻域最差 Sharpe / 最优 Sharpe 的差
    min_pbo_pass: bool = True  # PBO < 0.5 才通过(可选)
    max_pbo: float = 0.5
    min_deflated_sharpe: float = 0.0
    min_probabilistic_sharpe: float = 0.95

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    """验证计划 —— 时间切分、滚动模式、试验预算。"""

    mode: ValidationMode
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    train_window_days: int = 504  # 默认 2 年交易日
    test_window_days: int = 63  # 默认季度
    step_days: int = 63
    trial_budget: int = 50
    random_seed: int = 0
    benchmark_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.train_start >= self.train_end:
            raise ValueError("train_start must be < train_end")
        if self.validation_start < self.train_end:
            raise ValueError("validation_start must be >= train_end (no look-ahead bleed)")
        if self.validation_end > self.test_start:
            raise ValueError(
                "validation_end must be <= test_start (test set frozen before validation)"
            )
        if self.test_start >= self.test_end:
            raise ValueError("test_start must be < test_end")
        if self.train_window_days <= 0:
            raise ValueError("train_window_days must be > 0")
        if self.test_window_days <= 0:
            raise ValueError("test_window_days must be > 0")
        if self.step_days <= 0:
            raise ValueError("step_days must be > 0")
        if self.trial_budget <= 0:
            raise ValueError("trial_budget must be > 0")

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["mode"] = self.mode.value
        d["train_start"] = self.train_start.isoformat()
        d["train_end"] = self.train_end.isoformat()
        d["validation_start"] = self.validation_start.isoformat()
        d["validation_end"] = self.validation_end.isoformat()
        d["test_start"] = self.test_start.isoformat()
        d["test_end"] = self.test_end.isoformat()
        return d


@dataclass(frozen=True, slots=True)
class RobustnessPlan:
    """稳健性压力测试计划。"""

    # 参数邻域分析:围绕最优参数的 ±step 扫描
    neighbourhood_steps: int = 5  # 每维度扫描点数
    neighbourhood_relative_step: float = 0.1  # 步长相对值(0.1 = ±10%)

    # 成本压力:测试 1x / 2x / 3x 佣金与印花税
    cost_multipliers: tuple[float, ...] = (1.0, 2.0, 3.0)

    # 滑点压力(bps)
    slippage_stress_bps: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0)

    # 延迟模拟(交易日数,1 = 下一 Bar,2 = 下两 Bar)
    execution_delay_bars: tuple[int, ...] = (1, 2)

    # 关键市场阶段(子区间)
    stress_phases: tuple[str, ...] = (
        "2018-Q4",  # 中美贸易战
        "2020-Q1",  # 新冠冲击
        "2022-Q1",  # 俄乌 + 加息
        "2024-Q1",  # 微盘股崩盘
    )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchExperiment:
    """研究实验根契约 —— 假设 + 验证计划 + 门。

    生命周期:
    * HYPOTHESIS → 创建后 status=HYPOTHESIS,假设已冻结。
    * IN_SAMPLE  → runner 进入训练阶段后切换。
    * VALIDATED_OOS → 通过最终揭盲。
    * REJECTED → 任一门未通过。
    * SUPERSEDED → 用户新建 experiment 取代。
    """

    experiment_id: str
    hypothesis: str
    version_stamp: VersionStamp
    plan: ValidationPlan
    thresholds: AcceptanceThresholds
    robustness: RobustnessPlan
    strategy_params_space: dict[str, object]  # 候选参数空间
    status: ExperimentStatus = ExperimentStatus.HYPOTHESIS
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    frozen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finalized_at: datetime | None = None
    trials_used: int = 0
    final_test_unsealed: bool = False
    rejection_reason: str | None = None
    supersedes_id: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.hypothesis.strip():
            raise ValueError("hypothesis must not be empty")
        if self.status == ExperimentStatus.VALIDATED_OOS and not self.final_test_unsealed:
            raise ValueError("VALIDATED_OOS requires final_test_unsealed=True (one-time unseal)")

    def can_run_trial(self) -> bool:
        """是否还能继续跑试验(预算未耗尽 + 状态合法)。"""
        if self.status in (
            ExperimentStatus.VALIDATED_OOS,
            ExperimentStatus.REJECTED,
            ExperimentStatus.SUPERSEDED,
        ):
            return False
        return self.trials_used < self.plan.trial_budget

    def can_unseal_final(self) -> bool:
        """是否允许揭盲(只在 IN_SAMPLE 后期,且只允许一次)。"""
        return self.status == ExperimentStatus.IN_SAMPLE and not self.final_test_unsealed

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis": self.hypothesis,
            "version_stamp": self.version_stamp.as_dict(),
            "version_checksum": self.version_stamp.checksum(),
            "plan": self.plan.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "robustness": self.robustness.as_dict(),
            "strategy_params_space": self.strategy_params_space,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            "trials_used": self.trials_used,
            "final_test_unsealed": self.final_test_unsealed,
            "rejection_reason": self.rejection_reason,
            "supersedes_id": self.supersedes_id,
            "notes": self.notes,
        }


def new_experiment(
    *,
    hypothesis: str,
    version_stamp: VersionStamp,
    plan: ValidationPlan,
    thresholds: AcceptanceThresholds,
    robustness: RobustnessPlan | None = None,
    strategy_params_space: dict[str, object] | None = None,
    supersedes_id: str | None = None,
    notes: str = "",
) -> ResearchExperiment:
    """工厂函数:生成新 experiment_id 并冻结假设。"""
    return ResearchExperiment(
        experiment_id=uuid4().hex[:16],
        hypothesis=hypothesis,
        version_stamp=version_stamp,
        plan=plan,
        thresholds=thresholds,
        robustness=robustness or RobustnessPlan(),
        strategy_params_space=strategy_params_space or {},
        supersedes_id=supersedes_id,
        notes=notes,
    )


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """单窗口绩效快照。"""

    role: WindowRole
    start: date
    end: date
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    information_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    monthly_win_rate: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["role"] = self.role.value
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d


@dataclass(frozen=True, slots=True)
class RobustnessProbe:
    """单次稳健性压力测试结果。"""

    probe_kind: str  # "neighbourhood" / "cost" / "slippage" / "delay" / "phase"
    label: str  # 人读标签,如 "param_window=15" / "cost_x2" / "2020-Q1"
    sharpe_ratio: float
    max_drawdown: float
    total_return: float
    passed: bool
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StatisticalReport:
    """多重试验修正与置信区间报告。"""

    deflated_sharpe_ratio: float
    probabilistic_sharpe_ratio: float
    pbo: float  # Probability of Backtest Overfitting
    bootstrap_sharpe_ci_low: float
    bootstrap_sharpe_ci_high: float
    bootstrap_mdd_ci_low: float
    bootstrap_mdd_ci_high: float
    n_trials: int
    methodology_notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """单次试验完整记录(赢家 + 输家)。

    重要:``status=FAILED`` / ``REJECTED`` 也必须入库 —— 多重试验修正需要
    真实试验总数,只保存赢家会让 Deflated Sharpe / PBO 严重失真。
    """

    trial_id: str
    experiment_id: str
    trial_index: int
    parameters: dict[str, object]
    status: TrialStatus
    in_sample_metrics: WindowMetrics | None = None
    oos_metrics: WindowMetrics | None = None
    walk_forward_windows: tuple[WindowMetrics, ...] = ()
    robustness_probes: tuple[RobustnessProbe, ...] = ()
    statistical_report: StatisticalReport | None = None
    failure_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "trial_index": self.trial_index,
            "parameters": self.parameters,
            "status": self.status.value,
            "in_sample_metrics": (
                self.in_sample_metrics.as_dict() if self.in_sample_metrics else None
            ),
            "oos_metrics": self.oos_metrics.as_dict() if self.oos_metrics else None,
            "walk_forward_windows": [w.as_dict() for w in self.walk_forward_windows],
            "robustness_probes": [p.as_dict() for p in self.robustness_probes],
            "statistical_report": (
                self.statistical_report.as_dict() if self.statistical_report else None
            ),
            "failure_reason": self.failure_reason,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass(frozen=True, slots=True)
class ExperimentVerdict:
    """实验最终裁决(由 runner 在揭盲后产出)。"""

    experiment_id: str
    status: ExperimentStatus
    reason: str
    best_trial_id: str | None = None
    unsealed_metrics: WindowMetrics | None = None
    final_statistical_report: StatisticalReport | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "status": self.status.value,
            "reason": self.reason,
            "best_trial_id": self.best_trial_id,
            "unsealed_metrics": (
                self.unsealed_metrics.as_dict() if self.unsealed_metrics else None
            ),
            "final_statistical_report": (
                self.final_statistical_report.as_dict() if self.final_statistical_report else None
            ),
        }


def transition_status(
    experiment: ResearchExperiment,
    new_status: ExperimentStatus,
    *,
    rejection_reason: str | None = None,
) -> ResearchExperiment:
    """状态机转换(纯函数,返回新 immutable 实例)。

    合法转换:
    * HYPOTHESIS → IN_SAMPLE
    * IN_SAMPLE → VALIDATED_OOS(需 final_test_unsealed=True)
    * IN_SAMPLE → REJECTED
    * 任意 → SUPERSEDED

    任何其他转换 raise ValueError —— 防止跳过门 / 重复揭盲 / 回退状态。
    """
    cur = experiment.status
    if cur == new_status:
        return experiment

    allowed = {
        (ExperimentStatus.HYPOTHESIS, ExperimentStatus.IN_SAMPLE),
        (ExperimentStatus.IN_SAMPLE, ExperimentStatus.VALIDATED_OOS),
        (ExperimentStatus.IN_SAMPLE, ExperimentStatus.REJECTED),
        (ExperimentStatus.HYPOTHESIS, ExperimentStatus.REJECTED),
        (ExperimentStatus.HYPOTHESIS, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.IN_SAMPLE, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.VALIDATED_OOS, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.REJECTED, ExperimentStatus.SUPERSEDED),
    }
    if (cur, new_status) not in allowed:
        raise ValueError(f"illegal status transition: {cur.value} -> {new_status.value}")

    if new_status == ExperimentStatus.VALIDATED_OOS and not experiment.final_test_unsealed:
        raise ValueError("cannot transition to VALIDATED_OOS without one-time final test unseal")

    finalized_at = (
        datetime.now(UTC)
        if new_status in (ExperimentStatus.VALIDATED_OOS, ExperimentStatus.REJECTED)
        else experiment.finalized_at
    )
    return replace(
        experiment,
        status=new_status,
        rejection_reason=rejection_reason if new_status == ExperimentStatus.REJECTED else None,
        finalized_at=finalized_at,
    )


def mark_final_test_unsealed(experiment: ResearchExperiment) -> ResearchExperiment:
    """一次性揭盲最终测试集(不可逆)。

    调用方必须确保:
    1. ``experiment.status == IN_SAMPLE``;
    2. 已选定最优 trial;
    3. 之前从未调用过此函数(``final_test_unsealed`` 为 False)。
    """
    if experiment.final_test_unsealed:
        raise ValueError("final test already unsealed — re-unsealing is forbidden")
    if not experiment.can_unseal_final():
        raise ValueError(f"cannot unseal final test in status={experiment.status.value}")
    return replace(experiment, final_test_unsealed=True)


def increment_trials_used(experiment: ResearchExperiment, by: int = 1) -> ResearchExperiment:
    """增加试验计数(单调递增,超出预算 raise)。"""
    if by <= 0:
        raise ValueError("by must be > 0")
    new_count = experiment.trials_used + by
    if new_count > experiment.plan.trial_budget:
        raise ValueError(f"trial budget exhausted: {new_count} > {experiment.plan.trial_budget}")
    return replace(experiment, trials_used=new_count)


def deserialize_experiment(data: dict[str, Any]) -> ResearchExperiment:
    """从持久化字典重建 ResearchExperiment(用于 repo / API)。

    严格还原所有字段,缺失字段 raise KeyError —— 防止通过部分数据绕过门。
    """
    vs_raw = data["version_stamp"]
    plan_raw = data["plan"]
    thr_raw = data["thresholds"]
    rob_raw = data["robustness"]

    version_stamp = VersionStamp(
        matching_model_version=vs_raw["matching_model_version"],
        asset_rules_version=vs_raw["asset_rules_version"],
        factor_version=vs_raw.get("factor_version"),
        dataset_versions=dict(vs_raw.get("dataset_versions", {})),
        selection_config=dict(vs_raw.get("selection_config", {})),
        strategy_kind=vs_raw["strategy_kind"],
        code_artifact_id=vs_raw.get("code_artifact_id"),
        code_artifact_name=vs_raw.get("code_artifact_name"),
        code_kind=vs_raw.get("code_kind"),
        code_commit=vs_raw.get("code_commit"),
    )
    plan = ValidationPlan(
        mode=ValidationMode(plan_raw["mode"]),
        train_start=date.fromisoformat(plan_raw["train_start"]),
        train_end=date.fromisoformat(plan_raw["train_end"]),
        validation_start=date.fromisoformat(plan_raw["validation_start"]),
        validation_end=date.fromisoformat(plan_raw["validation_end"]),
        test_start=date.fromisoformat(plan_raw["test_start"]),
        test_end=date.fromisoformat(plan_raw["test_end"]),
        train_window_days=plan_raw.get("train_window_days", 504),
        test_window_days=plan_raw.get("test_window_days", 63),
        step_days=plan_raw.get("step_days", 63),
        trial_budget=plan_raw.get("trial_budget", 50),
        random_seed=plan_raw.get("random_seed", 0),
        benchmark_symbol=plan_raw.get("benchmark_symbol"),
    )
    thresholds = AcceptanceThresholds(**thr_raw)
    robustness = RobustnessPlan(**rob_raw)

    return ResearchExperiment(
        experiment_id=data["experiment_id"],
        hypothesis=data["hypothesis"],
        version_stamp=version_stamp,
        plan=plan,
        thresholds=thresholds,
        robustness=robustness,
        strategy_params_space=dict(data.get("strategy_params_space", {})),
        status=ExperimentStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        frozen_at=datetime.fromisoformat(data["frozen_at"]),
        finalized_at=(
            datetime.fromisoformat(data["finalized_at"]) if data.get("finalized_at") else None
        ),
        trials_used=int(data.get("trials_used", 0)),
        final_test_unsealed=bool(data.get("final_test_unsealed", False)),
        rejection_reason=data.get("rejection_reason"),
        supersedes_id=data.get("supersedes_id"),
        notes=data.get("notes", ""),
    )
