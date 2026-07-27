"""样本外验证契约 + 状态机的单元测试。

覆盖:
* ``ResearchExperiment`` 假设冻结 + 试验预算 + 揭盲一次性;
* ``transition_status`` 非法转换被拒;
* ``increment_trials_used`` 超预算报错;
* ``mark_final_test_unsealed`` 重复调用报错;
* ``deserialize_experiment`` 往返一致性;
* 时间切分 ``generate_walk_forward_windows`` 防泄漏;
* ``assert_no_overlap`` 跨角色重叠被拒。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    RobustnessPlan,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    WindowRole,
    deserialize_experiment,
    increment_trials_used,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_backtest.validation.splitter import (
    WindowSlice,
    assert_no_overlap,
    generate_walk_forward_windows,
    make_final_test_slice,
    make_in_sample_slice,
    make_validation_slice,
)


def _stamp() -> VersionStamp:
    return VersionStamp(
        matching_model_version="v2",
        asset_rules_version="v1",
        factor_version="v1",
        dataset_versions={"daily_metrics": "2024-01-01"},
        selection_config={},
        strategy_kind="ma_cross",
    )


def _plan(
    *,
    mode: ValidationMode = ValidationMode.ROLLING,
    train_start: date = date(2020, 1, 1),
    train_end: date = date(2021, 12, 31),
    val_start: date = date(2022, 1, 1),
    val_end: date = date(2022, 12, 31),
    test_start: date = date(2023, 1, 1),
    test_end: date = date(2023, 6, 30),
    trial_budget: int = 10,
) -> ValidationPlan:
    return ValidationPlan(
        mode=mode,
        train_start=train_start,
        train_end=train_end,
        validation_start=val_start,
        validation_end=val_end,
        test_start=test_start,
        test_end=test_end,
        train_window_days=504,
        test_window_days=63,
        step_days=63,
        trial_budget=trial_budget,
    )


class TestExperimentLifecycle:
    def test_new_experiment_starts_in_hypothesis(self) -> None:
        exp = new_experiment(
            hypothesis="测试假设:均线交叉在 ETF 上有 alpha",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        assert exp.status == ExperimentStatus.HYPOTHESIS
        assert not exp.final_test_unsealed
        assert exp.trials_used == 0
        assert exp.can_run_trial()

    def test_empty_hypothesis_rejected(self) -> None:
        with pytest.raises(ValueError, match="hypothesis"):
            new_experiment(
                hypothesis="   ",
                version_stamp=_stamp(),
                plan=_plan(),
                thresholds=AcceptanceThresholds(),
            )

    def test_supersede_chain(self) -> None:
        exp1 = new_experiment(
            hypothesis="假设 v1",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        exp2 = new_experiment(
            hypothesis="假设 v2",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
            supersedes_id=exp1.experiment_id,
        )
        assert exp2.supersedes_id == exp1.experiment_id


class TestStatusMachine:
    def test_legal_transitions(self) -> None:
        exp = new_experiment(
            hypothesis="legal",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        # HYPOTHESIS → IN_SAMPLE
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        assert exp.status == ExperimentStatus.IN_SAMPLE
        # 揭盲 + → VALIDATED_OOS
        exp = mark_final_test_unsealed(exp)
        exp = transition_status(exp, ExperimentStatus.VALIDATED_OOS)
        assert exp.status == ExperimentStatus.VALIDATED_OOS
        assert exp.finalized_at is not None

    def test_skip_to_validated_without_unseal_fails(self) -> None:
        exp = new_experiment(
            hypothesis="legal",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        # 没有 unseal → 不能转 VALIDATED_OOS
        with pytest.raises(ValueError, match="final test unseal"):
            transition_status(exp, ExperimentStatus.VALIDATED_OOS)

    def test_illegal_skip_from_hypothesis_to_validated(self) -> None:
        exp = new_experiment(
            hypothesis="legal",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        with pytest.raises(ValueError, match="illegal status transition"):
            transition_status(exp, ExperimentStatus.VALIDATED_OOS)

    def test_illegal_rejected_back_to_in_sample(self) -> None:
        exp = new_experiment(
            hypothesis="legal",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        exp = transition_status(exp, ExperimentStatus.REJECTED, rejection_reason="bad")
        with pytest.raises(ValueError, match="illegal status transition"):
            transition_status(exp, ExperimentStatus.IN_SAMPLE)

    def test_rejection_records_reason(self) -> None:
        exp = new_experiment(
            hypothesis="legal",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        exp = transition_status(
            exp, ExperimentStatus.REJECTED, rejection_reason="oos failed"
        )
        assert exp.rejection_reason == "oos failed"
        assert exp.finalized_at is not None


class TestTrialBudget:
    def test_increment_trials_used(self) -> None:
        exp = new_experiment(
            hypothesis="budget",
            version_stamp=_stamp(),
            plan=_plan(trial_budget=3),
            thresholds=AcceptanceThresholds(),
        )
        exp = increment_trials_used(exp)
        assert exp.trials_used == 1
        exp = increment_trials_used(exp, by=2)
        assert exp.trials_used == 3

    def test_budget_exhausted_raises(self) -> None:
        exp = new_experiment(
            hypothesis="budget",
            version_stamp=_stamp(),
            plan=_plan(trial_budget=2),
            thresholds=AcceptanceThresholds(),
        )
        exp = increment_trials_used(exp, by=2)
        with pytest.raises(ValueError, match="trial budget exhausted"):
            increment_trials_used(exp)

    def test_can_run_trial_after_finalized(self) -> None:
        exp = new_experiment(
            hypothesis="budget",
            version_stamp=_stamp(),
            plan=_plan(trial_budget=10),
            thresholds=AcceptanceThresholds(),
        )
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        exp = mark_final_test_unsealed(exp)
        exp = transition_status(exp, ExperimentStatus.VALIDATED_OOS)
        assert not exp.can_run_trial()


class TestUnsealOnce:
    def test_double_unseal_raises(self) -> None:
        exp = new_experiment(
            hypothesis="once",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        exp = transition_status(exp, ExperimentStatus.IN_SAMPLE)
        exp = mark_final_test_unsealed(exp)
        with pytest.raises(ValueError, match="already unsealed"):
            mark_final_test_unsealed(exp)

    def test_unseal_only_in_in_sample(self) -> None:
        exp = new_experiment(
            hypothesis="once",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
        )
        # HYPOTHESIS 状态不能揭盲
        with pytest.raises(ValueError, match="cannot unseal"):
            mark_final_test_unsealed(exp)


class TestSerialization:
    def test_roundtrip(self) -> None:
        exp = new_experiment(
            hypothesis="roundtrip 假设",
            version_stamp=_stamp(),
            plan=_plan(),
            thresholds=AcceptanceThresholds(),
            robustness=RobustnessPlan(),
            strategy_params_space={"window": [5, 10, 20]},
            notes="测试 notes",
        )
        data = exp.as_dict()
        restored = deserialize_experiment(data)
        assert restored.experiment_id == exp.experiment_id
        assert restored.hypothesis == exp.hypothesis
        assert restored.plan.mode == exp.plan.mode
        assert restored.thresholds.min_oos_sharpe == exp.thresholds.min_oos_sharpe
        assert restored.robustness.neighbourhood_steps == exp.robustness.neighbourhood_steps
        assert restored.strategy_params_space == exp.strategy_params_space


class TestPlanValidation:
    def test_train_end_after_train_start(self) -> None:
        with pytest.raises(ValueError, match="train_start"):
            _plan(train_start=date(2021, 1, 1), train_end=date(2020, 1, 1))

    def test_validation_after_train(self) -> None:
        with pytest.raises(ValueError, match="validation_start"):
            _plan(
                train_end=date(2021, 12, 31),
                val_start=date(2021, 6, 1),
            )

    def test_test_after_validation(self) -> None:
        with pytest.raises(ValueError, match="validation_end"):
            _plan(
                val_end=date(2023, 1, 1),
                test_start=date(2022, 1, 1),
            )


class TestWalkForward:
    def test_rolling_generates_multiple_windows(self) -> None:
        windows = generate_walk_forward_windows(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            test_start=date(2020, 6, 1),
            test_end=date(2023, 12, 31),
            train_window_days=63,
            test_window_days=21,
            step_days=21,
        )
        assert len(windows) >= 3
        # 每个窗口的 train_end < test_start
        for w in windows:
            assert w.train.end < w.test.start
            assert not w.train.overlaps(w.test)

    def test_expanding_train_start_fixed(self) -> None:
        windows = generate_walk_forward_windows(
            mode=ValidationMode.EXPANDING,
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            test_start=date(2020, 6, 1),
            test_end=date(2023, 12, 31),
            train_window_days=63,
            test_window_days=21,
            step_days=21,
        )
        assert len(windows) >= 1
        # 训练集起点固定
        for w in windows:
            assert w.train.start == date(2020, 1, 1)

    def test_no_window_crosses_test_end(self) -> None:
        test_end = date(2023, 6, 30)
        windows = generate_walk_forward_windows(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            test_start=date(2020, 6, 1),
            test_end=test_end,
            train_window_days=63,
            test_window_days=21,
            step_days=21,
        )
        assert len(windows) >= 1
        for w in windows:
            assert w.test.end <= test_end

    def test_window_indices_incremental(self) -> None:
        windows = generate_walk_forward_windows(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            test_start=date(2020, 6, 1),
            test_end=date(2023, 6, 30),
            train_window_days=126,
            test_window_days=63,
            step_days=63,
        )
        indices = [w.window_index for w in windows]
        assert indices == list(range(len(indices)))
        # 时间单调递增
        for i in range(1, len(windows)):
            assert windows[i].train.start >= windows[i - 1].train.start

    def test_too_short_returns_empty(self) -> None:
        windows = generate_walk_forward_windows(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2020, 6, 30),
            test_start=date(2020, 7, 1),
            test_end=date(2020, 7, 15),  # 太短
            train_window_days=126,
            test_window_days=63,
            step_days=63,
        )
        assert windows == []

    def test_test_before_train_raises(self) -> None:
        with pytest.raises(ValueError, match="train_start"):
            generate_walk_forward_windows(
                mode=ValidationMode.ROLLING,
                train_start=date(2022, 1, 1),
                train_end=date(2023, 1, 1),
                test_start=date(2021, 1, 1),  # 在 train_start 之前
                test_end=date(2024, 1, 1),
                train_window_days=63,
                test_window_days=21,
                step_days=21,
            )


class TestAssertNoOverlap:
    def test_no_overlap_passes(self) -> None:
        train = make_in_sample_slice(
            train_start=date(2020, 1, 1), train_end=date(2020, 12, 31)
        )
        val = make_validation_slice(
            validation_start=date(2021, 1, 1), validation_end=date(2021, 6, 30)
        )
        test = make_final_test_slice(
            test_start=date(2021, 7, 1), test_end=date(2021, 12, 31)
        )
        # 不应抛
        assert_no_overlap(train, val, test)

    def test_overlap_across_roles_raises(self) -> None:
        train = make_in_sample_slice(
            train_start=date(2020, 1, 1), train_end=date(2020, 12, 31)
        )
        # validation 与 train 重叠
        val = WindowSlice(
            window_index=0,
            role=WindowRole.VALIDATION,
            start=date(2020, 6, 1),
            end=date(2021, 6, 30),
        )
        with pytest.raises(ValueError, match="time leakage"):
            assert_no_overlap(train, val)
