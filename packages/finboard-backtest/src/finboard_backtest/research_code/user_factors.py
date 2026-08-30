"""用户自定义因子的可引用性与入队门控(issue #217)。

用户因子(u_ 前缀)的值来自沙箱执行(``finboard_research_code_run``)
落库的 feature snapshot;可引用性由 artifact ``status=active`` 把关:

* 编译期(``compile_strategy_spec(user_factor_sources=...)``)—— 名单
  外的用户因子直接 ``StrategySpecError``(retired / 不存在 fail-visible);
* 入队期(``user_factor_reference_gate_error``,REST+MCP 共用,对齐
  #186/#203 秒级失败风格)—— 引用的用户因子不在 active 名单 → 拒绝;
  声明 ``rebalance_frequency``(multi_period)且引用用户因子 → 拒绝
  (沙箱因子观测绑定单一 decision_at,多期重算不支持,未来扩展需
  逐期独立 run)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from finboard_data.factor_lab import is_user_factor_name, sandbox_factor_name

#: research_code 的 kind 白名单里 user 因子固定为 factor
_USER_FACTOR_KIND = "factor"
_ACTIVE = "active"


async def active_user_factor_names(session: Any) -> frozenset[str]:
    """查询当前 ``status=active`` 的沙箱用户因子名集合(u_ 前缀)。"""
    from finboard_persistence import ResearchCodeArtifactRepository

    repo = ResearchCodeArtifactRepository(session)
    artifacts = await repo.list_artifacts(
        kind=_USER_FACTOR_KIND, status=_ACTIVE, limit=500
    )
    return frozenset(sandbox_factor_name(item.name) for item in artifacts)


def user_factor_reference_gate_error(
    *,
    required_factor_sources: Collection[str],
    active_user_factors: Collection[str],
    parameters: dict[str, Any] | None,
) -> str | None:
    """入队期用户因子门控;返回错误文案或 None(放行)。

    1. 引用的用户因子不在 active 名单 → 拒绝(附缺失名单);
    2. multi_period(``parameters.rebalance_frequency``)引用用户因子 → 拒绝。
    """
    referenced = {name for name in required_factor_sources if is_user_factor_name(name)}
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


__all__ = [
    "active_user_factor_names",
    "user_factor_reference_gate_error",
]
