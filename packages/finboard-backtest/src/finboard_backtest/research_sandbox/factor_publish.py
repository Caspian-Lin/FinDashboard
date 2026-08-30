"""沙箱因子输出落库为 FeatureSnapshot 兼容观测(issue #217)。

执行器成功路径的后续处理:

1. **输出质量门** —— NaN 比例 / 覆盖率 / 长度校验,阈值来自 settings
   (``research_sandbox_max_nan_ratio`` / ``research_sandbox_min_coverage``)。
   不合格拒绝入库,run 置 failed(``quality_gate_failed``),错误信息
   指明阈值与实际值(可操作);
2. **快照构造** —— scores 的有限值观测化为 ``FeatureSnapshot``
   (``dataset_release_id=None`` + ``source_run_id`` 锚定 run,
   ``dataset_release_checksum`` 承载挂载 manifest checksum,数据面
   PIT 锚点与 #216 物理隔离闭环)。

观测语义:``observed_at = available_at = decision_at`` —— 快照层面统一
假设「该因子值在决策时点可得」,与发布快照口径一致;真实计算发生在
之后的沙箱容器里,这属于离线研究的既定语义(冻结重放)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from finboard_data.factor_lab import (
    FeatureObservation,
    FeatureSnapshot,
    build_feature_snapshot,
    sandbox_factor_name,
)

#: 质量门失败错误码(不可重试;修因子代码后重新提交 run)
QUALITY_GATE_FAILED = "quality_gate_failed"

_SOURCE = "research_code_run"


class QualityGateError(Exception):
    """输出质量门未通过;message 指明阈值与实际值。"""


@dataclass(frozen=True)
class QualityGateReport:
    """质量门评估结果(无论通过与否都可序列化进 run metrics)。"""

    passed: bool
    n_scored: int
    n_finite: int
    universe_size: int
    nan_ratio: float
    coverage: float
    max_nan_ratio: float
    min_coverage: float
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "n_scored": self.n_scored,
            "n_finite": self.n_finite,
            "universe_size": self.universe_size,
            "nan_ratio": self.nan_ratio,
            "coverage": self.coverage,
            "thresholds": {
                "max_nan_ratio": self.max_nan_ratio,
                "min_coverage": self.min_coverage,
            },
            "failures": list(self.failures),
        }


def check_output_quality(
    scores: Mapping[str, float],
    *,
    universe_size: int,
    max_nan_ratio: float,
    min_coverage: float,
) -> QualityGateReport:
    """NaN 比例 / 覆盖率 / 长度校验(纯函数,便于单测)。

    * NaN 比例 = 非有限值(含 NaN/inf)占输出标的数;超上限拒绝;
    * 覆盖率 = 有限值标的数 / 挂载 universe 大小;低于下限拒绝;
    * 长度校验 = 输出非空且不超出 universe(harness 已挡候选池外
      symbol,这里再防一层执行器侧回归)。
    """
    n_scored = len(scores)
    n_finite = sum(1 for value in scores.values() if math.isfinite(value))
    nan_ratio = (n_scored - n_finite) / n_scored if n_scored else 1.0
    coverage = n_finite / universe_size if universe_size > 0 else 0.0
    failures: list[str] = []
    if n_scored == 0:
        failures.append("输出为空(0 个标的的 score),拒绝入库")
    if n_scored > universe_size:
        failures.append(
            f"输出标的数 {n_scored} 超出挂载 universe 大小 {universe_size}"
        )
    if n_scored and nan_ratio > max_nan_ratio:
        failures.append(
            f"NaN 比例 {nan_ratio:.4f} 超过上限 max_nan_ratio="
            f"{max_nan_ratio}({n_scored - n_finite}/{n_scored} 个非有限值)"
        )
    if coverage < min_coverage:
        failures.append(
            f"覆盖率 {coverage:.4f} 低于下限 min_coverage={min_coverage}"
            f"({n_finite}/{universe_size} 个有限值)"
        )
    return QualityGateReport(
        passed=not failures,
        n_scored=n_scored,
        n_finite=n_finite,
        universe_size=universe_size,
        nan_ratio=nan_ratio,
        coverage=coverage,
        max_nan_ratio=max_nan_ratio,
        min_coverage=min_coverage,
        failures=tuple(failures),
    )


def build_factor_snapshot(
    *,
    factor_artifact_name: str,
    run_id: str,
    decision_at: datetime,
    commit: str,
    mount_manifest_checksum: str,
    scores: Mapping[str, float],
    quality: QualityGateReport,
) -> FeatureSnapshot:
    """把通过质量门的 scores 构造为 feature snapshot 兼容观测。

    非有限值不进观测(``FeatureObservation`` 要求有限值);缺值标的
    的处理由消费端既有 missing 语义承担(与 builtin 因子一致)。
    """
    if not quality.passed:
        raise QualityGateError(";".join(quality.failures))
    feature_name = sandbox_factor_name(factor_artifact_name)
    observations = tuple(
        FeatureObservation(
            symbol=symbol,
            feature_name=feature_name,
            value=float(value),
            observed_at=decision_at,
            available_at=decision_at,
            source=_SOURCE,
            source_version=commit,
        )
        for symbol, value in sorted(scores.items())
        if math.isfinite(value)
    )
    if not observations:
        raise QualityGateError("没有可入库的有限值观测")
    return build_feature_snapshot(
        dataset_release_id=None,
        dataset_release_checksum=mount_manifest_checksum,
        decision_at=decision_at,
        code_version=commit,
        observations=observations,
        source_run_id=run_id,
        issues=(
            (
                f"quality_gate: nan_ratio={quality.nan_ratio:.4f}, "
                f"coverage={quality.coverage:.4f}, "
                f"n_finite={quality.n_finite}/{quality.universe_size}"
            ),
        ),
    )


async def sandbox_snapshot_dataset_release_ids(
    session: Any,
    snapshot: FeatureSnapshot,
) -> set[str] | None:
    """沙箱快照的数据发布集合校验输入(issue #217 入队门控)。

    发布快照(``dataset_release_id`` 非空)返回 None(走既有单发布
    绑定校验);沙箱快照查其锚定 run 冻结的 ``dataset_release_ids``,
    run 缺失时抛 ``QualityGateError``(数据链断裂,fail-visible)。
    """
    if snapshot.dataset_release_id is not None:
        return None
    if snapshot.source_run_id is None:
        raise QualityGateError(
            f"因子快照 {snapshot.snapshot_id} 既无发布锚点也无 run 锚点"
        )
    from finboard_persistence import ResearchCodeRunRepository

    run = await ResearchCodeRunRepository(session).get(snapshot.source_run_id)
    if run is None:
        raise QualityGateError(
            f"沙箱因子快照 {snapshot.snapshot_id} 锚定的 run 不存在: "
            f"{snapshot.source_run_id}"
        )
    return set(run.dataset_release_ids)


__all__ = [
    "QUALITY_GATE_FAILED",
    "QualityGateError",
    "QualityGateReport",
    "build_factor_snapshot",
    "check_output_quality",
    "sandbox_snapshot_dataset_release_ids",
]
