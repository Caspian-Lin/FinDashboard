"""research_run 失败上下文包裹(issue #263)。

multi_period 中期失败的 error_summary 原本只有最后一行裸异常,缺「哪个
决策点、哪个 stage、绑定到哪个发布」,远程定位根因要交叉翻 run detail /
job phase / artifacts 三个入口。本模块提供两层协作的包裹:

* **数据加载期标记**——``signal_engine.build_decision_load_contexts`` 的
  逐期循环在 bare raise 前经 :func:`attach_decision_load_context` 给异常
  对象挂标记:异常类型 / 消息 / traceback 全不变(既有 ``pytest.raises``
  与具名错误文案零破坏),runner 侧经 :func:`read_decision_load_context`
  读回失败期次的精确决策日与 bars 主发布;
* **执行期上下文**——runner 逐决策循环内随进度更新 :class:`FailureContext`
  (stage / 决策日期 / 第几期),通用 ``except Exception`` 收口经
  :func:`build_failure_summary` 在 ``str(exc)`` 前拼一行结构化头部。

边界:只有通用收口走 :func:`build_failure_summary`;unsupported /
hard_constraint / interrupted / non_deterministic_replay 四个专项分支的
error_summary 保持 ``str(exc)`` 原样,fail-closed 状态机与 #188 进度上报
语义不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from finboard_backtest.research_run.contracts import ResearchRunManifest

#: 异常对象上的加载期标记属性名。bare raise 重抛原对象,只挂属性、不改类型。
_LOAD_CONTEXT_ATTR = "__finboard_decision_load_context__"


@dataclass(frozen=True)
class DecisionLoadContextMarker:
    """加载期异常标记:失败期次的精确决策时点与 bars 主发布。"""

    decision_at: datetime
    release_id: str


def attach_decision_load_context(
    exc: BaseException,
    *,
    decision_at: datetime,
    release_id: str,
) -> None:
    """给加载期异常挂上下文标记(尽力而为;失败不影响原异常语义)。"""
    try:
        setattr(
            exc,
            _LOAD_CONTEXT_ATTR,
            DecisionLoadContextMarker(decision_at=decision_at, release_id=release_id),
        )
    except Exception:  # 标记只是可观测性增强,挂载失败不掩盖原始错误
        return


def read_decision_load_context(
    exc: BaseException,
) -> DecisionLoadContextMarker | None:
    """读回加载期标记;未挂标记(或非本链路异常)返回 None。"""
    marker = getattr(exc, _LOAD_CONTEXT_ATTR, None)
    if isinstance(marker, DecisionLoadContextMarker):
        return marker
    return None


@dataclass
class FailureContext:
    """runner 执行期的可变失败上下文,随循环进度逐点更新。

    * ``stage``:``decision_load``(输入构建期)→ ``decision_execute``(校验/
      持久化间隙)→ 具体 stage 值(universe/features/…/ledger)→ ``report``;
    * ``decision_date``:当前决策的 business_date(加载期由标记提供,执行期
      来自已收到的 decision);
    * ``decision_index``:1-based 期次序号(头部展示用;artifact 的
      ``sequence`` 是 0-based,二者勿混)。
    """

    stage: str | None = None
    decision_date: date | None = None
    decision_index: int | None = None


def build_failure_summary(
    exc: BaseException,
    context: FailureContext,
    manifest: ResearchRunManifest,
) -> str:
    """通用收口的 error_summary:头部结构化上下文 + 原始异常消息。

    加载期标记优先(失败期次的精确决策日);执行期用 runner 循环上下文。
    头部字段按序拼接,空字段省略;无任何上下文(循环前失败,如
    ``validate_manifest``)时返回原始消息,与既有行为一致。头部置于消息
    最前,与 worker 的保头保尾截断(:func:`truncate_summary`)配合,截断后
    stage + 决策日仍可读。
    """
    stage = context.stage
    decision_date = context.decision_date
    release_id: str | None = None

    marker = read_decision_load_context(exc)
    if marker is not None:
        stage = stage or "decision_load"
        decision_date = (
            marker.decision_at.date()
            if isinstance(marker.decision_at, datetime)
            else marker.decision_at
        )
        release_id = marker.release_id

    fields: list[str] = []
    if stage is not None:
        fields.append(f"stage={stage}")
    if decision_date is not None:
        fields.append(f"decision={decision_date.isoformat()}")
    if context.decision_index is not None:
        fields.append(f"decision_index={context.decision_index}")
    if release_id is not None:
        fields.append(f"release={release_id}")
    dataset_releases = ",".join(ref.artifact_id for ref in manifest.dataset_releases)
    if dataset_releases:
        fields.append(f"dataset_releases={dataset_releases}")
    factor_snapshots = ",".join(ref.artifact_id for ref in manifest.factor_snapshots)
    if factor_snapshots:
        fields.append(f"factor_snapshots={factor_snapshots}")
    factor_series = ",".join(ref.artifact_id for ref in manifest.factor_series)
    if factor_series:
        fields.append(f"factor_series={factor_series}")

    message = str(exc)
    if not fields:
        return message
    return f"[{'; '.join(fields)}] {message}"


__all__ = [
    "DecisionLoadContextMarker",
    "FailureContext",
    "attach_decision_load_context",
    "build_failure_summary",
    "read_decision_load_context",
]
