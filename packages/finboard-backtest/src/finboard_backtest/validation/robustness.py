"""稳健性 / 压力测试(issue #57)。

提供四类稳健性诊断:

1. **参数邻域分析** —— 围绕最优参数做 +-10% 网格扫描,检查"孤岛最优"。
2. **成本压力** —— 1x / 2x / 3x 佣金与印花税、5/10/20 bps 滑点。
3. **执行延迟** —— 1 / 2 Bar 延迟(测试信号当天 vs 下下 Bar)。
4. **关键市场阶段** —— 子区间(2018-Q4 贸易战 / 2020-Q1 新冠 / 2022-Q1 加息 / 2024-Q1 微盘股)。

任一压力测试失败(``passed=False``)的 trial 不能标记 VALIDATED_OOS。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast

from finboard_backtest.validation.contracts import RobustnessPlan, RobustnessProbe

# ---------------------------------------------------------------------------
# 参数邻域分析
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParameterNeighbour:
    """参数邻域中的一个采样点。"""

    parameters: dict[str, object]
    label: str


def generate_neighbourhood(
    base_params: Mapping[str, object],
    *,
    numeric_steps: int = 5,
    relative_step: float = 0.1,
) -> list[ParameterNeighbour]:
    """围绕 base_params 生成参数邻域采样点。

    只对数值型参数做扫描(int / float / Decimal);字符串 / 布尔 / 列表参数
    保持原值。每个数值参数生成 ``numeric_steps`` 个邻域点(包括 base)。

    参数
    ----
    base_params
        最优 trial 的参数。
    numeric_steps
        每个数值参数扫描的点数(奇数,中心点为 base)。
    relative_step
        步长相对值(0.1 = ±10%)。

    返回
    ----
    ``list[ParameterNeighbour]`` —— 至少包含 base 本身。
    """
    if numeric_steps < 1:
        numeric_steps = 1
    if numeric_steps % 2 == 0:
        numeric_steps += 1  # 保证中心是 base
    if relative_step <= 0:
        relative_step = 0.1

    # 识别数值参数
    numeric_keys: list[str] = []
    for k, v in base_params.items():
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            numeric_keys.append(k)

    if not numeric_keys:
        return [ParameterNeighbour(dict(base_params), "base")]

    # 围绕每个数值参数生成 offsets,笛卡尔积
    offsets_per_key: dict[str, list[tuple[float, str]]] = {}
    half = numeric_steps // 2
    for key in numeric_keys:
        base_val = float(cast(float, base_params[key]))
        offsets: list[tuple[float, str]] = []
        for i in range(-half, half + 1):
            ratio = 1.0 + i * relative_step
            offsets.append((base_val * ratio, f"{key}={base_val * ratio:.4g}"))
        offsets_per_key[key] = offsets

    # 笛卡尔积(限制总点数防爆炸)
    max_combinations = 50
    result: list[ParameterNeighbour] = []
    _cartesian(
        list(offsets_per_key.items()),
        dict(base_params),
        result,
        max_combinations,
    )
    if not any(p.label == "base" for p in result):
        result.insert(0, ParameterNeighbour(dict(base_params), "base"))
    return result


def _cartesian(
    items: list[tuple[str, list[tuple[float, str]]]],
    base: dict[str, object],
    out: list[ParameterNeighbour],
    max_size: int,
    idx: int = 0,
    current: dict[str, object] | None = None,
    current_label: list[str] | None = None,
) -> None:
    """递归生成笛卡尔积。"""
    if current is None:
        current = dict(base)
    if current_label is None:
        current_label = []

    if len(out) >= max_size:
        return

    if idx >= len(items):
        label = "_".join(current_label) if current_label else "base"
        out.append(ParameterNeighbour(dict(current), label))
        return

    key, offsets = items[idx]
    is_modified = False
    for val, lbl in offsets:
        if len(out) >= max_size:
            return
        old = current.get(key)
        if isinstance(old, float):
            current[key] = val
        elif isinstance(old, int) and not isinstance(old, bool):
            current[key] = int(val)
        elif isinstance(old, Decimal):
            current[key] = Decimal(str(val))
        else:
            current[key] = val
        current_label.append(lbl)
        _cartesian(items, base, out, max_size, idx + 1, current, current_label)
        current_label.pop()
        current[key] = old
        is_modified = True
    if not is_modified:
        _cartesian(items, base, out, max_size, idx + 1, current, current_label)


# ---------------------------------------------------------------------------
# 成本 / 滑点 / 延迟压力
# ---------------------------------------------------------------------------


def cost_stress_configs(
    plan: RobustnessPlan,
    base_commission: Decimal,
    base_stamp_tax: Decimal,
) -> list[tuple[str, Decimal, Decimal]]:
    """生成成本压力测试配置列表。

    返回
    ----
    ``[(label, commission_rate, stamp_tax_rate), ...]``
    """
    out: list[tuple[str, Decimal, Decimal]] = []
    for m in plan.cost_multipliers:
        out.append(
            (
                f"cost_x{m}",
                base_commission * Decimal(str(m)),
                base_stamp_tax * Decimal(str(m)),
            )
        )
    return out


def slippage_stress_configs(plan: RobustnessPlan) -> list[tuple[str, Decimal]]:
    """生成滑点压力测试配置(bps)。"""
    return [(f"slippage_{s}bps", Decimal(str(s))) for s in plan.slippage_stress_bps]


def delay_stress_configs(plan: RobustnessPlan) -> list[tuple[str, int]]:
    """生成执行延迟压力测试配置(Bar 数)。"""
    return [(f"delay_{d}bar", d) for d in plan.execution_delay_bars]


# ---------------------------------------------------------------------------
# 关键市场阶段
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketPhase:
    """压力测试子区间定义。"""

    label: str
    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


DEFAULT_STRESS_PHASES: tuple[MarketPhase, ...] = (
    MarketPhase(label="2018-Q1", start=date(2018, 1, 1), end=date(2018, 3, 31)),
    MarketPhase(label="2018-Q4", start=date(2018, 10, 1), end=date(2018, 12, 31)),
    MarketPhase(label="2020-Q1", start=date(2020, 1, 1), end=date(2020, 3, 31)),
    MarketPhase(label="2022-Q1", start=date(2022, 1, 1), end=date(2022, 3, 31)),
    MarketPhase(label="2022-Q4", start=date(2022, 10, 1), end=date(2022, 12, 31)),
    MarketPhase(label="2024-Q1", start=date(2024, 1, 1), end=date(2024, 3, 31)),
    MarketPhase(label="2024-Q4", start=date(2024, 10, 1), end=date(2024, 12, 31)),
)


def stress_phases_for_range(
    start: date,
    end: date,
    phases: Sequence[MarketPhase] | None = None,
) -> list[MarketPhase]:
    """返回 [start, end] 区间内出现过的压力阶段。

    用于过滤出与回测窗口相交的市场阶段,避免报告不相关的子区间。
    """
    phases = phases if phases is not None else DEFAULT_STRESS_PHASES
    out: list[MarketPhase] = []
    for p in phases:
        # 区间相交
        if p.start <= end and p.end >= start:
            # 截断到 [start, end] 内
            out.append(
                MarketPhase(
                    label=p.label,
                    start=max(p.start, start),
                    end=min(p.end, end),
                )
            )
    return out


# ---------------------------------------------------------------------------
# 稳健性判定
# ---------------------------------------------------------------------------


def evaluate_robustness(
    probes: Sequence[RobustnessProbe],
    *,
    max_sharpe_drop: float = 0.5,
    base_sharpe: float = 0.0,
) -> bool:
    """根据全部 probe 判定 trial 是否稳健。

    参数
    ----
    probes
        所有 robustness probe 结果。
    max_sharpe_drop
        允许的 Sharpe 下降幅度(相对 base_sharpe)。最差 probe 的 Sharpe 与
        base 之差 > max_sharpe_drop 时判失败。
    base_sharpe
        IS 最优 Sharpe(作为基准)。
    """
    if not probes:
        return True  # 没做压力测试,默认通过(调用方应保证已做)

    worst_sharpe = min(p.sharpe_ratio for p in probes)
    if base_sharpe > 0:
        drop = (base_sharpe - worst_sharpe) / base_sharpe
        if drop > max_sharpe_drop:
            return False
    # 任一 probe 显式失败 → 整体失败
    return not any(not p.passed for p in probes)


# ---------------------------------------------------------------------------
# Probe 工厂
# ---------------------------------------------------------------------------


ProbeRunner = Callable[..., dict[str, object]]
"""Probe 执行函数签名:接受任意参数,返回包含 sharpe/max_drawdown/total_return 的字典。"""


def make_probe(
    probe_kind: str,
    label: str,
    *,
    sharpe_ratio: float,
    max_drawdown: float,
    total_return: float,
    thresholds: dict[str, float] | None = None,
    detail: dict[str, object] | None = None,
) -> RobustnessProbe:
    """构造一个 RobustnessProbe(简化工厂)。"""
    thresholds = thresholds or {"min_sharpe": 0.0, "max_drawdown": -0.50}
    passed = (
        sharpe_ratio >= thresholds["min_sharpe"]
        and max_drawdown >= thresholds["max_drawdown"]
    )
    return RobustnessProbe(
        probe_kind=probe_kind,
        label=label,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        total_return=total_return,
        passed=passed,
        detail=detail or {},
    )
