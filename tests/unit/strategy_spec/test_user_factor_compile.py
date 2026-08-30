"""用户自定义因子(u_ 前缀)的编译期校验与入队门控(issue #217)。"""

from __future__ import annotations

import pytest

from finboard_backtest.research_code import user_factor_reference_gate_error
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
)


def _spec_with_user_factor() -> ResearchStrategySpec:
    """multi_factor 模板把第一个因子节点换成 u_ 用户因子。"""

    payload = build_strategy_template(
        "multi_factor",
        strategy_id="test_user_factor",
        dataset_release_ids=("frozen-release-v1",),
    ).canonical_payload()
    for node in payload["feature_graph"]["nodes"]:
        if node.get("source") == "pb":
            node["source"] = "u_agent_alpha"
            break
    else:  # pragma: no cover - 模板变化时防御
        raise AssertionError("multi_factor 模板应含 source=pb 的节点")
    return ResearchStrategySpec.model_validate(payload)


class TestCompileUserFactor:
    def test_active_user_factor_compiles(self) -> None:
        spec = _spec_with_user_factor()
        plan = compile_registered_strategy_spec(
            spec, user_factor_sources=frozenset({"u_agent_alpha"})
        )
        assert "u_agent_alpha" in plan.required_factor_sources

    def test_inactive_user_factor_rejected(self) -> None:
        spec = _spec_with_user_factor()
        with pytest.raises(StrategySpecError, match="用户因子不可引用"):
            compile_registered_strategy_spec(spec)
        with pytest.raises(StrategySpecError, match="status=active"):
            compile_registered_strategy_spec(
                spec, user_factor_sources=frozenset({"u_other"})
            )

    def test_user_factor_kind_must_be_factor(self) -> None:
        payload = _spec_with_user_factor().canonical_payload()
        for node in payload["feature_graph"]["nodes"]:
            if node.get("source") == "u_agent_alpha":
                node["kind"] = "market_input"
                break
        spec = ResearchStrategySpec.model_validate(payload)
        with pytest.raises(StrategySpecError, match="kind 须为 factor"):
            compile_registered_strategy_spec(
                spec, user_factor_sources=frozenset({"u_agent_alpha"})
            )

    def test_builtin_path_unaffected(self) -> None:
        spec = build_strategy_template(
            "multi_factor",
            strategy_id="test_builtin",
            dataset_release_ids=("frozen-release-v1",),
        )
        plan = compile_registered_strategy_spec(spec)
        assert "pb" in plan.required_factor_sources


class TestEnqueueGate:
    def test_no_user_factor_passes(self) -> None:
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"pb", "momentum"},
                active_user_factors=frozenset(),
                parameters=None,
            )
            is None
        )

    def test_retired_user_factor_rejected(self) -> None:
        error = user_factor_reference_gate_error(
            required_factor_sources={"pb", "u_agent_alpha"},
            active_user_factors=frozenset({"u_other"}),
            parameters=None,
        )
        assert error is not None
        assert "u_agent_alpha" in error
        assert "非 active" in error

    def test_multi_period_user_factor_rejected(self) -> None:
        error = user_factor_reference_gate_error(
            required_factor_sources={"u_agent_alpha"},
            active_user_factors=frozenset({"u_agent_alpha"}),
            parameters={"rebalance_frequency": "monthly"},
        )
        assert error is not None
        assert "multi_period" in error
        assert "single_shot" in error

    def test_active_user_factor_single_shot_passes(self) -> None:
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_agent_alpha"},
                active_user_factors=frozenset({"u_agent_alpha"}),
                parameters=None,
            )
            is None
        )
