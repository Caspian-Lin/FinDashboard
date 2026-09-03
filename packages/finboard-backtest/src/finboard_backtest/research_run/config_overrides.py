"""manifest 配置分区(portfolio_config / risk_config)消费与入队组合预检(issue #303)。

manifest 配置分区的语义此前只有 portfolio_config 有真实消费者,risk_config 是
死分区 —— 全仓仅 payload 守卫 / checksum 序列化 / from_payload 三处机械引用,
连它名义对应的 risk_exit_policy 读的都是 ``strategy_spec.risk_exit_policy``:

* ``portfolio_config`` 管**组合约束**(``PortfolioConstraints``):单资产 /
  单 sleeve 权重上限、现金缓冲、杠杆、``max_risk_contribution`` 等,读
  ``portfolio_config["overrides"]``,键缺省回退 ``strategy_spec.portfolio_policy``
  (``max_risk_contribution`` 缺省 0.35);
* ``risk_config`` 管**风险退出**(``RiskExitPolicy``):stop-loss / 止盈 /
  波动止损 / 持有期 / 回撤降风险 / 冷却,读 ``risk_config["overrides"]``,形态
  ``{"rules": [{"rule_type": "price_stop_loss", "enabled": true,
  "threshold": 0.08}, ...]}``,按 ``rule_type`` 与
  ``strategy_spec.risk_exit_policy`` 同名合并、overrides 覆盖同名键。

两个分区的键位不可混(组合约束键写进 risk_config 不生效,反之亦然)。
manifest 原样冻结(checksum 语义不变),merge / 覆盖只发生在消费端;运行期
(``PortfolioPipelineAdapter``)与入队预检(REST 422 / MCP invalid_argument)
共用本模块的解析函数,保证两边口径不漂移。所有非法配置 fail-closed:入队期
秒级拒绝,运行期经 ``ResearchConstraintViolationError`` 使 run REJECTED。
纯研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date

from pydantic import ValidationError

from finboard_backtest.portfolio.contracts import MAX_WEIGHT_EPSILON
from finboard_backtest.strategy_spec.contracts import RiskExitPolicy
from finboard_backtest.strategy_spec.universe_precheck import UniversePoolPreview

#: ``max_risk_contribution`` 的缺省值(``_constraints_from_manifest`` 既有默认,
#: 隐含买入池 n >= ceil(1/0.35) = 3)。
DEFAULT_MAX_RISK_CONTRIBUTION = 0.35

#: ``risk_config.overrides`` 唯一合法的顶层键。
_RISK_OVERRIDE_KEYS = frozenset({"rules"})

#: risk_config.overrides 的合法形态说明(错误消息共用,单一来源)。
_RISK_OVERRIDE_SHAPE = (
    'risk_config.overrides 合法形态: {"rules": [{"rule_type": "price_stop_loss", '
    '"enabled": true, "threshold": 0.08}, ...]} —— 按 rule_type 与 '
    "strategy_spec.risk_exit_policy 同名合并、overrides 覆盖同名键(未声明字段"
    "继承基准值;新 rule_type 须给全字段,启用止损类规则须给 threshold)。"
    "分区键位:组合约束(如 max_risk_contribution)在 portfolio_config,"
    "风险退出在 risk_config。"
)


def section_overrides(section: Mapping[str, object]) -> dict[str, object]:
    """manifest 配置分区 → overrides 子 dict(与既有 ``_section_overrides`` 同语义)。"""
    value = section.get("overrides", {})
    return dict(value) if isinstance(value, dict) else {}


def effective_max_risk_contribution(
    portfolio_overrides: Mapping[str, object],
) -> float:
    """解析生效的风险贡献硬约束上限(默认 0.35,overrides 同名键覆盖)。

    与 ``PortfolioPipelineAdapter._constraints_from_manifest`` 消费同一份
    overrides,入队预检与运行期口径一致。合法域 ``0 < 值 <= 1``(与
    ``PortfolioConstraints.__post_init__`` 一致;=1 表示不构成约束,builder
    不启用风险贡献投影);非法值(非数值 / bool / 越界)抛带键位与修复路径的
    ``ValueError`` —— 入队期秒级拒绝,运行期 fail-closed。
    """
    raw = portfolio_overrides.get("max_risk_contribution")
    if raw is None:
        return DEFAULT_MAX_RISK_CONTRIBUTION
    key = "portfolio_config.overrides['max_risk_contribution']"
    hint = (
        f"{key} 非法;合法域 0 < 值 <= 1(=1 表示不启用该约束),"
        f"未覆盖时默认 {DEFAULT_MAX_RISK_CONTRIBUTION:g}。"
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"{hint}得到 {type(raw).__name__} 类型的 {raw!r}。")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{hint}无法解析为数值: {raw!r}。") from exc
    if not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError(f"{hint}得到越界值 {raw!r}。")
    return value


def min_pool_for_risk_cap(threshold: float) -> int:
    """风险贡献上限隐含的最小买入池规模 ``ceil(1/threshold)``。

    ``risk_budget.enforce_risk_contribution_cap`` 在 ``threshold + tolerance <
    1/n`` 时抛 ``RiskBudgetError``(fail-closed),因此 long-only 组合至少需要
    ``n >= 1/threshold`` 只买入标的,静态不可行可在入队期预判。
    """
    return max(1, math.ceil(1.0 / threshold))


def merge_risk_exit_policy(
    base: RiskExitPolicy,
    risk_overrides: Mapping[str, object],
) -> RiskExitPolicy:
    """把 ``risk_config.overrides`` 合并进基准风险退出策略(issue #303)。

    覆盖语义:按 ``rule_type`` 同名合并,overrides 条目覆盖基准规则的同名字段
    (未声明的字段继承基准值);基准中不存在的 ``rule_type`` 视为新增规则,
    必须自含全部必填字段(经 ``RiskExitPolicy`` 模型校验 fail-closed,含
    unique rule_type 检查)。overrides 为空时原样返回基准(零行为变化)。
    同一 rule_type 在 overrides 内重复声明视为配置歧义,直接拒绝。
    非法形态 / 非法字段值抛带修复路径的 ``ValueError``。
    """
    if not risk_overrides:
        return base
    unknown = sorted(set(risk_overrides) - _RISK_OVERRIDE_KEYS)
    if unknown:
        raise ValueError(
            f"risk_config.overrides 含未知键 {unknown};{_RISK_OVERRIDE_SHAPE}"
        )
    rules_override = risk_overrides.get("rules", ())
    if not isinstance(rules_override, (list, tuple)):
        raise ValueError(
            "risk_config.overrides['rules'] 必须是规则对象列表,得到 "
            f"{type(rules_override).__name__};{_RISK_OVERRIDE_SHAPE}"
        )
    merged: dict[str, dict[str, object]] = {}
    for rule in base.rules:
        dump = {k: v for k, v in rule.model_dump().items() if v is not None}
        merged[str(dump["rule_type"])] = dump
    seen_types: set[str] = set()
    for index, item in enumerate(rules_override):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"risk_config.overrides['rules'][{index}] 必须是对象,得到 "
                f"{type(item).__name__};{_RISK_OVERRIDE_SHAPE}"
            )
        rule_type = item.get("rule_type")
        if not isinstance(rule_type, str) or not rule_type:
            raise ValueError(
                f"risk_config.overrides['rules'][{index}] 缺少合法 rule_type: "
                f"{item!r};{_RISK_OVERRIDE_SHAPE}"
            )
        if rule_type in seen_types:
            raise ValueError(
                f"risk_config.overrides['rules'] 中 rule_type={rule_type} 重复声明,"
                f"同一规则只允许覆盖一次;{_RISK_OVERRIDE_SHAPE}"
            )
        seen_types.add(rule_type)
        if rule_type in merged:
            fields = dict(merged[rule_type])
            fields.update({k: v for k, v in item.items() if k != "rule_type"})
            merged[rule_type] = fields
        else:
            merged[rule_type] = {"rule_type": rule_type, **dict(item)}
    try:
        return RiskExitPolicy.model_validate({"rules": list(merged.values())})
    except ValidationError as exc:
        raise ValueError(
            f"risk_config.overrides 合并后未通过风险退出策略校验: {exc};"
            f"{_RISK_OVERRIDE_SHAPE}"
        ) from exc


def research_portfolio_gate_error(
    *,
    preview: UniversePoolPreview,
    risk_exit_policy: RiskExitPolicy,
    portfolio_overrides: Mapping[str, object],
    risk_overrides: Mapping[str, object],
    decision_date: date,
) -> str | None:
    """入队期组合可行性预检:返回拒绝原因,放行返回 None(issue #303)。

    REST(``POST /api/research/runs`` → 422)与 MCP(``finboard_run_queue`` /
    ``finboard_backtest_run`` strategy 形态 → invalid_argument)共用本门控:

    1. ``portfolio_config.overrides['max_risk_contribution']`` 非法值入队即拒;
    2. ``risk_config.overrides`` 形态 / 字段非法入队即拒(按规格的真实基准
       策略重放运行期同一 merge,否则组合阶段才 fail-closed,浪费整轮执行);
    3. 静态候选池可评估时(``total_candidates > 0``,与 #186 空池门控同口径),
       生效阈值 < 1 且 ``preview.included < ceil(1/阈值)`` → 秒级拒绝,附排除
       统计、生效阈值与键位修复路径 —— 组合阶段 ``RiskBudgetError`` 数学不可行
       会让整个 run 执行完才 REJECTED。逐期真实买入池入队期不可精确预知
       (信号只买子集、每期选股池变化),运行期 fail-closed 兜底保持不变
       (#91);池不可静态评估(发布无 instruments)时不在此拦截。
    """
    try:
        threshold = effective_max_risk_contribution(portfolio_overrides)
    except ValueError as exc:
        return str(exc)
    try:
        merge_risk_exit_policy(risk_exit_policy, risk_overrides)
    except ValueError as exc:
        return str(exc)
    if preview.total_candidates == 0:
        return None
    if threshold >= 1.0 - MAX_WEIGHT_EPSILON:
        # 与 builder 同口径:=1 不启用风险贡献投影,不构成买入池规模约束。
        return None
    required = min_pool_for_risk_cap(threshold)
    if preview.included >= required:
        return None
    stats = "、".join(
        f"{reason}={count}"
        for reason, count in sorted(preview.excluded_by_condition.items())
    )
    return (
        "入队预检失败:组合阶段风险贡献硬约束在当前候选池下数学不可行 —— 生效 "
        f"max_risk_contribution={threshold:g}(键位 "
        f"portfolio_config.overrides['max_risk_contribution'],未覆盖时默认 "
        f"{DEFAULT_MAX_RISK_CONTRIBUTION:g})要求买入池 >= "
        f"ceil(1/{threshold:g})={required} 只,而静态候选池仅 {preview.included} 只"
        f"(决策日 {decision_date.isoformat()};排除统计: {stats or '无'})。"
        "运行到组合阶段会 RiskBudgetError fail-closed、整个 run REJECTED"
        "(hard_constraint_rejected)。修复:放宽 universe 过滤扩大候选池,或经 "
        "portfolio_config.overrides 调高 max_risk_contribution(合法域 "
        "0 < 值 <= 1,=1 关闭该约束)。逐期真实买入池入队期不可精确预知,"
        "运行期 fail-closed 兜底保持不变。"
    )


__all__ = [
    "DEFAULT_MAX_RISK_CONTRIBUTION",
    "effective_max_risk_contribution",
    "merge_risk_exit_policy",
    "min_pool_for_risk_cap",
    "research_portfolio_gate_error",
    "section_overrides",
]
