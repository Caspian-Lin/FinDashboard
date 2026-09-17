"""``user_code`` 策略的 schema 交叉校验、编译期门控与入队门控(issue #218)。"""

from __future__ import annotations

import pytest

from finboard_backtest.research_code import (
    freeze_user_code_commit,
    user_code_reference_gate_error,
)
from finboard_backtest.research_run.signal_engine import (
    single_shot_snapshot_gate_error,
)
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
)


def _user_code_spec(commit: str | None = None) -> ResearchStrategySpec:
    """multi_factor 模板改造为 user_code:清空 graph/rules + 挂 code_artifact。"""
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        SignalRules,
        StrategyCodeArtifactRef,
    )

    template = build_strategy_template(
        "multi_factor",
        strategy_id="test_user_code",
        dataset_release_ids=("frozen-release-v1",),
    )
    ref = StrategyCodeArtifactRef(name="mean_reversion_zscore")
    frozen = ref.model_copy(update={"commit": commit}) if commit else ref
    return template.model_copy(
        update={
            "strategy_kind": "user_code",
            "feature_graph": FeatureGraph(nodes=(), outputs=()),
            "signal_rules": SignalRules(rules=()),
            "code_artifact": frozen,
        }
    )


class TestSchemaCrossValidation:
    def test_user_code_requires_code_artifact(self) -> None:
        spec = _user_code_spec()
        payload = spec.canonical_payload()
        del payload["code_artifact"]
        with pytest.raises(Exception, match="code_artifact"):
            ResearchStrategySpec.model_validate(payload)

    def test_non_user_code_rejects_code_artifact(self) -> None:
        spec = _user_code_spec()
        payload = spec.canonical_payload()
        payload["strategy_kind"] = "multi_factor"
        # multi_factor 空图本就非法;先恢复模板的图与规则以隔离变量
        template = build_strategy_template(
            "multi_factor",
            strategy_id="test_user_code",
            dataset_release_ids=("frozen-release-v1",),
        ).canonical_payload()
        payload["feature_graph"] = template["feature_graph"]
        payload["signal_rules"] = template["signal_rules"]
        with pytest.raises(Exception, match="仅用于 user_code"):
            ResearchStrategySpec.model_validate(payload)

    def test_other_kinds_still_require_nonempty_graph(self) -> None:
        template = build_strategy_template(
            "multi_factor",
            strategy_id="test_graph",
            dataset_release_ids=("frozen-release-v1",),
        ).canonical_payload()
        template["feature_graph"]["nodes"] = []
        template["feature_graph"]["outputs"] = []
        with pytest.raises(Exception, match="不能为空"):
            ResearchStrategySpec.model_validate(template)

    def test_reject_executable_payload_still_applies(self) -> None:
        """无代码边界不变:code_artifact 只是引用,携带可执行形态仍被拒。"""
        from finboard_backtest.strategy_spec.contracts import (
            StrategySpecError,
            reject_executable_payload,
        )

        with pytest.raises(StrategySpecError):
            reject_executable_payload(
                {"code_artifact": {"name": "x", "module": "strategy.py"}}
            )


class TestCompileGate:
    def test_compile_ok_with_active_source(self) -> None:
        plan = compile_registered_strategy_spec(
            _user_code_spec(), user_code_sources={"mean_reversion_zscore"}
        )
        assert plan.spec.strategy_kind == "user_code"
        assert plan.feature_order == ()
        assert plan.required_factor_sources == ()

    def test_compile_rejects_inactive_artifact(self) -> None:
        with pytest.raises(StrategySpecError, match="不可用"):
            compile_registered_strategy_spec(
                _user_code_spec(), user_code_sources={"other_strategy"}
            )

    def test_compile_rejects_when_sources_not_provided(self) -> None:
        with pytest.raises(StrategySpecError, match="不可用"):
            compile_registered_strategy_spec(_user_code_spec())


class TestEnqueueGate:
    def test_passes_for_multi_period_active_artifact(self) -> None:
        assert (
            user_code_reference_gate_error(
                code_artifact_name="mr",
                code_artifact_commit=None,
                active_user_strategies={"mr": "c" * 40},
                sandbox_enabled=True,
                frozen_snapshot_count=0,
                parameters={"rebalance_frequency": "monthly"},
            )
            is None
        )

    def test_rejects_when_sandbox_disabled(self) -> None:
        error = user_code_reference_gate_error(
            code_artifact_name="mr",
            code_artifact_commit=None,
            active_user_strategies={"mr": "c" * 40},
            sandbox_enabled=False,
            frozen_snapshot_count=0,
            parameters={"rebalance_frequency": "monthly"},
        )
        assert error is not None
        assert "沙箱" in error

    def test_rejects_inactive_artifact(self) -> None:
        error = user_code_reference_gate_error(
            code_artifact_name="mr",
            code_artifact_commit=None,
            active_user_strategies={},
            sandbox_enabled=True,
            frozen_snapshot_count=0,
            parameters={"rebalance_frequency": "monthly"},
        )
        assert error is not None
        assert "rollback" in error

    def test_rejects_commit_mismatch(self) -> None:
        error = user_code_reference_gate_error(
            code_artifact_name="mr",
            code_artifact_commit="d" * 40,
            active_user_strategies={"mr": "c" * 40},
            sandbox_enabled=True,
            frozen_snapshot_count=0,
            parameters={"rebalance_frequency": "monthly"},
        )
        assert error is not None
        assert "active" in error

    def test_rejects_single_shot_without_snapshots(self) -> None:
        error = user_code_reference_gate_error(
            code_artifact_name="mr",
            code_artifact_commit=None,
            active_user_strategies={"mr": "c" * 40},
            sandbox_enabled=True,
            frozen_snapshot_count=0,
            parameters=None,
        )
        assert error is not None
        assert "single_shot" in error

    def test_single_shot_with_snapshots_passes(self) -> None:
        assert (
            user_code_reference_gate_error(
                code_artifact_name="mr",
                code_artifact_commit=None,
                active_user_strategies={"mr": "c" * 40},
                sandbox_enabled=True,
                frozen_snapshot_count=1,
                parameters=None,
            )
            is None
        )

    def test_non_user_code_passes_through(self) -> None:
        assert (
            user_code_reference_gate_error(
                code_artifact_name=None,
                code_artifact_commit=None,
                active_user_strategies={},
                sandbox_enabled=False,
                frozen_snapshot_count=0,
                parameters=None,
            )
            is None
        )

    def test_snapshot_gate_covers_user_code(self) -> None:
        error = single_shot_snapshot_gate_error(
            strategy_kind="user_code",
            required_factor_sources=[],
            frozen_snapshot_count=0,
            parameters={},
        )
        assert error is not None
        assert "user_code" in error

    def test_freeze_user_code_commit(self) -> None:
        spec = _user_code_spec()
        frozen = freeze_user_code_commit(spec, {"mean_reversion_zscore": "a" * 40})
        assert frozen.code_artifact is not None
        assert frozen.code_artifact.commit == "a" * 40
        # 已冻结的不重复覆盖
        again = freeze_user_code_commit(frozen, {"mean_reversion_zscore": "b" * 40})
        assert again.code_artifact.commit == "a" * 40
