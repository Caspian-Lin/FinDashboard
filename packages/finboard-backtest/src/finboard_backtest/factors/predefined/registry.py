"""平台预置因子目录(issue #398,批次 0;批次 #399-#402 的注册地基)。

目录条目 = 「公式即代码」:name / 公式描述 / 数据依赖 / 方向 /
signal_eligible / 参数化窗口,``compute`` 是平台可信代码 —— 构建走
**进程内** factor_series 通道(免用户因子的容器税,审计 / 内容寻址 /
覆盖检查全套同构,见 ``research_sandbox.predefined_runner``)。

**引用命名** ``p_<name>``(``PREDEFINED_FACTOR_PREFIX``,与用户因子
``u_`` 对称,见 ``finboard_data.factor_lab``);目录内部只存裸名。

**批次 0 样板族** ``return_{21,63,126,252}d``:同一参数化实现按 tushare
命名展开注册(动量族 return_N;批次 1 起的 ~75 个量价因子照此模式批量
注册,注册指南见 PR「新预置因子注册指南」)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
)
from finboard_backtest.factors.predefined.operators import ts_delay, ts_delta
from finboard_data.factor_lab import FactorPreference

#: 因子裸名规则(目录内部不带 p_/u_ 前缀)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 目录级 schema 版本(参与 commit 锚;调整采样/依赖语义等跨因子规则时递增)
PREDEFINED_FACTORS_SCHEMA_VERSION = "v1"

#: compute 的函数签名:(PredefinedFactorInput) -> FactorSeriesFrame
PredefinedFactorCompute = Callable[[PredefinedFactorInput], FactorSeriesFrame]


@dataclass(frozen=True)
class PredefinedFactorDefinition:
    """一个平台预置因子的目录条目。

    * ``name`` —— 裸名(如 ``return_21d``),引用名 = ``p_return_21d``;
    * ``data_dependencies`` —— 数据依赖声明,条目形如
      ``"bars.<field>"`` / ``"daily_metrics.<field>"`` /
      ``"financial_indicators.<field>"`` / ``"index_bars.close"``(指数
      基准行情,引擎按发布 instruments 识别);构建引擎据此决定加载哪些
      挂载数据集,#401/#402 基本面因子由此声明研究发布依赖;
    * ``direction`` —— 研究语义方向(``FactorPreference``;HIGHER =
      值越大越看多),只做研究偏好,信号方向仍由策略规格决定;
    * ``implementation_version`` —— **实现版本**:公式 / 数值语义任何
      变化必须递增;它是内容寻址 commit 锚的原料(版本不变 → 同参数
      重建命中缓存,版本变化 → 新序列);
    * ``cross_section`` —— 最终值是截面算子产物:构建采样面收窄到
      可交易域(#380:截面分母不得混入 benchmark-only);时序因子
      False(全挂载标的,消费端统一剔除基准)。
    """

    name: str
    title: str
    family: str
    direction: FactorPreference
    signal_eligible: bool
    data_dependencies: tuple[str, ...]
    window: int | None
    implementation_version: str
    compute: PredefinedFactorCompute
    cross_section: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"预置因子名 {self.name!r} 不合规则(小写 [a-z][a-z0-9_]*,"
                "目录内部不带 p_/u_ 前缀)"
            )
        if not self.title or not self.family or not self.implementation_version:
            raise ValueError(f"{self.name}: title/family/implementation_version 必填")
        if self.window is not None and self.window < 1:
            raise ValueError(f"{self.name}: window 须 >= 1 或 None")
        if not self.data_dependencies:
            raise ValueError(f"{self.name}: 必须声明数据依赖")
        for item in self.data_dependencies:
            if "." not in item:
                raise ValueError(
                    f"{self.name}: 数据依赖 {item!r} 须为 '<dataset>.<field>' 形态"
                )


def _momentum_return(window: int) -> PredefinedFactorCompute:
    """参数化的 ``return_Nd`` 实现(同族窗口变体一份代码,#398)。

    ``return_Nd = close[t] / close[t-N] - 1``——N 根 bar 的区间收益
    (qfq 收盘;停牌缺行使窗口自然后移到最近可得 bar,「N 根 bar」
    而非「N 个日历日」);历史不足 N 根 → 缺测(None)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, Any] = {}
        for symbol, series in closes.items():
            base = ts_delay(series.values, window)
            per_symbol[symbol] = ts_delta(series.values, window) / base
        return inp.sample(per_symbol)

    return compute


def _definition(
    name: str,
    *,
    title: str,
    family: str,
    window: int,
    direction: FactorPreference = FactorPreference.HIGHER,
    cross_section: bool = False,
    signal_eligible: bool = True,
    data_dependencies: tuple[str, ...] = ("bars.close",),
    implementation_version: str = "1",
) -> PredefinedFactorDefinition:
    """``return_Nd`` 族的条目工厂(唯一公式实现,窗口变体展开注册)。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=signal_eligible,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version=implementation_version,
        compute=_momentum_return(window),
        cross_section=cross_section,
    )


#: 目录(裸名 → 条目)。批次 0 样板:return_Nd 动量族(4 窗口变体);
#: 批次 1-4(~150+ 因子)按同一模式在此追加注册。
PREDEFINED_FACTORS: dict[str, PredefinedFactorDefinition] = {
    item.name: item
    for item in (
        _definition(
            "return_21d",
            title="return_21d = close / close[-21] - 1(21 根 bar 区间收益,动量)",
            family="momentum",
            window=21,
        ),
        _definition(
            "return_63d",
            title="return_63d = close / close[-63] - 1(季度动量)",
            family="momentum",
            window=63,
        ),
        _definition(
            "return_126d",
            title="return_126d = close / close[-126] - 1(半年动量)",
            family="momentum",
            window=126,
        ),
        _definition(
            "return_252d",
            title="return_252d = close / close[-252] - 1(年度动量)",
            family="momentum",
            window=252,
        ),
    )
}


def get_predefined_factor(name: str) -> PredefinedFactorDefinition:
    """按裸名取目录条目;未知名 KeyError 附可用清单(入队/编译期秒拒用)。"""
    try:
        return PREDEFINED_FACTORS[name]
    except KeyError:
        raise KeyError(
            f"未注册的平台预置因子: {name!r};可用: {sorted(PREDEFINED_FACTORS)}"
        ) from None


def is_registered_predefined_factor(name: str) -> bool:
    """裸名(或 p_ 引用名)是否已注册。"""
    bare = name.removeprefix("p_")
    return bare in PREDEFINED_FACTORS


def predefined_factor_names() -> tuple[str, ...]:
    """全部已注册裸名(稳定排序)。"""
    return tuple(sorted(PREDEFINED_FACTORS))


def predefined_factor_commit(name: str) -> str:
    """内容寻址的 commit 锚:``predefined-<sha256[:12]>``。

    锚原料 = schema 版本 + 目录条目的**语义字段**(name / 公式依赖 /
    窗口 / 截面标记 / 实现版本);title 等文档性字段不参与(改文案不使
    序列失效)。任何语义变化(实现版本递增 / 依赖调整)→ 新锚 → 新
    series_key → 托管重建;不变 → 同参数重建命中缓存。
    """
    item = get_predefined_factor(name)
    payload = {
        "schema_version": PREDEFINED_FACTORS_SCHEMA_VERSION,
        "name": item.name,
        "data_dependencies": sorted(item.data_dependencies),
        "window": item.window,
        "cross_section": item.cross_section,
        "implementation_version": item.implementation_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"predefined-{digest[:12]}"


def _validate_catalog() -> None:
    """导入期目录防漂移(与 BENCHMARK_INDEX_REGISTRY 同风格)。"""
    for name, item in PREDEFINED_FACTORS.items():
        if item.name != name:
            raise ValueError(f"目录键与条目名不一致: {name} vs {item.name}")
        expected = predefined_factor_commit(name)
        if not expected.startswith("predefined-"):
            raise ValueError(f"commit 锚形态非法: {name}")


_validate_catalog()

__all__ = [
    "PREDEFINED_FACTORS",
    "PREDEFINED_FACTORS_SCHEMA_VERSION",
    "PredefinedFactorCompute",
    "PredefinedFactorDefinition",
    "get_predefined_factor",
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
]
