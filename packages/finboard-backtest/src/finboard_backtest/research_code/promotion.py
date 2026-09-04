"""研究代码晋级门(issue #219)。

研究代码的 ``status`` 只描述登记簿生命周期(draft/active/retired),不能把
它当成“已经验证”的证明。本模块把两个机器门集中成一个纯函数:

* screen:IC、换手率、与既有因子的相关性上限;
* OOS:#57 ``ResearchExperiment`` 已完成最终样本外揭盲。

MCP/API/模拟盘只应消费 ``is_promoted_artifact`` 判定为真的版本。这里不
连接数据库、不执行用户代码,也不触及实盘订单、成交、持仓或风控。

issue #311:相关性检查 fail-closed —— ``correlation`` 缺失或空 mapping
不再零迭代静默 pass,按证据的 ``correlation_baselines`` 分流具名 failure;
screen 逐项检查状态(含 not_evaluated)经 ``screen_checks`` 进 evidence。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROMOTION_PENDING = "pending"
PROMOTION_PASSED = "passed"
PROMOTION_FAILED = "failed"

#: screen 逐项检查状态(issue #311,随 evidence 归档供审计检索)。
CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_NOT_EVALUATED = "not_evaluated"

#: 横截面确无 baseline 因子可比时的修复路径(拼进具名 failure,可操作)。
_CORRELATION_NOT_EVALUATED_FIX = (
    "修复:screen run 横截面需含至少一个非用户因子特征"
    "(builtin 因子或价格特征)作为相关性 baseline,重跑 screen 后再晋级"
)


@dataclass(frozen=True, slots=True)
class PromotionScreenThresholds:
    """晋级 screen 的冻结阈值。

