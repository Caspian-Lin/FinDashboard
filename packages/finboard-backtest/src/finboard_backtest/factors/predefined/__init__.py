"""平台预置因子(批次 0 基座,issue #398)。

三个子模块:

* :mod:`operators` —— 算子库(ts_* 时序 / cs_* 截面,NaN 纪律逐值锁定);
* :mod:`context` —— 因子输入契约(SymbolSeries / PredefinedFactorInput /
  采样);
* :mod:`registry` —— 目录(name/公式/数据依赖/signal_eligible/direction,
  ``return_{21,63,126,252}d`` 样板族)与内容寻址 commit 锚。

构建通道见 ``finboard_backtest.research_sandbox.predefined_runner``
(进程内执行,免容器;审计 / 内容寻址 / 覆盖检查与用户因子同构);
消费端引用名 = ``p_<name>``(与用户因子 ``u_`` 对称)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
    end_of_day,
    sample_series_frame,
)
from finboard_backtest.factors.predefined.operators import (
    cs_demean,
    cs_neutralize,
    cs_rank,
    cs_regression_resid,
    cs_scale,
    cs_winsorize,
    cs_zscore,
    rolling_ols_resid,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_decay,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    PREDEFINED_FACTORS_SCHEMA_VERSION,
    PredefinedFactorCompute,
    PredefinedFactorDefinition,
    get_predefined_factor,
    is_registered_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)

__all__ = [
    "PREDEFINED_FACTORS",
    "PREDEFINED_FACTORS_SCHEMA_VERSION",
    "FactorSeriesFrame",
    "PredefinedFactorCompute",
    "PredefinedFactorDefinition",
    "PredefinedFactorInput",
    "SymbolSeries",
    "cs_demean",
    "cs_neutralize",
    "cs_rank",
    "cs_regression_resid",
    "cs_scale",
    "cs_winsorize",
    "cs_zscore",
    "end_of_day",
    "get_predefined_factor",
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
    "rolling_ols_resid",
    "sample_series_frame",
    "ts_argmax",
    "ts_argmin",
    "ts_corr",
    "ts_cov",
    "ts_decay",
    "ts_delay",
    "ts_delta",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_std",
    "ts_sum",
]
