"""白名单验证器测试 —— 越权代码 / 未知因子 / 未来数据 / 参数爆炸。"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    ALLOWED_FIELDS,
    ALLOWED_OPERATORS,
    FactorHypothesis,
    HypothesisValidationResult,
    HypothesisValidator,
    ParameterSpec,
)


def _make_hypothesis(**overrides: object) -> FactorHypothesis:
    defaults: dict[str, object] = {
        "name": "test",
        "economic_mechanism": "mechanism",
        "input_fields": ("close", "volume"),
        "decision_timing": "close",
        "formula": "rank(ts_mean(close, 20))",
        "direction": "long_high",
    }
    defaults.update(overrides)
    return FactorHypothesis(**defaults)  # type: ignore[arg-type]


class TestWhitelist:
    def test_allowed_fields_non_empty(self) -> None:
        assert len(ALLOWED_FIELDS) > 20
        assert "close" in ALLOWED_FIELDS
        assert "volume" in ALLOWED_FIELDS

    def test_allowed_operators_non_empty(self) -> None:
        assert len(ALLOWED_OPERATORS) > 20
        assert "rank" in ALLOWED_OPERATORS
        assert "ts_mean" in ALLOWED_OPERATORS

    def test_no_broker_keywords_in_fields(self) -> None:
        for f in ALLOWED_FIELDS:
            assert "broker" not in f.lower()
            assert "order" not in f.lower()

    def test_no_dangerous_operators(self) -> None:
        for op in ALLOWED_OPERATORS:
            assert op not in {"exec", "eval", "import", "open", "compile"}


class TestValidatorBasics:
    def test_valid_hypothesis_passes(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis())
        assert result.is_valid
        assert len(result.errors) == 0

    def test_unknown_field_rejected(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis(input_fields=("close", "unknown_field")))
        assert not result.is_valid
        assert any("unknown_field" in e for e in result.errors)

    def test_unknown_operator_rejected(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis(formula="rank(unknown_op(close, 20))"))
        assert not result.is_valid
        assert any("unknown_op" in e for e in result.errors)

    def test_invalid_timing_rejected(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis(decision_timing="future_close"))
        assert not result.is_valid
        assert any("决策时点" in e for e in result.errors)

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            _make_hypothesis(direction="invalid_dir")


class TestDangerousCode:
    @pytest.mark.parametrize(
        "formula",
        [
            "import os",
            "exec(rank(close))",
            "eval('print(1)')",
            "__import__('os')",
            "open('/etc/passwd')",
            "subprocess.run(['ls'])",
            "os.system('rm -rf /')",
            "requests.get('http://evil.com')",
            "SELECT * FROM accounts",
            "INSERT INTO orders VALUES(...)",
            "DROP TABLE positions",
            "DELETE FROM trades",
            "shell('ls')",
            "globals()['x']",
            "pickle.load(f)",
            "broker.send_order()",
            "order_manager.place()",
            "kill_switch.disable()",
        ],
    )
    def test_dangerous_patterns_rejected(self, formula: str) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis(formula=formula))
        assert not result.is_valid
        assert any("危险" in e or "未知" in e for e in result.errors)


class TestParameterBudget:
    def test_budget_within_limit(self) -> None:
        v = HypothesisValidator(max_parameter_budget=100)
        params = tuple(
            ParameterSpec(f"p{i}", 1.0, 10.0, grid_size=3)
            for i in range(4)
        )
        result = v.validate(_make_hypothesis(parameters=params))
        assert result.is_valid

    def test_budget_exceeds_limit(self) -> None:
        v = HypothesisValidator(max_parameter_budget=50)
        params = tuple(
            ParameterSpec(f"p{i}", 1.0, 10.0, grid_size=3)
            for i in range(4)
        )
        result = v.validate(_make_hypothesis(parameters=params))
        assert not result.is_valid
        assert any("参数预算" in e for e in result.errors)

    def test_budget_warning(self) -> None:
        v = HypothesisValidator(max_parameter_budget=10000)
        params = tuple(
            ParameterSpec(f"p{i}", 1.0, 10.0, grid_size=4)
            for i in range(4)
        )
        result = v.validate(_make_hypothesis(parameters=params))
        assert result.is_valid
        assert any("参数预算较高" in w for w in result.warnings)


class TestValidationResult:
    def test_valid_with_no_errors(self) -> None:
        r = HypothesisValidationResult(is_valid=True)
        assert r.passed

    def test_invalid_with_errors_raises(self) -> None:
        with pytest.raises(ValueError, match="is_valid=True"):
            HypothesisValidationResult(
                is_valid=True,
                errors=("should not be here",),
            )

    def test_invalid_with_errors_ok(self) -> None:
        r = HypothesisValidationResult(
            is_valid=False,
            errors=("bad",),
        )
        assert not r.passed


class TestMissingReferences:
    def test_no_references_warning(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis())
        assert any("文献" in w for w in result.warnings)

    def test_no_failure_scenarios_warning(self) -> None:
        v = HypothesisValidator()
        result = v.validate(_make_hypothesis())
        assert any("失效场景" in w for w in result.warnings)
