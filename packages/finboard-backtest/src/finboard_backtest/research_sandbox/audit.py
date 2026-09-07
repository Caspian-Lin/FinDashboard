"""前缀不变性审计引擎(issue #359,纯函数,build_fn 注入)。

协议 v2(``factor.compute_series``)把逐日 PIT 从「单日容器物理隔离」改为
「窗口物理隔离 + 访问器契约」,违规定义:**value[t] 依赖了
available_at > t 的数据**。本模块用可重放性把该违规定义变成可检测的
数学性质 —— 前缀不变性(prefix invariance):

* **truncation(截断)**:把窗口截断到 cut 再算(``build([t0, cut])``),
  与全窗口计算(``build([t0, t1])``)在 ``[t0, cut]`` 上逐值相等 —— 截断
  只会移除 cut 之后的数据,若 cut 之前的价值因此改变,说明它们读到了
  cut 之后的数据;
* **perturbation(扰动)**:build_fn 第二参 ``perturb_from = cut`` 表示
  **严格晚于 cut 日**的数据被扰动,cut 当日及之前的可用数据不变;
  重算后 ``[t0, cut]``(含 cut 当日)的值必须不变 —— cut 当日截面读了
  cut+1 的数据即被检出,同一否定命题的独立实现。

两种模式互为对照:一个诚实的 trailing 因子两者都通过;故意读未来的
因子两者都会在**首个分歧日期**暴露(报告携带该日基线/变体因子值对照)。

``build_fn`` 是被审计的区间构建函数(注入:测试给纯函数,#360 编排给
``run_factor_series_container`` 的包装),签名
``(dates: Sequence[date], perturb_from: date | None) -> FactorSeriesLike``
—— 返回任何带 ``.dates`` / ``.values`` 的对象(kit ``FactorSeries``)或
``Mapping[date, Mapping[str, float | None]]``;truncation 模式忽略第二参。

编排接入(build 抽样 / promote 门)随存储 issue(#360)落地,本模块只做
纯引擎。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

#: build_fn 的返回形态(kit FactorSeries 或等价 Mapping)
FactorSeriesLike = Mapping[date, Mapping[str, float | None]] | object

BuildFn = Callable[[Sequence[date], "date | None"], Awaitable[FactorSeriesLike]]

AuditMode = Literal["truncation", "perturbation"]


@dataclass(frozen=True)
class PrefixInvarianceReport:
    """一次前缀不变性审计的结果。

    ``passed=False`` 时 ``first_divergence_date`` 为**全部分歧中最早**的
    决策日,``baseline_values`` / ``variant_values`` 为该日的因子值对照
    (基线 = 全窗口干净重算;变体 = 截断/扰动后的重算),``divergent_cut``
    指出暴露该分歧的 cut 点。比较为精确逐值(None 与 NaN 视为同一缺测)。
    """

    mode: AuditMode
    passed: bool
    cut_points: tuple[date, ...]
    audited_dates: tuple[date, ...]
    checks_run: int
    first_divergence_date: date | None = None
    divergent_cut: date | None = None
    baseline_values: Mapping[str, float | None] = field(default_factory=dict)
    variant_values: Mapping[str, float | None] = field(default_factory=dict)
    divergent_symbols: tuple[str, ...] = ()
    failure: str | None = None


def _cross_section(result: FactorSeriesLike, day: date) -> Mapping[str, float | None] | None:
    values = getattr(result, "values", result)
    if isinstance(values, Mapping):
        cross = values.get(day)
        if cross is None:
            return None
        return cross if isinstance(cross, Mapping) else None
    return None


def _value_equal(a: float | None, b: float | None) -> bool:
    """精确逐值比较(None 与 NaN 视为同一缺测;inf 只与 inf 相等)。"""
    if a is None or b is None:
        both_missing = (a is None or (isinstance(a, float) and math.isnan(a))) and (
            b is None or (isinstance(b, float) and math.isnan(b))
        )
        return both_missing
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    return bool(a == b)


def _first_divergence(
    baseline: FactorSeriesLike,
    variant: FactorSeriesLike,
    compare_dates: Sequence[date],
) -> tuple[date, list[str], dict[str, float | None], dict[str, float | None]] | None:
    """在 compare_dates 升序上找首个分歧日;返回 (日, 分歧标的, 基线, 变体)。"""
    for day in compare_dates:
        base_cross = _cross_section(baseline, day)
        var_cross = _cross_section(variant, day)
        if base_cross is None and var_cross is None:
            continue
        if base_cross is None or var_cross is None:
            # 一侧整日缺测另一侧有值 —— 截断/扰动凭空造出或抹掉整日截面
            return (
                day,
                ["<missing-cross-section>"],
                dict(base_cross or {}),
                dict(var_cross or {}),
            )
        symbols = sorted(set(base_cross) | set(var_cross))
        divergent = [
            s for s in symbols if not _value_equal(base_cross.get(s), var_cross.get(s))
        ]
        if divergent:
            return (
                day,
                divergent,
                {s: base_cross.get(s) for s in divergent},
                {s: var_cross.get(s) for s in divergent},
            )
    return None


def _render_failure(
    mode: AuditMode,
    cut: date,
    day: date,
    divergent: Sequence[str],
    baseline: Mapping[str, float | None],
    variant: Mapping[str, float | None],
) -> str:
    sample = divergent[0]
    return (
        f"前缀不变性审计未通过(mode={mode},cut={cut.isoformat()}):"
        f"首个分歧日期 {day.isoformat()},标的 {sample} 基线值 "
        f"{baseline.get(sample)!r} vs 变体值 {variant.get(sample)!r}"
        f"({len(divergent)} 个分歧标的)—— value[t] 依赖了 available_at > t "
        "的数据,违反 compute_series PIT 契约"
    )


async def run_prefix_invariance_audit(
    build_fn: BuildFn,
    *,
    mode: AuditMode,
    cut_points: Sequence[date],
    dates: Sequence[date] | None = None,
) -> PrefixInvarianceReport:
    """对一个区间构建函数跑前缀不变性审计(纯引擎,无 IO)。

    参数:

    * ``build_fn``:``(dates, perturb_from) -> FactorSeriesLike``;truncation
      模式以截断后的日期序列调用(忽略第二参),perturbation 模式以完整
      日期序列 + 扰动起点调用;
    * ``mode``:``truncation`` 或 ``perturbation``;
    * ``cut_points``:cut 点序列(升序;每个 cut 产生一次对照重算);
    * ``dates``:完整审计窗口的决策日(升序)。**缺省 = ``cut_points``
      本身**(此时最后一个 cut 的对照是空操作,只在前几个 cut 上有意义);
      正式编排(#360)应显式传入全窗口决策日。

    检出即短路:首个分歧(最早的 cut x 最早的日期)即返回,不再跑剩余
    cut —— 审计目标是定位与拒绝,不是穷举。
    """
    full_dates = tuple(dates) if dates is not None else tuple(cut_points)
    if not full_dates:
        raise ValueError("审计窗口 dates 不能为空")
    if list(full_dates) != sorted(full_dates):
        raise ValueError("dates 须按升序排列")
    ordered_cuts = tuple(sorted(set(cut_points)))
    if not ordered_cuts:
        raise ValueError("cut_points 不能为空")
    lower, upper = full_dates[0], full_dates[-1]
    outside = [c for c in ordered_cuts if c < lower or c > upper]
    if outside:
        raise ValueError(
            f"cut_points 含窗口外日期: {outside[:3]}(window [{lower}, {upper}])"
        )

    baseline = await build_fn(full_dates, None)
    checks = 0
    for cut in ordered_cuts:
        if mode == "truncation":
            prefix = [d for d in full_dates if d <= cut]
            if not prefix:
                continue
            variant = await build_fn(prefix, None)
            compare_dates = prefix
        else:
            variant = await build_fn(full_dates, cut)
            compare_dates = [d for d in full_dates if d <= cut]
            if not compare_dates:
                continue
        checks += 1
        found = _first_divergence(baseline, variant, compare_dates)
        if found is not None:
            day, divergent, base_cross, var_cross = found
            return PrefixInvarianceReport(
                mode=mode,
                passed=False,
                cut_points=ordered_cuts,
                audited_dates=full_dates,
                checks_run=checks,
                first_divergence_date=day,
                divergent_cut=cut,
                baseline_values=base_cross,
                variant_values=var_cross,
                divergent_symbols=tuple(divergent),
                failure=_render_failure(
                    mode, cut, day, divergent, base_cross, var_cross
                ),
            )
    return PrefixInvarianceReport(
        mode=mode,
        passed=True,
        cut_points=ordered_cuts,
        audited_dates=full_dates,
        checks_run=checks,
    )


__all__ = [
    "AuditMode",
    "BuildFn",
    "FactorSeriesLike",
    "PrefixInvarianceReport",
    "run_prefix_invariance_audit",
]
