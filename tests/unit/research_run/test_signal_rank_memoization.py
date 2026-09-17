"""信号排名类规则的横截面排名每期每特征只算一次(2026-09-13 决策段热点)。

py-spy 采样 556 期全市场 replay 的决策段:``_rank_ratios`` 以 22.2% 的
CPU 占比位居第二 —— 根因是 ``_rule_matches`` 在 RANK_TOP / RANK_BOTTOM
分支里**逐标的**调用 ``_rank_ratios(finals[feature_id])``,每次都把整个
截面重排一遍(O(n² log n):n≈200 标的 x 每期 2 条排名规则 ≈ 400 次全排序/
期)。排名只依赖当期 ``node_finals``,按 feature_id 预计算一次即可,值与
逐标的重算逐位相同(同一函数、同一输入)。

本用例锁定「每特征每期恰一次」这一性能契约(值与语义由既有排名测试覆盖)。
纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from finboard_backtest.research_run import signal_engine as signal_engine_module
from finboard_backtest.research_run.signal_engine import evaluate_signal_rules
from finboard_backtest.strategy_spec import (
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
    build_strategy_template,
)


def _spec_with_rules(*rules: SignalRule):
    spec = build_strategy_template(
        "multi_factor", strategy_id="rank-memo", dataset_release_ids=("release-v1",)
    )
    return spec.model_copy(
        update={
            "signal_rules": SignalRules(
                rules=rules,
                conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
                default_action=SignalAction.NEUTRAL,
            )
        }
    )


def _rule(rule_id: str, comparator: SignalComparator) -> SignalRule:
    return SignalRule(
        rule_id=rule_id,
        feature_id="momentum",
        comparator=comparator,
        action=SignalAction.BUY,
        threshold=0.3,
        rationale="排名规则",
    )


def test_rank_rules_compute_cross_section_rank_once_per_feature(monkeypatch) -> None:
    """两个排名规则共享同一特征:截面排名恰算一次(而非每标的各一次)。"""
    calls: list[int] = []
    original = signal_engine_module._rank_ratios

    def counting(values: dict[str, float]) -> dict[str, float]:
        calls.append(len(values))
        return original(values)

    monkeypatch.setattr(signal_engine_module, "_rank_ratios", counting)
    symbols = {f"S{index:03d}.SZ": 3.0 - index * 0.01 for index in range(120)}
    spec = _spec_with_rules(
        _rule("top_buy", SignalComparator.RANK_TOP),
        _rule("bottom_buy", SignalComparator.RANK_BOTTOM),
    )

    signals = evaluate_signal_rules(
        spec,
        node_finals={"momentum": dict(symbols)},
        node_series={},
        included_symbols=frozenset(symbols),
        factor_snapshot_id=None,
    )

    assert signals
    # 两个规则同一特征 → 恰一次;旧实现为 120(标的数) x 2(规则)= 240 次。
    assert calls == [len(symbols)]


def test_rank_rules_distinct_features_compute_once_each(monkeypatch) -> None:
    """不同特征各算一次(缓存按 feature_id 分键,不串味)。"""
    calls: list[str] = []
    original = signal_engine_module._rank_ratios

    def counting(values: dict[str, float]) -> dict[str, float]:
        calls.append("call")
        return original(values)

    monkeypatch.setattr(signal_engine_module, "_rank_ratios", counting)
    symbols = {f"S{index:03d}.SZ": 3.0 - index * 0.01 for index in range(50)}
    first = _rule("top_buy", SignalComparator.RANK_TOP)
    second = SignalRule(
        rule_id="top_vol",
        feature_id="volatility_20d",
        comparator=SignalComparator.RANK_TOP,
        action=SignalAction.BUY,
        threshold=0.3,
        rationale="波动率排名",
    )
    spec = _spec_with_rules(first, second)

    evaluate_signal_rules(
        spec,
        node_finals={"momentum": dict(symbols), "volatility_20d": dict(symbols)},
        node_series={},
        included_symbols=frozenset(symbols),
        factor_snapshot_id=None,
    )

    assert calls == ["call", "call"]


def test_non_rank_rules_do_not_touch_rank_cache(monkeypatch) -> None:
    """非排名规则(阈值比较)不触发截面排名计算。"""
    calls: list[int] = []
    original = signal_engine_module._rank_ratios

    def counting(values: dict[str, float]) -> dict[str, float]:
        calls.append(len(values))
        return original(values)

    monkeypatch.setattr(signal_engine_module, "_rank_ratios", counting)
    symbols = {f"S{index:03d}.SZ": 3.0 - index * 0.01 for index in range(30)}
    spec = _spec_with_rules(
        SignalRule(
            rule_id="threshold_buy",
            feature_id="momentum",
            comparator=SignalComparator.GREATER_THAN,
            action=SignalAction.BUY,
            threshold=2.0,
            rationale="阈值规则",
        )
    )

    signals = evaluate_signal_rules(
        spec,
        node_finals={"momentum": dict(symbols)},
        node_series={},
        included_symbols=frozenset(symbols),
        factor_snapshot_id=None,
    )

    assert signals
    assert calls == []
