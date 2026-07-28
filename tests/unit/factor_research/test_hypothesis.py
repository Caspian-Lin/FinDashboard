"""FactorHypothesis 数据模型与状态机测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    FactorHypothesis,
    HypothesisStatus,
    ParameterSpec,
    Reference,
)


class TestParameterSpec:
    def test_basic_creation(self) -> None:
        p = ParameterSpec("lookback", 5.0, 60.0, grid_size=5)
        assert p.name == "lookback"
        assert p.grid_size == 5

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name"):
            ParameterSpec("", 0.0, 1.0)

    def test_zero_grid_rejected(self) -> None:
        with pytest.raises(ValueError, match="grid_size"):
            ParameterSpec("x", 0.0, 1.0, grid_size=0)

    def test_min_greater_than_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_value"):
            ParameterSpec("x", 10.0, 5.0)

    def test_default_grid_size(self) -> None:
        p = ParameterSpec("x", 0.0, 1.0)
        assert p.grid_size == 1


class TestReference:
    def test_basic_creation(self) -> None:
        ref = Reference(title="Test Paper", authors="Author", year=2023)
        assert ref.title == "Test Paper"
        assert ref.accessed_at is not None

    def test_empty_title_rejected(self) -> None:
        with pytest.raises(ValueError, match="title"):
            Reference(title="")


class TestFactorHypothesis:
    def _make_valid(self) -> FactorHypothesis:
        return FactorHypothesis(
            name="低波动率因子",
            economic_mechanism="低波动率股票长期跑赢高波动率股票",
            input_fields=("close", "volume"),
            decision_timing="close",
            formula="rank(ts_std(returns_1d, 20))",
            direction="long_low",
            applicable_assets=("沪深300",),
            expected_failure_scenarios=("牛市末期",),
            parameters=(ParameterSpec("lookback", 10.0, 60.0, grid_size=3),),
            references=(Reference(title="Low Volatility Anomaly"),),
        )

    def test_basic_creation(self) -> None:
        h = self._make_valid()
        assert h.name == "低波动率因子"
        assert h.status == HypothesisStatus.PROPOSED
        assert h.hypothesis_id.startswith("fh-")
        assert h.parameter_budget == 3
        assert h.experiments_remaining == 3

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name"):
            FactorHypothesis(
                name="",
                economic_mechanism="x",
                input_fields=("close",),
                decision_timing="close",
                formula="rank(close)",
                direction="long_high",
            )

    def test_empty_mechanism_rejected(self) -> None:
        with pytest.raises(ValueError, match="economic_mechanism"):
            FactorHypothesis(
                name="test",
                economic_mechanism="",
                input_fields=("close",),
                decision_timing="close",
                formula="rank(close)",
                direction="long_high",
            )

    def test_empty_fields_rejected(self) -> None:
        with pytest.raises(ValueError, match="input_fields"):
            FactorHypothesis(
                name="test",
                economic_mechanism="x",
                input_fields=(),
                decision_timing="close",
                formula="rank(close)",
                direction="long_high",
            )

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            FactorHypothesis(
                name="test",
                economic_mechanism="x",
                input_fields=("close",),
                decision_timing="close",
                formula="rank(close)",
                direction="invalid",
            )

    def test_empty_formula_rejected(self) -> None:
        with pytest.raises(ValueError, match="formula"):
            FactorHypothesis(
                name="test",
                economic_mechanism="x",
                input_fields=("close",),
                decision_timing="close",
                formula="",
                direction="long_high",
            )

    def test_parameter_budget_multi_params(self) -> None:
        h = FactorHypothesis(
            name="test",
            economic_mechanism="x",
            input_fields=("close",),
            decision_timing="close",
            formula="rank(close)",
            direction="long_high",
            parameters=(
                ParameterSpec("a", 1.0, 10.0, grid_size=5),
                ParameterSpec("b", 1.0, 10.0, grid_size=4),
            ),
        )
        assert h.parameter_budget == 20

    def test_with_status_valid_transition(self) -> None:
        h = self._make_valid()
        approved = h.with_status(
            HypothesisStatus.APPROVED,
            approved_by="alice",
        )
        assert approved.status == HypothesisStatus.APPROVED
        assert approved.approved_by == "alice"
        assert h.status == HypothesisStatus.PROPOSED

    def test_with_status_invalid_transition(self) -> None:
        h = self._make_valid()
        with pytest.raises(ValueError, match="非法状态转换"):
            h.with_status(HypothesisStatus.VALIDATED_OOS)

    def test_with_status_terminal(self) -> None:
        h = self._make_valid()
        approved = h.with_status(HypothesisStatus.APPROVED)
        in_sample = approved.with_status(HypothesisStatus.IN_SAMPLE)
        validated = in_sample.with_status(HypothesisStatus.VALIDATED_OOS)
        with pytest.raises(ValueError, match="非法状态转换"):
            validated.with_status(HypothesisStatus.PROPOSED)

    def test_is_terminal(self) -> None:
        h = self._make_valid()
        assert not h.is_terminal
        approved = h.with_status(HypothesisStatus.APPROVED)
        in_sample = approved.with_status(HypothesisStatus.IN_SAMPLE)
        validated = in_sample.with_status(HypothesisStatus.VALIDATED_OOS)
        assert validated.is_terminal

    def test_experiments_remaining(self) -> None:
        h = self._make_valid()
        assert h.experiments_remaining == 3
        with_count = FactorHypothesis(
            name=h.name,
            economic_mechanism=h.economic_mechanism,
            input_fields=h.input_fields,
            decision_timing=h.decision_timing,
            formula=h.formula,
            direction=h.direction,
            parameters=h.parameters,
            experiment_count=2,
        )
        assert with_count.experiments_remaining == 1

    def test_to_text_includes_key_fields(self) -> None:
        h = self._make_valid()
        text = h.to_text()
        assert "低波动率因子" in text
        assert h.hypothesis_id in text
        assert "close" in text
        assert "proposed" in text

    def test_supersedes_id_default_none(self) -> None:
        h = self._make_valid()
        assert h.supersedes_id is None
