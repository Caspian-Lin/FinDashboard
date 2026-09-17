"""screen 用途 draft 代码产物显式绑定的入队门控(issue #234)。

#219 晋级门要求完成的 ResearchRun 提供 screen 证据,而编译期/入队名单只认
``active + promotion_status=passed`` —— draft 产物拿不到 screen 证据,永远
无法第一次 promote(鸡生蛋)。#234 建立显式、可审计的 screen 专用通道:

* **声明层**:规格经 ``screen_artifact_bindings`` 声明精确绑定
  (kind/name/artifact_id/可选 commit);编译期(:func:`compile_strategy_spec`)
  把绑定名并入**本规格**的可引用名单,普通规格不受影响;
* **实绑层**(本模块):入队期(REST ``POST /api/research/runs`` 与 MCP
  ``finboard_run_queue`` 共用,对齐 #186/#203 秒级失败风格)逐条按 DB 校验
  —— 产物存在、非 retired、kind/name 一致、commit 一致;通过后把
  commit + artifact_id 冻结进 manifest(strategy 通道写 code_artifact,
  factor 通道由快照 source_run_id → RCR 追溯承载);
* **兜底层**:promote 侧 ``_screen_evidence`` 的四向一致性校验
  (name/kind/artifact_id/commit)不变,screen 运行不可挪作他版代码的证据。

安全边界:绑定只放行**显式声明且实绑成功**的 draft;retired / active 未
晋级 / 错绑一律具名拒绝,非 screen 引用门行为完全不变。纯离线研究域,
不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from finboard_backtest.research_code.promotion import is_promoted_artifact

_FACTOR_BINDING_KIND = "factor"
_STRATEGY_BINDING_KIND = "strategy"


@dataclass(frozen=True)
class ScreenBindingResolution:
    """实绑校验通过后的解析结果(供入队门合并名单与冻结 commit)。"""

    #: strategy 绑定 {name: commit}(并入 user_code 入队门名单 + 冻结)
    strategy_commits: dict[str, str] = field(default_factory=dict)
    #: strategy 绑定 {name: artifact_id}(冻结进 code_artifact.artifact_id)
    strategy_artifact_ids: dict[str, str] = field(default_factory=dict)
    #: factor 绑定的 u_ 因子名集合(并入用户因子入队门名单)
    user_factor_names: frozenset[str] = frozenset()
    #: 逐条绑定审计(实绑后的精确引用,供诊断)
    bindings: tuple[dict[str, Any], ...] = ()


async def resolve_screen_bindings(
    session: Any,
    *,
    spec: Any,
) -> tuple[ScreenBindingResolution | None, str | None]:
    """按 DB 实绑校验规格声明的 screen 绑定(REST+MCP 共用)。

    返回 ``(resolution, error)``:无绑定 → ``(None, None)``,行为与
    #217/#218 既有门完全一致;有绑定但任一条实绑失败 → 具名错误文案。
    """
    bindings = list(getattr(spec, "screen_artifact_bindings", ()) or ())
    if not bindings:
        return None, None

    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    audit: list[dict[str, Any]] = []
    factor_names: set[str] = set()
    strategy_commits: dict[str, str] = {}
    strategy_artifact_ids: dict[str, str] = {}
    for binding in bindings:
        artifact = await repo.get(binding.artifact_id)
        if artifact is None:
            return None, (
                f"screen 绑定的代码产物不存在: artifact_id={binding.artifact_id}"
                f"(kind={binding.kind} name={binding.name})"
            )
        if artifact.kind != binding.kind or artifact.name != binding.name:
            return None, (
                f"screen 绑定与产物不一致: 声明 kind={binding.kind} "
                f"name={binding.name},产物 kind={artifact.kind} "
                f"name={artifact.name}(artifact_id={binding.artifact_id})"
            )
        if artifact.status == "retired":
            return None, (
                f"screen 绑定的代码产物已 retired: artifact_id={artifact.artifact_id}"
                f"(kind={artifact.kind} name={artifact.name});"
                "retired 产物不可作为 screen 证据来源"
            )
        if artifact.status == "active" and not is_promoted_artifact(artifact):
            return None, (
                f"screen 绑定的代码产物 active 但未通过晋级门: "
                f"artifact_id={artifact.artifact_id} "
                f"promotion_status={getattr(artifact, 'promotion_status', None)};"
                "该状态不一致,请核对产物登记"
            )
        if binding.commit is not None and binding.commit != artifact.commit:
            return None, (
                f"screen 绑定声明 commit {binding.commit[:12]} 与产物 commit "
                f"{artifact.commit[:12]} 不一致"
                f"(artifact_id={artifact.artifact_id})"
            )
        audit.append(
            {
                "kind": artifact.kind,
                "name": artifact.name,
                "artifact_id": artifact.artifact_id,
                "commit": artifact.commit,
                "checksum": artifact.checksum,
                "status": artifact.status,
            }
        )
        if binding.kind == _FACTOR_BINDING_KIND:
            from finboard_data.factor_lab import sandbox_factor_name

            factor_names.add(sandbox_factor_name(artifact.name))
        elif binding.kind == _STRATEGY_BINDING_KIND:
            strategy_commits[artifact.name] = artifact.commit
            strategy_artifact_ids[artifact.name] = artifact.artifact_id
    resolution = ScreenBindingResolution(
        strategy_commits=strategy_commits,
        strategy_artifact_ids=strategy_artifact_ids,
        user_factor_names=frozenset(factor_names),
        bindings=tuple(audit),
    )
    return resolution, None


async def screen_factor_snapshot_gate_error(
    session: Any,
    *,
    resolution: ScreenBindingResolution,
    snapshots: list[Any],
) -> str | None:
    """factor 通道 screen 运行的快照证据预检(REST+MCP 共用)。

    每条 factor 绑定必须引用至少一份「由该产物的 RCR 执行产出」的沙箱
    快照(snapshot.source_run_id → research_code_runs 行,artifact/name/
    kind/commit 四向一致);缺失秒级拒绝 —— 否则 run 完成后没有可用
    screen 证据,promote 必失败,提前到入队期暴露(#186 风格)。
    """
    factor_bindings = [
        item for item in resolution.bindings if item["kind"] == _FACTOR_BINDING_KIND
    ]
    if not factor_bindings:
        return None

    from finboard_persistence import ResearchCodeRunRepository

    missing: list[str] = []
    for binding in factor_bindings:
        matched = False
        for snapshot in snapshots:
            source_run_id = getattr(snapshot, "source_run_id", None)
            if not source_run_id:
                continue
            code_run = await ResearchCodeRunRepository(session).get(source_run_id)
            if code_run is None:
                continue
            if (
                code_run.artifact_id == binding["artifact_id"]
                and code_run.kind == binding["kind"]
                and code_run.name == binding["name"]
                and code_run.commit == binding["commit"]
            ):
                matched = True
                break
        if not matched:
            missing.append(
                f"{binding['name']}(artifact_id={binding['artifact_id']},"
                f"commit={binding['commit'][:12]})"
            )
    if missing:
        held = ", ".join(
            sorted(
                {
                    str(getattr(snapshot, "snapshot_id", ""))
                    for snapshot in snapshots
                }
            )
        )
        feature_names = sorted(
            {
                observation.feature_name
                for snapshot in snapshots
                for observation in getattr(snapshot, "observations", ()) or ()
            }
        )
        return (
            "factor 通道 screen 运行缺少绑定产物的沙箱快照证据: "
            f"{missing}。请先 finboard_research_code_run(显式 artifact_id)对 "
            "draft 产物执行沙箱并把产出快照加入 factor_snapshot_ids;"
            f"本次声明的快照({held})特征 {feature_names} 未锚定到绑定产物。"
        )
    return None


__all__ = [
    "ScreenBindingResolution",
    "resolve_screen_bindings",
    "screen_factor_snapshot_gate_error",
]
