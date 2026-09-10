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

**批次 3(#401)财务因子族** ``fin_*``(40 个 Growth / Quality):
数据依赖 = ``financial_indicators.<field>``,输入是**公告序列**(每行
一次公告修订,按 available_at 升序),采样取「决策日可见的最近一次
公告」→ 公告频率步进函数;单季 QoQ 直接用上游 q_ 前缀单季字段(诚实
取数,不做跨报告期自推导),加速度族为同比增速的公告序一阶差分。

**批次 4(#402)价值 / 质量族** ``val_*`` / ``qlt_*`` / ``qmj_*``:
原料为三表 + dividend 研究发布(``income_statements`` /
``balance_sheets`` / ``cashflow_statements`` / ``dividends``,#397)。
取数经 ``research_dataset(kind, field)``(公告步进,announcement_date
PIT)与 ``dividend_events()``(事件史);分子 = 决策日可见的最近一次
公告值,分母 = daily_metrics 市值 / 收盘价(同日可见口径)—— 财报
稀疏期的前向填充与 #187 联合装配同口径。诚实边界:利润表 / 现金流量
表流量科目为**报告期累计值**(未年化 / 未 TTM,#401 同边界);
``qmj_*`` 支柱与合成为截面算子产物(``cross_section=True``,采样面
收窄到可交易域),合成口径见各条目 title/docstring。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from finboard_backtest.factors.predefined.context import (
    DividendEventHistory,
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
)
from finboard_backtest.factors.predefined.operators import (
    cs_rank,
    ts_delay,
    ts_delta,
)
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
        return inp.sample(closes, per_symbol)

    return compute


def _financial_level(field: str) -> PredefinedFactorCompute:
    """财务公告水平因子实现(#401):公告字段原值直接作为因子值。

    值 = 该标的财务公告序列(每行一次公告修订)的字段值,采样取
    「决策日可见的最近一次公告」—— 财务指标天然是公告频率的步进函数
    (公告之间持有上一期值);比率字段已由摄取层归一为小数
    (percent 类 ÷100,倍数/比率类原值)。因子必须因果:位置 ``i`` 的
    输出就是公告 ``i`` 自身,天然只依赖 ``<= i`` 的行。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        financial = inp.financial_indicators(field)
        return inp.sample(
            financial, {s: series.values for s, series in financial.items()}
        )

    return compute


def _financial_accel(field: str) -> PredefinedFactorCompute:
    """财务公告加速度因子实现(#401):同比增速的公告序一阶差分。

    ``value[i] = growth[i] - growth[i-1]``——最新公告的同比增速相对
    **上一条公告**(即上一报告期)的变化,衡量增长动量(加速/减速)。
    诚实边界:分母是「上一条公告」而非严格「去年同报告期」——上游
    修订公告(同报告期二次公告)会作为独立行插入序列,该位置差分值
    为修订前后增速之差(通常接近 0);首条公告 → NaN(缺测)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        financial = inp.financial_indicators(field)
        per_symbol = {
            symbol: ts_delta(series.values, 1)
            for symbol, series in financial.items()
        }
        return inp.sample(financial, per_symbol)

    return compute


def _financial_definition(
    name: str,
    *,
    title: str,
    family: str,
    field: str,
    direction: FactorPreference = FactorPreference.HIGHER,
    compute: PredefinedFactorCompute | None = None,
) -> PredefinedFactorDefinition:
    """财务因子条目工厂(#401):公告频率步进序列,无参数化窗口。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=True,
        data_dependencies=(f"financial_indicators.{field}",),
        window=None,
        implementation_version="1",
        compute=compute or _financial_level(field),
    )


# --------------------------------------------------------------------- #
# 批次 4(#402):三表 / dividend 消费的价值 / 质量因子机制
# --------------------------------------------------------------------- #

#: 精确股息率的滚动窗口长度(「近 12 个月」= 决策日往前 365 天,半开
#: 区间 ``(day - 365, day]``,按除权除息日归属)
_DPS_WINDOW_DAYS = 365

#: 逐标的原始截面(``{symbol: float}``,缺测 NaN;供支柱 rank 合成)
_RawCross = Callable[[PredefinedFactorInput, date], dict[str, float]]


def _safe_div(numerator: float, denominator: float) -> float:
    """缺测纪律除法:任一端 NaN、分母 <= 0 → NaN(采样层统一归一 None)。

    分母(市值 / 收盘价 / 收入 / 利润等)在本族比值里均为正量:
    负值与 0 一样按数据异常缺测处理(不虚构反号比值;「亏损每股收益
    分母」类语义由调用方先行具名拒绝,见 ``_payout_ratio_raw``)。
    """
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        return math.nan
    if denominator <= 0.0:
        return math.nan
    return numerator / denominator


def _dataset_series(
    inp: PredefinedFactorInput, kind: str, field: str
) -> dict[str, SymbolSeries]:
    """按 kind 分发到对应取数口(daily_metrics 专属口 / 公告类通用口)。"""
    if kind == "daily_metrics":
        return inp.daily_metrics(field)
    return inp.research_dataset(kind, field)


@dataclass(frozen=True)
class _NumComponent:
    """比值分子的一项:``sign * value``;缺测按可选/必选两种语义。

    * ``required=True`` —— 该项缺测 → 整个分子缺测(差值/单科目语义,
      如应计 = 净利润 - 经营现金流,缺一不可);
    * ``required=False`` —— 该项缺测按 0 计(合计语义:预收款项 +
      合同负债新旧准则并存、商誉/无形资产未报告常态为空;全部分子
      项均缺测仍 → None)。
    """

    kind: str
    field: str
    sign: float = 1.0
    required: bool = False


def _ratio_compute(
    numerator: tuple[_NumComponent, ...],
    denominator: tuple[str, str],
) -> PredefinedFactorCompute:
    """公告步进比值因子的通用实现(#402)。

    每个决策日:分子 = 各公告序列「决策日可见最近公告」的带符号合成,
    分母 = ``denominator`` 序列同日可见值(市值 / 收盘价等日频或公告
    序列);任意一端不可见 / 分母非正 → None。universe = 分子任一序列
    覆盖的标的(无分子数据 = 结构性缺测,不入截面,#401 同语义)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        num_series = [
            _dataset_series(inp, item.kind, item.field) for item in numerator
        ]
        den_kind, den_field = denominator
        den_series = _dataset_series(inp, den_kind, den_field)
        universe = tuple(dict.fromkeys(
            symbol for series in num_series for symbol in series
        ))
        frame: FactorSeriesFrame = {}
        for day in inp.decision_dates:
            cross: dict[str, float | None] = {}
            for symbol in universe:
                total = 0.0
                any_value = False
                blocked = False
                for series, item in zip(num_series, numerator, strict=True):
                    value = (
                        series[symbol].asof(day)
                        if symbol in series
                        else math.nan
                    )
                    if not math.isfinite(value):
                        if item.required:
                            blocked = True
                        continue
                    total += item.sign * value
                    any_value = True
                if blocked or not any_value:
                    cross[symbol] = None
                    continue
                den = (
                    den_series[symbol].asof(day)
                    if symbol in den_series
                    else math.nan
                )
                value = _safe_div(total, den)
                cross[symbol] = value if math.isfinite(value) else None
            frame[day] = cross
        return frame

    return compute


def _ratio_definition(
    name: str,
    *,
    title: str,
    family: str,
    numerator: tuple[_NumComponent, ...],
    denominator: tuple[str, str],
    direction: FactorPreference = FactorPreference.HIGHER,
) -> PredefinedFactorDefinition:
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=True,
        data_dependencies=tuple(
            dict.fromkeys(
                [f"{item.kind}.{item.field}" for item in numerator]
                + [f"{denominator[0]}.{denominator[1]}"]
            )
        ),
        window=None,
        implementation_version="1",
        compute=_ratio_compute(numerator, denominator),
    )


def _dps_ttm_at(history: DividendEventHistory, day: date) -> float:
    """近 12 个月每股现金分红(税前,元/股;除权除息日归属)。

    * 决策日可见行 = ``available_at <= 决策日日终``(PIT;迟到公告在
      可见前不计,可见后补进窗口 —— 除息日已过的分红照常计入,与
      「除权除息日对齐」一致);
    * 同一 ``report_period``(分红年度)的多条进展行(预案/股东大会/
      实施)取**决策日可见的最新一行**的 (cash_div, ex_date),消除
      进展口径重复计数;
    * 窗口 = ``(day - 365, day]``:最新行 ``ex_date`` 落入窗口才计入
      (未到实施阶段 ex_date 为空 → 不计;已公告但尚未除息 → 不计,
      价格尚未除息调整)。
    """
    rows = history.visible_rows(day)
    if not rows:
        # 决策日无任何可见分红进展 → 缺测(不是 0:未知 ≠ 零分红)
        return math.nan
    latest: dict[date, int] = {}
    for row in history.visible_rows(day):
        period = history.report_periods[row]
        if period is not None:
            latest[period] = row
    window_start = day - timedelta(days=_DPS_WINDOW_DAYS)
    total = 0.0
    for row in latest.values():
        ex_date = history.ex_dates[row]
        if ex_date is None or not (window_start < ex_date <= day):
            continue
        cash = float(history.cash_div[row])
        if math.isfinite(cash):
            total += cash
    return total


def _dps_ttm_cross(inp: PredefinedFactorInput, day: date) -> dict[str, float]:
    """分红事件史 → 决策日近 12 个月每股分红截面(缺测 NaN)。"""
    return {
        symbol: _dps_ttm_at(history, day)
        for symbol, history in inp.dividend_events().items()
    }


def _dividend_yield_raw(inp: PredefinedFactorInput, day: date) -> dict[str, float]:
    """精确股息率原始截面:近 12 个月每股分红 / 决策日可见收盘价。"""
    dps = _dps_ttm_cross(inp, day)
    closes = inp.daily_metrics("close")
    return {
        symbol: _safe_div(value, closes[symbol].asof(day) if symbol in closes else math.nan)
        for symbol, value in dps.items()
    }


def _payout_ratio_raw(inp: PredefinedFactorInput, day: date) -> dict[str, float]:
    """现金分红率原始截面:近 12 个月每股分红 / 最新公告每股收益。

    每股收益 ``<= 0``(亏损)→ NaN(分红率无意义,缺测不虚构);
    分子为 0(不分红)→ 0(有效值:零分红)。
    """
    dps = _dps_ttm_cross(inp, day)
    eps = inp.research_dataset("financial_indicators", "eps")
    out: dict[str, float] = {}
    for symbol, value in dps.items():
        eps_value = eps[symbol].asof(day) if symbol in eps else math.nan
        if not math.isfinite(eps_value) or eps_value <= 0.0:
            out[symbol] = math.nan
            continue
        out[symbol] = _safe_div(value, eps_value)
    return out


@dataclass(frozen=True)
class _PillarComponent:
    """QMJ 支柱成分:原始截面 + 方向(LOWER-better 成分 rank 反转)。"""

    raw: _RawCross
    invert: bool = False


def _field_cross(kind: str, field: str) -> _RawCross:
    """公告字段 → 原始截面取数(QMJ 支柱成分的缺省形态)。"""

    def cross(inp: PredefinedFactorInput, day: date) -> dict[str, float]:
        series = _dataset_series(inp, kind, field)
        return {
            symbol: item.asof(day) for symbol, item in series.items()
        }

    return cross


def _pillar_rank_cross(
    components: tuple[_PillarComponent, ...],
    inp: PredefinedFactorInput,
    day: date,
) -> dict[str, float | None]:
    """单支柱合成:成分截面 rank 的等权均值(#380 截面契约)。

    * 截面 = 可交易域(``inp.tradable_symbols``,#380:截面分母不得混入
      benchmark-only);缺测成分不入 rank 分母(cs_rank 契约);
    * LOWER-better 成分 rank 反转(1 - rank,值域 [0, 1));
    * 成分 rank 的等权均值;全部成分缺测 → None(不入截面)。
    """
    rank_maps: list[dict[str, float | None]] = []
    for component in components:
        raw = {
            symbol: value if math.isfinite(value) else None
            for symbol, value in component.raw(inp, day).items()
            if symbol in inp.tradable_symbols
        }
        ranked = cs_rank(raw)
        if component.invert:
            ranked = {
                symbol: (None if value is None else 1.0 - value)
                for symbol, value in ranked.items()
            }
        rank_maps.append(ranked)
    out: dict[str, float | None] = {}
    for symbol in inp.tradable_symbols:
        values: list[float] = []
        for ranks in rank_maps:
            value = ranks.get(symbol)
            if value is not None:
                values.append(value)
        if values:
            out[symbol] = sum(values, 0.0) / len(values)
    return out


def _pillar_compute(
    components: tuple[_PillarComponent, ...],
) -> PredefinedFactorCompute:
    """QMJ 支柱因子实现(截面 rank 均值,per 决策日)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        return {
            day: _pillar_rank_cross(components, inp, day)
            for day in inp.decision_dates
        }

    return compute


def _pillar_definition(
    name: str,
    *,
    title: str,
    components: tuple[_PillarComponent, ...],
    data_dependencies: tuple[str, ...],
) -> PredefinedFactorDefinition:
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family="quality",
        direction=FactorPreference.HIGHER,
        signal_eligible=True,
        data_dependencies=data_dependencies,
        window=None,
        implementation_version="1",
        compute=_pillar_compute(components),
        cross_section=True,
    )


def _qmj_components() -> (
    tuple[tuple[str, tuple[_PillarComponent, ...], tuple[str, ...]], ...]
):
    """QMJ 支柱注册表:(支柱名, 成分, 数据依赖)——支柱与综合共用,
    保证「可单独引用的支柱因子」与综合因子口径单一事实源。"""
    profitability = (
        _PillarComponent(_field_cross("financial_indicators", "return_on_equity")),
        _PillarComponent(_field_cross("financial_indicators", "return_on_assets")),
        _PillarComponent(_field_cross("financial_indicators", "gross_profit_margin")),
        _PillarComponent(_field_cross("financial_indicators", "ocf_to_revenue")),
    )
    growth = (
        _PillarComponent(_field_cross("financial_indicators", "revenue_yoy")),
        _PillarComponent(_field_cross("financial_indicators", "net_profit_yoy")),
    )
    safety = (
        _PillarComponent(
            _field_cross("financial_indicators", "debt_to_assets"), invert=True
        ),
        _PillarComponent(
            _field_cross("financial_indicators", "debt_to_equity"), invert=True
        ),
        _PillarComponent(
            _field_cross("financial_indicators", "equity_multiplier"), invert=True
        ),
    )
    payout = (
        _PillarComponent(_dividend_yield_raw),
        _PillarComponent(_payout_ratio_raw),
    )
    return (
        (
            "qmj_profitability",
            profitability,
            (
                "financial_indicators.return_on_equity",
                "financial_indicators.return_on_assets",
                "financial_indicators.gross_profit_margin",
                "financial_indicators.ocf_to_revenue",
            ),
        ),
        (
            "qmj_growth",
            growth,
            ("financial_indicators.revenue_yoy", "financial_indicators.net_profit_yoy"),
        ),
        (
            "qmj_safety",
            safety,
            (
                "financial_indicators.debt_to_assets",
                "financial_indicators.debt_to_equity",
                "financial_indicators.equity_multiplier",
            ),
        ),
        (
            "qmj_payout",
            payout,
            (
                "dividends.cash_div",
                "dividends.ex_date",
                "daily_metrics.close",
                "financial_indicators.eps",
            ),
        ),
    )


def _qmj_compute() -> PredefinedFactorCompute:
    """QMJ 综合因子实现:四大支柱截面值的等权均值(per 决策日)。

    缺测支柱按可用支柱均值合成;四大支柱全部缺测 → None(不虚构)。
    支柱值自身 = :func:`_pillar_rank_cross` 的组内成分 rank 均值 ——
    综合因子与各支柱因子的合成口径同源(:func:`_qmj_components`
    单一事实源),逐支柱可单独引用。
    """
    pillar_specs = tuple(
        components for _, components, _ in _qmj_components()
    )

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        frame: FactorSeriesFrame = {}
        for day in inp.decision_dates:
            pillar_values = [
                _pillar_rank_cross(components, inp, day)
                for components in pillar_specs
            ]
            cross: dict[str, float | None] = {}
            for symbol in inp.tradable_symbols:
                values: list[float] = []
                for pillar in pillar_values:
                    pillar_value = pillar.get(symbol)
                    if pillar_value is not None:
                        values.append(pillar_value)
                if values:
                    cross[symbol] = sum(values, 0.0) / len(values)
            frame[day] = cross
        return frame

    return compute


def _pillar_and_qmj_entries() -> tuple[PredefinedFactorDefinition, ...]:
    """AQR QMJ 四支柱 + 综合因子的目录条目(#402)。

    支柱合成口径(AQR QMJ,Asness-Frazzini-Pedersen 2019 的可计算代理):

    * 盈利 profitability:ROE / ROA / 毛利率 / 经营现金流收入比(高好);
    * 成长 growth:营收同比 / 归母净利同比(高好);
    * 安全 safety:资产负债率 / 产权比率 / 权益乘数(高差 → rank 反转);
    * 支付 payout:精确股息率 / 现金分红率(高好;依赖本批次 dividend
      明细,精确口径见 ``val_dividend_yield`` / ``qlt_payout_ratio``);

    每支柱 = 组内成分**截面 rank**(缺测不入分母)的等权均值,LOWER-better
    成分 rank 反转;综合 = 四支柱截面值的等权均值,缺测支柱按可用支柱
    均值合成,全缺测 → None。支柱与综合均 ``cross_section=True``
    (采样面收窄到可交易域,#380),条目均入目录可单独引用。
    """
    specs = _qmj_components()
    pillar_titles = {
        "qmj_profitability": (
            "qmj_profitability = 盈利支柱(ROE/ROA/毛利率/经营现金流收入比 "
            "截面 rank 等权均值,AQR QMJ 盈利性)"
        ),
        "qmj_growth": (
            "qmj_growth = 成长支柱(营收同比/归母净利同比 截面 rank 等权均值,"
            "AQR QMJ 成长性)"
        ),
        "qmj_safety": (
            "qmj_safety = 安全支柱(资产负债率/产权比率/权益乘数 rank 反转后等权均值,"
            "低杠杆 = 高安全,AQR QMJ 安全性)"
        ),
        "qmj_payout": (
            "qmj_payout = 支付支柱(精确股息率/现金分红率 截面 rank 等权均值,"
            "AQR QMJ 支付性;依赖 dividend 明细精确口径)"
        ),
    }
    pillar_entries = tuple(
        _pillar_definition(
            name,
            title=pillar_titles[name],
            components=components,
            data_dependencies=deps,
        )
        for name, components, deps in specs
    )
    qmj_deps = tuple(
        dict.fromkeys(dep for _, _, deps in specs for dep in deps)
    )
    qmj_entry = PredefinedFactorDefinition(
        name="qmj",
        title=(
            "qmj = Quality Minus Junk 综合质量 = 四支柱(盈利/成长/安全/支付)"
            "截面值等权均值;缺测支柱按可用支柱均值合成,全缺测 → None;"
            "支柱口径见 qmj_* 各条目(AQR QMJ,2019)"
        ),
        family="quality",
        direction=FactorPreference.HIGHER,
        signal_eligible=True,
        data_dependencies=qmj_deps,
        window=None,
        implementation_version="1",
        compute=_qmj_compute(),
        cross_section=True,
    )
    return (*pillar_entries, qmj_entry)


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


def _raw_cross_frame(raw: _RawCross) -> PredefinedFactorCompute:
    """原始截面 → 决策日帧(非有限值归一 None;universe = 截面键)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        return {
            day: {
                symbol: value if math.isfinite(value) else None
                for symbol, value in raw(inp, day).items()
            }
            for day in inp.decision_dates
        }

    return compute


#: 目录(裸名 → 条目)。批次 0 样板:return_Nd 动量族(4 窗口变体);
#: 批次 1-4(~150+ 因子)按同一模式在此追加注册。
#:
#: 批次 3(#401):fina_indicator 白名单扩展解锁的 40 个 Growth / Quality
#: 财务因子 —— 数据依赖 = ``financial_indicators.<field>``,公告频率步进
#: 序列(采样取决策日可见的最近一次公告),字段由 #401 扩展白名单提供
#: (announcement_date PIT,available_at = 公告次日零点上海时区)。
#:
#: 批次 4(#402):三表 + dividend 消费 —— ``val_*``(11 个经典价值)、
#: ``qlt_*``(9 个质量补全)、``qmj_*``(AQR QMJ 四支柱 + 综合,截面
#: rank 合成口径见 :func:`_qmj_components` / 各条目 title)。
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
        # ---- 批次 3(#401):Growth 族(13)--------------------------------
        _financial_definition(
            "fin_revenue_yoy",
            title="fin_revenue_yoy = 营业总收入同比增长率(累计口径,公告步进)",
            family="growth",
            field="revenue_yoy",
        ),
        _financial_definition(
            "fin_operating_revenue_yoy",
            title="fin_operating_revenue_yoy = 营业收入同比增长率(or_yoy)",
            family="growth",
            field="operating_revenue_yoy",
        ),
        _financial_definition(
            "fin_basic_eps_yoy",
            title="fin_basic_eps_yoy = 基本每股收益同比增长率",
            family="growth",
            field="basic_eps_yoy",
        ),
        _financial_definition(
            "fin_deducted_np_yoy",
            title="fin_deducted_np_yoy = 扣非归母净利润同比增长率(dt_netprofit_yoy)",
            family="growth",
            field="deducted_netprofit_yoy",
        ),
        _financial_definition(
            "fin_operating_profit_yoy",
            title="fin_operating_profit_yoy = 营业利润同比增长率(op_yoy)",
            family="growth",
            field="operating_profit_yoy",
        ),
        _financial_definition(
            "fin_netprofit_yoy",
            title="fin_netprofit_yoy = 归母净利润同比增长率(累计口径)",
            family="growth",
            field="net_profit_yoy",
        ),
        _financial_definition(
            "fin_ocf_yoy",
            title="fin_ocf_yoy = 经营活动现金流净额同比增长率(ocf_yoy)",
            family="growth",
            field="operating_cash_flow_yoy",
        ),
        _financial_definition(
            "fin_revenue_yoy_q",
            title="fin_revenue_yoy_q = 营业总收入同比增长率(单季度)",
            family="growth",
            field="revenue_yoy_q",
        ),
        _financial_definition(
            "fin_revenue_qoq",
            title="fin_revenue_qoq = 营业总收入环比增长率(单季度)",
            family="growth",
            field="revenue_qoq",
        ),
        _financial_definition(
            "fin_netprofit_yoy_q",
            title="fin_netprofit_yoy_q = 归母净利润同比增长率(单季度)",
            family="growth",
            field="netprofit_yoy_q",
        ),
        _financial_definition(
            "fin_netprofit_qoq",
            title="fin_netprofit_qoq = 归母净利润环比增长率(单季度)",
            family="growth",
            field="netprofit_qoq",
        ),
        _financial_definition(
            "fin_np_yoy_accel",
            title="fin_np_yoy_accel = 归母净利润同比增速的公告序一阶差分(增长加速度)",
            family="growth",
            field="net_profit_yoy",
            compute=_financial_accel("net_profit_yoy"),
        ),
        _financial_definition(
            "fin_revenue_yoy_accel",
            title="fin_revenue_yoy_accel = 营业总收入同比增速的公告序一阶差分(增长加速度)",
            family="growth",
            field="revenue_yoy",
            compute=_financial_accel("revenue_yoy"),
        ),
        # ---- 批次 3(#401):Quality 盈利族(12)---------------------------
        _financial_definition(
            "fin_roe",
            title="fin_roe = 净资产收益率(摊薄)",
            family="quality",
            field="return_on_equity",
        ),
        _financial_definition(
            "fin_roe_waa",
            title="fin_roe_waa = 加权平均净资产收益率",
            family="quality",
            field="weighted_return_on_equity",
        ),
        _financial_definition(
            "fin_roe_deducted",
            title="fin_roe_deducted = 净资产收益率(扣除非经常损益)",
            family="quality",
            field="roe_deducted",
        ),
        _financial_definition(
            "fin_roe_q",
            title="fin_roe_q = 净资产收益率(单季度)",
            family="quality",
            field="roe_q",
        ),
        _financial_definition(
            "fin_roa",
            title="fin_roa = 总资产报酬率(roa)",
            family="quality",
            field="return_on_assets",
        ),
        _financial_definition(
            "fin_roa_np",
            title="fin_roa_np = 总资产净利率(npta)",
            family="quality",
            field="return_on_assets_np",
        ),
        _financial_definition(
            "fin_roa_q",
            title="fin_roa_q = 总资产净利率(单季度,q_npta)",
            family="quality",
            field="return_on_assets_q",
        ),
        _financial_definition(
            "fin_roic",
            title="fin_roic = 投入资本回报率(roic)",
            family="quality",
            field="roic",
        ),
        _financial_definition(
            "fin_gross_margin",
            title="fin_gross_margin = 销售毛利率",
            family="quality",
            field="gross_profit_margin",
        ),
        _financial_definition(
            "fin_net_margin",
            title="fin_net_margin = 销售净利率",
            family="quality",
            field="net_profit_margin",
        ),
        _financial_definition(
            "fin_gross_margin_q",
            title="fin_gross_margin_q = 销售毛利率(单季度)",
            family="quality",
            field="grossprofit_margin_q",
        ),
        _financial_definition(
            "fin_net_margin_q",
            title="fin_net_margin_q = 销售净利率(单季度)",
            family="quality",
            field="netprofit_margin_q",
        ),
        # ---- 批次 3(#401):Quality 营运效率族(5)------------------------
        _financial_definition(
            "fin_inventory_turnover",
            title="fin_inventory_turnover = 存货周转率(次/报告期)",
            family="quality",
            field="inventory_turnover",
        ),
        _financial_definition(
            "fin_receivables_turnover",
            title="fin_receivables_turnover = 应收账款周转率(次/报告期)",
            family="quality",
            field="receivables_turnover",
        ),
        _financial_definition(
            "fin_current_assets_turnover",
            title="fin_current_assets_turnover = 流动资产周转率(次/报告期)",
            family="quality",
            field="current_assets_turnover",
        ),
        _financial_definition(
            "fin_fixed_assets_turnover",
            title="fin_fixed_assets_turnover = 固定资产周转率(次/报告期)",
            family="quality",
            field="fixed_assets_turnover",
        ),
        _financial_definition(
            "fin_total_assets_turnover",
            title="fin_total_assets_turnover = 总资产周转率(次/报告期)",
            family="quality",
            field="total_assets_turnover",
        ),
        # ---- 批次 3(#401):Quality 流动性 / 偿债族(6)-------------------
        _financial_definition(
            "fin_current_ratio",
            title="fin_current_ratio = 流动比率(流动资产/流动负债)",
            family="quality",
            field="current_ratio",
        ),
        _financial_definition(
            "fin_quick_ratio",
            title="fin_quick_ratio = 速动比率",
            family="quality",
            field="quick_ratio",
        ),
        _financial_definition(
            "fin_debt_to_assets",
            title="fin_debt_to_assets = 资产负债率(越高越看空)",
            family="quality",
            field="debt_to_assets",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_debt_to_equity",
            title="fin_debt_to_equity = 产权比率(负债/股东权益,越高越看空)",
            family="quality",
            field="debt_to_equity",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_interest_coverage",
            title="fin_interest_coverage = 已获利息倍数(EBIT/利息费用,ICR)",
            family="quality",
            field="interest_coverage",
        ),
        _financial_definition(
            "fin_equity_multiplier",
            title="fin_equity_multiplier = 权益乘数(总资产/股东权益,越高越看空)",
            family="quality",
            field="equity_multiplier",
            direction=FactorPreference.LOWER,
        ),
        # ---- 批次 3(#401):Quality 费用 / 现金流质量族(4)---------------
        _financial_definition(
            "fin_expense_ratio",
            title="fin_expense_ratio = 销售期间费用率(期间费用/营业总收入,越高越看空)",
            family="quality",
            field="expense_to_revenue",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_ocf_to_revenue",
            title="fin_ocf_to_revenue = 经营现金流净额/营业收入(盈利现金含量)",
            family="quality",
            field="ocf_to_revenue",
        ),
        _financial_definition(
            "fin_ocf_to_debt",
            title="fin_ocf_to_debt = 经营现金流净额/负债合计(偿债现金保障)",
            family="quality",
            field="ocf_to_debt",
        ),
        _financial_definition(
            "fin_ocfps",
            title="fin_ocfps = 每股经营活动现金流净额",
            family="quality",
            field="operating_cash_flow_per_share",
        ),
        # ---- 批次 4(#402):Value 族(11)--------------------------
        # 流量分子为报告期累计口径(公告步进,未年化/未 TTM,#401 同边界);
        # 分母为 daily_metrics 市值/收盘价(同日可见口径,未复权)。
        _ratio_definition(
            "val_fcf_to_market",
            title="val_fcf_to_market = 自由现金流(cashflow.free_cashflow,公告累计)/ 总市值",
            family="value",
            numerator=(_NumComponent("cashflow_statements", "free_cashflow", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_ocf_to_market",
            title="val_ocf_to_market = 经营现金流净额(cashflow.n_cashflow_act,公告累计)/ 总市值",
            family="value",
            numerator=(_NumComponent("cashflow_statements", "n_cashflow_act", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_ebitda_to_market",
            title="val_ebitda_to_market = EBITDA(income.ebitda,公告累计)/ 总市值",
            family="value",
            numerator=(_NumComponent("income_statements", "ebitda", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_ebit_to_market",
            title="val_ebit_to_market = EBIT(income.ebit,公告累计)/ 总市值",
            family="value",
            numerator=(_NumComponent("income_statements", "ebit", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_bm",
            title="val_bm = 账面市值比(精确版:归母股东权益/总市值,自三表计算)",
            family="value",
            numerator=(_NumComponent("balance_sheets", "total_hldr_eqy_exc_min_int", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_tangible_bm",
            title=(
                "val_tangible_bm = 有形账面市值比 = (归母权益 - 商誉 - 无形资产)/ 总市值"
                "(商誉/无形缺项按 0 计,合计语义)"
            ),
            family="value",
            numerator=(
                _NumComponent("balance_sheets", "total_hldr_eqy_exc_min_int", required=True),
                _NumComponent("balance_sheets", "goodwill", sign=-1.0),
                _NumComponent("balance_sheets", "intan_assets", sign=-1.0),
            ),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_earnings_to_price",
            title="val_earnings_to_price = 净利价格比 E/P(归母净利润/总市值 ≡ 每股收益/价格)",
            family="value",
            numerator=(_NumComponent("income_statements", "n_income_attr_p", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_sales_to_price",
            title="val_sales_to_price = 销收价格比 S/P(营业收入/总市值 ≡ 每股收入/价格)",
            family="value",
            numerator=(_NumComponent("income_statements", "revenue", required=True),),
            denominator=("daily_metrics", "total_market_cap"),
        ),
        _ratio_definition(
            "val_ocf_to_price",
            title="val_ocf_to_price = 经营现金流价格比(经营现金流净额/流通市值)",
            family="value",
            numerator=(_NumComponent("cashflow_statements", "n_cashflow_act", required=True),),
            denominator=("daily_metrics", "circulating_market_cap"),
        ),
        PredefinedFactorDefinition(
            name="val_dividend_yield",
            title=(
                "val_dividend_yield = 精确股息率 = 近 12 个月每股现金分红(税前)/ 收盘价"
                "(dividend 明细计算,除权除息日归属 (d-365, d],同分红年度取最新进展行;"
                "替代 dividend_yield_ttm 滚动近似,#397/#402)"
            ),
            family="value",
            direction=FactorPreference.HIGHER,
            signal_eligible=True,
            data_dependencies=(
                "dividends.cash_div",
                "dividends.ex_date",
                "daily_metrics.close",
            ),
            window=None,
            implementation_version="1",
            compute=_raw_cross_frame(_dividend_yield_raw),
        ),
        PredefinedFactorDefinition(
            name="val_dps_ttm",
            title=(
                "val_dps_ttm = 近 12 个月每股现金分红(税前,元/股;除权除息日归属"
                " (d-365, d],同分红年度取决策日可见最新进展行;无可见进展 → 缺测)"
            ),
            family="value",
            direction=FactorPreference.HIGHER,
            signal_eligible=True,
            data_dependencies=("dividends.cash_div", "dividends.ex_date"),
            window=None,
            implementation_version="1",
            compute=_raw_cross_frame(_dps_ttm_cross),
        ),
        # ---- 批次 4(#402):Quality 补全族(9)----------------------
        _ratio_definition(
            "qlt_advance_receipts_ratio",
            title=(
                "qlt_advance_receipts_ratio = 预收(含合同负债)收入占比 = "
                "(预收款项 + 合同负债)/ 营业收入(新旧准则科目并存,缺项按 0 计;"
                "占用下游资金能力,越高越看多)"
            ),
            family="quality",
            numerator=(
                _NumComponent("balance_sheets", "adv_receipts"),
                _NumComponent("balance_sheets", "contract_liab"),
            ),
            denominator=("income_statements", "revenue"),
        ),
        _ratio_definition(
            "qlt_prepayment_ratio",
            title="qlt_prepayment_ratio = 预付账款占总资产比(被上游占用,越高越看空)",
            family="quality",
            numerator=(_NumComponent("balance_sheets", "prepayment", required=True),),
            denominator=("balance_sheets", "total_assets"),
            direction=FactorPreference.LOWER,
        ),
        _ratio_definition(
            "qlt_inventory_turnover_detail",
            title="qlt_inventory_turnover_detail = 明细存货周转率 = 营业成本/存货(次/报告期,未年化)",
            family="quality",
            numerator=(_NumComponent("income_statements", "oper_cost", required=True),),
            denominator=("balance_sheets", "inventories"),
        ),
        _ratio_definition(
            "qlt_receivables_turnover_detail",
            title="qlt_receivables_turnover_detail = 明细应收周转率 = 营业收入/应收账款(次/报告期,未年化)",
            family="quality",
            numerator=(_NumComponent("income_statements", "revenue", required=True),),
            denominator=("balance_sheets", "accounts_receiv"),
        ),
        _ratio_definition(
            "qlt_payables_turnover_detail",
            title=(
                "qlt_payables_turnover_detail = 明细应付周转率 = 营业成本/应付账款"
                "(次/报告期;越高=对上游付款越快、占款能力越弱,越高越看空)"
            ),
            family="quality",
            numerator=(_NumComponent("income_statements", "oper_cost", required=True),),
            denominator=("balance_sheets", "acct_payable"),
            direction=FactorPreference.LOWER,
        ),
        _ratio_definition(
            "qlt_accrual_ratio",
            title=(
                "qlt_accrual_ratio = 应计比率 = (净利润 - 经营现金流净额)/ 总资产"
                "(Sloan;应计越高盈利质量越差,越高越看空)"
            ),
            family="quality",
            numerator=(
                _NumComponent("cashflow_statements", "net_profit", required=True),
                _NumComponent("cashflow_statements", "n_cashflow_act", sign=-1.0, required=True),
            ),
            denominator=("balance_sheets", "total_assets"),
            direction=FactorPreference.LOWER,
        ),
        _ratio_definition(
            "qlt_ocf_to_profit",
            title=(
                "qlt_ocf_to_profit = 利润现金含量 = 经营现金流净额/净利润"
                "(净利润 <= 0 → 缺测,不虚构符号)"
            ),
            family="quality",
            numerator=(_NumComponent("cashflow_statements", "n_cashflow_act", required=True),),
            denominator=("cashflow_statements", "net_profit"),
        ),
        _ratio_definition(
            "qlt_sales_cash_ratio",
            title="qlt_sales_cash_ratio = 销售收现比 = 销售商品提供劳务收到的现金/营业收入(收入的现金含量)",
            family="quality",
            numerator=(_NumComponent("cashflow_statements", "c_fr_sale_sg", required=True),),
            denominator=("income_statements", "revenue"),
        ),
        PredefinedFactorDefinition(
            name="qlt_payout_ratio",
            title=(
                "qlt_payout_ratio = 现金分红率 = 近 12 个月每股分红/最新公告每股收益"
                "(每股收益 <= 0 → 缺测;AQR payout 支柱成分)"
            ),
            family="quality",
            direction=FactorPreference.HIGHER,
            signal_eligible=True,
            data_dependencies=(
                "dividends.cash_div",
                "dividends.ex_date",
                "financial_indicators.eps",
            ),
            window=None,
            implementation_version="1",
            compute=_raw_cross_frame(_payout_ratio_raw),
        ),
        *_pillar_and_qmj_entries(),
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
