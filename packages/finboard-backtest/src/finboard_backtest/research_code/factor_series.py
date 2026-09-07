"""因子序列工件的入队冻结与托管重建门控(issue #360,REST+MCP 共用)。

* **入队冻结** —— run 引用的 ``factor_series_ids`` 逐条解析后:

  - 换发布守卫:series 锚定的 ``release_id`` 不在本次 ``dataset_release_ids``
    时具名拒绝,附失效 series 清单 + 重建代价预估(N 条 x 预计分钟);
    批量重建 = 一次入队 N 个既有 ``finboard_factor_series_build`` job,
    不新造编排器(前缀不变性审计免费充当窗口扩展重建的一致性自检);
  - u_ 因子门控联动:被声明 series 覆盖的 u_ 因子跳过 #217 的
    multi_period 拒绝(观测不再绑定单一 decision_at,序列按决策日索引)
    与 single_shot 缺快照清单(序列提供逐日观测);
  - manifest 冻结:``factor_series = [{series_id, content_checksum}]`` 入
    manifest checksum 与 input_checksum(#218 active commit 冻结先例)。

* **validate 提示** —— 引用 u_ 因子且存在锚定其它发布的既有序列时给具名
  warning(#355 ``user_factor_anchor_warnings`` 同风格,不阻断)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from finboard_backtest.research_run.contracts import FrozenArtifactRef
from finboard_data.factor_lab import (
    USER_FACTOR_PREFIX,
    is_user_factor_name,
    sandbox_factor_name,
)

#: 入队拒绝文案的具名标记(issue #360,供测试 / agent 检索)
SERIES_RELEASE_MISMATCH_CODE = "series_release_mismatch"

#: validate 通道 warning 的具名标记(issue #360)
FACTOR_SERIES_ANCHOR_WARNING_CODE = "factor_series_anchor_mismatch"

#: 单条序列重建的预计分钟数(容器逐决策日执行 + 审计抽样;粗估用于
#: 重建代价提示,不构成 SLA)
ESTIMATED_REBUILD_MINUTES_PER_SERIES = 2

#: manifest 冻结引用的 version 标记(= series_key 的内容寻址版本前缀)
FACTORS_SERIES_REF_VERSION = "v2"


@dataclass(frozen=True)
class FactorSeriesReleaseMismatch:
    """单个序列的「锚定发布不在本次冻结清单」失配记录(换发布守卫)。"""

    series_id: str
    #: 因子名(u_ 前缀)
    factor_name: str
    #: 序列锚定的 bars 主发布
    anchored_release_id: str
    #: 窗口(重建代价预估的上下文)
    window_start: str
    window_end: str


def series_release_mismatches(
    records: Sequence[Any],
    requested_release_ids: Collection[str],
) -> list[FactorSeriesReleaseMismatch]:
    """逐序列收集「release_id 不在本次冻结清单」的失配(REST+MCP 共用)。

    全匹配返回空列表(零噪音);失配方向为「series 锚定发布失效」——
    换 bars 主发布后既有序列不在新发布上,须托管重建后以新 series_id 引用。
    """
    requested = set(requested_release_ids)
    return [
        FactorSeriesReleaseMismatch(
            series_id=str(record.series_id),
            factor_name=sandbox_factor_name(str(record.code_artifact)),
            anchored_release_id=str(record.release_id),
            window_start=str(record.window_start),
            window_end=str(record.window_end),
        )
        for record in records
        if str(record.release_id) not in requested
    ]


def factor_series_rebuild_error(
    mismatches: Sequence[FactorSeriesReleaseMismatch],
) -> str:
    """把失配记录渲染为入队拒绝文案(REST 422 / MCP invalid_argument 共用)。

    附失效 series 清单 + 重建代价预估(N 条 x 预计分钟);重建 = 一次
    入队 N 个既有 ``finboard_factor_series_build`` job(复用 job 基础设施,
    内容寻址缓存使未受影响的序列自动 unchanged,不新造编排器)。
    """
    lines = [
        f"因子序列锚定发布失配({SERIES_RELEASE_MISMATCH_CODE}):"
        f"{len(mismatches)} 个引用序列锚定的 bars 主发布不在本次冻结清单中"
        "(典型场景:更换 bars 主发布后,引用的既有因子序列未随新发布重建)。",
    ]
    for item in mismatches:
        lines.append(
            f"- 序列 {item.series_id}(因子: {item.factor_name}):"
            f"锚定发布 {item.anchored_release_id},"
            f"窗口 {item.window_start}~{item.window_end};"
        )
    estimate = len(mismatches) * ESTIMATED_REBUILD_MINUTES_PER_SERIES
    lines.append(
        f"修复路径二选一:(a) 把锚定发布一并加入本次 dataset_release_ids;"
        f"(b) 托管重建 —— 一次入队 {len(mismatches)} 个 "
        "finboard_factor_series_build(对新发布逐条重建,预计 "
        f"{estimate} 分钟)后以新 series_id 入队。序列内容寻址,重建时未受"
        "影响的输入组合自动命中缓存(unchanged)。"
    )
    return "\n".join(lines)


def series_covered_factor_names(records: Sequence[Any]) -> frozenset[str]:
    """声明序列覆盖的 u_ 因子名集合(``u_<code_artifact>``)。"""
    return frozenset(
        sandbox_factor_name(str(record.code_artifact)) for record in records
    )


def build_factor_series_refs(
    records: Sequence[Any],
) -> tuple[FrozenArtifactRef, ...]:
    """把序列记录冻结为 manifest 引用(按 series_id 排序,确定性)。"""
    return tuple(
        FrozenArtifactRef(
            artifact_id=str(record.series_id),
            version=FACTORS_SERIES_REF_VERSION,
            checksum=str(record.content_checksum),
            capabilities=(
                f"factor:{sandbox_factor_name(str(record.code_artifact))}",
            ),
        )
        for record in sorted(records, key=lambda item: str(item.series_id))
    )


@dataclass(frozen=True)
class FactorSeriesAnchorWarning:
    """validate 通道的序列锚定失配提示(issue #360,不阻断)。"""

    code: str
    factor_name: str
    series_id: str
    anchored_release_id: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "factor_name": self.factor_name,
            "series_id": self.series_id,
            "anchored_release_id": self.anchored_release_id,
            "message": self.message,
        }