阈值是展示/治理层的保守默认值,可由调用方在系统配置层冻结;单次
 promote 的 evidence 会完整保存实际使用的值,不能用更宽松的阈值覆盖
    已有证据。
    """

    min_abs_rank_ic: float = 0.02
    max_average_turnover: float = 0.80
    max_abs_correlation: float = 0.80
    min_periods: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("min_abs_rank_ic", self.min_abs_rank_ic),
            ("max_average_turnover", self.max_average_turnover),
            ("max_abs_correlation", self.max_abs_correlation),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数")
        if self.min_abs_rank_ic < 0:
            raise ValueError("min_abs_rank_ic 不能为负")
        if not 0 <= self.max_average_turnover <= 1:
            raise ValueError("max_average_turnover 必须位于 [0,1]")
        if not 0 <= self.max_abs_correlation <= 1:
            raise ValueError("max_abs_correlation 必须位于 [0,1]")
        if self.min_periods < 1:
            raise ValueError("min_periods 必须 >= 1")

    def as_dict(self) -> dict[str, object]:
        return {
            "min_abs_rank_ic": self.min_abs_rank_ic,
            "max_average_turnover": self.max_average_turnover,
            "max_abs_correlation": self.max_abs_correlation,
            "min_periods": self.min_periods,
        }


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    """两道晋级门的可审计结果。"""

    screen_passed: bool
    validation_passed: bool
    failures: tuple[str, ...]
    screen_metrics: dict[str, Any] | None
    validation_summary: dict[str, Any]
    thresholds: PromotionScreenThresholds
    #: screen 逐项检查状态(issue #311):检查名 -> pass|fail|not_evaluated。
    #: correlation 在横截面无 baseline 因子可比时记 not_evaluated 而非缺席。
    screen_checks: dict[str, str]

    @property
    def passed(self) -> bool:
        return self.screen_passed and self.validation_passed and not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "screen_passed": self.screen_passed,
            "validation_passed": self.validation_passed,
            "failures": list(self.failures),
            "screen_checks": dict(self.screen_checks),
            "screen": self.screen_metrics,
            "validation": self.validation_summary,
            "thresholds": self.thresholds.as_dict(),
        }


class PromotionGateError(ValueError):
    """机器晋级门未通过。"""


def evaluate_promotion_gates(
    *,
    screen: Mapping[str, object] | None,
    validation: Mapping[str, object] | object | None,
    thresholds: PromotionScreenThresholds | None = None,
    artifact_id: str | None = None,
    artifact_kind: str | None = None,
    artifact_name: str | None = None,
    artifact_commit: str | None = None,
    require_artifact_binding: bool = False,
) -> PromotionGateResult:
    """评估 screen + OOS 两道门,永不因缺证据而默认通过。

    ``screen`` 可以是 ``factor_screen`` / ``strategy_screen`` 段,也可以是
    其中的单个 metrics 对象。``validation`` 接受 #57 实验的 ``as_dict``
    结果或同形状 mapping。MCP 正式晋级会打开 ``require_artifact_binding``,
    要求 OOS 实验的 version_stamp 绑定同一个 artifact 与 commit。

    issue #311:screen 逐项检查状态(n_periods/rank_ic/average_turnover/
    correlation -> pass|fail|not_evaluated)随结果返回;correlation 缺失
    或空 mapping 按 ``correlation_baselines`` 分流具名 failure,不静默 pass。
    """
    resolved = thresholds or PromotionScreenThresholds()
    screen_metrics, screen_failures, screen_checks = _screen_metrics_and_failures(
        screen, thresholds=resolved, artifact_name=artifact_name
    )
    validation_data = _object_as_mapping(validation)
    validation_summary, validation_failures = _validation_summary_and_failures(
        validation_data,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        artifact_name=artifact_name,
        artifact_commit=artifact_commit,
        require_artifact_binding=require_artifact_binding,
    )
    failures = tuple(dict.fromkeys((*screen_failures, *validation_failures)))
    return PromotionGateResult(
        screen_passed=not screen_failures,
        validation_passed=not validation_failures,
        failures=failures,
        screen_metrics=screen_metrics,
        validation_summary=validation_summary,
        thresholds=resolved,
        screen_checks=screen_checks,
    )


def require_promotion_gates(**kwargs: Any) -> PromotionGateResult:
    """评估并在未通过时抛出带具体门名的错误。"""
    result = evaluate_promotion_gates(**kwargs)
    if not result.passed:
        raise PromotionGateError("研究代码产物未通过晋级门: " + ", ".join(result.failures))
    return result


def is_promoted_artifact(artifact: object | None) -> bool:
    """判断 artifact 是否可以进入正式 composite/ResearchRun/模拟盘。

    旧 mock/旧数据库对象没有 ``promotion_status`` 时,只有 active 才按
    legacy 兼容路径视为 passed;draft/retired 永远不能放行。
    """
    if artifact is None or getattr(artifact, "status", None) != "active":
        return False
    raw = getattr(artifact, "promotion_status", None)
    if raw is None:
        return True
    return bool(raw == PROMOTION_PASSED)


def promotion_status(artifact: object | None) -> str:
    """读取 artifact 晋级状态并对旧对象提供安全兼容回退。"""
    if artifact is None:
        return PROMOTION_PENDING
    raw = getattr(artifact, "promotion_status", None)
    if raw in {PROMOTION_PENDING, PROMOTION_PASSED, PROMOTION_FAILED}:
        return str(raw)
    return PROMOTION_PASSED if getattr(artifact, "status", None) == "active" else PROMOTION_PENDING


def _screen_metrics_and_failures(
    screen: Mapping[str, object] | None,
    *,
    thresholds: PromotionScreenThresholds,
    artifact_name: str | None,
) -> tuple[dict[str, Any] | None, list[str], dict[str, str]]:
    if screen is None:
        return None, ["screen_missing"], {}
    root = dict(screen)
    metrics = _select_screen_metrics(root, artifact_name=artifact_name)
    if metrics is None:
        return None, ["screen_metrics_missing"], {}

    failures: list[str] = []
    checks: dict[str, str] = {}

    def _record(check: str, failure: str | None) -> None:
        if failure is None:
            checks[check] = CHECK_PASS
        else:
            failures.append(failure)
            checks[check] = CHECK_FAIL

    periods = _finite_number(metrics.get("n_periods", root.get("n_periods")))
    if periods is None:
        _record("n_periods", "screen.n_periods_missing")
    elif periods < thresholds.min_periods:
        _record(
            "n_periods",
            f"screen.n_periods_below_minimum({periods:g}<{thresholds.min_periods})",
        )
    else:
        _record("n_periods", None)

    rank_ic = _finite_number(metrics.get("rank_ic", metrics.get("ic")))
    if rank_ic is None:
        _record("rank_ic", "screen.rank_ic_missing")
    elif abs(rank_ic) < thresholds.min_abs_rank_ic:
        _record(
            "rank_ic",
            f"screen.rank_ic_below_minimum(abs={abs(rank_ic):g}<{thresholds.min_abs_rank_ic:g})",
        )
    else:
        _record("rank_ic", None)

    turnover = _finite_number(metrics.get("average_turnover", metrics.get("turnover")))
    if turnover is None:
        _record("average_turnover", "screen.average_turnover_missing")
    elif turnover < 0 or turnover > thresholds.max_average_turnover:
        _record(
            "average_turnover",
            f"screen.turnover_above_maximum({turnover:g}>{thresholds.max_average_turnover:g})",
        )
    else:
        _record("average_turnover", None)

    correlations = metrics.get("correlation", metrics.get("correlations"))
    if not isinstance(correlations, Mapping) or not correlations:
        # issue #311:``{}`` 与缺失同罪 —— 空 mapping 零迭代曾零 failure 静默
        # pass,违背本模块「永不因缺证据而默认通过」。按证据的
        # ``correlation_baselines`` 分流:显式 [](横截面确无 baseline 因子
        # 可比,factor_screen/strategy_screen 的如实记录)记 not_evaluated 并
        # 附修复路径;缺失 / 形状异常 / 声明过 baseline 一律按证据缺失兜底,
        # 不虚构 not_evaluated。两条路径都不得 pass。
        if _declares_empty_baselines(root):
            failures.append(
                f"screen.correlation_not_evaluated({_CORRELATION_NOT_EVALUATED_FIX})"
            )
            checks["correlation"] = CHECK_NOT_EVALUATED
        else:
            failures.append("screen.correlation_missing")
            checks["correlation"] = CHECK_FAIL
    else:
        breached = False
        for baseline, raw_value in sorted(correlations.items(), key=lambda item: str(item[0])):
            value = _finite_number(raw_value)
            if value is None:
                failures.append(f"screen.correlation_invalid({baseline})")
                breached = True
            elif abs(value) > thresholds.max_abs_correlation:
                failures.append(
                    f"screen.correlation_above_maximum({baseline}:abs={abs(value):g}"
                    f">{thresholds.max_abs_correlation:g})"
                )
                breached = True
        checks["correlation"] = CHECK_FAIL if breached else CHECK_PASS
    return metrics, failures, checks


def _declares_empty_baselines(root: Mapping[str, object]) -> bool:
    """screen 证据是否显式声明横截面无相关性 baseline(``correlation_baselines=[]``)。

    只有显式空列表才判定「确无 baseline 可比」(factor_screen /
    strategy_screen 对横截面无对照特征的如实记录,#217);缺失或形状
    异常的证据一律按 ``screen.correlation_missing`` 兜底,不虚构
    not_evaluated。
    """
    baselines = root.get("correlation_baselines")
    return isinstance(baselines, (list, tuple)) and len(baselines) == 0


def _select_screen_metrics(
    root: Mapping[str, object], *, artifact_name: str | None
) -> dict[str, Any] | None:
    """从 factor_screen/strategy_screen 外壳取出目标 artifact 的 metrics。"""
    factors = root.get("factors")
    if isinstance(factors, Mapping):
        candidates = [artifact_name or ""]
        if artifact_name:
            candidates.append(f"u_{artifact_name}")
        for key in candidates:
            value = factors.get(key)
            if isinstance(value, Mapping):
                return dict(value)
        if len(factors) == 1:
            value = next(iter(factors.values()))
            return dict(value) if isinstance(value, Mapping) else None
        return None
    strategy = root.get("strategy")
    if isinstance(strategy, Mapping):
        return dict(strategy)
    # 已经是单因子/单策略指标对象。
    if any(key in root for key in ("rank_ic", "ic", "average_turnover", "turnover")):
        return dict(root)
    return None


def _validation_summary_and_failures(
    validation: Mapping[str, object] | None,
    *,
    artifact_id: str | None,
    artifact_kind: str | None,
    artifact_name: str | None,
    artifact_commit: str | None,
    require_artifact_binding: bool,
) -> tuple[dict[str, Any], list[str]]:
    if validation is None:
        return {"status": None, "final_test_unsealed": False}, ["validation_missing"]
    status = _enum_value(validation.get("status"))
    unsealed = validation.get("final_test_unsealed") is True
    failures: list[str] = []
    if status != "validated_oos":
        failures.append(f"validation.status_not_validated_oos({status or 'missing'})")
    if not unsealed:
        failures.append("validation.final_test_not_unsealed")

    version_stamp = validation.get("version_stamp")
    stamp = dict(version_stamp) if isinstance(version_stamp, Mapping) else {}
    bound_id = _first_text(stamp, "code_artifact_id", "artifact_id")
    bound_commit = _first_text(stamp, "code_commit", "commit")
    if require_artifact_binding:
        if not artifact_id:
            failures.append("validation.artifact_id_context_missing")
        elif bound_id != artifact_id:
            failures.append(
                f"validation.artifact_id_mismatch({bound_id or 'missing'}!={artifact_id})"
            )
        if not artifact_commit:
            failures.append("validation.code_commit_context_missing")
        elif bound_commit != artifact_commit:
            failures.append(
                f"validation.code_commit_mismatch({bound_commit or 'missing'}!={artifact_commit[:12]})"
            )
        expected_kind = "factor" if artifact_kind == "factor" else "user_code"
        stamp_kind = _first_text(stamp, "code_kind", "strategy_kind")
        if stamp_kind not in {expected_kind, artifact_kind}:
            failures.append(
                f"validation.code_kind_mismatch({stamp_kind or 'missing'}!={expected_kind})"
            )
        if artifact_name and not _first_text(stamp, "code_artifact_name", "artifact_name"):
            failures.append("validation.code_artifact_name_missing")
        elif artifact_name:
            bound_name = _first_text(stamp, "code_artifact_name", "artifact_name")
            if bound_name != artifact_name:
                failures.append(
                    f"validation.code_artifact_name_mismatch({bound_name}!={artifact_name})"
                )

    summary: dict[str, Any] = {
        "experiment_id": validation.get("experiment_id"),
        "status": status,
        "final_test_unsealed": unsealed,
        # issue #310:调用方在 validation mapping 上附带的派生结论语义
        # (supported|not_supported|inconclusive)透传进证据摘要——
        # validated_oos 只代表 OOS 流程完成,不代表假设获支持;缺失为 None
        # (旧调用方不携带),不参与门判定。
        "oos_outcome": _enum_value(validation.get("oos_outcome")),
        "finalized_at": validation.get("finalized_at"),
        "version_checksum": validation.get("version_checksum"),
    }
    if stamp:
        summary["version_stamp"] = {
            key: stamp[key]
            for key in (
                "code_artifact_id",
                "code_artifact_name",
                "code_kind",
                "code_commit",
            )
            if key in stamp
        }
    return summary, failures


def _object_as_mapping(value: Mapping[str, object] | object | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        candidate = as_dict()
        return candidate if isinstance(candidate, Mapping) else None
    return None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw)


def _first_text(mapping: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "CHECK_FAIL",
    "CHECK_NOT_EVALUATED",
    "CHECK_PASS",
    "PROMOTION_FAILED",
    "PROMOTION_PASSED",
    "PROMOTION_PENDING",
    "PromotionGateError",
    "PromotionGateResult",
    "PromotionScreenThresholds",
    "evaluate_promotion_gates",
    "is_promoted_artifact",
    "promotion_status",
    "require_promotion_gates",
]
