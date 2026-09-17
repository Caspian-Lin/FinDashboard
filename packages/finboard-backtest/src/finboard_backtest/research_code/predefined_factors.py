"""平台预置因子(p_ 前缀)的入队门控与 series 覆盖检查(issue #398)。

与用户因子(#217/#361,``user_factors.py``)同构的两层门控,差异只在
数据锚:预置因子的公式由平台目录钉死(实现版本锚内容寻址),构建
``params`` 恒为空,因此覆盖检查反查 ``find_matching`` 以 ``params={}``
计算期望 series_key(用户因子以运行参数反查,#361 语义不变):

* **编译期**(``strategy_spec.compiler``)—— 引用的 ``p_`` 因子必须在
  ``factors.predefined`` 目录注册(未注册 fail-visible);
* **入队期**(本模块,REST+MCP 共用)——
  1. 引用的 ``p_`` 因子不在目录 → 拒绝(``predefined_factor_unregistered``);
  2. single_shot 引用未被 ``factor_series_ids`` 声明覆盖的 ``p_`` 因子 →
     拒绝(``predefined_factor_series_undeclared``):p_ 因子无快照路径,
     逐日观测只能来自序列声明(对齐 #203 single_shot 缺快照秒拒风格);
  3. multi_period 未声明覆盖的 ``p_`` 因子 → series 覆盖检查(拒绝的是
     「数据没备齐」):无序列 / 锚定发布不一致 / 覆盖不足均具名拒绝,
     附 ``finboard_factor_series_build`` 重建命令;#399 起对声明
     ``min_history_bars`` 的长窗口因子追加覆盖起点检查
     (``predefined_factor_series_coverage_start_missing``,首个决策日
     全缺测 = 发布历史不足)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from finboard_backtest.factors.predefined import (
    get_predefined_factor,
    is_registered_predefined_factor,
    predefined_factor_names,
)
from finboard_data.factor_lab import (
    PREDEFINED_FACTOR_PREFIX,
    is_predefined_factor_name,
)

if TYPE_CHECKING:
    from datetime import date

    from finboard_backtest.research_code.user_factors import SeriesLookup

#: 入队拒绝文案的具名标记(issue #398,供测试 / agent 检索)
PREDEFINED_UNREGISTERED_CODE = "predefined_factor_unregistered"

#: single_shot 未声明序列的具名标记(issue #398)
PREDEFINED_SERIES_UNDECLARED_CODE = "predefined_factor_series_undeclared"

#: multi_period 覆盖不足的具名标记(issue #398)
PREDEFINED_COVERAGE_MISSING_CODE = "predefined_factor_series_coverage_missing"

#: 覆盖起点缺失的具名标记(issue #399:min_history_bars 声明的长窗口因子
#: 在首个决策日全缺测——发布历史不足,构建窗口须前移)
PREDEFINED_COVERAGE_START_MISSING_CODE = "predefined_factor_series_coverage_start_missing"

#: 序列锚定发布与 run bars 主发布不一致的具名标记(issue #398)
PREDEFINED_ANCHOR_MISMATCH_CODE = "predefined_factor_series_anchor_mismatch"

#: 覆盖不足时缺失决策日期的有界预览上限(与 #361 同值)
PREDEFINED_COVERAGE_PREVIEW_LIMIT = 10


class SeriesCoverageProbe(Protocol):
    """series 覆盖检查协议(#361 同形;默认经 #360 的
    ``series_coverage_missing`` lazy import)。"""

    def __call__(
        self,
        series: Any,
        *,
        decision_dates: Sequence[date],
    ) -> Sequence[date]: ...


def referenced_predefined_factors(
    required_factor_sources: Collection[str],
    *,
    series_covered_factors: Collection[str] = frozenset(),
) -> frozenset[str]:
    """引用的 p_ 因子名集合(可按声明序列收窄)。"""
    covered = set(series_covered_factors)
    return frozenset(
        name
        for name in required_factor_sources
        if is_predefined_factor_name(name) and name not in covered
    )


def predefined_factor_reference_gate_error(
    *,
    required_factor_sources: Collection[str],
    series_covered_factors: Collection[str] = frozenset(),
    multi_period: bool,
) -> str | None:
    """入队期 p_ 因子引用门控;返回错误文案或 None(放行)。

    1. 引用的 p_ 因子未在目录注册 → 拒绝(附可用清单);
    2. single_shot 引用未被声明序列覆盖的 p_ 因子 → 拒绝(p_ 因子无
       快照路径,逐日观测只能来自 factor_series_ids;multi_period 放行,
       由 :func:`predefined_factor_series_coverage_gate_error` 做覆盖检查)。
    """
    referenced = referenced_predefined_factors(
        required_factor_sources,
        series_covered_factors=series_covered_factors,
    )
    unregistered = sorted(
        name
        for name in referenced
        if not is_registered_predefined_factor(name)
    )
    if unregistered:
        return (
            f"引用的平台预置因子未注册({PREDEFINED_UNREGISTERED_CODE}): "
            f"{unregistered};可用: {list(predefined_factor_names())}。"
            "请修正策略规格的因子引用,或先在预置因子目录注册。"
        )
    if not multi_period and referenced:
        undeclared = sorted(referenced)
        return (
            f"引用的平台预置因子未声明因子序列({PREDEFINED_SERIES_UNDECLARED_CODE}): "
            f"{undeclared}。p_ 因子的逐日观测只能来自 factor_series 通道:"
            "请先 finboard_factor_series_build(kind=predefined_factor)对本次"
            "bars 主发布与决策窗口构建序列,再把 series_id 放进入队 payload 的 "
            "factor_series_ids(single_shot 决策时点无逐日观测路径)。"
        )
    return None


def _default_series_coverage_missing(
    series: Any,
    *,
    decision_dates: Sequence[date],
) -> Sequence[date]:
    """#360 的 ``series_coverage_missing``(lazy import,Protocol 化)。"""
    from finboard_persistence.factor_series_repo import series_coverage_missing

    result: Sequence[date] = series_coverage_missing(
        series, decision_dates=decision_dates
    )
    return result


