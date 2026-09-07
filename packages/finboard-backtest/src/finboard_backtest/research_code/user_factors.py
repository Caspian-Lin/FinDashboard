"""用户自定义因子的可引用性与入队门控(issue #217)。

用户因子(u_ 前缀)的值来自沙箱执行(``finboard_research_code_run``)
落库的 feature snapshot;可引用性由 artifact ``status=active`` 且
``promotion_status=passed`` 把关:

* 编译期(``compile_strategy_spec(user_factor_sources=...)``)—— active+passed 名单
  外的用户因子直接 ``StrategySpecError``(retired / 不存在 fail-visible);
* 入队期(``user_factor_reference_gate_error``,REST+MCP 共用,对齐
  #186/#203 秒级失败风格)—— 引用的用户因子不在 active 名单 → 拒绝;
  声明 ``rebalance_frequency``(multi_period)且引用用户因子 → 拒绝
  (沙箱因子观测绑定单一 decision_at,多期重算不支持,未来扩展需
  逐期独立 run);
* 快照锚定预检(issue #355,REST+MCP 共用)—— 引用快照锚定的数据发布
  ⊄ 本次 ``dataset_release_ids`` 时入队即拒(逐快照全量列出 + 修复路径;
  ⊆ 约束本身与快照 PIT 语义不变),``strategy_validate`` 通道给具名
  warning 提示(不阻断)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from finboard_backtest.research_code.promotion import is_promoted_artifact
from finboard_data.factor_lab import (
    USER_FACTOR_PREFIX,
    is_user_factor_name,
    sandbox_factor_name,
)

#: research_code 的 kind 白名单里 user 因子固定为 factor
_USER_FACTOR_KIND = "factor"
_ACTIVE = "active"

#: 入队拒绝文案的具名标记(issue #355,供测试 / agent 检索)
SNAPSHOT_ANCHOR_MISMATCH_CODE = "snapshot_anchor_mismatch"

#: validate 通道 warning 的具名标记(issue #355)
USER_FACTOR_ANCHOR_WARNING_CODE = "user_factor_anchor_mismatch"


async def active_user_factor_names(session: Any) -> frozenset[str]:
    """查询当前 ``status=active/promotion_status=passed`` 的用户因子名集合。"""
    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    artifacts = await repo.list_artifacts(kind=_USER_FACTOR_KIND, status=_ACTIVE, limit=500)
    return frozenset(
        sandbox_factor_name(item.name) for item in artifacts if is_promoted_artifact(item)
    )


def user_factor_reference_gate_error(
    *,
    required_factor_sources: Collection[str],
    active_user_factors: Collection[str],
    parameters: dict[str, Any] | None,
    series_covered_factors: Collection[str] = frozenset(),
) -> str | None:
    """入队期用户因子门控;返回错误文案或 None(放行)。

    1. 引用的用户因子不在 active+passed 名单 → 拒绝(附缺失名单);
    2. multi_period(``parameters.rebalance_frequency``)引用用户因子 → 拒绝。

    issue #360:被声明 ``factor_series_ids`` 覆盖的 u_ 因子跳过上述两条 ——
    序列是内容寻址的冻结工件(逐决策日观测,不再绑定单一 decision_at),
    multi_period 按决策日索引 series.values,artifact 生命周期(retired 等)
    不影响已冻结序列的可引用性(同 manifest 冻结先例)。
    """
    covered = set(series_covered_factors)
    referenced = {
        name
        for name in required_factor_sources
        if is_user_factor_name(name) and name not in covered
    }
    if not referenced:
        return None
    frequency = (parameters or {}).get("rebalance_frequency")
    if frequency:
        return (
            f"execution_mode=multi_period(rebalance_frequency={frequency})不支持引用"
            f"用户自定义因子: {sorted(referenced)}。沙箱因子观测绑定单一 "
            "decision_at(finboard_research_code_run 的快照),多期每期重算"
            "仅覆盖价格因子;请去掉 rebalance_frequency 走 single_shot,"
            "决策时点由快照 decision_at 推导。"
        )
    missing = referenced - set(active_user_factors)
    if missing:
        return (
            f"引用的用户自定义因子不存在或 artifact 非 active: {sorted(missing)}"
            "。用户因子在研究代码 artifact retired 后不可再被新运行引用"
            "(已入队运行的 manifest 冻结不受影响);请恢复 artifact"
            "(finboard_research_code_rollback)或移除该因子引用。"
        )
    return None


# --------------------------------------------------------------------------- #
# 快照锚定预检(issue #355):换 bars 主发布后,引用的既有用户因子快照
# 锚定发布 ⊄ 本次 dataset_release_ids 的前置可见化。只做提示/前置拒绝,
# ⊆ 约束本身与快照 PIT 语义不变。
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SnapshotAnchorMismatch:
    """单个因子快照的「锚定发布 ⊄ 本次冻结清单」失配记录(issue #355)。

    ``anchor_kind`` 区分两种锚点(#217 数据锚定二选一):

    * ``sandbox_run`` —— 沙箱快照(dataset_release_id=None),锚定其
      ``source_run_id`` 指向 RCR 冻结的发布集合;
    * ``published_release`` —— 发布快照(绑定向单一数据发布)。
    """

    snapshot_id: str
    #: 快照观测携带的因子名(u_ 前缀用户因子 / 因子目录名,排序去重)
    factor_names: tuple[str, ...]
    #: 快照锚定的数据发布集合(排序)
    anchored_release_ids: tuple[str, ...]
    #: 锚定 - 本次冻结清单(失配方向:锚定 ⊄ 本次,排序)
    missing_release_ids: tuple[str, ...]
    #: ``sandbox_run`` | ``published_release``
    anchor_kind: str
    #: 沙箱快照的锚定 run(发布快照为 None)
    run_id: str | None = None


async def snapshot_anchor_mismatches(
    session: Any,
    *,
    snapshots: Sequence[Any],
    requested_release_ids: Collection[str],
) -> list[SnapshotAnchorMismatch]:
    """逐快照收集「锚定发布 ⊄ 本次冻结清单」失配(REST+MCP 入队共用)。

    与既有 #217 门控同判定口径(经
    ``sandbox_snapshot_dataset_release_ids``),差异只在失败形态:收集
    **全部**失配快照一次性返回,由调用方渲染具名错误;全匹配返回空列表
    (零噪音)。锚定链断裂(run 缺失等)沿既有语义抛 ``QualityGateError``
    fail-visible,不在本层吞掉。
    """
    from finboard_backtest.research_sandbox.factor_publish import (
        sandbox_snapshot_dataset_release_ids,
    )

    requested = set(requested_release_ids)
    mismatches: list[SnapshotAnchorMismatch] = []
    for snapshot in snapshots:
        anchored_ids = await sandbox_snapshot_dataset_release_ids(session, snapshot)
        if anchored_ids is None:
            # 发布快照:绑定向单一数据发布(缺失锚点在解析层已 fail-visible)。
            release_id = snapshot.dataset_release_id
            if release_id is None or release_id in requested:
                continue
            anchored: set[str] = {release_id}
            missing = anchored - requested
            anchor_kind = "published_release"
            run_id = None
        else:
            if anchored_ids <= requested:
                continue
            anchored = set(anchored_ids)
            missing = anchored - requested
            anchor_kind = "sandbox_run"
            run_id = snapshot.source_run_id
        mismatches.append(
            SnapshotAnchorMismatch(
                snapshot_id=str(snapshot.snapshot_id),
                factor_names=tuple(
                    sorted({str(item.feature_name) for item in snapshot.observations})
                ),
                anchored_release_ids=tuple(sorted(anchored)),
                missing_release_ids=tuple(sorted(missing)),
                anchor_kind=anchor_kind,
                run_id=None if run_id is None else str(run_id),
            )
        )
    return mismatches


def snapshot_anchor_mismatch_error(
    mismatches: Sequence[SnapshotAnchorMismatch],
) -> str:
    """把失配记录渲染为入队拒绝文案(REST 422 / MCP invalid_argument 共用)。

    逐快照列出名称 / 锚定发布 / 失配方向,附两条修复路径:(a) 把锚定发布
    一并加入 dataset_release_ids;(b) 对新 bars 发布重算沙箱因子
    (``finboard_research_code_run`` 提交 RCR → 质量门 → 新快照)后引用。
    """
    lines = [
        f"因子快照锚定发布失配({SNAPSHOT_ANCHOR_MISMATCH_CODE}):"
        f"{len(mismatches)} 个引用快照的锚定数据发布不在本次冻结清单中"
        "(典型场景:更换 bars 主发布后,引用的既有因子快照未随新发布重算)。"
    ]
    for item in mismatches:
        kind_label = (
            f"沙箱 run {item.run_id} 锚定"
            if item.anchor_kind == "sandbox_run"
            else "发布快照绑定"
        )
        factor_label = (
            f"(因子: {', '.join(item.factor_names)})" if item.factor_names else ""
        )
        lines.append(
            f"- 快照 {item.snapshot_id}{factor_label}[{kind_label}]:"
            f"锚定发布 {list(item.anchored_release_ids)},"
            f"缺失 {list(item.missing_release_ids)}(锚定 ⊄ 本次冻结清单);"
        )
    lines.append(
        "修复路径二选一:(a) 把缺失发布一并加入本次 dataset_release_ids;"
        "(b) 引用锚定本次发布集合的快照 —— 沙箱因子对新 bars 发布重算"
        "(finboard_research_code_run 提交 RCR → 质量门 → 新快照)后以新 "
        "snapshot_id 入队。⊆ 约束与快照 PIT 语义不变。"
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class UserFactorAnchorWarning:
    """validate 通道的用户因子快照锚定失配提示(issue #355,不阻断)。"""

    code: str
    #: 用户因子名(u_ 前缀)
    factor_name: str
    #: 证据 run(该锚定发布集合下最新一次成功 RCR)
    run_id: str
    #: 该 run 产出的可引用快照
    snapshot_id: str
    anchored_release_ids: tuple[str, ...]
    missing_release_ids: tuple[str, ...]
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "factor_name": self.factor_name,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "anchored_release_ids": list(self.anchored_release_ids),
            "missing_release_ids": list(self.missing_release_ids),
            "message": self.message,
        }


async def user_factor_anchor_warnings(
    session: Any,
    *,
    required_factor_sources: Collection[str],
    dataset_release_ids: Collection[str],
) -> tuple[UserFactorAnchorWarning, ...]:
    """validate 通道锚定预检:引用的 u_ 因子若存在锚定发布 ⊄ 本次
    ``dataset_release_ids`` 的既有沙箱快照,逐因子逐锚定集合给具名提示。

    validate 只见规格、不见 ``factor_snapshot_ids``,此处按因子名回溯其
    全部成功 RCR(任一 commit;与入队期「快照锚定 ⊆ 冻结清单」同口径)
    的已产出快照;直接引用将被入队秒级拒绝,提前在此指路(修复路径与
    :func:`snapshot_anchor_mismatch_error` 一致)。全匹配返回空元组
    (零噪音)。纯提示层,不改变任何约束。
    """
    referenced = sorted(
        {name for name in required_factor_sources if is_user_factor_name(name)}
    )
    requested = set(dataset_release_ids)
    if not referenced or not requested:
        return ()
    from finboard_persistence import ResearchCodeRunRepository

    run_repo = ResearchCodeRunRepository(session)
    warnings: list[UserFactorAnchorWarning] = []
    for name in referenced:
        runs = await run_repo.list_runs(
            kind=_USER_FACTOR_KIND,
            name=name[len(USER_FACTOR_PREFIX) :],
            status="succeeded",
            limit=500,
        )
        seen_anchors: set[frozenset[str]] = set()
        # list_runs 按创建时间倒序:首个命中的 run 即该锚定集合下最新证据。
        for run in runs:
            if run.output_snapshot_id is None:
                continue
            anchored = frozenset(run.dataset_release_ids)
            if anchored <= requested or anchored in seen_anchors:
                continue
            seen_anchors.add(anchored)
            missing = sorted(anchored - requested)
            warnings.append(
                UserFactorAnchorWarning(
                    code=USER_FACTOR_ANCHOR_WARNING_CODE,
                    factor_name=name,
                    run_id=run.run_id,
                    snapshot_id=run.output_snapshot_id,
                    anchored_release_ids=tuple(sorted(anchored)),
                    missing_release_ids=tuple(missing),
                    message=(
                        f"用户因子 {name} 的既有沙箱快照(run {run.run_id})锚定发布 "
                        f"{sorted(anchored)} 不在本次 dataset_release_ids"
                        f"(缺失 {missing},锚定 ⊄ 本次):直接引用该快照入队将被拒"
                        f"({SNAPSHOT_ANCHOR_MISMATCH_CODE})。修复路径:(a) 把缺失发布"
                        "一并加入 dataset_release_ids;(b) 对新 bars 发布重算沙箱因子"
                        "(finboard_research_code_run 提交 RCR → 质量门 → 新快照)后引用。"
                    ),
                )
            )
    return tuple(warnings)


__all__ = [
    "SNAPSHOT_ANCHOR_MISMATCH_CODE",
    "USER_FACTOR_ANCHOR_WARNING_CODE",
    "SnapshotAnchorMismatch",
    "UserFactorAnchorWarning",
    "active_user_factor_names",
    "snapshot_anchor_mismatch_error",
    "snapshot_anchor_mismatches",
    "user_factor_anchor_warnings",
    "user_factor_reference_gate_error",
]
