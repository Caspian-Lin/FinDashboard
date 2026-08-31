"""screen 用途 draft 产物显式绑定的编译期与冻结单测(issue #234)。

覆盖验收:
* 绑定声明的 draft 用户因子/策略名并入**本规格**可引用名单(编译放行);
* 未声明绑定的普通规格引用 draft 名单外因子仍被拒绝(引用门不变);
* 绑定了未被 feature_graph 引用的因子 → 报错(引用完整性);
* 结构校验:重复绑定 / strategy 绑定与 code_artifact.name 不一致 /
  strategy 绑定落在非 user_code 规格 → 一律拒绝;
* ``freeze_user_code_commit`` 把实绑 commit + artifact_id 冻结进
  code_artifact;普通入队(不传 artifact_ids)行为与 #218 完全一致。
"""

from __future__ import annotations

import pytest

from finboard_backtest.research_code import freeze_user_code_commit
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
)

FACTOR_NAME = "agent_alpha"
U_FACTOR = f"u_{FACTOR_NAME}"
ARTIFACT_ID = "RC-" + "a" * 24
COMMIT = "c" * 40


def _factor_screen_spec() -> ResearchStrategySpec:
    """multi_factor 模板把 pb 节点换成 u_ 用户因子,并声明 screen 绑定。"""
    payload = build_strategy_template(
        "multi_factor",
        strategy_id="test_screen_factor",
        dataset_release_ids=("frozen-release-v1",),
    ).canonical_payload()
    for node in payload["feature_graph"]["nodes"]:
        if node.get("source") == "pb":
            node["source"] = U_FACTOR
            break
    else:  # pragma: no cover - 模板变化时防御
        raise AssertionError("multi_factor 模板应含 source=pb 的节点")
    payload["screen_artifact_bindings"] = [
        {
            "kind": "factor",
            "name": FACTOR_NAME,
            "artifact_id": ARTIFACT_ID,
            "commit": COMMIT,
        }
    ]
    return ResearchStrategySpec.model_validate(payload)


def _user_code_screen_spec(
    *,
    binding_name: str = "agent_strategy",
    code_name: str = "agent_strategy",
) -> ResearchStrategySpec:
    """multi_factor 模板改造为 user_code(空 graph/rules)+ screen 绑定。

    经 canonical payload + model_validate 构造,交叉校验器会真实执行
    (model_copy 不跑校验器)。
    """
    payload = build_strategy_template(
        "multi_factor",
        strategy_id="test_screen_strategy",
        dataset_release_ids=("frozen-release-v1",),
    ).canonical_payload()
    payload["strategy_kind"] = "user_code"
    payload["feature_graph"] = {"nodes": [], "outputs": []}
    payload["signal_rules"] = {"rules": []}
    payload["code_artifact"] = {"name": code_name}
    payload["screen_artifact_bindings"] = [
        {
            "kind": "strategy",
            "name": binding_name,
            "artifact_id": "RC-" + "b" * 24,
        }
    ]
    return ResearchStrategySpec.model_validate(payload)


class TestCompileScreenBinding:
    def test_bound_draft_factor_compiles(self) -> None:
        """绑定声明的 draft 用户因子编译放行(active 名单为空)。"""
        spec = _factor_screen_spec()
        plan = compile_registered_strategy_spec(spec, user_factor_sources=frozenset())
        assert U_FACTOR in plan.required_factor_sources

    def test_unbound_draft_factor_still_rejected(self) -> None:
        """不声明绑定的普通规格引用 draft 名单外因子仍被拒绝(引用门不变)。"""
        payload = _factor_screen_spec().canonical_payload()
        payload.pop("screen_artifact_bindings")
        spec = ResearchStrategySpec.model_validate(payload)
        with pytest.raises(StrategySpecError, match="screen_artifact_bindings"):
            compile_registered_strategy_spec(spec, user_factor_sources=frozenset())

    def test_binding_without_reference_rejected(self) -> None:
        """绑定了未被 feature_graph 引用的因子 → 报错(引用完整性)。"""
        payload = _factor_screen_spec().canonical_payload()
        for node in payload["feature_graph"]["nodes"]:
            if node.get("source") == U_FACTOR:
                node["source"] = "pb"
                break
        spec = ResearchStrategySpec.model_validate(payload)
        with pytest.raises(StrategySpecError, match="未被 feature_graph 引用"):
            compile_registered_strategy_spec(spec)

    def test_bound_draft_strategy_compiles(self) -> None:
        """绑定声明的 draft 策略 artifact 编译放行(active 名单为空)。"""
        spec = _user_code_screen_spec()
        # 编译通过(未抛 StrategySpecError)即绑定放行生效。
        compile_registered_strategy_spec(spec, user_code_sources=frozenset())

    def test_unbound_draft_strategy_still_rejected(self) -> None:
        payload = _user_code_screen_spec().canonical_payload()
        payload.pop("screen_artifact_bindings")
        spec = ResearchStrategySpec.model_validate(payload)
        with pytest.raises(StrategySpecError, match="screen_artifact_bindings"):
            compile_registered_strategy_spec(spec, user_code_sources=frozenset())


