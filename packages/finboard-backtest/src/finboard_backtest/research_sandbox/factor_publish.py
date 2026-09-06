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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from finboard_data.factor_lab import (
    FeatureObservation,
    FeatureSnapshot,
    build_feature_snapshot,
    sandbox_factor_name,
)

#: 质量门失败错误码(不可重试;修因子代码后重新提交 run)
QUALITY_GATE_FAILED = "quality_gate_failed"

#: missing_symbols 清单截断上限(总数另记,防大候选池撑爆 metrics)
MAX_MISSING_SYMBOLS = 50

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
    #: 挂载 universe 中未产出有限值的标的(排序后截断,issue #237);
    #: 调用方未提供 universe 清单时为空,总数仍记入 missing_symbols_total
    missing_symbols: tuple[str, ...] = ()
    missing_symbols_total: int = 0

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
            "missing_symbols": list(self.missing_symbols),
            "missing_symbols_total": self.missing_symbols_total,
        }


def check_output_quality(
    scores: Mapping[str, float],
    *,
    universe_size: int,
    max_nan_ratio: float,
    min_coverage: float,
    universe_symbols: Sequence[str] | None = None,
) -> QualityGateReport:
    """NaN 比例 / 覆盖率 / 长度校验(纯函数,便于单测)。

    * NaN 比例 = 非有限值(含 NaN/inf)占输出标的数;超上限拒绝;
    * 覆盖率 = 有限值标的数 / 挂载 universe 大小;低于下限拒绝;
    * 长度校验 = 输出非空且不超出 universe(harness 已挡候选池外
      symbol,这里再防一层执行器侧回归);
    * 缺失标的(issue #237):提供 ``universe_symbols`` 时点名未产出
      有限值的标的(排序后截断到前 ``MAX_MISSING_SYMBOLS`` 只),总数
      另记 ``missing_symbols_total``;未提供时只记总数。
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
    finite_symbols = {
        symbol for symbol, value in scores.items() if math.isfinite(value)
    }
    if universe_symbols is not None:
        missing_all = sorted(
            {str(symbol) for symbol in universe_symbols} - finite_symbols
        )
        missing = tuple(missing_all[:MAX_MISSING_SYMBOLS])
        missing_total = len(missing_all)
    else:
        # 未提供清单时不点名;总数按 universe 差额估计(下限 0)
        missing = ()
        missing_total = max(universe_size - n_finite, 0)
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
        missing_symbols=missing,
        missing_symbols_total=missing_total,
    )


@dataclass(frozen=True)
class SeriesQualityReport:
    """区间输出(factor_series)质量门评估(issue #359,逐日截面口径)。

    聚合口径:nan_ratio / coverage 在「决策日 x 候选标的」全部格子上
    计算(None / NaN / inf 均计非有限值);``worst_day_nan_ratio`` 为逐日
    截面 nan_ratio 的最大值(定位最差决策日,修复因子用)。
    """

    passed: bool
    n_dates: int
    universe_size: int
    nan_ratio: float
    coverage: float
    worst_day: date | None
    worst_day_nan_ratio: float
    max_nan_ratio: float
    min_coverage: float
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "n_dates": self.n_dates,
            "universe_size": self.universe_size,
            "nan_ratio": self.nan_ratio,
            "coverage": self.coverage,
            "worst_day": self.worst_day.isoformat() if self.worst_day else None,
            "worst_day_nan_ratio": self.worst_day_nan_ratio,
            "thresholds": {
                "max_nan_ratio": self.max_nan_ratio,
                "min_coverage": self.min_coverage,
            },
            "failures": list(self.failures),
        }


def check_series_quality(
    values: Mapping[date, Mapping[str, float | None]],
    *,
    dates: Sequence[date],
    universe: Sequence[str],
    max_nan_ratio: float,
    min_coverage: float,
) -> SeriesQualityReport:
    """区间输出质量门(纯函数;阈值复用 ``research_sandbox_*`` 设置)。

    逐日截面口径:每日截面按 ``{symbol: float | None}`` 评估 ——
    ``None`` / 非有限值均计缺测;聚合成整窗 nan_ratio / coverage 后按
    阈值判定,另报最差决策日的 nan_ratio(排查锚点)。空截面(没有任何
    打分格子)直接拒绝。
    """
    n_days = len(dates)
    n_universe = len(universe)
    n_cells = n_days * n_universe
    n_scored = 0
    n_finite = 0
    worst_day: date | None = None
    worst_day_nan_ratio = 0.0
    for day in dates:
        cross = values.get(day, {})
        day_scored = 0
        day_finite = 0
        for symbol in universe:
            value = cross.get(symbol)
            if value is None:
                continue
            day_scored += 1
            if math.isfinite(value):
                day_finite += 1
        n_scored += day_scored
        n_finite += day_finite
        day_nan_ratio = (
            1.0 - (day_finite / day_scored) if day_scored else 1.0
        )
        if day_nan_ratio > worst_day_nan_ratio:
            worst_day_nan_ratio = day_nan_ratio
            worst_day = day
    nan_ratio = 1.0 - (n_finite / n_scored) if n_scored else 1.0
    coverage = n_finite / n_cells if n_cells else 0.0
    failures: list[str] = []
    if n_scored == 0:
        failures.append(
            "区间输出为空(全部决策日均无任何打分格子),拒绝"
        )
    if n_scored and nan_ratio > max_nan_ratio:
        failures.append(
            f"整窗 NaN 比例 {nan_ratio:.4f} 超过上限 max_nan_ratio="
            f"{max_nan_ratio}({n_scored - n_finite}/{n_scored} 个非有限值)"
        )
    if n_scored and worst_day_nan_ratio > max_nan_ratio:
        failures.append(
            f"最差决策日 {worst_day} NaN 比例 {worst_day_nan_ratio:.4f} "
            f"超过上限 max_nan_ratio={max_nan_ratio}"
        )
    if coverage < min_coverage:
        failures.append(
            f"覆盖率 {coverage:.4f} 低于下限 min_coverage={min_coverage}"
            f"({n_finite}/{n_cells} 个有限值格子)"
        )
    return SeriesQualityReport(
        passed=not failures,
        n_dates=n_days,
        universe_size=n_universe,
        nan_ratio=nan_ratio,
        coverage=coverage,
        worst_day=worst_day,
        worst_day_nan_ratio=worst_day_nan_ratio,
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
                f"n_finite={quality.n_finite}/{quality.universe_size}, "
                f"missing_symbols_total={quality.missing_symbols_total}"
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
    "SeriesQualityReport",
    "build_factor_snapshot",
    "check_output_quality",
    "check_series_quality",
    "sandbox_snapshot_dataset_release_ids",
]
