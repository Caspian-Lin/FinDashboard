"""样本外验证流水线(issue #57)。

公开 API
========

* ``ResearchExperiment`` / ``ValidationPlan`` / ``TrialRecord`` —— 数据契约
* ``ValidationRunner`` —— 编排器
* ``generate_walk_forward_windows`` —— 时间切分
* ``stationary_bootstrap`` / ``deflated_sharpe_ratio`` /
  ``probabilistic_sharpe_ratio`` /
  ``probability_of_backtest_overfitting`` —— 统计工具
* ``grid_candidates`` —— 参数网格生成

设计原则
========

1. 假设先行冻结:``ResearchExperiment.hypothesis`` 创建后不可修改。
2. 试验预算预先声明:``trial_budget`` 限制候选总数。
3. 最终揭盲只允许一次:``mark_final_test_unsealed`` 不可逆。
4. 失败也算试验:``TrialRecord`` 保存全部候选。
5. 门在实验前冻结:``AcceptanceThresholds`` 在创建时确定。
6. 不触及交易红线:只消费历史数据 / 离线计算。
"""

from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ExperimentVerdict,
    ResearchExperiment,
    RobustnessPlan,
    RobustnessProbe,
    StatisticalReport,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    WindowMetrics,
    WindowRole,
    deserialize_experiment,
    increment_trials_used,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_backtest.validation.robustness import (
    DEFAULT_STRESS_PHASES,
    MarketPhase,
    ParameterNeighbour,
    evaluate_robustness,
    generate_neighbourhood,
    make_probe,
    stress_phases_for_range,
)
from finboard_backtest.validation.runner import (
    TrialRunner,
    ValidationRunner,
    WindowResult,
    compute_window_metrics,
    grid_candidates,
)
from finboard_backtest.validation.splitter import (
    WalkForwardWindow,
    WindowSlice,
    assert_no_overlap,
    generate_walk_forward_windows,
    make_final_test_slice,
    make_in_sample_slice,
    make_validation_slice,
)
from finboard_backtest.validation.statistics import (
    BootstrapResult,
    PBOReport,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_from_returns,
    stationary_bootstrap,
)

__all__ = [
    "DEFAULT_STRESS_PHASES",
    "AcceptanceThresholds",
    "BootstrapResult",
    "ExperimentStatus",
    "ExperimentVerdict",
    "MarketPhase",
    "PBOReport",
    "ParameterNeighbour",
    "ResearchExperiment",
    "RobustnessPlan",
    "RobustnessProbe",
    "StatisticalReport",
    "TrialRecord",
    "TrialRunner",
    "TrialStatus",
    "ValidationMode",
    "ValidationPlan",
    "ValidationRunner",
    "VersionStamp",
    "WalkForwardWindow",
    "WindowMetrics",
    "WindowResult",
    "WindowRole",
    "WindowSlice",
    "assert_no_overlap",
    "compute_window_metrics",
    "deflated_sharpe_ratio",
    "deserialize_experiment",
    "evaluate_robustness",
    "generate_neighbourhood",
    "generate_walk_forward_windows",
    "grid_candidates",
    "increment_trials_used",
    "make_final_test_slice",
    "make_in_sample_slice",
    "make_probe",
    "make_validation_slice",
    "mark_final_test_unsealed",
    "new_experiment",
    "probabilistic_sharpe_ratio",
    "probability_of_backtest_overfitting",
    "sharpe_from_returns",
    "stationary_bootstrap",
    "stress_phases_for_range",
    "transition_status",
]