async def predefined_factor_series_coverage_gate_error(
    *,
    referenced_predefined: Collection[str],
    series_lookup: SeriesLookup,
    bars_release_id: str,
    dataset_release_ids: Sequence[str],
    decision_dates: Sequence[date],
    window_start: date,
    window_end: date,
    coverage_missing: SeriesCoverageProbe | None = None,
) -> str | None:
    """multi_period x p_ 因子的 series 覆盖检查;返回错误文案或 None(放行)。

    逐个引用的 p_ 因子(判定语义与 #361 用户因子通道同构):

    1. ``find_matching`` 找不到 series → 具名拒绝(附重建命令);
    2. series 的 ``release_id`` 与 run 的 bars 主发布不一致 → 具名锚定拒绝;
    3. 覆盖不足 → 具名拒绝(缺失决策日期有界预览 + 重建命令)。

    与用户因子的差异:反查 ``params={}``(预置因子构建恒无参数)。
    ``decision_dates`` 由调用方按声明的 ``decision_schedule`` 推导;
    空列表不构成覆盖缺口,放行(执行期既有根因报错兜底)。
    """
    referenced = sorted(referenced_predefined)
    if not referenced or not decision_dates:
        return None
    probe = coverage_missing or _default_series_coverage_missing
    for name in referenced:
        series = await series_lookup.find_matching(
            code_artifact=name.removeprefix(PREDEFINED_FACTOR_PREFIX),
            release_id=bars_release_id,
            dataset_release_ids=tuple(dataset_release_ids),
            params={},
            window_start=window_start,
            window_end=window_end,
        )
        rebuild_hint = (
            f"请先 finboard_factor_series_build(kind=predefined_factor, "
            f"name={name.removeprefix(PREDEFINED_FACTOR_PREFIX)!r})对本次 "
            f"bars 主发布与决策窗口构建序列后重新入队"
        )
        if series is None:
            return (
                f"平台预置因子 {name} 在决策窗口内未找到已构建的因子 series"
                f"({PREDEFINED_COVERAGE_MISSING_CODE};window "
                f"{window_start.isoformat()}→{window_end.isoformat()},"
                f"bars 主发布 {bars_release_id})。{rebuild_hint}"
            )
        series_release = getattr(series, "release_id", None)
        if series_release != bars_release_id:
            return (
                f"平台预置因子 {name} 的 series 锚定发布 {series_release!r} "
                f"与本次 bars 主发布 {bars_release_id!r} 不一致"
                f"({PREDEFINED_ANCHOR_MISMATCH_CODE})。series 必须锚定本次"
                f"运行的 bars 主发布(换发布需重建);{rebuild_hint}"
            )
        missing = sorted(probe(series, decision_dates=decision_dates))
        if missing:
            preview = ", ".join(
                day.isoformat() for day in missing[:PREDEFINED_COVERAGE_PREVIEW_LIMIT]
            )
            more = (
                f"(共 {len(missing)} 天,仅列前 {PREDEFINED_COVERAGE_PREVIEW_LIMIT})"
                if len(missing) > PREDEFINED_COVERAGE_PREVIEW_LIMIT
                else ""
            )
            return (
                f"平台预置因子 {name} 的 series 未覆盖全部决策日"
                f"({PREDEFINED_COVERAGE_MISSING_CODE}):缺失 {len(missing)} 个"
                f"决策日{more}: {preview}。{rebuild_hint}"
            )
        coverage_start_error = _coverage_start_error(
            name, series, decision_dates, rebuild_hint
        )
        if coverage_start_error is not None:
            return coverage_start_error
    return None


