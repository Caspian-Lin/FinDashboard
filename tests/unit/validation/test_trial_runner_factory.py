"""#244:trial runner 配置预校验单元测试。

``default_trial_runner_factory`` 的配置校验段(capital / params / 策略名 /
基础参数)在 runner 构建期 fail-fast:任何一项不合法都在行情源构建与首个
trial 启动之前具名 ``trial_runner_invalid_config`` 拒绝,不消耗试验预算,
不留下「str + decimal.Decimal」式的逐 trial 不透明失败原因。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from finboard_backtest.background_jobs.contracts import ExecutorError
from finboard_backtest.background_jobs.executors.validation_experiment import (
    _coerce_capital,
    default_trial_runner_factory,
)
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    RobustnessPlan,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    new_experiment,
)


def _experiment(selection_config: dict[str, object]) -> Any:
    return new_experiment(
        hypothesis="配置校验测试",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version=None,
            dataset_versions={},
            selection_config=selection_config,
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
            train_window_days=21,
            test_window_days=10,
            step_days=10,
            trial_budget=4,
        ),
        thresholds=AcceptanceThresholds(min_in_sample_sharpe=-10.0),
        robustness=RobustnessPlan(),
    )


class TestCoerceCapital:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (100000, Decimal("100000")),
            ("250000.5", Decimal("250000.5")),
            (12.5, Decimal("12.5")),
            (Decimal("3000"), Decimal("3000")),
            (" 42000 ", Decimal("42000")),
        ],
    )
    def test_accepts_numeric_forms(self, raw: object, expected: Decimal) -> None:
        assert _coerce_capital(raw, "exp-1") == expected

    @pytest.mark.parametrize(
        "raw",
        [
            True,  # bool 是 int 子类,显式排除
            False,
            None,
            [100000],
            {"capital": 1},
            "abc",
            "",
            0,
            -1000,
            float("inf"),
        ],
    )
    def test_rejects_named(self, raw: object) -> None:
        with pytest.raises(ExecutorError) as exc_info:
            _coerce_capital(raw, "exp-1")
        assert exc_info.value.code == "trial_runner_invalid_config"
        assert "capital" in exc_info.value.summary


class TestFactoryConfigGate:
    async def test_params_not_mapping_rejected(self) -> None:
        experiment = _experiment(
            {
                "validation_trial_runner": {
                    "strategy": "ma_cross",
                    "symbols": ["510300.SH"],
                    "params": ["not-a-mapping"],
                }
            }
        )
        with pytest.raises(ExecutorError) as exc_info:
            await default_trial_runner_factory(None, experiment)  # type: ignore[arg-type]
        assert exc_info.value.code == "trial_runner_invalid_config"
        assert "params" in exc_info.value.summary

    async def test_unknown_strategy_named_with_available_list(self) -> None:
        experiment = _experiment(
            {
                "validation_trial_runner": {
                    "strategy": "no_such_strategy",
                    "symbols": ["510300.SH"],
                }
            }
        )
        with pytest.raises(ExecutorError) as exc_info:
            await default_trial_runner_factory(None, experiment)  # type: ignore[arg-type]
        assert exc_info.value.code == "trial_runner_invalid_config"
        # 错误附可用策略清单,操作者不用翻代码找合法值
        assert "ma_cross" in exc_info.value.summary

    async def test_invalid_base_params_named(self) -> None:
        experiment = _experiment(
            {
                "validation_trial_runner": {
                    "strategy": "ma_cross",
                    "symbols": ["510300.SH"],
                    "params": {"short_window": "not-an-int"},
                }
            }
        )
        with pytest.raises(ExecutorError) as exc_info:
            await default_trial_runner_factory(None, experiment)  # type: ignore[arg-type]
        assert exc_info.value.code == "trial_runner_invalid_config"
        assert "short_window" in exc_info.value.summary

    async def test_capital_string_accepted_and_coerced(self) -> None:
        """E2E 崩溃原样输入(capital 数字字符串)在构建期通过并强转。"""
        experiment = _experiment(
            {
                "validation_trial_runner": {
                    "strategy": "ma_cross",
                    "symbols": ["510300.SH"],
                    "capital": "200000",
                }
            }
        )
        runner = await default_trial_runner_factory(None, experiment)  # type: ignore[arg-type]
        assert callable(runner)
