"""``user_code`` 策略的可引用性与入队门控(issue #218)。

user_code 策略引用 ``research_code_artifacts`` 的 ``kind=strategy``
artifact;可引用性三道闸的前两道在这里(artifact 必须 active+passed,第三道 = 执行期数据全部来自
冻结 manifest,commit 已冻结,artifact 后续 retired 不影响已入队运行):

* 编译期(``compile_strategy_spec(user_code_sources=...)``)—— active+passed 名单外
  的 artifact 直接 ``StrategySpecError``(retired / 不存在 fail-visible);
* 入队期(:func:`user_code_reference_gate_error`,REST+MCP 共用,对齐
  #186/#203/#217 秒级失败风格)—— 引用不在 active+passed 名单 / commit 与
  active 不一致 / 沙箱未启用 → 拒绝;放行时把 **active commit 冻结进
  manifest 的 strategy_spec.code_artifact.commit**。

issue #234 screen 通道:显式绑定(``screen_artifact_bindings``)经
``resolve_screen_bindings`` 实绑校验后,把绑定产物的 commit/ID 并入调用方
传入的名单与冻结映射 —— 仅声明绑定的规格生效,普通引用门行为不变。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from typing import Any

from finboard_backtest.research_code.promotion import is_promoted_artifact
from finboard_backtest.research_run.contracts import (
    ResearchExecutionMode,
    execution_mode_for,
)

_USER_CODE_KIND = "user_code"
_STRATEGY_KIND = "strategy"
_ACTIVE = "active"


async def active_user_strategy_names(session: Any) -> frozenset[str]:
    """查询当前 ``status=active/promotion_status=passed`` 的策略 artifact 名集合。"""
    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    artifacts = await repo.list_artifacts(kind=_STRATEGY_KIND, status=_ACTIVE, limit=500)
    return frozenset(item.name for item in artifacts if is_promoted_artifact(item))


async def active_user_strategy_commits(session: Any) -> dict[str, str]:
    """``{artifact_name: active_commit}``(入队期冻结 commit 用)。"""
    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    artifacts = await repo.list_artifacts(kind=_STRATEGY_KIND, status=_ACTIVE, limit=500)
    return {item.name: item.commit for item in artifacts if is_promoted_artifact(item)}


def user_code_reference_gate_error(
    *,
    code_artifact_name: str | None,
    code_artifact_commit: str | None,
    active_user_strategies: dict[str, str] | Any,  # {name: active_commit}
    sandbox_enabled: bool,
    frozen_snapshot_count: int,
    parameters: dict[str, Any] | None,
) -> str | None:
    """入队期 user_code 门控;返回错误文案或 None(放行)。

    1. 非 user_code 规格(name 为 None)直接放行;
    2. 沙箱未启用 → 拒绝(fail-fast,与 worker 执行期同口径);
    3. 引用的 artifact 不在 active+passed 名单 → 拒绝(附 rollback 路径);
    4. 声明的 commit 与 active 不一致 → 拒绝(历史版本先 rollback);
    5. single_shot(未声明 decision_schedule / rebalance_frequency)且未冻结
       快照 → 拒绝(user_code 决策时点与信号引擎同口径:single_shot 来自快照)。
    """
    if code_artifact_name is None:
        return None
    if not sandbox_enabled:
        return (
            "user_code 策略执行需要研究沙箱(research_sandbox_enabled=true;"
            "本机 Docker Desktop + docker/research-sandbox 镜像)。请先启用沙箱"
            "再入队 user_code 运行"
        )
    active = dict(active_user_strategies or {})
    active_commit = active.get(code_artifact_name)
    if active_commit is None:
        status_note = "非 active 或未通过 screen+OOS 晋级门"
        return (
            f"user_code 策略引用的代码 artifact 不存在或非 active: "
            f"{code_artifact_name!r}({status_note})。策略代码经 finboard_research_code_submit"
            "(kind=strategy)提交;artifact retired 后不可被新运行引用"
            "(已入队运行的 manifest 冻结不受影响);请恢复 artifact"
            "(finboard_research_code_rollback)或更换引用"
        )
    if code_artifact_commit is not None and code_artifact_commit != active_commit:
        return (
            f"指定 commit {code_artifact_commit[:12]} 不是 active+passed 引用"
            f"(active={active_commit[:12]});历史版本先 "
            "finboard_research_code_rollback 再入队"
        )
    # issue #361:multi_period 判定跟随 decision_schedule(含 custom)或
    # legacy rebalance_frequency;single_shot 的决策时点只能来自快照。
    if (
        execution_mode_for(parameters or {}) is ResearchExecutionMode.SINGLE_SHOT
        and frozen_snapshot_count <= 0
    ):
        return (
            "execution_mode=single_shot(未声明 parameters.decision_schedule):"
            "user_code 策略该路径的决策时点只能来自冻结因子快照,但 "
            "factor_snapshot_ids 为空。请声明 parameters.decision_schedule"
            "(或 legacy rebalance_frequency=daily/weekly/monthly/quarterly)"
            "走多期回放(decide 每期从冻结发布重算),"
            "或冻结至少一份特征快照以提供决策时点"
        )
    return None


def freeze_user_code_commit(
    spec: Any,
    active_commits: dict[str, str],
    artifact_ids: dict[str, str] | None = None,
) -> Any:
    """把 active commit 冻结进 spec 的 ``code_artifact.commit``。

    入队期调用(spec.code_artifact.commit 为 None = 引用 active);冻结后
    manifest 的 input_checksum 覆盖代码版本,重放确定性由此保证(#218)。
    ``artifact_ids``(#234,可选)把实绑校验通过的 screen 绑定产物 id 一并
    冻结进 ``code_artifact.artifact_id``,promote 侧四向校验据此兜底;
    普通入队不传,行为与 #218 完全一致。
    spec 是 frozen pydantic 模型,经 ``model_copy(update=...)`` 派生。
    """
    ref = spec.code_artifact
    if ref is None:
        return spec
    updates: dict[str, str] = {}
    if ref.commit is None and ref.name in active_commits:
        updates["commit"] = active_commits[ref.name]
    ids = artifact_ids or {}
    if ref.artifact_id is None and ref.name in ids:
        updates["artifact_id"] = ids[ref.name]
    if not updates:
        return spec
    frozen = ref.model_copy(update=updates)
    return spec.model_copy(update={"code_artifact": frozen})


__all__ = [
    "active_user_strategy_commits",
    "active_user_strategy_names",
    "freeze_user_code_commit",
    "user_code_reference_gate_error",
]
