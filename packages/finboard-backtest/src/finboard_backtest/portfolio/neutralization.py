"""风险因子中性化约束投影 —— active 暴露 ≤ 阈值的只减仓投影(issue #266)。

约束语义:对声明的每个风险因子 ``f`` 与阈值 ``t``,
``|Σ_i (w_i - baseline_i) · f_i| <= t``。``baseline`` 为基准权重
(builder 现有语义中基准 = 现金,即默认零向量;可显式传入以支持
基准相对语义),故该约束即 **active 暴露** 上限。

投影算法(与 ``enforce_risk_contribution_cap`` 同一「只减仓」哲学):
每轮对每个超限因子,把与违规方向同号的持仓权重按**闭式解**比例一次性缩放
(暴露对同号持仓缩放比例是线性的,一步精确到上限;不重新分配到其他标的,
释放的权重转为现金),随后多因子交替迭代至全部满足。任何一步都不会增大任何
权重,权重序列单调不增有下界,迭代必然在有限步内收敛;理论上不可行
(如基准权重主导暴露、减权无法进展)时不收敛并**失败关闭**,不静默放行。
active 暴露的度量域是「持仓 与 基准的并集」—— 基准独有标的贡献
``(0 - b_i)·f_i`` 常数项,排除会低估暴露;只减仓保证它们永远不会被缩放。

因子暴露观测缺失时的降级(issue #266,对齐 #213 风格):缺失任一**持仓**
标的的暴露观测即跳过该因子约束,产出具名 warning
``factor_neutralization_inactive:<factor>``,不静默失效;约束本体不可满足
才是 fail-closed 错误。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    RiskFactorLimit,
)

NEUTRALIZATION_CONSTRAINT = "risk_factor_neutralization"
"""builder 审计行中「已执行的中性化投影」约束名(硬约束)。"""

NEUTRALIZATION_SKIPPED_CONSTRAINT = "risk_factor_neutralization_skipped"
"""builder 审计行中「因暴露观测缺失而跳过」约束名(软约束,不阻断 run)。"""

NEUTRALIZATION_INACTIVE_WARNING = "factor_neutralization_inactive"
"""因子暴露观测缺失、约束未生效的具名 warning 前缀(#213 风格)。"""

DEFAULT_MAX_ITERATIONS = 100
"""多因子交替投影的最大轮数;每轮只减权,单调有界必收敛,超限即不可行。"""


@dataclass(frozen=True, slots=True)
class FactorNeutralizationAudit:
    """单因子中性化投影的逐项审计结果。

    ``enforced=False`` 表示该因子因暴露观测缺失被跳过(降级),
    此时 ``warning`` 携带具名 warning,``before/after_exposure`` 为 None
    (未测量,不得伪装成已满足)。
    """

    factor: str
    limit: float
    before_exposure: float | None
    after_exposure: float | None
    iterations: int
    enforced: bool
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class FactorNeutralizationResult:
    """风险因子中性化投影的整体结果。

    ``converged=False`` 表示存在已声明但无法满足的因子约束(不可行或
    无进展),调用方必须失败关闭。
    """

    weights: dict[str, float]
    audits: tuple[FactorNeutralizationAudit, ...]
    iterations: int
    converged: bool
    changed: bool


def _active_exposure(
    weights: npt.NDArray[np.float64],
    baseline: npt.NDArray[np.float64],
    factor: npt.NDArray[np.float64],
) -> float:
    return float(((weights - baseline) * factor).sum())


def _scale_for_limit(
    weights: npt.NDArray[np.float64],
    baseline: npt.NDArray[np.float64],
    factor: npt.NDArray[np.float64],
    *,
    limit: float,
    tolerance: float,
) -> tuple[float, npt.NDArray[np.bool_], bool]:
    """计算把 |active 暴露| 压到 ``limit`` 内所需的同号持仓缩放比例。

    暴露对「同号 offender 持仓统一乘 s」是线性关系
    ``e(s)·dir = e(1)·dir + (s-1)·A``(其中 ``A = Σ_offenders w·signed``),
    闭式解 ``s = 1 + (limit·dir - e(1)·dir) / A`` 后夹紧到 [0, 1]。返回
    ``(scale, actionable, progressed)``;``actionable`` 是必须缩放的持仓
    掩码 —— **只缩 offender**,与非同号持仓(含对冲方向持仓)无关,
    缩放闭式解与掩码必须配套使用,全向量统一缩放会破坏该线性关系。
    ``progressed=False`` 表示该因子在当前权重向量下无法通过减权改善
    (理论上不可行),调用方据此失败关闭。
    """
    active = weights - baseline
    exposure = float((active * factor).sum())
    if abs(exposure) <= limit + tolerance:
        return 1.0, np.zeros(weights.shape, dtype=bool), True
    # 负向超限等价于镜像因子后的正向超限,统一处理。
    direction = 1.0 if exposure > 0 else -1.0
    signed = factor * direction
    offenders = (active * signed) > tolerance
    held = np.abs(weights) > MAX_WEIGHT_EPSILON
    actionable = offenders & held
    a = float((weights[actionable] * signed[actionable]).sum())
    if a <= tolerance:
        return 1.0, actionable, False
    scale = 1.0 + (limit - exposure * direction) / a
    if not math.isfinite(scale):
        return 1.0, actionable, False
    return float(min(1.0, max(0.0, scale))), actionable, True


def project_risk_factor_neutralization(
    weights: dict[str, float],
    limits: tuple[RiskFactorLimit, ...],
    factor_exposures: dict[str, dict[str, float]],
    *,
    baseline_weights: dict[str, float] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    tolerance: float = 1e-9,
) -> FactorNeutralizationResult:
    """把 active 风险因子暴露投影到声明上限内(只减仓,释放权重转现金)。

    参数:
        weights: 当前目标权重(通常已过硬约束投影;只减仓投影保证
            单资产 / sleeve / gross 上限与最小现金不会被破坏)。
        limits: 声明的因子暴露上限(builder 从 ``PortfolioConstraints``
            透传;空 tuple 直接原样返回)。
        factor_exposures: {因子名: {标的: 暴露观测}},来自冻结特征;
            None 或非有限值视同缺失。
        baseline_weights: 基准权重;None / 空 = 现金基准(active = w)。
        max_iterations: 多因子交替投影最大轮数。
        tolerance: 暴露满足判定与「无进展」判定的数值容忍。

    返回:
        :class:`FactorNeutralizationResult`;``converged=False`` 时调用方
        必须失败关闭,不得静默采用投影中间结果。
    """
    if max_iterations <= 0:
        raise ValueError("max_iterations 必须为正")
    if tolerance <= 0:
        raise ValueError("tolerance 必须为正")

    if not limits or not weights:
        return FactorNeutralizationResult(
            weights=dict(weights),
            audits=(),
            iterations=0,
            converged=True,
            changed=False,
        )

    # 计算域取「持仓 与 基准的并集」:基准独有标的贡献 (0 - b_i)·f_i 的常数项,
    # 排除会低估 active 暴露;它们不持仓、永远不会被缩放(只减仓),
    # 但缺暴露观测时同样使约束不可测量 → 跳过并具名 warning。
    baseline_map = baseline_weights or {}
    symbols = sorted(set(weights) | set(baseline_map))
    vector = np.array([float(weights.get(s, 0.0)) for s in symbols], dtype=np.float64)
    baseline_vector = np.array(
        [float(baseline_map.get(s, 0.0)) for s in symbols],
        dtype=np.float64,
    )

    skipped_audits: list[FactorNeutralizationAudit] = []
    enforced: list[tuple[RiskFactorLimit, npt.NDArray[np.float64], float, int]] = []
    for limit in sorted(limits, key=lambda item: item.factor):
        observations = factor_exposures.get(limit.factor, {})
        missing = [
            symbol
            for symbol, weight, baseline_weight in zip(
                symbols, vector.tolist(), baseline_vector.tolist(), strict=True
            )
            if (abs(weight) > MAX_WEIGHT_EPSILON or abs(baseline_weight) > MAX_WEIGHT_EPSILON)
            and not _finite_observation(observations.get(symbol))
        ]
        if missing:
            preview = ",".join(missing[:5]) + ("…" if len(missing) > 5 else "")
            skipped_audits.append(
                FactorNeutralizationAudit(
                    factor=limit.factor,
                    limit=limit.max_active_exposure,
                    before_exposure=None,
                    after_exposure=None,
                    iterations=0,
                    enforced=False,
                    warning=(
                        f"{NEUTRALIZATION_INACTIVE_WARNING}:{limit.factor}:"
                        f" {len(missing)}/{len(symbols)} 个持仓标的缺失暴露观测"
                        f" ({preview}),约束跳过不静默失效"
                    ),
                )
            )
            continue
        factor_vector = np.array(
            [float(observations[symbol]) for symbol in symbols], dtype=np.float64
        )
        before = _active_exposure(vector, baseline_vector, factor_vector)
        enforced.append((limit, factor_vector, before, 0))

    changed = False
    iterations_used = 0
    all_within = not enforced
    for iteration in range(1, max_iterations + 1):
        progress = False
        all_within = True
        for index, (limit, factor_vector, _, _) in enumerate(enforced):
            scale, actionable, progressed = _scale_for_limit(
                vector,
                baseline_vector,
                factor_vector,
                limit=limit.max_active_exposure,
                tolerance=tolerance,
            )
            if not progressed:
                # 减权无法改善(如基准权重主导暴露),标记不可满足。
                all_within = False
                continue
            if scale < 1.0 - tolerance:
                # 只缩 actionable 同号 offender(与闭式解的线性化假设一致);
                # 非同号/零仓位保持不变,释放的权重转为现金。
                vector = np.where(actionable, vector * scale, vector)
                progress = True
                changed = True
                iterations_used = iteration
                enforced[index] = (
                    limit,
                    factor_vector,
                    enforced[index][2],
                    enforced[index][3] + 1,
                )
            if (
                abs(_active_exposure(vector, baseline_vector, factor_vector))
                > limit.max_active_exposure + tolerance
            ):
                all_within = False
        if all_within:
            iterations_used = iteration - 1 if not progress else iterations_used
            break
        if not progress:
            # 整轮无进展且仍有因子超限:理论上不可行,失败关闭。
            break

    enforced_audits = [
        FactorNeutralizationAudit(
            factor=limit.factor,
            limit=limit.max_active_exposure,
            before_exposure=before,
            after_exposure=_active_exposure(vector, baseline_vector, factor_vector),
            iterations=used,
            enforced=True,
        )
        for limit, factor_vector, before, used in enforced
    ]
    # 输出保持与输入 weights 相同的键集:基准独有标的只参与暴露度量,
    # 不应伪装成 0 权重目标仓位混进下游 TargetWeight。
    projected = dict(zip(symbols, vector.tolist(), strict=True))
    return FactorNeutralizationResult(
        weights={symbol: float(projected[symbol]) for symbol in weights},
        audits=tuple(sorted(enforced_audits + skipped_audits, key=lambda item: item.factor)),
        iterations=iterations_used,
        converged=all_within,
        changed=changed,
    )


def neutralization_audit_rows(
    result: FactorNeutralizationResult,
) -> list[tuple[str, str, float, float, float | None, bool, str]]:
    """把投影结果转成 builder 的 ``ConstraintAdjustment`` 字段元组。

    返回 ``(constraint, symbol, before, after, limit, passed, reason)``,
    供 ``build_portfolio`` 逐项落审计;skipped 因子 ``passed=False`` 且
    reason 携带具名 warning(硬/软由约束名决定,见 builder)。
    """
    rows: list[tuple[str, str, float, float, float | None, bool, str]] = []
    for audit in result.audits:
        if not audit.enforced:
            assert audit.warning is not None
            rows.append(
                (
                    NEUTRALIZATION_SKIPPED_CONSTRAINT,
                    audit.factor,
                    0.0,
                    0.0,
                    audit.limit,
                    False,
                    audit.warning,
                )
            )
            continue
        before = abs(float(audit.before_exposure or 0.0))
        after = abs(float(audit.after_exposure or 0.0))
        passed = (
            result.converged
            and audit.after_exposure is not None
            and after <= audit.limit + 1e-9
        )
        rows.append(
            (
                NEUTRALIZATION_CONSTRAINT,
                audit.factor,
                before,
                after,
                audit.limit,
                passed,
                (
                    "风险因子 active 暴露只减仓投影;"
                    f"factor_iterations={audit.iterations},"
                    f"limit={audit.limit:.6f}"
                ),
            )
        )
    return rows


def _finite_observation(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "NEUTRALIZATION_CONSTRAINT",
    "NEUTRALIZATION_INACTIVE_WARNING",
    "NEUTRALIZATION_SKIPPED_CONSTRAINT",
    "FactorNeutralizationAudit",
    "FactorNeutralizationResult",
    "neutralization_audit_rows",
    "project_risk_factor_neutralization",
]
