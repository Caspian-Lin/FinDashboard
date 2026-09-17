"""无代码研究策略契约、注册表和安全边界。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from finboard_backtest.strategy_spec import (
    LIFECYCLE_STAGES,
    ResearchStrategySpec,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
    list_strategy_capabilities,
    migrate_strategy_payload,
    structured_diff,
)


def _template(kind: str = "multi_factor") -> ResearchStrategySpec:
    return build_strategy_template(
        kind,
        strategy_id=f"test_{kind}",
        dataset_release_ids=("frozen-release-v1",),
    )


def test_registry_adapts_issue_60_to_64_and_ma_cross() -> None:
    capabilities = list_strategy_capabilities()
    assert {item.kind for item in capabilities} == {
        "multi_factor",
        "etf_rotation",
        "mean_reversion",
        "convertible_double_low",
        "futures_tsmom",
        "ma_cross",
        # issue #218:沙箱策略代码(decide → 目标权重)。
        "user_code",
    }
    assert {item.issue for item in capabilities if 60 <= item.issue <= 64} == set(
        range(60, 65)
    )

    for capability in capabilities:
        if not capability.supports_no_code_template:
            # user_code 无 web 无代码模板(代码本体走 #215 MCP 通道)。
            assert capability.kind == "user_code"
            continue
        spec = _template(capability.kind)
        plan = compile_registered_strategy_spec(spec)
        assert spec.compatibility is not None
        assert plan.can_execute is False
        assert plan.lifecycle_stages == LIFECYCLE_STAGES
        assert plan.feature_order
        assert plan.dataset_release_ids == ("frozen-release-v1",)
        assert spec.portfolio_policy.constraint_refs
        assert all(rule.rationale for rule in spec.risk_exit_policy.rules)


def test_factor_graph_rejects_cycle_unknown_reference_and_illegal_operator() -> None:
    payload = _template("ma_cross").canonical_payload()
    payload["feature_graph"]["nodes"][0] = {
        "node_id": "close",
        "label": "循环节点",
        "kind": "transform",
        "operator": "sma",
        "inputs": ["ma_short"],
        "window": 2,
        "weights": [],
        "source": None,
        "lower_percentile": None,
        "upper_percentile": None,
        "lag_bars": 0,
    }
    with pytest.raises(ValidationError, match="循环依赖"):
        ResearchStrategySpec.model_validate(payload)

    payload = _template().canonical_payload()
    payload["signal_rules"]["rules"][0]["feature_id"] = "missing_feature"
    with pytest.raises(ValidationError, match="未知特征"):
        ResearchStrategySpec.model_validate(payload)

    payload = _template().canonical_payload()
    payload["feature_graph"]["nodes"][0]["operator"] = "user_expression"
    with pytest.raises(ValidationError, match="user_expression"):
        ResearchStrategySpec.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("python_code",), "print('trade')"),
        (("module_path",), "strategies.alpha"),
        (("description",), "__import__('os').system('id')"),
        (("description",), "{{ dangerous_template }}"),
        (("description",), "/tmp/strategy.py"),
    ],
)
def test_spec_rejects_code_import_template_and_paths(
    path: tuple[str, ...],
    value: str,
) -> None:
    payload = _template().canonical_payload()
    payload[path[0]] = value
    with pytest.raises((ValidationError, StrategySpecError, ValueError)):
        ResearchStrategySpec.model_validate(payload)


def test_compile_fails_for_disabled_factor_and_missing_dataset_release() -> None:
    spec = _template("multi_factor")
    with pytest.raises(StrategySpecError, match="已停用因子"):
        compile_registered_strategy_spec(
            spec,
            disabled_factors=frozenset({"momentum"}),
        )
    with pytest.raises(StrategySpecError, match="历史数据发布"):
        compile_registered_strategy_spec(
            spec,
            available_dataset_release_ids=frozenset({"another-release"}),
        )


def test_v0_migration_and_structured_diff_are_data_only() -> None:
    original = _template()
    legacy = original.canonical_payload()
    legacy.pop("schema_version")
    legacy["risk_policy"] = legacy.pop("risk_exit_policy")
    migrated = migrate_strategy_payload(legacy)
    restored = ResearchStrategySpec.model_validate(migrated)
    assert restored == original

    changed_payload = deepcopy(original.canonical_payload())
    changed_payload["portfolio_policy"]["max_target_weight"] = 0.05
    changed = ResearchStrategySpec.model_validate(changed_payload)
    diff = structured_diff(original, changed)
    assert [item.path for item in diff] == [
        "$.portfolio_policy.max_target_weight"
    ]


def test_factor_signal_target_position_chain_is_explicit_but_has_no_order_fields() -> None:
    spec = _template("etf_rotation")
    payload = spec.canonical_payload()
    assert payload["feature_graph"]["outputs"] == ["trend", "momentum"]
    assert {rule["action"] for rule in payload["signal_rules"]["rules"]} == {
        "buy",
        "sell",
    }
    assert payload["portfolio_policy"]["allocation_method"] == "inverse_volatility"
    assert "broker" not in str(payload).lower()
    assert "client_order_id" not in str(payload)
    assert payload["execution_model"]["reject_same_bar_fill"] is True