async def factor_series_anchor_warnings(
    session: Any,
    *,
    required_factor_sources: Collection[str],
    dataset_release_ids: Collection[str],
) -> tuple[FactorSeriesAnchorWarning, ...]:
    """validate 通道锚定预检:引用的 u_ 因子若存在锚定其它发布的既有
    序列,逐序列给具名提示(修复路径与 :func:`factor_series_rebuild_error`
    一致)。全匹配 / 无引用返回空元组(零噪音)。纯提示层,不改变任何约束。
    """
    referenced = sorted(
        {name for name in required_factor_sources if is_user_factor_name(name)}
    )
    if not referenced or not dataset_release_ids:
        return ()
    from finboard_persistence import FactorSeriesRepository

    repo = FactorSeriesRepository(session)
    warnings: list[FactorSeriesAnchorWarning] = []
    for name in referenced:
        records = await repo.list_for_artifact(name.removeprefix(USER_FACTOR_PREFIX))
        mismatches = series_release_mismatches(records, dataset_release_ids)
        for item in mismatches:
            warnings.append(
                FactorSeriesAnchorWarning(
                    code=FACTOR_SERIES_ANCHOR_WARNING_CODE,
                    factor_name=name,
                    series_id=item.series_id,
                    anchored_release_id=item.anchored_release_id,
                    message=(
                        f"用户因子 {name} 的既有因子序列({item.series_id})锚定发布 "
                        f"{item.anchored_release_id} 不在本次 dataset_release_ids;"
                        f"直接引用该序列入队将被拒({SERIES_RELEASE_MISMATCH_CODE})。"
                        "修复路径:(a) 把锚定发布一并加入 dataset_release_ids;"
                        "(b) finboard_factor_series_build 对新发布托管重建后引用。"
                    ),
                )
            )
    return tuple(warnings)


__all__ = [
    "ESTIMATED_REBUILD_MINUTES_PER_SERIES",
    "FACTORS_SERIES_REF_VERSION",
    "FACTOR_SERIES_ANCHOR_WARNING_CODE",
    "SERIES_RELEASE_MISMATCH_CODE",
    "FactorSeriesAnchorWarning",
    "FactorSeriesReleaseMismatch",
    "build_factor_series_refs",
    "factor_series_anchor_warnings",
    "factor_series_rebuild_error",
    "series_covered_factor_names",
    "series_release_mismatches",
]
