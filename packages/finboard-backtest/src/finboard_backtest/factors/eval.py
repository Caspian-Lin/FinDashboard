"""因子质量评估闭环(issue #403):逐因子 IC / 分组单调性 / 换手衰减 /
覆盖起点声明 → 结构化 JSON 报告。

C1-C4 批量产出 130+ 预置因子后,本模块把「坏因子静默进入评分组合」的
风险变成**可见的量化证据**。三层:

* **纯指标引擎**(:func:`evaluate_factor_values`)——输入因子值面板
  ``{date: {symbol: float | None}}`` 与评估窗收盘价面板,产出单因子
  结构化报告:IC / RankIC / ICIR、分位组收益与单调性、截面 rank 自相关
  (换手率)与衰减、覆盖率与覆盖起点(:attr:`PredefinedFactorDefinition.
  min_history_bars` 声明 vs 实际首个非缺测日,#399 覆盖检查同口径);
* **治理规则**(:func:`signal_eligible_governance_findings`)——#214
  目录语义规则化:规模 / 风险 / 流动性暴露族默认 ``signal_eligible=False``,
  违反者进「疑似标注错误」清单(**不批量改标注**,人工拍板);
* **两个数据面驱动**——:func:`evaluate_catalog_synthetic`(合成数据
  全目录跑通,可入 CI 的快速模式)与
  :func:`evaluate_catalog_on_release`(真实冻结发布小窗口,经
  ``research_sandbox.predefined_runner`` 的挂载装配)。

诚实边界:合成模式的 IC / 分组数字只证明**机制跑通**(有意的横截面
结构使信号类因子 IC 非退化),不是研究结论;报告 ``data_face.mode``
显式标注数据口径。结论 flag(ok / no_data / insufficient_cross_section)
与 flags(coverage_start_missing / window_shorter_than_declaration /
low_coverage / ic_near_zero)是逐因子可直接消费的判定面。

纯离线研究域,不连 broker 不下单;评估只读目录与数据,不落库。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from finboard_backtest.factors.predefined.context import (
    DividendEventHistory,
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    PredefinedFactorDefinition,
    get_predefined_factor,
    predefined_factor_names,
)

#: 报告 schema 版本(结构调整时递增)
EVAL_REPORT_SCHEMA_VERSION = "v1"

#: 暴露 / 风险 / 流动性族(#214 目录语义:默认 ``signal_eligible=False``,
#: 不直接转换为信号;评估报告对违反者出「疑似标注错误」清单)
EXPOSURE_FAMILIES: frozenset[str] = frozenset({"size", "risk", "liquidity"})

#: 覆盖率低位的 flag 阈值(低于即 ``low_coverage``,信息性)
LOW_COVERAGE_RATIO = 0.5

#: IC 近零的 flag 阈值(|ic_mean| 与 |rank_ic_mean| 均低于即 ``ic_near_zero``)
NEAR_ZERO_IC = 0.01

#: 评估窗收盘价面板:{交易日 → {symbol → 收盘价}}(前向收益的原料)
ClosePanel = Mapping[date, Mapping[str, float]]


@dataclass(frozen=True)
class FactorEvalConfig:
    """评估口径(报告冻结原文,复算可对齐)。

    * ``horizon`` —— 前向收益持有期(交易日);末端不足一个 horizon 的
      决策日不进 IC / 分组统计;
    * ``n_groups`` —— 分位组数(按当期因子值升序等分);
    * ``min_cross_section`` —— 当日有效 (因子值, 前向收益) 对数下限,
      低于该值的日子不进统计(截面过小 Pearsons 不稳定);
    * ``min_ic_dates`` —— IC 序列最短长度,不足则 IC / 分组指标为 None
      (``insufficient_cross_section``);
    * ``decay_lags`` —— 自相关衰减的决策日步长集合(lag k = 相隔 k 个
      决策日的截面 rank 相关)。
    """

    horizon: int = 5
    n_groups: int = 5
    min_cross_section: int = 5
    min_ic_dates: int = 8
    decay_lags: tuple[int, ...] = (1, 4, 13)

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "n_groups": self.n_groups,
            "min_cross_section": self.min_cross_section,
            "min_ic_dates": self.min_ic_dates,
            "decay_lags": list(self.decay_lags),
        }


@dataclass(frozen=True)
class FactorEvalReport:
    """单因子评估报告(factor-level,``as_dict`` 即 JSON 产物条目)。"""

    factor: str
    family: str
    signal_eligible: bool
    direction: str
    min_history_bars: int | None
    coverage_ratio: float
    first_effective_date: str | None
    coverage_start_missing: bool
    n_evaluable_dates: int
    status: str
    flags: tuple[str, ...] = ()
    ic_mean: float | None = None
    ic_ir: float | None = None
    rank_ic_mean: float | None = None
    rank_ic_ir: float | None = None
    ic_positive_ratio: float | None = None
    group_returns: tuple[float, ...] | None = None
    group_monotonicity: float | None = None
    rank_autocorr_lag1: float | None = None
    turnover: float | None = None
    autocorr_decay: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "family": self.family,
            "signal_eligible": self.signal_eligible,
            "direction": self.direction,
            "min_history_bars": self.min_history_bars,
            "coverage_ratio": round(self.coverage_ratio, 6),
            "first_effective_date": self.first_effective_date,
            "coverage_start_missing": self.coverage_start_missing,
            "n_evaluable_dates": self.n_evaluable_dates,
            "status": self.status,
            "flags": list(self.flags),
            "ic_mean": _round(self.ic_mean),
            "ic_ir": _round(self.ic_ir),
            "rank_ic_mean": _round(self.rank_ic_mean),
            "rank_ic_ir": _round(self.rank_ic_ir),
            "ic_positive_ratio": _round(self.ic_positive_ratio),
            "group_returns": (
                None
                if self.group_returns is None
                else [round(item, 6) for item in self.group_returns]
            ),
            "group_monotonicity": _round(self.group_monotonicity),
            "rank_autocorr_lag1": _round(self.rank_autocorr_lag1),
            "turnover": _round(self.turnover),
            "autocorr_decay": {
                lag: _round(value) for lag, value in self.autocorr_decay.items()
            },
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


# --------------------------------------------------------------------- #
# 统计原语(显式实现,避免引入 scipy 依赖;ties 取平均秩)
# --------------------------------------------------------------------- #


def _average_ranks(values: Sequence[float]) -> list[float]:
    """平均秩(并列值取秩均值;Spearman 的 ties 口径)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _pearson(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Pearson 相关;方差为 0(常数截面)或样本不足 → None。"""
    n = len(pairs)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    var_y = sum((y - mean_y) ** 2 for _, y in pairs)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Spearman 秩相关(平均秩 + Pearson);并列全同等退化输入 → None。"""
    if len(pairs) < 2:
        return None
    rank_x = _average_ranks([x for x, _ in pairs])
    rank_y = _average_ranks([y for _, y in pairs])
    return _pearson(list(zip(rank_x, rank_y, strict=True)))


# --------------------------------------------------------------------- #
# 纯指标引擎
# --------------------------------------------------------------------- #


def evaluate_factor_values(
    definition: PredefinedFactorDefinition,
    values: FactorSeriesFrame,
    close_panel: ClosePanel,
    *,
    config: FactorEvalConfig | None = None,
) -> FactorEvalReport:
    """因子值面板 → 单因子评估报告(纯函数)。

    ``values`` = 因子值 ``{date: {symbol: float | None}}``(决策日为键);
    ``close_panel`` = 评估窗**全交易日**收盘价面板(前向收益原料,合成
    面与发布面同契约)。IC 的方向语义保持原始值:direction=LOWER 的因子
    IC 预期为负(报告消费方按 |IC| 与方向联合解读,flags 不做方向翻转)。
    """
    cfg = config or FactorEvalConfig()
    trading_days = sorted(close_panel)
    day_pos = {day: pos for pos, day in enumerate(trading_days)}
    decision_days = sorted(values)

    cells = 0
    finite_cells = 0
    first_effective: date | None = None
    for day in decision_days:
        cross = values[day]
        cells += len(cross)
        for value in cross.values():
            if value is not None and math.isfinite(value):
                finite_cells += 1
                if first_effective is None:
                    first_effective = day
    coverage = (finite_cells / cells) if cells else 0.0

    # 覆盖起点(#399 口径):声明 min_history_bars 的因子,首个决策日
    # 全缺测 = 发布历史不足(评估面只报告,入队硬门由 #399 通道承担)。
    first_day_cross = values.get(decision_days[0], {}) if decision_days else {}
    coverage_start_missing = definition.min_history_bars is not None and (
        not first_day_cross
        or all(value is None for value in first_day_cross.values())
    )

    ic_series: list[float] = []
    rank_ic_series: list[float] = []
    group_sums = [0.0] * cfg.n_groups
    group_counts = [0] * cfg.n_groups

    for day in decision_days:
        pos = day_pos.get(day)
        if pos is None or pos + cfg.horizon >= len(trading_days):
            continue
        forward_day = trading_days[pos + cfg.horizon]
        closes_today = close_panel[day]
        closes_forward = close_panel[forward_day]
        pairs: list[tuple[float, float]] = []
        for symbol, value in values[day].items():
            if value is None or not math.isfinite(value):
                continue
            close_today = closes_today.get(symbol)
            close_forward = closes_forward.get(symbol)
            if (
                close_today is None
                or close_forward is None
                or close_today <= 0.0
            ):
                continue
            pairs.append((value, close_forward / close_today - 1.0))
        if len(pairs) < cfg.min_cross_section:
            continue
        ic = _pearson(pairs)
        rank_ic = _spearman(pairs)
        if ic is not None:
            ic_series.append(ic)
        if rank_ic is not None:
            rank_ic_series.append(rank_ic)
        # 分位组:按因子值升序等分,组均值前向收益逐日累计
        ordered = sorted(pairs, key=lambda item: item[0])
        size = len(ordered) // cfg.n_groups
        if size < 1:
            continue
        for group in range(cfg.n_groups):
            start = group * size
            stop = start + size if group < cfg.n_groups - 1 else len(ordered)
            chunk = ordered[start:stop]
            if not chunk:
                continue
            group_sums[group] += sum(ret for _, ret in chunk) / len(chunk)
            group_counts[group] += 1

    n_evaluable = min(
        (len(ic_series) if ic_series else 0),
        (len(rank_ic_series) if rank_ic_series else 0),
    )
    ic_mean = _mean(ic_series)
    ic_ir = _ratio(ic_mean, _std(ic_series))
    rank_ic_mean = _mean(rank_ic_series)
    rank_ic_ir = _ratio(rank_ic_mean, _std(rank_ic_series))
    ic_positive_ratio = (
        (sum(1 for item in ic_series if item > 0.0) / len(ic_series))
        if ic_series
        else None
    )

    group_returns: tuple[float, ...] | None = None
    group_monotonicity: float | None = None
    if n_evaluable >= cfg.min_ic_dates and all(
        count > 0 for count in group_counts
    ):
        group_returns = tuple(
            total / count
            for total, count in zip(group_sums, group_counts, strict=True)
        )
        group_monotonicity = _spearman(
            [(float(index), ret) for index, ret in enumerate(group_returns)]
        )

    decay = _rank_autocorrs(values, decision_days, cfg.decay_lags)
    lag1 = decay.get("1")
    flags: list[str] = []
    if cells == 0 or finite_cells == 0:
        status = "no_data"
        flags.append("no_data")
    elif n_evaluable < cfg.min_ic_dates:
        status = "insufficient_cross_section"
    else:
        status = "ok"
    if coverage_start_missing:
        flags.append("coverage_start_missing")
    if (
        definition.min_history_bars is not None
        and len(trading_days) < definition.min_history_bars
    ):
        flags.append("window_shorter_than_declaration")
    if 0.0 < coverage < LOW_COVERAGE_RATIO:
        flags.append("low_coverage")
    if (
        ic_mean is not None
        and rank_ic_mean is not None
        and abs(ic_mean) < NEAR_ZERO_IC
        and abs(rank_ic_mean) < NEAR_ZERO_IC
    ):
        flags.append("ic_near_zero")

    return FactorEvalReport(
        factor=definition.name,
        family=definition.family,
        signal_eligible=definition.signal_eligible,
        direction=definition.direction.value,
        min_history_bars=definition.min_history_bars,
        coverage_ratio=coverage,
        first_effective_date=(
            first_effective.isoformat() if first_effective is not None else None
        ),
        coverage_start_missing=coverage_start_missing,
        n_evaluable_dates=n_evaluable,
        status=status,
        flags=tuple(flags),
        ic_mean=ic_mean,
        ic_ir=ic_ir,
        rank_ic_mean=rank_ic_mean,
        rank_ic_ir=rank_ic_ir,
        ic_positive_ratio=ic_positive_ratio,
        group_returns=group_returns,
        group_monotonicity=group_monotonicity,
        rank_autocorr_lag1=lag1,
        turnover=None if lag1 is None else 1.0 - lag1,
        autocorr_decay=dict(decay),
    )


def _mean(series: Sequence[float]) -> float | None:
    return sum(series) / len(series) if series else None


def _std(series: Sequence[float]) -> float | None:
    """样本标准差(ddof=1);不足两个样本 → None。"""
    if len(series) < 2:
        return None
    mean = sum(series) / len(series)
    var = sum((item - mean) ** 2 for item in series) / (len(series) - 1)
    return math.sqrt(var)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0.0:
        return None
    return numerator / denominator


def _rank_autocorrs(
    values: FactorSeriesFrame,
    decision_days: Sequence[date],
    lags: Sequence[int],
) -> dict[str, float | None]:
    """截面 rank 自相关(lag k = 相隔 k 个决策日,重叠标的)。

    换手率 = ``1 - rank_autocorr(lag1)``:截面排名逐步长翻转的比例,
    高换手因子在交易成本下可实现的净收益显著低于 IC 表面值。
    """
    out: dict[str, float | None] = {}
    for lag in sorted({int(item) for item in lags}):
        correlations: list[float] = []
        for i in range(len(decision_days) - lag):
            left = values.get(decision_days[i], {})
            right = values.get(decision_days[i + lag], {})
            pairs = [
                (left_value, right_value)
                for symbol, left_value in left.items()
                if left_value is not None and math.isfinite(left_value)
                for right_value in (right.get(symbol),)
                if right_value is not None and math.isfinite(right_value)
            ]
            correlation = _spearman(pairs)
            if correlation is not None:
                correlations.append(correlation)
        out[str(lag)] = _mean(correlations)
    return out


# --------------------------------------------------------------------- #
# signal_eligible 治理(#214 规则化;不批量改标注,人工拍板)
# --------------------------------------------------------------------- #


def signal_eligible_governance_findings() -> list[dict[str, Any]]:
    """目录 ``signal_eligible`` 与 #214 家族规则的逐条对照。

    规则:暴露 / 风险 / 流动性族(:data:`EXPOSURE_FAMILIES`)的条目默认
    ``signal_eligible=False``(规模 / 波动 / 换手是**风险暴露或选域
    变量**,不直接转换为多头信号);违反者列入「疑似标注错误」清单,
    附家族与当前标注 —— 处置(改标注或给豁免理由)由人工拍板,本工具
    不改目录。
    """
    findings: list[dict[str, Any]] = []
    for name in predefined_factor_names():
        item = PREDEFINED_FACTORS[name]
        if item.family in EXPOSURE_FAMILIES and item.signal_eligible:
            findings.append(
                {
                    "factor": name,
                    "family": item.family,
                    "signal_eligible": item.signal_eligible,
                    "rule": (
                        f"family={item.family} 属暴露/风险/流动性族,"
                        "#214 语义默认 signal_eligible=False"
                    ),
                }
            )
    return findings


# --------------------------------------------------------------------- #
# 报告组装(两个数据面共用)
# --------------------------------------------------------------------- #


def resolve_eval_factors(factors: Sequence[str] | None) -> tuple[str, ...]:
    """评估因子清单(None = 全目录;p_/裸名均可,未注册具名拒绝)。"""
    if not factors:
        return predefined_factor_names()
    resolved: list[str] = []
    for item in factors:
        bare = item.removeprefix("p_")
        if bare not in PREDEFINED_FACTORS:
            raise KeyError(
                f"未注册的平台预置因子: {item!r};可用前几个: "
                f"{list(predefined_factor_names())[:8]} ..."
            )
        resolved.append(bare)
    return tuple(dict.fromkeys(resolved))


def build_report(
    *,
    reports: Sequence[FactorEvalReport],
    data_face: dict[str, Any],
    config: FactorEvalConfig,
    window: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """报告 payload(schema_version + 口径 + 逐因子 + 治理 + 摘要)。"""
    findings = signal_eligible_governance_findings()
    status_counts: dict[str, int] = {}
    for report in reports:
        status_counts[report.status] = status_counts.get(report.status, 0) + 1
    scored = [
        report
        for report in reports
        if report.rank_ic_mean is not None
    ]
    ranked = sorted(scored, key=lambda item: abs(item.rank_ic_mean or 0.0))
    return {
        "schema_version": EVAL_REPORT_SCHEMA_VERSION,
        "data_face": data_face,
        "config": config.as_dict(),
        "window": window,
        "summary": {
            "factor_count": len(reports),
            "status_counts": status_counts,
            "ic_available": len(scored),
            "strongest_abs_rank_ic": (
                [item.factor for item in ranked[-3:]] if ranked else []
            ),
            "weakest_abs_rank_ic": (
                [item.factor for item in ranked[:3]] if ranked else []
            ),
            "signal_eligible_suspected": len(findings),
        },
        "signal_eligible_governance": {
            "rule": (
                "family in {size, risk, liquidity} 默认 signal_eligible=false"
                "(#214 暴露/风险/流动性语义);违例=疑似标注错误,人工拍板"
            ),
            "suspected": findings,
        },
        "factors": [report.as_dict() for report in reports],
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


# --------------------------------------------------------------------- #
# 合成数据面(全目录跑通;机制冒烟,非研究结论)
# --------------------------------------------------------------------- #

_SYNTHETIC_BENCHMARK = "000300.SH"

#: 公告类数据集 kind(与 predefined_runner 挂载装配同表;独立常量防私有耦合)
_ANNOUNCED_KINDS: frozenset[str] = frozenset(
    {
        "financial_indicators",
        "income_statements",
        "balance_sheets",
        "cashflow_statements",
        "dividends",
    }
)

#: daily_metrics 合成字段(目录依赖 union 之外的基础字段:close 为
#: 市值/股本类因子分母,val_* 比值分母同口径)
_SYNTHETIC_DAILY_FIELDS: tuple[str, ...] = (
    "close",
    "turnover_rate",
    "total_market_cap",
    "circulating_market_cap",
    "float_shares",
    "total_shares",
)


def synthetic_announced_fields() -> dict[str, tuple[str, ...]]:
    """目录声明 union → 公告类数据集的合成字段清单(动态,免逐因子维护)。"""
    fields: dict[str, set[str]] = {}
    for item in PREDEFINED_FACTORS.values():
        for dep in item.data_dependencies:
            kind, _, field_name = dep.partition(".")
            if kind in _ANNOUNCED_KINDS and kind != "dividends":
                fields.setdefault(kind, set()).add(field_name)
    return {kind: tuple(sorted(names)) for kind, names in fields.items()}


@dataclass(frozen=True)
class SyntheticUniverse:
    """合成评估宇宙(确定性 rng;横截面结构使信号类因子 IC 非退化)。"""

    days: tuple[date, ...]
    symbols: tuple[str, ...]
    benchmark: str
    bars: dict[str, dict[str, SymbolSeries]]
    daily: dict[str, dict[str, SymbolSeries]]
    announced: dict[str, dict[str, dict[str, SymbolSeries]]]
    dividends: dict[str, DividendEventHistory]

    @property
    def close_panel(self) -> dict[date, dict[str, float]]:
        """全交易日收盘价面板(bars close;前向收益原料)。"""
        panel: dict[date, dict[str, float]] = {}
        for symbol in (*self.symbols, self.benchmark):
            series = self.bars["close"][symbol]
            for position, day in enumerate(series.dates):
                panel.setdefault(day, {})[symbol] = float(series.values[position])
        return panel


class _SyntheticInput(PredefinedFactorInput):
    """合成评估输入面:显式注入 bars / daily / 公告序列 / 分红事件史。"""

    def __init__(
        self,
        definition: PredefinedFactorDefinition,
        universe: SyntheticUniverse,
        *,
        decision_dates: tuple[date, ...],
    ) -> None:
        super().__init__(
            factor_name=definition.name,
            decision_dates=decision_dates,
            tradable_symbols=universe.symbols,
            benchmark_only_symbols=frozenset({universe.benchmark}),
        )
        self._universe = universe

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return dict(self._universe.bars.get(field, {}))

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._universe.daily.get(field, {}))

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return self.research_dataset("financial_indicators", field)

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        if kind == "daily_metrics":
            return self.daily_metrics(field)
        return dict(self._universe.announced.get(kind, {}).get(field, {}))

    def dividend_events(self) -> dict[str, DividendEventHistory]:
        return dict(self._universe.dividends)

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol: Any,
        per_symbol_values: Any,
    ) -> FactorSeriesFrame:
        from finboard_backtest.factors.predefined.context import (
            sample_series_frame,
        )

        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
        )