class TestScreenBindingContracts:
    def test_duplicate_binding_rejected(self) -> None:
        payload = _factor_screen_spec().canonical_payload()
        payload["screen_artifact_bindings"].append(
            {"kind": "factor", "name": FACTOR_NAME, "artifact_id": "RC-" + "c" * 24}
        )
        with pytest.raises(ValueError, match="重复绑定"):
            ResearchStrategySpec.model_validate(payload)

    def test_duplicate_artifact_id_rejected(self) -> None:
        payload = _factor_screen_spec().canonical_payload()
        payload["screen_artifact_bindings"].append(
            {"kind": "factor", "name": "other_alpha", "artifact_id": ARTIFACT_ID}
        )
        with pytest.raises(ValueError, match="重复绑定 artifact"):
            ResearchStrategySpec.model_validate(payload)

    def test_strategy_binding_name_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="不一致"):
            _user_code_screen_spec(binding_name="other_strategy")

    def test_strategy_binding_requires_user_code(self) -> None:
        payload = build_strategy_template(
            "multi_factor",
            strategy_id="test_screen_wrong_kind",
            dataset_release_ids=("frozen-release-v1",),
        ).canonical_payload()
        payload["screen_artifact_bindings"] = [
            {
                "kind": "strategy",
                "name": "agent_strategy",
                "artifact_id": "RC-" + "b" * 24,
            }
        ]
        with pytest.raises(ValueError, match="user_code"):
            ResearchStrategySpec.model_validate(payload)


class TestFreezeUserCodeCommit:
    def _spec(self, **ref_kwargs: object) -> ResearchStrategySpec:
        payload = _user_code_screen_spec().canonical_payload()
        payload["code_artifact"] = {"name": "agent_strategy", **ref_kwargs}
        return ResearchStrategySpec.model_validate(payload)

    def test_freeze_commit_and_artifact_id(self) -> None:
        spec = self._spec()
        frozen = freeze_user_code_commit(
            spec,
            {"agent_strategy": COMMIT},
            artifact_ids={"agent_strategy": "RC-" + "b" * 24},
        )
        assert frozen.code_artifact is not None
        assert frozen.code_artifact.commit == COMMIT
        assert frozen.code_artifact.artifact_id == "RC-" + "b" * 24

    def test_freeze_without_artifact_ids_keeps_legacy_behavior(self) -> None:
        spec = self._spec()
        frozen = freeze_user_code_commit(spec, {"agent_strategy": COMMIT})
        assert frozen.code_artifact is not None
        assert frozen.code_artifact.commit == COMMIT
        assert frozen.code_artifact.artifact_id is None

    def test_declared_commit_not_overwritten(self) -> None:
        spec = self._spec(commit=COMMIT)
        other = "d" * 40
        frozen = freeze_user_code_commit(
            spec,
            {"agent_strategy": other},
            artifact_ids={"agent_strategy": "RC-" + "b" * 24},
        )
        assert frozen.code_artifact is not None
        assert frozen.code_artifact.commit == COMMIT
        assert frozen.code_artifact.artifact_id == "RC-" + "b" * 24

    def test_freeze_artifact_id_idempotent(self) -> None:
        spec = self._spec(commit=COMMIT, artifact_id="RC-" + "b" * 24)
        frozen = freeze_user_code_commit(
            spec,
            {"agent_strategy": COMMIT},
            artifact_ids={"agent_strategy": "RC-" + "b" * 24},
        )
        assert frozen is spec
