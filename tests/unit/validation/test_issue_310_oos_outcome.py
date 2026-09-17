"""``oos_outcome`` 派生语义 + 全拒揭盲具名警示(issue #310)。

``validated_oos`` 只表示「OOS 流程(含一次性揭盲)走完且最终门通过」,
不表示「假设获支持」——#245 决策 A(screen 门取 abs(rank_ic))下,best
trial 在 OOS walk-forward 被拒后继续揭盲是设计使然。本文件锁定:

* 派生规则三态(:func:`derive_oos_outcome`):supported / not_supported /
  inconclusive 的精确边界;
* 全 trial OOS 被拒仍揭盲:不硬阻断、状态机与一次性语义零变化,但打具名
  WARNING ``validation.unseal_with_rejected_trials`` 并写入 experiment.notes;
* 晋级门证据摘要透传 ``oos_outcome``(不参与门判定)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
import structlog
from structlog.testing import capture_logs

import finboard_backtest.validation.runner as runner_module
from finboard_backtest.research_code.promotion import evaluate_promotion_gates
from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    OosOutcome,
    ResearchExperiment,
    RobustnessPlan,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    WindowMetrics,
    WindowRole,
    append_note,
    best_oos_trial,
    derive_oos_outcome,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_backtest.validation.runner import ValidationRunner


@pytest.fixture(autouse=True)
def _unfiltered_structlog():
    """隔离 ``setup_logging`` 对 structlog 的全局污染,保日志断言确定性。

    任何先行的测试调用 ``setup_logging``(``cache_logger_on_first_use=True``)
    且 runner 模块 logger 已被先行 first-use 后,wrapper 缓存的是真实处理器
    链,``capture_logs`` 永远抓空(与 tests/unit/data/test_cache_read_cache.py
    同根因)。测试期重置为不缓存并重建 runner 模块 logger,结束恢复。
    """
    saved_config = structlog.get_config()
    saved_logger = runner_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    runner_module.logger = structlog.get_logger("finboard_backtest.validation.runner")
    yield
    runner_module.logger = saved_logger
    structlog.configure(**saved_config)


# ---------------------------------------------------------------------------
# 领域对象构造
# ---------------------------------------------------------------------------


def _experiment(thresholds: AcceptanceThresholds | None = None) -> ResearchExperiment:
    return new_experiment(
        hypothesis="oos_outcome 派生语义验证假设(占位,长度足够)",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version=None,
            dataset_versions={},
            selection_config={},
            strategy_kind="ma_cross",
        ),
        plan=ValidationPlan(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2020, 6, 30),
            validation_start=date(2020, 7, 1),
            validation_end=date(2020, 12, 31),
            test_start=date(2021, 1, 1),
            test_end=date(2021, 6, 30),
            train_window_days=126,
            test_window_days=21,
            step_days=21,
            trial_budget=4,
        ),
        thresholds=thresholds or AcceptanceThresholds(),
        robustness=RobustnessPlan(stress_phases=()),
        strategy_params_space={"window": [5]},
    )


def _to_in_sample(exp: ResearchExperiment) -> ResearchExperiment:
    return transition_status(exp, ExperimentStatus.IN_SAMPLE)


def _to_validated_oos(exp: ResearchExperiment) -> ResearchExperiment:
    """IN_SAMPLE → 揭盲 → VALIDATED_OOS(合法状态机路径)。"""
    unsealed = mark_final_test_unsealed(_to_in_sample(exp))
    return transition_status(unsealed, ExperimentStatus.VALIDATED_OOS)


def _to_rejected_after_unseal(exp: ResearchExperiment) -> ResearchExperiment:
    """IN_SAMPLE → 揭盲 → 揭盲门未过 REJECTED。"""
    unsealed = mark_final_test_unsealed(_to_in_sample(exp))
    return transition_status(
        unsealed, ExperimentStatus.REJECTED, rejection_reason="final_test_fail"
    )


def _trial(
    status: TrialStatus,
    *,
    oos: bool = False,
    is_sharpe: float = 1.0,
    failure_reason: str | None = None,
) -> TrialRecord:
    """构造单 trial;oos=True 时携带 OOS 证据(进过 walk-forward)。"""
    return TrialRecord(
        trial_id="exp-t0",
        experiment_id="exp",
        trial_index=0,
        parameters={"window": 5},
        status=status,
        in_sample_metrics=WindowMetrics(
            role=WindowRole.TRAIN,
            start=date(2020, 1, 1),
            end=date(2020, 6, 30),
            sharpe_ratio=is_sharpe,
        ),
        oos_metrics=(
            WindowMetrics(
                role=WindowRole.TEST,
                start=date(2020, 7, 1),
                end=date(2020, 12, 31),
                sharpe_ratio=0.2,
            )
            if oos
            else None
        ),
        failure_reason=failure_reason,
    )


def _with_notes(exp: ResearchExperiment, notes: str) -> ResearchExperiment:
    return replace(exp, notes=notes)


# ---------------------------------------------------------------------------
# 派生规则三态
# ---------------------------------------------------------------------------


class TestDeriveOosOutcome:
    def test_validated_oos_with_rejected_best_trial_is_not_supported(self) -> None:
        """动机场景:status=validated_oos 但 best trial OOS 被拒 → not_supported。

        OOS 流程完成 ≠ 假设获支持(#245 决策 A 下被拒后继续揭盲是设计使然)。
        """
        exp = _to_validated_oos(_experiment())
        trials = [
            _trial(TrialStatus.SELECTED, is_sharpe=0.5),
            _trial(
                TrialStatus.REJECTED,
                oos=True,
                is_sharpe=2.0,
                failure_reason="oos_sharpe=-0.1 < 0.5",
            ),
        ]
        assert exp.status is ExperimentStatus.VALIDATED_OOS
        assert exp.final_test_unsealed is True
        assert derive_oos_outcome(exp, trials) is OosOutcome.NOT_SUPPORTED

    def test_supported_when_oos_passed_and_unseal_passed(self) -> None:
        """best trial OOS 门过 + 揭盲达标 → supported。"""
        exp = _to_validated_oos(_experiment())
        trials = [_trial(TrialStatus.SELECTED, oos=True, is_sharpe=2.0)]
        assert derive_oos_outcome(exp, trials) is OosOutcome.SUPPORTED

    def test_not_supported_when_final_test_failed(self) -> None:
        """OOS 门过但揭盲未达标(REJECTED)→ not_supported。"""
        exp = _to_rejected_after_unseal(_experiment())
        trials = [_trial(TrialStatus.SELECTED, oos=True)]
        assert derive_oos_outcome(exp, trials) is OosOutcome.NOT_SUPPORTED

    def test_oos_rejected_without_unseal_is_not_supported(self) -> None:
        """best trial OOS 门被拒即判 not_supported(不依赖揭盲是否已消耗)。"""
        exp = _to_in_sample(_experiment())
        trials = [_trial(TrialStatus.REJECTED, oos=True, failure_reason="oos gate")]
        assert derive_oos_outcome(exp, trials) is OosOutcome.NOT_SUPPORTED

    def test_inconclusive_without_trials(self) -> None:
        """无 trial → inconclusive。"""
        exp = _to_in_sample(_experiment())
        assert derive_oos_outcome(exp, []) is OosOutcome.INCONCLUSIVE

    def test_inconclusive_without_oos_evidence(self) -> None:
        """trial 存在但 OOS 未跑(无 oos_metrics)→ inconclusive。"""
        exp = _to_in_sample(_experiment())
        trials = [_trial(TrialStatus.SELECTED)]
        assert derive_oos_outcome(exp, trials) is OosOutcome.INCONCLUSIVE

    def test_inconclusive_when_all_walk_forward_windows_failed(self) -> None:
        """全部 walk-forward 窗口失败:trial REJECTED 但无 OOS 指标 → 无法判定。

        runner 在窗口全失败时早退(replace(status=REJECTED) 但不设
        oos_metrics),持久化形状上与「OOS 未跑」不可区分 → inconclusive。
        """
        exp = _to_in_sample(_experiment())
        trials = [
            _trial(
                TrialStatus.REJECTED,
                oos=False,
                failure_reason="all walk-forward windows failed",
            )
        ]
        assert derive_oos_outcome(exp, trials) is OosOutcome.INCONCLUSIVE

    def test_inconclusive_when_oos_passed_but_not_unsealed(self) -> None:
        """OOS 门过但流程未走完(未揭盲)→ inconclusive。"""
        exp = _to_in_sample(_experiment())
        trials = [_trial(TrialStatus.SELECTED, oos=True)]
        assert derive_oos_outcome(exp, trials) is OosOutcome.INCONCLUSIVE

    def test_is_gate_rejection_is_inconclusive(self) -> None:
        """IS 阶段即被拒(从未进入 OOS)→ inconclusive(OOS 语义不适用)。"""
        exp = _to_in_sample(_experiment())
        trials = [_trial(TrialStatus.REJECTED, failure_reason="is gate")]
        assert derive_oos_outcome(exp, trials) is OosOutcome.INCONCLUSIVE

    def test_best_oos_trial_prefers_higher_is_sharpe(self) -> None:
        """防御:多个携带 OOS 证据的 trial 取 IS Sharpe 最高者为 best。"""
        trials = [
            _trial(TrialStatus.REJECTED, oos=True, is_sharpe=0.1),
            _trial(TrialStatus.SELECTED, oos=True, is_sharpe=3.0),
        ]
        assert best_oos_trial(trials) is trials[1]
        exp = _to_validated_oos(_experiment())
        assert derive_oos_outcome(exp, trials) is OosOutcome.SUPPORTED


class TestAppendNote:
    def test_append_to_empty_notes(self) -> None:
        exp = _experiment()
        updated = append_note(exp, "n1")
        assert updated.notes == "n1"

    def test_append_joins_with_newline(self) -> None:
        exp = _with_notes(_experiment(), "existing")
        updated = append_note(exp, "n2")
        assert updated.notes == "existing\nn2"

    def test_original_instance_untouched(self) -> None:
        exp = _experiment()
        append_note(exp, "n1")
        assert exp.notes == ""


# ---------------------------------------------------------------------------
# runner:全 trial OOS 被拒仍揭盲(不硬阻断)+ 具名警示
# ---------------------------------------------------------------------------

#: 只把 DSR 门调到不可达(999),使 OOS 门必挂而揭盲门(sharpe/mdd/CI)不受影响。
_OOS_FAIL_THRESHOLDS = AcceptanceThresholds(
    min_in_sample_sharpe=-10.0,
    min_oos_sharpe=-10.0,
    max_oos_drawdown=1.0,
    min_oos_calmar=-100.0,
    min_oos_information_ratio=-100.0,
    min_pbo_pass=False,
    min_deflated_sharpe=999.0,
    min_probabilistic_sharpe=0.0,
)

_LOOSE_THRESHOLDS = AcceptanceThresholds(
    min_in_sample_sharpe=-10.0,
    min_oos_sharpe=-10.0,
    max_oos_drawdown=1.0,
    min_oos_calmar=-100.0,
    min_oos_information_ratio=-100.0,
    min_pbo_pass=False,
    min_deflated_sharpe=-100.0,
    min_probabilistic_sharpe=0.0,
)


class _ScriptedRunner:
    """确定性脚本化收益:OOS/IS 温和上涨、final test 强上涨(揭盲门必过)。

    交替日收益(无随机)给出非退化 std;final test 的 Sharpe 远高于 OOS
    收益的 bootstrap CI 下界 → 揭盲门确定通过,测试不依赖统计运气。
    """

    def __init__(self, *, test_start: date) -> None:
        self.test_start = test_start
        self.calls: list[date] = []

    async def __call__(
        self,
        *,
        start: date,
        end: date,
        params: dict[str, object],
        config_overrides: dict[str, object] | None = None,
    ) -> BacktestResult:
        del params, config_overrides, end
        self.calls.append(start)
        seq = (0.04, 0.02) if start >= self.test_start else (0.015, 0.005)
        level = 100.0
        curve: list[tuple[date, Decimal]] = []
        cur = start
        for i in range(30):
            level *= 1 + seq[i % 2]
            curve.append((cur, Decimal(str(round(level, 6)))))
            cur += timedelta(days=1)
        return BacktestResult(equity_curve=curve)


async def _run_flow(
    thresholds: AcceptanceThresholds,
) -> tuple[ValidationRunner, TrialRecord]:
    """跑完 IS + walk-forward(未揭盲),返回 runner 与 best trial。"""
    exp = _experiment(thresholds=thresholds)
    runner = ValidationRunner(
        experiment=exp, trial_runner=_ScriptedRunner(test_start=exp.plan.test_start)
    )
    await runner.run_in_sample([{"window": 5}])
    best = await runner.run_walk_forward()
    assert best is not None
    return runner, best


class TestUnsealWithRejectedTrials:
    async def test_rejected_best_trial_still_unseals_with_named_warning(self) -> None:
        """best trial OOS 被拒:揭盲照常 + 具名 WARNING + notes 落实验记录。"""
        runner, best = await _run_flow(_OOS_FAIL_THRESHOLDS)
        assert best.status is TrialStatus.REJECTED
        assert best.oos_metrics is not None
        assert best.failure_reason is not None
        notes_before = runner.experiment.notes

        with capture_logs() as logs:
            verdict = await runner.unseal_final_test()

        # 不硬阻断:揭盲照常执行,一次性语义与状态机推进不变
        assert runner.experiment.final_test_unsealed is True
        assert verdict.status is ExperimentStatus.VALIDATED_OOS
        assert runner.experiment.status is ExperimentStatus.VALIDATED_OOS
        # 具名 WARNING(含实验与 trial 定位)
        warnings = [
            e for e in logs if e.get("event") == "validation.unseal_with_rejected_trials"
        ]
        assert len(warnings) == 1
        assert warnings[0]["best_trial_id"] == best.trial_id
        assert warnings[0]["oos_failure_reason"] == best.failure_reason
        # 警示写入实验记录 notes(只追加一次,不覆盖既有 notes)
        assert "unseal_with_rejected_trials" in runner.experiment.notes
        assert best.failure_reason in runner.experiment.notes
        assert notes_before == ""  # 原状态无 notes

        # 验收标准:validated_oos + best trial rejected → oos_outcome=not_supported
        assert (
            derive_oos_outcome(runner.experiment, runner.trials)
            is OosOutcome.NOT_SUPPORTED
        )

    async def test_supported_flow_has_no_warning_note(self) -> None:
        """全部门通过的正常流程:无警示、notes 不变、oos_outcome=supported。"""
        runner, best = await _run_flow(_LOOSE_THRESHOLDS)
        assert best.status is TrialStatus.SELECTED

        with capture_logs() as logs:
            verdict = await runner.unseal_final_test()

        assert verdict.status is ExperimentStatus.VALIDATED_OOS
        assert not [
            e for e in logs if e.get("event") == "validation.unseal_with_rejected_trials"
        ]
        assert runner.experiment.notes == ""
        assert (
            derive_oos_outcome(runner.experiment, runner.trials) is OosOutcome.SUPPORTED
        )

    async def test_second_unseal_still_forbidden(self) -> None:
        """警示不改变揭盲一次性语义:第二次揭盲照旧被状态机拒绝。"""
        runner, _ = await _run_flow(_OOS_FAIL_THRESHOLDS)
        await runner.unseal_final_test()
        with pytest.raises(ValueError, match="already unsealed"):
            await runner.unseal_final_test()


# ---------------------------------------------------------------------------
# 晋级门证据摘要透传 oos_outcome(不参与门判定)
# ---------------------------------------------------------------------------


class TestPromotionEvidenceSurfacesOosOutcome:
    def test_validation_summary_carries_oos_outcome(self) -> None:
        gate = evaluate_promotion_gates(
            screen=None,
            validation={
                "status": "validated_oos",
                "final_test_unsealed": True,
                "oos_outcome": "not_supported",
            },
        )
        payload = gate.as_dict()
        assert payload["validation"]["oos_outcome"] == "not_supported"
        # 语义区分不改变门判定:status/unsealed 才是门,oos_outcome 仅展示
        assert payload["validation_passed"] is True

    def test_missing_oos_outcome_is_none(self) -> None:
        """旧调用方 / 旧证据不携带该字段 → None,不报错。"""
        gate = evaluate_promotion_gates(
            screen=None,
            validation={"status": "validated_oos", "final_test_unsealed": True},
        )
        assert gate.as_dict()["validation"]["oos_outcome"] is None