def build_synthetic_universe(
    *,
    n_symbols: int = 30,
    n_days: int = 504,
    seed: int = 403,
    start: date = date(2022, 1, 3),
) -> SyntheticUniverse:
    """确定性合成宇宙:连续日轴 + 共同市场因子 + 截面持久质量结构。

    * 价格:``日漂移 = alpha_i + 0.002 x quality_i``(quality_i ~ U(-1,1)
      截面持久)—— 量价与基本面因子对**未来收益**有真实可测的截面
      相关(机制冒烟需要非退化 IC;数值本身无研究含义);
    * 公告序列:季度公告(每 91 天一行),字段值 = 基准 x
      ``(1 + 0.3 x quality_i)`` 带温和时变;``available_at`` = 公告次日
      零点(公告步进取数与真实挂载同契约);
    * 分红事件史:年度进展行(预案 → 实施),``cash_div`` 与 quality_i
      正相关,``ex_date`` 落在年内(精确股息率可聚合)。
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    days = tuple(start + timedelta(days=i) for i in range(n_days))
    symbols = tuple(f"SY{i:04d}.SH" for i in range(n_symbols))
    quality = rng.uniform(-1.0, 1.0, size=n_symbols)
    alphas = rng.normal(0.0004, 0.0005, size=n_symbols)

    market_ret = rng.normal(0.0002, 0.010, size=n_days)
    closes: dict[str, np.ndarray] = {}
    for k, symbol in enumerate(symbols):
        drift = alphas[k] + 0.002 * quality[k]
        rets = market_ret * (0.7 + 0.3 * abs(quality[k])) + rng.normal(
            drift, 0.018, size=n_days
        )
        closes[symbol] = 20.0 * np.cumprod(1.0 + rets)
    benchmark_ret = market_ret + rng.normal(0.0, 0.002, size=n_days)
    closes[_SYNTHETIC_BENCHMARK] = 4000.0 * np.cumprod(1.0 + benchmark_ret)

    bars: dict[str, dict[str, SymbolSeries]] = {
        name: {} for name in ("close", "open", "high", "low", "volume", "amount")
    }
    available = tuple(
        _synthetic_available_at(day) for day in days
    )
    for symbol, close_values in closes.items():
        open_values = close_values * (
            1.0 + rng.normal(0.0, 0.004, size=n_days)
        )
        high_values = np.maximum(open_values, close_values) * (
            1.0 + np.abs(rng.normal(0.0, 0.006, size=n_days))
        )
        low_values = np.minimum(open_values, close_values) * (
            1.0 - np.abs(rng.normal(0.0, 0.006, size=n_days))
        )
        volume_values = rng.uniform(5e6, 2e7, size=n_days) * (
            1.0 + 0.5 * quality[0] if symbol == symbols[0] else 1.0
        )
        amount_values = volume_values * close_values
        for name, values in (
            ("close", close_values),
            ("open", open_values),
            ("high", high_values),
            ("low", low_values),
            ("volume", volume_values),
            ("amount", amount_values),
        ):
            bars[name][symbol] = SymbolSeries(
                dates=days,
                values=np.asarray(values, dtype=np.float64),
                available_at=available,
            )

    daily: dict[str, dict[str, SymbolSeries]] = {}
    close_by_symbol = {s: closes[s] for s in symbols}
    total_shares = rng.uniform(5e8, 2e9, size=n_symbols)
    float_ratio = rng.uniform(0.4, 0.9, size=n_symbols)
    daily["close"] = {
        symbol: SymbolSeries(
            dates=days,
            values=close_by_symbol[symbol],
            available_at=available,
        )
        for symbol in symbols
    }
    daily["turnover_rate"] = {
        symbol: SymbolSeries(
            dates=days,
            values=rng.uniform(0.3, 8.0, size=n_days)
            * (1.0 + 0.3 * quality[k]),
            available_at=available,
        )
        for k, symbol in enumerate(symbols)
    }
    daily["total_market_cap"] = {
        symbol: SymbolSeries(
            dates=days,
            values=close_by_symbol[symbol] * total_shares[k],
            available_at=available,
        )
        for k, symbol in enumerate(symbols)
    }
    daily["circulating_market_cap"] = {
        symbol: SymbolSeries(
            dates=days,
            values=close_by_symbol[symbol] * total_shares[k] * float_ratio[k],
            available_at=available,
        )
        for k, symbol in enumerate(symbols)
    }
    daily["float_shares"] = {
        symbol: SymbolSeries(
            dates=days,
            values=np.full(n_days, total_shares[k] * float_ratio[k]),
            available_at=available,
        )
        for k, symbol in enumerate(symbols)
    }
    daily["total_shares"] = {
        symbol: SymbolSeries(
            dates=days,
            values=np.full(n_days, total_shares[k]),
            available_at=available,
        )
        for k, symbol in enumerate(symbols)
    }

    # 公告序列:季度公告,值 = 基准 x (1 + 0.3 x quality) 带温和时变
    announced: dict[str, dict[str, dict[str, SymbolSeries]]] = {}
    announcement_positions = list(range(60, n_days, 91))
    field_bases = {
        "eps": 0.8,
        "roe": 0.08,
        "return_on_equity": 0.09,
        "return_on_assets": 0.05,
        "gross_profit_margin": 0.30,
        "net_profit_margin": 0.10,
        "ocf_to_revenue": 0.12,
        "revenue_yoy": 0.10,
        "net_profit_yoy": 0.12,
        "debt_to_assets": 0.45,
        "debt_to_equity": 0.85,
        "equity_multiplier": 1.9,
        "revenue": 8e9,
        "net_profit": 6e8,
        "operating_cashflow": 9e8,
        "total_assets": 6e10,
        "total_liabilities": 3e10,
        "total_equity": 3e10,
        "book_value_per_share": 8.0,
        "revenue_per_share": 12.0,
        "ocf_per_share": 1.5,
    }
    for kind, fields in synthetic_announced_fields().items():
        kind_tables: dict[str, dict[str, SymbolSeries]] = {}
        for field_name in fields:
            base = field_bases.get(field_name, 1.0)
            per_symbol: dict[str, SymbolSeries] = {}
            for k, symbol in enumerate(symbols):
                n_rows = len(announcement_positions)
                row_days = tuple(days[p] for p in announcement_positions)
                row_available = tuple(
                    _synthetic_available_at(days[min(p + 1, n_days - 1)])
                    for p in announcement_positions
                )
                values = base * (
                    1.0
                    + 0.3 * quality[k]
                    + rng.normal(0.0, 0.02, size=n_rows)
                    + 0.002 * np.arange(n_rows)
                )
                per_symbol[symbol] = SymbolSeries(
                    dates=row_days,
                    values=np.asarray(values, dtype=np.float64),
                    available_at=row_available,
                )
            kind_tables[field_name] = per_symbol
        announced[kind] = kind_tables

    # 分红事件史:年度(预案 + 实施),cash_div 与 quality 正相关
    dividends: dict[str, DividendEventHistory] = {}
    for k, symbol in enumerate(symbols):
        event_rows: list[
            tuple[date, date, date | None, date | None, float | None]
        ] = []
        year = 0
        while True:
            announce_pos = 30 + year * 244
            if announce_pos >= n_days:
                break
            impl_pos = min(announce_pos + 60, n_days - 1)
            cash = round(max(0.0, 0.02 + 0.02 * quality[k]) * 10.0, 4)
            ex_day = days[impl_pos]
            for offset, ex in ((0, None), (30, ex_day)):
                pos = announce_pos + offset
                if pos >= n_days:
                    break
                event_rows.append(
                    (
                        days[pos],
                        days[pos],
                        date(days[pos].year, 12, 31),
                        ex,
                        cash if ex is not None else None,
                    )
                )
            year += 1
        if event_rows:
            dividends[symbol] = DividendEventHistory(
                announcement_dates=tuple(item[0] for item in event_rows),
                available_at=tuple(
                    _synthetic_available_at(item[1]) for item in event_rows
                ),
                report_periods=tuple(item[2] for item in event_rows),
                ex_dates=tuple(item[3] for item in event_rows),
                cash_div=np.asarray(
                    [
                        math.nan if item[4] is None else item[4]
                        for item in event_rows
                    ],
                    dtype=np.float64,
                ),
            )

    return SyntheticUniverse(
        days=days,
        symbols=symbols,
        benchmark=_SYNTHETIC_BENCHMARK,
        bars=bars,
        daily=daily,
        announced=announced,
        dividends=dividends,
    )


def _synthetic_available_at(day: date) -> datetime:
    """合成序列的逐行 available_at(当日 15:00 UTC,与 A 股收盘可见同量级)。"""
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC)


def evaluate_catalog_synthetic(
    *,
    factors: Sequence[str] | None = None,
    config: FactorEvalConfig | None = None,
    n_symbols: int = 30,
    n_days: int = 504,
    warmup: int = 252,
    step: int = 5,
    seed: int = 403,
) -> dict[str, Any]:
    """合成数据全目录评估(机制冒烟;``data_face.mode="synthetic"``)。

    决策日 = 第 ``warmup`` 个交易日起每 ``step`` 日(给 252 根 bar 长窗
    因子留预热;声明 ``min_history_bars`` 更长的因子在报告中以
    ``window_shorter_than_declaration`` / ``no_data`` 如实呈现 —— 覆盖
    起点声明本身是评估对象之一)。
    """
    import time

    started = time.monotonic()
    cfg = config or FactorEvalConfig()
    names = resolve_eval_factors(factors)
    universe = build_synthetic_universe(
        n_symbols=n_symbols, n_days=n_days, seed=seed
    )
    decision_dates = tuple(universe.days[warmup :: step])
    close_panel = universe.close_panel

    reports: list[FactorEvalReport] = []
    for name in names:
        definition = get_predefined_factor(name)
        inp = _SyntheticInput(
            definition, universe, decision_dates=decision_dates
        )
        frame = definition.compute(inp)
        reports.append(
            evaluate_factor_values(definition, frame, close_panel, config=cfg)
        )
    reports.sort(key=lambda item: item.factor)
    return build_report(
        reports=reports,
        data_face={
            "mode": "synthetic",
            "seed": seed,
            "n_symbols": n_symbols,
            "n_days": n_days,
            "note": (
                "合成数据仅证明评估机制与因子实现跑通;IC/分组数值不构成"
                "研究结论。横截面持久质量结构使信号类因子 IC 非退化。"
            ),
        },
        config=cfg,
        window={
            "start": universe.days[0].isoformat(),
            "end": universe.days[-1].isoformat(),
            "trading_days": len(universe.days),
            "decision_dates": len(decision_dates),
            "decision_step": step,
            "warmup": warmup,
        },
        elapsed_seconds=time.monotonic() - started,
    )


# --------------------------------------------------------------------- #
# 真实冻结发布数据面(小窗口;挂载装配复用 predefined_runner 原语)
# --------------------------------------------------------------------- #


async def evaluate_catalog_on_release(
    *,
    release_id: str,
    dataset_release_ids: Sequence[str] = (),
    window_start: date,
    window_end: date,
    factors: Sequence[str] | None = None,
    config: FactorEvalConfig | None = None,
    decision_step: int = 5,
    settings: Any | None = None,
    release_provider_factory: Any | None = None,
) -> dict[str, Any]:
    """真实冻结发布小窗口评估(``data_face.mode="release"``)。

    一次窗口挂载物化(:func:`build_window_eval_bundle`,与 factor_series
    主构建同一挂载原语与 PIT 防线)复用于全目录逐因子计算;决策日 =
    发布交易日轴自 ``window_start`` 起每 ``decision_step`` 个交易日,
    末端不足一个 ``horizon`` 的决策日不进 IC / 分组统计(引擎口径)。
    """
    import time

    from finboard_backtest.research_sandbox.predefined_runner import (
        build_window_eval_bundle,
    )
    from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec
    from finboard_data.trading_calendar import trading_days

    started = time.monotonic()
    cfg = config or FactorEvalConfig()
    names = resolve_eval_factors(factors)
    all_days = tuple(
        sorted(
            day
            for day in trading_days(window_start, window_end)
            if window_start <= day <= window_end
        )
    )
    if not all_days:
        raise ValueError(
            f"窗口 [{window_start.isoformat()}, {window_end.isoformat()}] "
            "内无 A 股交易日(交易日历未加载或窗口非法)"
        )
    decision_dates = tuple(all_days[::decision_step])
    spec = FactorSeriesRunSpec(
        code_artifact="factor_eval",
        code_commit=f"factor-eval-{EVAL_REPORT_SCHEMA_VERSION}",
        release_id=release_id,
        dataset_release_ids=tuple(sorted(set(dataset_release_ids))),
        params={},
        window_start=window_start,
        window_end=window_end,
        dates=decision_dates,
    )
    bundle = await build_window_eval_bundle(
        spec,
        settings=settings,
        release_provider_factory=release_provider_factory,
    )

    reports: list[FactorEvalReport] = []
    for name in names:
        definition = get_predefined_factor(name)
        frame = definition.compute(bundle.input_for(definition))
        reports.append(
            evaluate_factor_values(definition, frame, bundle.close_panel, config=cfg)
        )
    reports.sort(key=lambda item: item.factor)
    return build_report(
        reports=reports,
        data_face={
            "mode": "release",
            "release_id": release_id,
            "dataset_release_ids": list(spec.dataset_release_ids),
            "note": (
                "真实冻结发布小窗口评估;窗口短于长窗口因子声明的历史"
                "需求时,相应因子如实呈现 no_data / "
                "window_shorter_than_declaration,不虚构数值。"
            ),
        },
        config=cfg,
        window={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "trading_days": len(all_days),
            "decision_dates": len(decision_dates),
            "decision_step": decision_step,
        },
        elapsed_seconds=time.monotonic() - started,
    )