def _coverage_start_error(
    name: str,
    series: Any,
    decision_dates: Sequence[date],
    rebuild_hint: str,
) -> str | None:
    """``min_history_bars`` 覆盖起点检查(issue #399,#361 覆盖检查的消费点)。

    声明了覆盖起点的因子(如 1320d 长窗口族)在**首个决策日**必须已有
    非缺测值:窗口挂载对发布历史不设下界,首个决策日全缺测 = 发布历史
    不足 ``min_history_bars`` 根 bar,序列前段(乃至全部)为设计内缺测,
    消费端拿到全 None 特征会静默 0 信号(#255 教训)——入队具名拒绝,
    指路「构建窗口前移 / 改用短窗口变体」。未声明 ``min_history_bars``
    的因子不做值检查(批次 0 语义零变化)。
    """
    definition = get_predefined_factor(name.removeprefix(PREDEFINED_FACTOR_PREFIX))
    warmup = definition.min_history_bars
    if warmup is None or not decision_dates:
        return None
    values: dict[str, dict[str, float | None]] = getattr(series, "values", None) or {}
    first_day = min(decision_dates)
    first_day_values = values.get(first_day.isoformat())
    if first_day_values and any(
        value is not None for value in first_day_values.values()
    ):
        return None
    realized_start = next(
        (
            day
            for day in getattr(series, "dates", ())
            if any(
                value is not None
                for value in (values.get(day.isoformat()) or {}).values()
            )
        ),
        None,
    )
    realized_note = (
        f"首个非缺测决策日 {realized_start.isoformat()}"
        if realized_start is not None
        else "窗口内无任何非缺测值"
    )
    return (
        f"平台预置因子 {name} 声明需要 >= {warmup} 根 bar 历史"
        f"(min_history_bars),但序列在首个决策日 {first_day.isoformat()} "
        f"全部缺测({realized_note};{PREDEFINED_COVERAGE_START_MISSING_CODE})"
        "——发布历史不足,覆盖起点晚于构建窗口起点。请把构建窗口 "
        "window_start 前移到覆盖所需历史之前,或改用短窗口变体;"
        f"{rebuild_hint}"
    )


__all__ = [
    "PREDEFINED_ANCHOR_MISMATCH_CODE",
    "PREDEFINED_COVERAGE_MISSING_CODE",
    "PREDEFINED_COVERAGE_PREVIEW_LIMIT",
    "PREDEFINED_COVERAGE_START_MISSING_CODE",
    "PREDEFINED_SERIES_UNDECLARED_CODE",
    "PREDEFINED_UNREGISTERED_CODE",
    "SeriesCoverageProbe",
    "predefined_factor_reference_gate_error",
    "predefined_factor_series_coverage_gate_error",
    "referenced_predefined_factors",
]
