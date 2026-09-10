"""平台预置因子目录(issue #398 批次 0 基座;#399 批次 1 量价 74 个)。

目录条目 = 「公式即代码」:name / 公式描述 / 数据依赖 / 方向 /
signal_eligible / 参数化窗口,``compute`` 是平台可信代码 —— 构建走
**进程内** factor_series 通道(免用户因子的容器税,审计 / 内容寻址 /
覆盖检查全套同构,见 ``research_sandbox.predefined_runner``)。

**引用命名** ``p_<name>``(``PREDEFINED_FACTOR_PREFIX``,与用户因子
``u_`` 对称,见 ``finboard_data.factor_lab``);目录内部只存裸名。

**家族清单**(批次 0:return_{21,63,126,252}d 动量样板;#399 批次 1
量价 74 个,同族窗口变体一份参数化实现展开注册):

* ``momentum`` —— 区间收益(批次 0 样板)/ 回归 alpha / 残差动量 /
  MACD / RSRS / 价格位置 / 相对强弱,17 个;
* ``reversal`` —— RSI / 乖离率,2 个;
* ``risk`` —— 已实现波动 / 波动率比 / beta / 特异波动 / 市场相关 /
  Sharpe / 偏度 / 峰度 / 下行波动 / 回撤深度,24 个(beta / 特异波动 /
  市场相关 / Sharpe 依赖 ``index_bars`` 市场收益;3 个 1320d 长窗口
  声明 ``min_history_bars`` 覆盖起点);
* ``liquidity`` —— 换手率 MA/STD/乖离/Z/比值、成交额 / 成交量均值、
  Amihud 非流动性、VWAP 偏离、量价相关,32 个;
* ``size`` —— 对数总市值 / 对数流通市值 / 流通股占比,3 个
  (signal_eligible=False,#214 风险暴露定位)。

调研清单(202 因子路线图)对照与增删差异见 PR #399 注册清单总表;
``log_price`` / ``ma_20d`` / ``price_dist`` / ``days_down_up`` 经总览
拍板不注册(变换非因子 / 原料 / 文献弱)。

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
**批次 0 样板族** ``return_{21,63,126,252}d``:同一参数化实现按 tushare
命名展开注册(动量族 return_N)。

**批次 2:Alpha101 量价因子族** ``alpha101_{N}``(issue #400,31 个):
WorldQuant《101 Formulaic Alphas》(Kakushadze 2015)经 tushare
``factor_list`` 口径圈定的 31 个纯 OHLCV 截面因子,公式逐条直译为 C0
算子组合。批次级共享口径(逐因子 docstring 只写差异):

* ``Returns`` = ``close / ts_delay(close, 1) - 1``(qfq 收盘日收益);
* ``VWAP`` = ``amount / volume``(issue 验收口径的近似;量纲 = 缓存
  两列原生单位比,非保证「元/股」——akshare 股票(元/手)下为
  100x VWAP、tushare(千元/手)下为 10x;排名类公式对恒定缩放不敏感,
  与价格做差比较的因子(#5/#11/#19/#25/#41/#57)受量纲影响,属已知
  近似,跨源量纲归一留后续);
* ``ADV20`` = ``ts_mean(volume, 20)`(与主流复现一致取成交量而非成交额);
* ``Rank(x)`` = 逐日截面百分位 ``cs_rank``,分母 = 可交易域
  (#380:benchmark-only 不进截面分母,``_cs_rank_series`` 收窄);
* ``Ts_*`` = operators 时序算子(因果 trailing 窗口,前缀不变性审计
  兜底);条件 ``IF / ?:`` 经 ``ew_where``(条件不可判 → NaN);
* **行业中性化**:论文与 tushare 口径下本批次 31 条公式**均不含**
  ``indneutralize`` 项(论文仅 Alpha#47/#99 使用,不在本批次);行业
  分组装配(``inp.industry_groups()``,冻结发布 instruments.industry
  近似 #185/#212 分组)与 :func:`industry_neutralize` 助手(缺组 →
  缺测 + 计数)随本批次交付,供分组 sanity 与后续批次消费。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

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
    rolling_ols_resid,
    ts_corr,
    ts_cov,
    ts_delay,
    ts_delta,
    ts_downside_std,
    ts_ema,
    ts_kurt,
    ts_max,
    ts_mean,
    ts_min,
    ts_skew,
    CrossSection,
    cs_neutralize,
    cs_rank,
    ew_div,
    ew_gt,
    ew_log,
    ew_lt,
    ew_sign,
    ew_signed_power,
    ew_where,
    ts_argmax,
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
      False(全挂载标的,消费端统一剔除基准);
    * ``min_history_bars`` —— **覆盖起点声明**(可选,issue #399):因子
      值非缺测需要至少 N 根 bar 历史(如 1320d 长窗口 ≈5.5 年)。声明后
      #361 覆盖检查消费:序列在首个决策日全缺测 → 入队具名拒绝
      (``predefined_factor_series_coverage_start_missing``),杜绝长窗口
      因子在短历史发布上构建出全 None 序列静默进 run(#255 教训)。
      None = 未声明(批次 0 语义,覆盖检查零变化);声明值进 commit 锚
      (None 时省略键,批次 0 锚逐字节稳定)。
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
    min_history_bars: int | None = None

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
        if self.min_history_bars is not None:
            if self.min_history_bars < 1:
                raise ValueError(f"{self.name}: min_history_bars 须 >= 1 或 None")
            if self.window is not None and self.min_history_bars < self.window:
                raise ValueError(
                    f"{self.name}: min_history_bars({self.min_history_bars}) "
                    f"不得小于 window({self.window})(覆盖起点不早于主窗口)"
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
# --------------------------------------------------------------------- #
# 批次 1(#399)公共件:市场收益对齐与区间收益
# --------------------------------------------------------------------- #

#: Risk 族市场收益的基准指数(#256/#341 链路:指数与候选池同处一份
#: mixed bars 主发布,经 ``PredefinedFactorInput.index_bars`` 取数)。
MARKET_INDEX_SYMBOL = "000300.SH"

#: Sharpe 类日频年化常数(A 股年交易日惯例口径)。
_TRADING_DAYS_PER_YEAR = 250.0


def _interval_returns(closes: np.ndarray, window: int) -> np.ndarray:
    """N 根 bar 区间收益 ``close[t] / close[t-N] - 1``(与 return_Nd 同式)。"""
    base = ts_delay(closes, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        result: np.ndarray = closes / base - 1.0
    return result


def _daily_returns(closes: np.ndarray) -> np.ndarray:
    """日简单收益 ``close[i] / close[i-1] - 1``(首位置 NaN)。"""
    return _interval_returns(closes, 1)


def _aligned_pair(
    a: SymbolSeries, b: SymbolSeries
) -> tuple[SymbolSeries, np.ndarray]:
    """按业务日期交集对齐两条序列(a 轴为基准)。

    返回 (a 的重建子序列, b 的对齐值数组):只在两条序列**都有行**的
    日期保留(a 原行序的时间升序子序列,截断变体保序,前缀不变性结构
    性成立)。任一侧行缺失(停牌 / 数据缺口)的日期不进窗口——
    「窗口 = 共同交易日根数」,与 ts_* 的「window 根 bar」口径一致。
    """
    b_by_date = dict(zip(b.dates, b.values.tolist(), strict=True))
    keep = [i for i, day in enumerate(a.dates) if day in b_by_date]
    aligned_b = np.array([b_by_date[a.dates[i]] for i in keep], dtype=np.float64)
    aligned_a = SymbolSeries(
        dates=tuple(a.dates[i] for i in keep),
        values=a.values[keep],
        available_at=tuple(a.available_at[i] for i in keep),
    )
    return aligned_a, aligned_b


def _market_pair_frame(
    inp: PredefinedFactorInput,
    closes: Mapping[str, SymbolSeries],
    pair_fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
) -> FactorSeriesFrame:
    """市场收益依赖因子的通用驱动(批次 1 Risk / 回归 alpha 族)。

    逐标的与基准指数(:data:`MARKET_INDEX_SYMBOL`)按日期交集对齐后应用
    ``pair_fn(标的对齐值, 市场对齐值)``,再按**对齐后的序列轴**采样。
    发布不含基准指数 → 全缺测帧(采样为 None,fail-visible;
    ``min_history_bars`` 声明在入队期具名拒绝,#361 覆盖检查消费)。
    """
    market = inp.index_bars("close").get(MARKET_INDEX_SYMBOL)
    if market is None:
        nan_values: dict[str, np.ndarray] = {
            symbol: np.full(series.values.size, np.nan)
            for symbol, series in closes.items()
        }
        return inp.sample(closes, nan_values)
    series_axis: dict[str, SymbolSeries] = {}
    per_symbol_values: dict[str, np.ndarray] = {}
    for symbol, series in closes.items():
        aligned, market_values = _aligned_pair(series, market)
        series_axis[symbol] = aligned
        per_symbol_values[symbol] = pair_fn(aligned.values, market_values)
    return inp.sample(series_axis, per_symbol_values)


# --------------------------------------------------------------------- #
# 批次 1(#399)compute 工厂(同族窗口变体一份实现)
# --------------------------------------------------------------------- #


def _reg_alpha(window: int) -> PredefinedFactorCompute:
    """回归 alpha 族:日收益对市场收益 trailing 窗口 OLS 的截距
    (市场模型 Jensen's alpha,日频;beta = cov/var 组合既有算子)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            stock_ret = _daily_returns(stock_values)
            market_ret = _daily_returns(market_values)
            market_var = ts_cov(market_ret, market_ret, window)
            with np.errstate(invalid="ignore", divide="ignore"):
                beta = ts_cov(stock_ret, market_ret, window) / market_var
                result: np.ndarray = ts_mean(stock_ret, window) - beta * ts_mean(
                    market_ret, window
                )
            return result

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _resid_momentum(window: int) -> PredefinedFactorCompute:
    """残差动量:市场模型日残差(:func:`rolling_ols_resid`)在窗口内求和。

    需 ``2*window - 1`` 根 bar 历史(残差自 ``window-1`` 起,再求和
    ``window`` 根),``min_history_bars`` 据此声明。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            stock_ret = _daily_returns(stock_values)
            market_ret = _daily_returns(market_values)
            return ts_sum(rolling_ols_resid(stock_ret, market_ret, window), window)

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _macd_hist_norm() -> PredefinedFactorCompute:
    """MACD 柱(收盘价归一):``(DIF - DEA) * 2 / close``;DIF = EMA12 -
    EMA26,DEA = EMA9(DIF)(A 股 MACD 惯例 2 倍柱)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            close = series.values
            dif = ts_ema(close, 12) - ts_ema(close, 26)
            dea = ts_ema(dif, 9)
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = (dif - dea) * 2.0 / close
        return inp.sample(closes, per_symbol)

    return compute


def _rsrs(window: int, *, r2_weighted: bool) -> PredefinedFactorCompute:
    """RSRS 相对强度:high 对 low 的 trailing 窗口 OLS 斜率(阻力位相对
    支撑位的强度);``r2_weighted=True`` 时乘以拟合 R²(斜率置信度加权,
    标准 RSRS 指标)。high / low 同表逐行对齐,无需交集。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        highs = inp.bars("high")
        lows = inp.bars("low")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, high_series in highs.items():
            high = high_series.values
            low = lows[symbol].values
            slope = ts_cov(high, low, window) / ts_cov(low, low, window)
            if r2_weighted:
                rho = ts_corr(high, low, window)
                slope = slope * rho * rho
            per_symbol[symbol] = slope
        return inp.sample(highs, per_symbol)

    return compute


def _price_position(window: int) -> PredefinedFactorCompute:
    """价格位置:``(close - min_N) / (max_N - min_N)``(窗口内相对位置,
    52 周高点邻近效应;区间持平 → 0/0 → 缺测)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            close = series.values
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = (close - ts_min(close, window)) / (
                    ts_max(close, window) - ts_min(close, window)
                )
        return inp.sample(closes, per_symbol)

    return compute


def _ema_ratio(fast: int, slow: int) -> PredefinedFactorCompute:
    """快慢 EMA 之比减一(趋势强度,价格量纲自由)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            close = series.values
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = ts_ema(close, fast) / ts_ema(close, slow) - 1.0
        return inp.sample(closes, per_symbol)

    return compute


def _rs_vs_index(window: int) -> PredefinedFactorCompute:
    """相对强弱:股票 N 根 bar 区间收益 - 基准指数同窗区间收益。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            result: np.ndarray = _interval_returns(
                stock_values, window
            ) - _interval_returns(market_values, window)
            return result

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _momentum_skip_month(long_window: int, short_window: int) -> PredefinedFactorCompute:
    """12-1 动量:长窗区间收益 - 近期短窗区间收益(剔除近月反转效应)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            per_symbol[symbol] = _interval_returns(
                series.values, long_window
            ) - _interval_returns(series.values, short_window)
        return inp.sample(closes, per_symbol)

    return compute


def _rsi(window: int) -> PredefinedFactorCompute:
    """RSI:``100 x MA(涨幅, N) / (MA(涨幅, N) + MA(跌幅, N))``(简单
    均值口径;全平窗口 0/0 → 缺测)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            change = ts_delta(series.values, 1)
            avg_gain = ts_mean(np.maximum(change, 0.0), window)
            avg_loss = ts_mean(np.maximum(-change, 0.0), window)
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = 100.0 * avg_gain / (avg_gain + avg_loss)
        return inp.sample(closes, per_symbol)

    return compute


def _bias(window: int) -> PredefinedFactorCompute:
    """乖离率:``close / MA(close, N) - 1``(反转语义:高乖离看空)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            close = series.values
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = close / ts_mean(close, window) - 1.0
        return inp.sample(closes, per_symbol)

    return compute


def _realized_vol(window: int) -> PredefinedFactorCompute:
    """已实现波动率:日收益的 trailing 窗口样本标准差(未年化)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            per_symbol[symbol] = ts_std(_daily_returns(series.values), window)
        return inp.sample(closes, per_symbol)

    return compute


def _vol_ratio(fast: int, slow: int) -> PredefinedFactorCompute:
    """波动率比:短窗波动 / 长窗波动(>1 = 波动放大)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            returns = _daily_returns(series.values)
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = ts_std(returns, fast) / ts_std(returns, slow)
        return inp.sample(closes, per_symbol)

    return compute


def _beta(window: int) -> PredefinedFactorCompute:
    """市场 beta:日收益对市场收益 trailing 窗口 OLS 斜率
    (cov(r, m) / var(m);低 beta 异象 → direction LOWER)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            stock_ret = _daily_returns(stock_values)
            market_ret = _daily_returns(market_values)
            with np.errstate(invalid="ignore", divide="ignore"):
                result: np.ndarray = ts_cov(stock_ret, market_ret, window) / ts_cov(
                    market_ret, market_ret, window
                )
            return result

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _specific_vol(window: int) -> PredefinedFactorCompute:
    """特异波动:``总波动 x sqrt(max(0, 1 - rho^2))``(市场模型残差方差
    开方;corr 缺测窗口整窗 NaN 传播)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            stock_ret = _daily_returns(stock_values)
            market_ret = _daily_returns(market_values)
            total = ts_std(stock_ret, window)
            rho = ts_corr(stock_ret, market_ret, window)
            with np.errstate(invalid="ignore"):
                result: np.ndarray = total * np.sqrt(
                    np.clip(1.0 - rho * rho, 0.0, None)
                )
            return result

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _corr_market(window: int) -> PredefinedFactorCompute:
    """市场相关:日收益与市场收益的 trailing 窗口相关系数(低相关 =
    分散价值,弱先验 direction LOWER)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        def pair(stock_values: np.ndarray, market_values: np.ndarray) -> np.ndarray:
            return ts_corr(
                _daily_returns(stock_values), _daily_returns(market_values), window
            )

        return _market_pair_frame(inp, inp.bars("close"), pair)

    return compute


def _sharpe(window: int) -> PredefinedFactorCompute:
    """窗口 Sharpe:``mean(日收益) / std(日收益) x √250``(无风险利率 0)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            returns = _daily_returns(series.values)
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = (
                    ts_mean(returns, window)
                    / ts_std(returns, window)
                    * _TRADING_DAYS_PER_YEAR**0.5
                )
        return inp.sample(closes, per_symbol)

    return compute


def _return_skew(window: int) -> PredefinedFactorCompute:
    """日收益偏度(trailing 窗口,修正 Fisher-Pearson)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            per_symbol[symbol] = ts_skew(_daily_returns(series.values), window)
        return inp.sample(closes, per_symbol)

    return compute


def _return_kurt(window: int) -> PredefinedFactorCompute:
    """日收益超额峰度(trailing 窗口,pandas rolling.kurt 同式)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            per_symbol[symbol] = ts_kurt(_daily_returns(series.values), window)
        return inp.sample(closes, per_symbol)

    return compute


def _downside_vol(window: int) -> PredefinedFactorCompute:
    """下行波动:窗口内负日收益的样本标准差(:func:`ts_downside_std`)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            per_symbol[symbol] = ts_downside_std(
                _daily_returns(series.values), window
            )
        return inp.sample(closes, per_symbol)

    return compute


def _drawdown(window: int) -> PredefinedFactorCompute:
    """窗口回撤深度:``(max_N - close) / max_N``(≥0;距窗口最高收盘的
    回撤,深度越大越深)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            close = series.values
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = (ts_max(close, window) - close) / ts_max(
                    close, window
                )
        return inp.sample(closes, per_symbol)

    return compute


def _turnover_stat(
    kind: str, window: int, *, fast: int | None = None
) -> PredefinedFactorCompute:
    """换手率族统计量(``daily_metrics.turnover_rate``,单位 %)。

    ``kind``:``ma``(均值)/ ``std``(样本标准差)/ ``bias``(乖离
    t/MA-1)/ ``z``((t-MA)/STD)/ ``ratio``(fast 日均值 / window 日均值)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        turnover = inp.daily_metrics("turnover_rate")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in turnover.items():
            values = series.values
            if kind == "ma":
                per_symbol[symbol] = ts_mean(values, window)
            elif kind == "std":
                per_symbol[symbol] = ts_std(values, window)
            else:
                mean = ts_mean(values, window)
                with np.errstate(invalid="ignore", divide="ignore"):
                    if kind == "bias":
                        per_symbol[symbol] = values / mean - 1.0
                    elif kind == "z":
                        per_symbol[symbol] = (values - mean) / ts_std(values, window)
                    else:  # ratio
                        per_symbol[symbol] = ts_mean(values, fast or window) / mean
        return inp.sample(turnover, per_symbol)

    return compute


def _bars_field_mean(field: str, window: int) -> PredefinedFactorCompute:
    """bars 数值字段的 trailing 窗口均值(成交额 / 成交量流动性规模)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        series_by_symbol = inp.bars(field)
        per_symbol: dict[str, np.ndarray] = {
            symbol: ts_mean(series.values, window)
            for symbol, series in series_by_symbol.items()
        }
        return inp.sample(series_by_symbol, per_symbol)

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
def _amihud(window: int) -> PredefinedFactorCompute:
    """Amihud 非流动性:``|日收益| / 成交额`` 的 trailing 窗口均值
    (原始量纲;成交额 0 → inf → 采样归一 None,fail-visible)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        amounts = inp.bars("amount")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            returns = _daily_returns(series.values)
            with np.errstate(invalid="ignore", divide="ignore"):
                illiq = np.abs(returns) / amounts[symbol].values
            per_symbol[symbol] = ts_mean(illiq, window)
        return inp.sample(closes, per_symbol)

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
def _vwap_dev(window: int) -> PredefinedFactorCompute:
    """收盘价对窗口 VWAP 的偏离:``close / (Σamount / Σvolume) - 1``
    (VWAP 用 amount/volume 近似,issue #399;量纲自由)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        amounts = inp.bars("amount")
        volumes = inp.bars("volume")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in closes.items():
            amount_sum = ts_sum(amounts[symbol].values, window)
            volume_sum = ts_sum(volumes[symbol].values, window)
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = amount_sum / volume_sum
                per_symbol[symbol] = series.values / vwap - 1.0
        return inp.sample(closes, per_symbol)

    return compute


def _turnover_return_corr(window: int) -> PredefinedFactorCompute:
    """量价相关:换手率与日收益的 trailing 窗口相关系数(对齐 = bars
    与 daily_metrics 的日期交集,见 :func:`_aligned_pair`)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        turnover = inp.daily_metrics("turnover_rate")
        closes = inp.bars("close")
        series_axis: dict[str, SymbolSeries] = {}
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, turnover_series in turnover.items():
            close_series = closes.get(symbol)
            if close_series is None:
                continue
            aligned_close, turnover_values = _aligned_pair(close_series, turnover_series)
            series_axis[symbol] = aligned_close
            per_symbol[symbol] = ts_corr(
                turnover_values, _daily_returns(aligned_close.values), window
            )
        return inp.sample(series_axis, per_symbol)

    return compute


def _log_field(field: str) -> PredefinedFactorCompute:
    """daily_metrics 数值字段的自然对数(规模因子;非正值 → 缺测)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        series_by_symbol = inp.daily_metrics(field)
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in series_by_symbol.items():
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = np.log(series.values)
        return inp.sample(series_by_symbol, per_symbol)

    return compute


def _float_share_ratio() -> PredefinedFactorCompute:
    """流通股占比:``float_shares / total_shares``(流通结构暴露)。"""

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        float_shares = inp.daily_metrics("float_shares")
        total_shares = inp.daily_metrics("total_shares")
        per_symbol: dict[str, np.ndarray] = {}
        for symbol, series in float_shares.items():
            total = total_shares.get(symbol)
            if total is None:
                continue
            with np.errstate(invalid="ignore", divide="ignore"):
                per_symbol[symbol] = series.values / total.values
        return inp.sample(float_shares, per_symbol)

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
def _entry(
    name: str,
    *,
    title: str,
    family: str,
    compute: PredefinedFactorCompute,
    window: int | None = None,
    direction: FactorPreference = FactorPreference.HIGHER,
    signal_eligible: bool = True,
    cross_section: bool = False,
    data_dependencies: tuple[str, ...] = ("bars.close",),
    implementation_version: str = "1",
    min_history_bars: int | None = None,
) -> PredefinedFactorDefinition:
    """批次 1(#399)通用条目工厂:参数化 compute + 显式目录字段。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=signal_eligible,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version=implementation_version,
        compute=compute,
        cross_section=cross_section,
        min_history_bars=min_history_bars,
    )


def _vol_entries() -> tuple[PredefinedFactorDefinition, ...]:
    """risk 族 24 个(波动率窗口展开 / beta / 特异波动 / 市场相关 /
    Sharpe / 高阶矩 / 回撤;指数依赖因子声明 ``index_bars.close``,
    3 个 1320d 长窗口声明 ``min_history_bars`` 覆盖起点)。"""
    vol_windows = (20, 60, 120, 250)
    entries: list[PredefinedFactorDefinition] = [
        _entry(
            f"vol_{window}d",
            title=f"vol_{window}d = 日收益 trailing {window} 根 bar 样本标准差"
            "(已实现波动,未年化;低波动异象 direction LOWER)",
            family="risk",
            compute=_realized_vol(window),
            window=window,
            direction=FactorPreference.LOWER,
            signal_eligible=False,
        )
        for window in vol_windows
    ]
    entries.append(
        _entry(
            "vol_ratio_20_60d",
            title="vol_ratio_20_60d = vol_20d / vol_60d(>1 波动放大)",
            family="risk",
            compute=_vol_ratio(20, 60),
            window=60,
            direction=FactorPreference.LOWER,
            signal_eligible=False,
        )
    )
    for window in (60, 120, 250):
        entries.append(
            _entry(
                f"beta_{window}d",
                title=f"beta_{window}d = 日收益对市场收益(000300.SH)trailing "
                f"{window} 根 bar OLS 斜率(低 beta 异象 direction LOWER)",
                family="risk",
                compute=_beta(window),
                window=window,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
                data_dependencies=("bars.close", "index_bars.close"),
            )
        )
    entries.append(
        _entry(
            "beta_1320d",
            title="beta_1320d = 日收益对市场收益 trailing 1320 根 bar OLS 斜率"
            "(5 年长窗;min_history_bars=1320 声明覆盖起点)",
            family="risk",
            compute=_beta(1320),
            window=1320,
            direction=FactorPreference.LOWER,
            signal_eligible=False,
            data_dependencies=("bars.close", "index_bars.close"),
            min_history_bars=1320,
        )
    )
    for window in (60, 120, 250):
        entries.append(
            _entry(
                f"specific_vol_{window}d",
                title=f"specific_vol_{window}d = 总波动 x sqrt(max(0, 1 - ρ²))"
                f"({window} 根 bar 市场模型特异波动)",
                family="risk",
                compute=_specific_vol(window),
                window=window,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
                data_dependencies=("bars.close", "index_bars.close"),
            )
        )
    for window in (60, 120, 250):
        entries.append(
            _entry(
                f"corr_market_{window}d",
                title=f"corr_market_{window}d = 日收益与市场收益 trailing {window}"
                " 根 bar 相关系数(低相关 = 分散价值)",
                family="risk",
                compute=_corr_market(window),
                window=window,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
                data_dependencies=("bars.close", "index_bars.close"),
            )
        )
    entries.append(
        _entry(
            "corr_market_1320d",
            title="corr_market_1320d = 日收益与市场收益 trailing 1320 根 bar 相关"
            "系数(5 年长窗;min_history_bars=1320 声明覆盖起点)",
            family="risk",
            compute=_corr_market(1320),
            window=1320,
            direction=FactorPreference.LOWER,
            signal_eligible=False,
            data_dependencies=("bars.close", "index_bars.close"),
            min_history_bars=1320,
        )
    )
    for window in (60, 120, 250):
        entries.append(
            _entry(
                f"sharpe_{window}d",
                title=f"sharpe_{window}d = 日收益均值 / 日收益标准差 x √250"
                f"({window} 根 bar,无风险利率 0)",
                family="risk",
                compute=_sharpe(window),
                window=window,
                signal_eligible=False,
            )
        )
    entries.append(
        _entry(
            "sharpe_1320d",
            title="sharpe_1320d = 日收益均值 / 日收益标准差 x √250(5 年长窗;"
            "min_history_bars=1320 声明覆盖起点)",
            family="risk",
            compute=_sharpe(1320),
            window=1320,
            signal_eligible=False,
            min_history_bars=1320,
        )
    )
    entries.extend(
        (
            _entry(
                "skew_250d",
                title="skew_250d = 日收益 trailing 250 根 bar 偏度(修正 "
                "Fisher-Pearson;彩票偏好 direction LOWER)",
                family="risk",
                compute=_return_skew(250),
                window=250,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
            ),
            _entry(
                "kurt_250d",
                title="kurt_250d = 日收益 trailing 250 根 bar 超额峰度(尾部"
                "风险 direction LOWER)",
                family="risk",
                compute=_return_kurt(250),
                window=250,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
            ),
            _entry(
                "downside_vol_250d",
                title="downside_vol_250d = 窗口内负日收益的样本标准差(下行波动)",
                family="risk",
                compute=_downside_vol(250),
                window=250,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
            ),
            _entry(
                "drawdown_250d",
                title="drawdown_250d = (max_250 - close) / max_250(距窗口最高"
                "收盘的回撤深度)",
                family="risk",
                compute=_drawdown(250),
                window=250,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
            ),
        )
    )
    return tuple(entries)


def _liquidity_entries() -> tuple[PredefinedFactorDefinition, ...]:
    """liquidity 族 32 个(换手率 MA/STD/乖离/Z/比值 x 窗口展开,原料
    ``daily_metrics.turnover_rate``;成交额 / 成交量均值;Amihud 非流动
    性;VWAP 偏离;量价相关)。"""
    entries: list[PredefinedFactorDefinition] = []
    for window in (5, 10, 20, 60, 120, 250):
        entries.append(
            _entry(
                f"turnover_ma_{window}d",
                title=f"turnover_ma_{window}d = 换手率 trailing {window} 日均值"
                "(daily_metrics.turnover_rate)",
                family="liquidity",
                compute=_turnover_stat("ma", window),
                window=window,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            )
        )
    for window in (10, 20, 60, 120, 250):
        entries.append(
            _entry(
                f"turnover_std_{window}d",
                title=f"turnover_std_{window}d = 换手率 trailing {window} 日样本"
                "标准差(换手波动)",
                family="liquidity",
                compute=_turnover_stat("std", window),
                window=window,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            )
        )
    for window in (20, 60, 250):
        entries.append(
            _entry(
                f"turnover_bias_{window}d",
                title=f"turnover_bias_{window}d = 换手率 / {window} 日均值 - 1"
                "(换手乖离,放量异常)",
                family="liquidity",
                compute=_turnover_stat("bias", window),
                window=window,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            )
        )
    for window in (60, 250):
        entries.append(
            _entry(
                f"turnover_z_{window}d",
                title=f"turnover_z_{window}d = (换手率 - {window} 日均值) / "
                f"{window} 日标准差(标准化异常换手)",
                family="liquidity",
                compute=_turnover_stat("z", window),
                window=window,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            )
        )
    entries.extend(
        (
            _entry(
                "turnover_ratio_5_20d",
                title="turnover_ratio_5_20d = 5 日换手均值 / 20 日换手均值"
                "(短期换手趋势)",
                family="liquidity",
                compute=_turnover_stat("ratio", 20, fast=5),
                window=20,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            ),
            _entry(
                "turnover_ratio_20_60d",
                title="turnover_ratio_20_60d = 20 日换手均值 / 60 日换手均值",
                family="liquidity",
                compute=_turnover_stat("ratio", 60, fast=20),
                window=60,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate",),
            ),
        )
    )
    for window in (20, 60, 250):
        entries.append(
            _entry(
                f"amount_ma_{window}d",
                title=f"amount_ma_{window}d = 成交额 trailing {window} 根 bar 均值"
                "(流动性规模)",
                family="liquidity",
                compute=_bars_field_mean("amount", window),
                window=window,
                signal_eligible=False,
                data_dependencies=("bars.amount",),
            )
        )
    for window in (20, 60, 250):
        entries.append(
            _entry(
                f"volume_ma_{window}d",
                title=f"volume_ma_{window}d = 成交量 trailing {window} 根 bar 均值",
                family="liquidity",
                compute=_bars_field_mean("volume", window),
                window=window,
                signal_eligible=False,
                data_dependencies=("bars.volume",),
            )
        )
    for window in (20, 60, 120, 250):
        entries.append(
            _entry(
                f"amihud_{window}d",
                title=f"amihud_{window}d = |日收益| / 成交额 的 {window} 根 bar "
                "均值(Amihud 非流动性,原始量纲)",
                family="liquidity",
                compute=_amihud(window),
                window=window,
                signal_eligible=False,
                data_dependencies=("bars.close", "bars.amount"),
            )
        )
    for window in (20, 60):
        entries.append(
            _entry(
                f"vwap_dev_{window}d",
                title=f"vwap_dev_{window}d = close / (Σamount / Σvolume) - 1"
                f"({window} 根 bar VWAP 偏离,amount/volume 近似)",
                family="liquidity",
                compute=_vwap_dev(window),
                window=window,
                data_dependencies=("bars.close", "bars.amount", "bars.volume"),
            )
        )
    for window in (20, 60):
        entries.append(
            _entry(
                f"turnover_ret_corr_{window}d",
                title=f"turnover_ret_corr_{window}d = 换手率与日收益 {window} 根"
                " bar 相关系数(量价确认/背离)",
                family="liquidity",
                compute=_turnover_return_corr(window),
                window=window,
                direction=FactorPreference.LOWER,
                signal_eligible=False,
                data_dependencies=("daily_metrics.turnover_rate", "bars.close"),
            )
        )
    return tuple(entries)


def _size_entries() -> tuple[PredefinedFactorDefinition, ...]:
    """size 族 3 个(规模暴露,signal_eligible=False,#214;小市值溢价
    → direction LOWER;window=None,逐日截面原料值/变换)。"""
    return (
        _entry(
            "log_total_market_cap",
            title="log_total_market_cap = ln(总市值)(规模暴露;daily_metrics."
            "total_market_cap)",
            family="size",
            compute=_log_field("total_market_cap"),
            direction=FactorPreference.LOWER,
            signal_eligible=False,
            data_dependencies=("daily_metrics.total_market_cap",),
        ),
        _entry(
            "log_circulating_market_cap",
            title="log_circulating_market_cap = ln(流通市值)(daily_metrics."
            "circulating_market_cap)",
            family="size",
            compute=_log_field("circulating_market_cap"),
            direction=FactorPreference.LOWER,
            signal_eligible=False,
            data_dependencies=("daily_metrics.circulating_market_cap",),
        ),
        _entry(
            "float_share_ratio",
            title="float_share_ratio = 流通股本 / 总股本(流通结构暴露)",
            family="size",
            compute=_float_share_ratio(),
            direction=FactorPreference.LOWER,
            signal_eligible=False,
            data_dependencies=("daily_metrics.float_shares", "daily_metrics.total_shares"),
        ),
    )


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
# --------------------------------------------------------------------- #
# Alpha101 批次(issue #400)——共享口径辅助
# --------------------------------------------------------------------- #


def _values(
    series_by_symbol: Mapping[str, SymbolSeries],
) -> dict[str, np.ndarray]:
    """{symbol: SymbolSeries} → {symbol: 1-D float64 值数组}(逐行对齐)。"""
    return {symbol: series.values for symbol, series in series_by_symbol.items()}


def _per_symbol(
    values: Mapping[str, np.ndarray],
    fn: Callable[[np.ndarray], np.ndarray],
) -> dict[str, np.ndarray]:
    """逐标的套用一元时序/逐点算子(输入与输出逐行对齐)。"""
    return {symbol: fn(item) for symbol, item in values.items()}


def _per_symbol_pair(
    left: Mapping[str, np.ndarray],
    right: Mapping[str, np.ndarray],
    fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
) -> dict[str, np.ndarray]:
    """逐标的套用二元算子(两字段序列同一挂载表逐行对齐)。"""
    return {symbol: fn(left[symbol], right[symbol]) for symbol in left}


def _returns_values(closes: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """``Returns = close / delay(close, 1) - 1``(qfq 收盘;首行缺测)。"""
    return _per_symbol(
        closes, lambda c: ew_div(ts_delta(c, 1), ts_delay(c, 1))
    )


def _adv20_values(volumes: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """``ADV20`` = 20 日成交量均值(主流复现口径,量纲自洽)。"""
    return _per_symbol(volumes, lambda v: ts_mean(v, 20))


def _vwap_values(inp: PredefinedFactorInput) -> dict[str, np.ndarray]:
    """``VWAP ≈ amount / volume``(issue #400 验收口径;volume<=0 → NaN)。"""
    amounts = _values(inp.bars("amount"))
    volumes = _values(inp.bars("volume"))
    return _per_symbol_pair(amounts, volumes, lambda a, v: ew_div(a, v))


def _cs_transform(
    axis: Mapping[str, SymbolSeries],
    per_symbol: Mapping[str, np.ndarray],
    transform: Callable[[CrossSection], Mapping[str, float | None]],
    *,
    universe: Collection[str],
) -> dict[str, np.ndarray]:
    """逐自然日截面变换回填(Alpha101 的 ``Rank(x)`` 输入装配)。

    对每个业务日,取 ``universe`` 内标的当日有限值组截面喂给 ``transform``
    (如 :func:`cs_rank`),结果回填到各标的行序;非有限/缺测 → NaN。
    只用同日截面值 → 因果(截断前缀不变);``universe`` 外的标的
    (benchmark-only)全行 NaN,不进截面分母(#380)。
    """
    members = set(universe) & set(axis)
    by_date: dict[Any, list[tuple[str, int, float]]] = {}
    for symbol in sorted(members):
        series = axis[symbol]
        values = per_symbol[symbol]
        for position, day in enumerate(series.dates):
            value = float(values[position])
            if math.isfinite(value):
                by_date.setdefault(day, []).append((symbol, position, value))
    out = {s: np.full(v.size, np.nan) for s, v in per_symbol.items()}
    for items in by_date.values():
        cross = {symbol: value for symbol, _position, value in items}
        transformed = transform(cross)
        for symbol, position, _value in items:
            mapped = transformed.get(symbol)
            if mapped is not None and math.isfinite(mapped):
                out[symbol][position] = mapped
    return out


def _cs_rank_series(
    axis: Mapping[str, SymbolSeries],
    per_symbol: Mapping[str, np.ndarray],
    inp: PredefinedFactorInput,
) -> dict[str, np.ndarray]:
    """Alpha101 的 ``Rank(x)``:逐日截面百分位(分母 = 可交易域,#380)。"""
    return _cs_transform(axis, per_symbol, cs_rank, universe=inp.tradable_symbols)


def _signed_momentum_rule(delta: np.ndarray, window: int) -> np.ndarray:
    """Alpha101#9/#10 的条件符号规则:
    ``(0 < Ts_Min(d, w)) ? d : ((Ts_Max(d, w) < 0) ? d : -d)``。"""
    cond_rising = ew_gt(ts_min(delta, window), 0.0)
    cond_falling = ew_lt(ts_max(delta, window), 0.0)
    return ew_where(cond_rising, delta, ew_where(cond_falling, delta, -delta))


def _compute_alpha101_1(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(Ts_ArgMax(SignedPower(IF(Returns<0, StdDev(Returns,20), Close), 2), 5)) - 0.5``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    returns = _returns_values(closes)
    inner = {
        symbol: ew_where(
            ew_lt(item, 0.0), ts_std(item, 20), closes[symbol]
        )
        for symbol, item in returns.items()
    }
    positioned = _per_symbol(
        _per_symbol(inner, lambda x: ew_signed_power(x, 2.0)),
        lambda x: ts_argmax(x, 5),
    )
    ranked = _cs_rank_series(axis, positioned, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: x - 0.5))


def _compute_alpha101_2(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Rank(Delta(Log(Volume), 2)), Rank((Close-Open)/Open), 6)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    volumes = _values(inp.bars("volume"))
    left = _cs_rank_series(
        axis, _per_symbol(volumes, lambda v: ts_delta(ew_log(v), 2)), inp
    )
    right = _cs_rank_series(
        axis,
        _per_symbol_pair(closes, opens, lambda c, o: ew_div(c - o, o)),
        inp,
    )
    return inp.sample(
        axis, _per_symbol_pair(left, right, lambda a, b: -ts_corr(a, b, 6))
    )


def _compute_alpha101_3(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Rank(Open), Rank(Volume), 10)``。"""
    axis = inp.bars("open")
    ranked_open = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("close"), _values(inp.bars("volume")), inp
    )
    return inp.sample(
        axis,
        _per_symbol_pair(ranked_open, ranked_volume, lambda a, b: -ts_corr(a, b, 10)),
    )


def _compute_alpha101_4(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Ts_Rank(Rank(Low), 9)``。"""
    axis = inp.bars("low")
    ranked_low = _cs_rank_series(axis, _values(axis), inp)
    return inp.sample(axis, _per_symbol(ranked_low, lambda x: -ts_rank(x, 9)))


def _compute_alpha101_5(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(Open - Sum(VWAP,10)/10) * (-1 * Abs(Rank(Close - VWAP)))``。

    VWAP = amount/volume 近似(模块 docstring 量纲注记)。
    """
    axis = inp.bars("open")
    opens = _values(axis)
    closes = _values(inp.bars("close"))
    vwap = _vwap_values(inp)
    left = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, vwap, lambda o, v: o - ts_sum(v, 10) / 10.0),
        inp,
    )
    right = _cs_rank_series(
        axis,
        _per_symbol_pair(closes, vwap, lambda c, v: c - v),
        inp,
    )
    return inp.sample(
        axis,
        _per_symbol_pair(left, right, lambda a, b: a * -np.abs(b)),
    )


def _compute_alpha101_6(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Open, Volume, 10)``。"""
    axis = inp.bars("open")
    volumes = _values(inp.bars("volume"))
    return inp.sample(
        axis,
        _per_symbol_pair(_values(axis), volumes, lambda o, v: -ts_corr(o, v, 10)),
    )


def _compute_alpha101_7(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(ADV20 < Volume, -Ts_Rank(Abs(Delta(Close,7)), 60) * Sign(Delta(Close,7)), -1)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    adv20 = _adv20_values(volumes)
    delta7 = _per_symbol(closes, lambda c: ts_delta(c, 7))
    body = _per_symbol(
        delta7, lambda d: -ts_rank(np.abs(d), 60) * ew_sign(d)
    )
    condition = _per_symbol_pair(volumes, adv20, lambda v, a: ew_gt(v, a))
    fallback = {symbol: np.full(v.size, -1.0) for symbol, v in condition.items()}
    return inp.sample(
        axis,
        {
            symbol: ew_where(condition[symbol], body[symbol], fallback[symbol])
            for symbol in condition
        },
    )


def _compute_alpha101_8(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Sum(Open,5) * Sum(Returns,5) - Delay(Sum(Open,5) * Sum(Returns,5), 10))``。"""
    axis = inp.bars("close")
    opens = _values(inp.bars("open"))
    returns = _returns_values(_values(axis))
    combined = _per_symbol_pair(
        opens, returns, lambda o, r: ts_sum(o, 5) * ts_sum(r, 5)
    )
    diff = _per_symbol(combined, lambda x: x - ts_delay(x, 10))
    ranked = _cs_rank_series(axis, diff, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_9(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(0 < Ts_Min(Delta(Close,1),5), Delta(Close,1), IF(Ts_Max(Delta(Close,1),5) < 0, Delta(Close,1), -Delta(Close,1)))``。"""
    axis = inp.bars("close")
    delta1 = _per_symbol(_values(axis), lambda c: ts_delta(c, 1))
    return inp.sample(
        axis, _per_symbol(delta1, lambda d: _signed_momentum_rule(d, 5))
    )


def _compute_alpha101_10(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(IF(0 < Ts_Min(Delta(Close,1),4), Delta(Close,1), IF(Ts_Max(Delta(Close,1),4) < 0, Delta(Close,1), -Delta(Close,1))))``。"""
    axis = inp.bars("close")
    delta1 = _per_symbol(_values(axis), lambda c: ts_delta(c, 1))
    ruled = _per_symbol(delta1, lambda d: _signed_momentum_rule(d, 4))
    return inp.sample(axis, _cs_rank_series(axis, ruled, inp))


def _compute_alpha101_11(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(Rank(Ts_Max(VWAP-Close,3)) + Rank(Ts_Min(VWAP-Close,3))) * Rank(Delta(Volume,3))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    vwap = _vwap_values(inp)
    deviation = _per_symbol_pair(vwap, closes, lambda v, c: v - c)
    upper = _cs_rank_series(axis, _per_symbol(deviation, lambda x: ts_max(x, 3)), inp)
    lower = _cs_rank_series(axis, _per_symbol(deviation, lambda x: ts_min(x, 3)), inp)
    volume_rank = _cs_rank_series(
        axis, _per_symbol(volumes, lambda v: ts_delta(v, 3)), inp
    )
    return inp.sample(
        axis,
        {
            symbol: (upper[symbol] + lower[symbol]) * volume_rank[symbol]
            for symbol in upper
        },
    )


def _compute_alpha101_12(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Sign(Delta(Volume,1)) * (-1 * Delta(Close,1))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    return inp.sample(
        axis,
        {
            symbol: ew_sign(ts_delta(volumes[symbol], 1))
            * -ts_delta(closes[symbol], 1)
            for symbol in closes
        },
    )


def _compute_alpha101_13(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Covariance(Rank(Close), Rank(Volume), 5))``。"""
    axis = inp.bars("close")
    ranked_close = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    covariance = _per_symbol_pair(
        ranked_close, ranked_volume, lambda a, b: ts_cov(a, b, 5)
    )
    ranked = _cs_rank_series(axis, covariance, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_14(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Delta(Returns,3)) * Corr(Open, Volume, 10)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    volumes = _values(inp.bars("volume"))
    ranked_delta = _cs_rank_series(
        axis,
        _per_symbol(_returns_values(closes), lambda r: ts_delta(r, 3)),
        inp,
    )
    correlation = _per_symbol_pair(opens, volumes, lambda o, v: ts_corr(o, v, 10))
    return inp.sample(
        axis,
        _per_symbol_pair(ranked_delta, correlation, lambda a, b: -a * b),
    )


def _compute_alpha101_15(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Sum(Rank(Corr(Rank(High), Rank(Volume), 3)), 3)``。"""
    axis = inp.bars("high")
    ranked_high = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    correlation = _per_symbol_pair(
        ranked_high, ranked_volume, lambda a, b: ts_corr(a, b, 3)
    )
    ranked = _cs_rank_series(axis, correlation, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -ts_sum(x, 3)))


def _compute_alpha101_16(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Covariance(Rank(High), Rank(Volume), 5))``。"""
    axis = inp.bars("high")
    ranked_high = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    covariance = _per_symbol_pair(
        ranked_high, ranked_volume, lambda a, b: ts_cov(a, b, 5)
    )
    ranked = _cs_rank_series(axis, covariance, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_17(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Rank(Ts_Rank(Close,10)) * Rank(Delta(Delta(Close,1),1)) * Rank(Ts_Rank(Volume/ADV20,5))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    adv20 = _adv20_values(volumes)
    rank_price = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_rank(c, 10)), inp
    )
    rank_accel = _cs_rank_series(
        axis,
        _per_symbol(closes, lambda c: ts_delta(ts_delta(c, 1), 1)),
        inp,
    )
    relative_volume = _per_symbol_pair(volumes, adv20, lambda v, a: ew_div(v, a))
    rank_volume = _cs_rank_series(
        axis, _per_symbol(relative_volume, lambda r: ts_rank(r, 5)), inp
    )
    return inp.sample(
        axis,
        {
            symbol: -rank_price[symbol] * rank_accel[symbol] * rank_volume[symbol]
            for symbol in rank_price
        },
    )


def _compute_alpha101_18(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(StdDev(Abs(Close-Open),5) + (Close-Open) + Corr(Close,Open,10))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    body = _per_symbol_pair(
        closes,
        opens,
        lambda c, o: ts_std(np.abs(c - o), 5) + (c - o) + ts_corr(c, o, 10),
    )
    ranked = _cs_rank_series(axis, body, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_19(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Sign((Close-Delay(Close,7))+Delta(Close,7)) * (1+Rank(1+Sum(Returns,250))) - Rank(X)^2``;

    其中 ``X = Corr(Rank(VWAP-Close), Rank(Volume), 12) * Rank(Corr(Rank(Close),
    Rank(ADV20), 12))``(tushare 口径把论文的 scale/indneutralize 项写为
    ``-Rank(X) * Rank(X)``,逐字直译;注意 tushare 文本中第二个 Correlation
    外层有 ``Rank(...)``,与论文「两 Correlation 各自 Rank 后相乘」不同)。
    """
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    vwap = _vwap_values(inp)
    adv20 = _adv20_values(volumes)
    momentum_sign = _per_symbol(
        closes, lambda c: ew_sign((c - ts_delay(c, 7)) + ts_delta(c, 7))
    )
    long_return_rank = _cs_rank_series(
        axis,
        _per_symbol(returns, lambda r: 1.0 + ts_sum(r, 250)),
        inp,
    )
    term1 = _per_symbol_pair(
        momentum_sign, long_return_rank, lambda s, r: -s * (1.0 + r)
    )
    left_corr = _per_symbol_pair(
        _cs_rank_series(
            axis,
            _per_symbol_pair(vwap, closes, lambda v, c: v - c),
            inp,
        ),
        _cs_rank_series(inp.bars("volume"), volumes, inp),
        lambda a, b: ts_corr(a, b, 12),
    )
    right_corr = _per_symbol_pair(
        _cs_rank_series(axis, closes, inp),
        _cs_rank_series(axis, adv20, inp),
        lambda a, b: ts_corr(a, b, 12),
    )
    ranked_right_corr = _cs_rank_series(axis, right_corr, inp)
    product = _per_symbol_pair(
        left_corr, ranked_right_corr, lambda a, b: a * b
    )
    term2_rank = _cs_rank_series(axis, product, inp)
    return inp.sample(
        axis,
        _per_symbol_pair(
            term1, term2_rank, lambda a, r: a + (-r * r)
        ),
    )


def _compute_alpha101_20(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Rank(Open-Delay(High,1)) * Rank(Open-Delay(Close,1)) * Rank(Open-Delay(Low,1))``。"""
    axis = inp.bars("open")
    opens = _values(axis)
    highs = _values(inp.bars("high"))
    closes = _values(inp.bars("close"))
    lows = _values(inp.bars("low"))
    gap_high = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, highs, lambda o, h: o - ts_delay(h, 1)),
        inp,
    )
    gap_close = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, closes, lambda o, c: o - ts_delay(c, 1)),
        inp,
    )
    gap_low = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, lows, lambda o, low: o - ts_delay(low, 1)),
        inp,
    )
    return inp.sample(
        axis,
        {
            symbol: -gap_high[symbol] * gap_close[symbol] * gap_low[symbol]
            for symbol in gap_high
        },
    )


def _compute_alpha101_22(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Delta(Corr(High, Volume, 5), 5) * Rank(StdDev(Close, 20))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    volumes = _values(inp.bars("volume"))
    correlation_delta = _per_symbol_pair(
        highs, volumes, lambda h, v: ts_delta(ts_corr(h, v, 5), 5)
    )
    volatility_rank = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_std(c, 20)), inp
    )
    return inp.sample(
        axis,
        _per_symbol_pair(
            correlation_delta, volatility_rank, lambda a, b: -(a * b)
        ),
    )


def _compute_alpha101_23(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(Sum(High,20)/20 < High, -Delta(High,2), 0)``。"""
    axis = inp.bars("high")
    highs = _values(axis)
    return inp.sample(
        axis,
        {
            symbol: ew_where(
                ew_lt(ts_sum(highs[symbol], 20) / 20.0, highs[symbol]),
                -ts_delta(highs[symbol], 2),
                np.zeros(highs[symbol].size),
            )
            for symbol in highs
        },
    )


def _compute_alpha101_25(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(-1 * Returns * ADV20 * VWAP * (High - Close))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    adv20 = _adv20_values(volumes)
    vwap = _vwap_values(inp)
    body = {
        symbol: -returns[symbol]
        * adv20[symbol]
        * vwap[symbol]
        * (highs[symbol] - closes[symbol])
        for symbol in closes
    }
    return inp.sample(axis, _cs_rank_series(axis, body, inp))


def _compute_alpha101_33(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(-1 * (1 - Open/Close))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    body = _per_symbol_pair(opens, closes, lambda o, c: -(1.0 - ew_div(o, c)))
    return inp.sample(axis, _cs_rank_series(axis, body, inp))


def _compute_alpha101_34(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank((1 - Rank(StdDev(Returns,2)/StdDev(Returns,5))) + (1 - Rank(Delta(Close,1))))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    returns = _returns_values(closes)
    volatility_ratio = _per_symbol(
        returns, lambda r: ew_div(ts_std(r, 2), ts_std(r, 5))
    )
    rank_ratio = _cs_rank_series(axis, volatility_ratio, inp)
    rank_delta = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_delta(c, 1)), inp
    )
    combined = {
        symbol: (1.0 - rank_ratio[symbol]) + (1.0 - rank_delta[symbol])
        for symbol in rank_ratio
    }
    return inp.sample(axis, _cs_rank_series(axis, combined, inp))


def _compute_alpha101_41(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(High*Low)^0.5 - VWAP``(VWAP = amount/volume 近似,量纲注记见模块 docstring)。"""
    axis = inp.bars("close")
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    vwap = _vwap_values(inp)
    with np.errstate(invalid="ignore"):
        body = {
            symbol: np.sqrt(highs[symbol] * lows[symbol]) - vwap[symbol]
            for symbol in highs
        }
    return inp.sample(axis, body)


def _compute_alpha101_52(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(-Ts_Min(Low,5) + Delay(Ts_Min(Low,5),5)) * Rank((Sum(Returns,240)-Sum(Returns,20))/220) * Ts_Rank(Volume,5)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    lows = _values(inp.bars("low"))
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    low_trough = _per_symbol(lows, lambda low: ts_min(low, 5))
    head = _per_symbol(low_trough, lambda m: -m + ts_delay(m, 5))
    momentum_spread = _per_symbol(
        returns, lambda r: (ts_sum(r, 240) - ts_sum(r, 20)) / 220.0
    )
    ranked_spread = _cs_rank_series(axis, momentum_spread, inp)
    volume_rank = _per_symbol(volumes, lambda v: ts_rank(v, 5))
    return inp.sample(
        axis,
        {
            symbol: (head[symbol] * ranked_spread[symbol]) * volume_rank[symbol]
            for symbol in head
        },
    )


def _compute_alpha101_53(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Delta(((Close-Low)-(High-Close))/(Close-Low), 9)``(分母 0 → NaN,#400 除法纪律)。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            (closes[symbol] - lows[symbol]) - (highs[symbol] - closes[symbol]),
            closes[symbol] - lows[symbol],
        )
        for symbol in closes
    }
    return inp.sample(
        axis, _per_symbol(body, lambda x: -ts_delta(x, 9))
    )


def _compute_alpha101_54(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(-1 * (Low-Close) * Open^5) / ((Low-High) * Close^5)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            -1.0 * (lows[symbol] - closes[symbol]) * opens[symbol] ** 5,
            (lows[symbol] - highs[symbol]) * closes[symbol] ** 5,
        )
        for symbol in closes
    }
    return inp.sample(axis, body)


def _compute_alpha101_57(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-(Close - VWAP) / Decay_Linear(Rank(Ts_ArgMax(Close,30)), 2)``。

    VWAP = amount/volume 近似(量纲注记见模块 docstring)。
    """
    axis = inp.bars("close")
    closes = _values(axis)
    vwap = _vwap_values(inp)
    positioned = _per_symbol(closes, lambda c: ts_argmax(c, 30))
    ranked = _cs_rank_series(axis, positioned, inp)
    denominator = _per_symbol(ranked, lambda r: ts_decay(r, 2))
    body = {
        symbol: ew_div(vwap[symbol] - closes[symbol], denominator[symbol])
        for symbol in closes
    }
    return inp.sample(axis, body)


def _compute_alpha101_101(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(Close - Open) / ((High - Low) + 0.001)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            closes[symbol] - opens[symbol],
            (highs[symbol] - lows[symbol]) + 0.001,
        )
        for symbol in closes
    }
    return inp.sample(axis, body)


def industry_neutralize(
    values: CrossSection,
    groups: Mapping[str, str | None],
) -> tuple[dict[str, float | None], int]:
    """行业中性化(issue #400 口径):组内去均值,**缺组标签 → 缺测 + 计数**。

    与 :func:`operators.cs_neutralize`(缺组标的归入合成组一起去均值)
    的差异是本 issue 的验收口径:缺行业数据的标的宁可缺测(可见、
    可计数、由质量门把关)也不静默归组 —— 用法 = 因子 compute 经
    ``inp.industry_groups()`` 取 ``{symbol: 行业}`` 后传入本助手。
    组标签 = 冻结发布 ``instruments.industry``(#185,stock_basic 行业
    字符串近似 research_industry_memberships 一级分组;非 WQ
    ``indneutralize`` 的 rank 中性语义——组内原始值去均值)。
    返回 ``(中性化截面, 缺组标的数)``。
    """
    grouped = {
        symbol: (values[symbol] if (groups.get(symbol) or "") else None)
        for symbol in values
    }
    missing = sum(1 for symbol in values if not (groups.get(symbol) or ""))
    return cs_neutralize(grouped, groups), missing


def _alpha_definition(
    number: int,
    *,
    title: str,
    window: int | None,
    compute: PredefinedFactorCompute,
    cross_section: bool,
    data_dependencies: tuple[str, ...],
) -> PredefinedFactorDefinition:
    """Alpha101 批次条目工厂(issue #400)。

    方向 = 文献构建约定:WQ 论文按「因子值越高 → 预期收益越高」构造
    (负号已在公式内),故统一 ``HIGHER``;个别量纲敏感因子(41/54/57)
    的方向稳健性由 IC sanity 复核(PR 清单表标注)。
    """
    return PredefinedFactorDefinition(
        name=f"alpha101_{number}",
        title=title,
        family="alpha101",
        direction=FactorPreference.HIGHER,
        signal_eligible=True,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version="1",
        compute=compute,
        cross_section=cross_section,
    )


#: Alpha101 批次(tushare ``factor_list`` 圈定的 31 个;编号即 WQ 论文
#: 原始编号,缺号为论文中依赖行业/市值数据或非截面语义的未收录因子)。
#: ``cross_section=True`` = 公式含 ``Rank`` 类截面算子(构建期采样面
#: 收窄到可交易域,#380);纯时序条件公式 False。
_ALPHA101_FACTORS: tuple[PredefinedFactorDefinition, ...] = (
    _alpha_definition(
        1,
        title=(
            "Alpha101#1: Rank(Ts_ArgMax(SignedPower(IF(Returns<0, "
            "StdDev(Returns,20), Close), 2), 5)) - 0.5"
            "(负收益时段波动放大的极值位置排名)"
        ),
        window=20,
        compute=_compute_alpha101_1,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        2,
        title=(
            "Alpha101#2: -1 * Corr(Rank(Delta(Log(Volume), 2)), "
            "Rank((Close-Open)/Open), 6)(量变排名与日内动量排名的负相关)"
        ),
        window=6,
        compute=_compute_alpha101_2,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open", "bars.volume"),
    ),
    _alpha_definition(
        3,
        title=(
            "Alpha101#3: -1 * Corr(Rank(Open), Rank(Volume), 10)"
            "(开盘价排名与成交量排名的负相关)"
        ),
        window=10,
        compute=_compute_alpha101_3,
        cross_section=True,
        data_dependencies=("bars.open", "bars.volume"),
    ),
    _alpha_definition(
        4,
        title="Alpha101#4: -1 * Ts_Rank(Rank(Low), 9)(低价排名时序百分位的负值)",
        window=9,
        compute=_compute_alpha101_4,
        cross_section=True,
        data_dependencies=("bars.low",),
    ),
    _alpha_definition(
        5,
        title=(
            "Alpha101#5: Rank(Open - Sum(VWAP,10)/10) * "
            "(-1 * Abs(Rank(Close - VWAP)))(开盘对 VWAP 均值偏离 x 收盘对 VWAP 偏离)"
        ),
        window=10,
        compute=_compute_alpha101_5,
        cross_section=True,
        data_dependencies=(
            "bars.close",
            "bars.open",
            "bars.volume",
            "bars.amount",
        ),
    ),
    _alpha_definition(
        6,
        title=(
            "Alpha101#6: -1 * Corr(Open, Volume, 10)"
            "(开盘价与成交量的负相关;纯时序无截面算子)"
        ),
        window=10,
        compute=_compute_alpha101_6,
        cross_section=False,
        data_dependencies=("bars.open", "bars.volume"),
    ),
    _alpha_definition(
        7,
        title=(
            "Alpha101#7: IF(ADV20 < Volume, -Ts_Rank(Abs(Delta(Close,7)), 60) "
            "* Sign(Delta(Close,7)), -1)(放量日的 7 日动量符号加权)"
        ),
        window=60,
        compute=_compute_alpha101_7,
        cross_section=False,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        8,
        title=(
            "Alpha101#8: -1 * Rank(Sum(Open,5) * Sum(Returns,5) - "
            "Delay(Sum(Open,5) * Sum(Returns,5), 10))(开盘-收益联合动量反转)"
        ),
        window=10,
        compute=_compute_alpha101_8,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        9,
        title=(
            "Alpha101#9: IF(0 < Ts_Min(Delta(Close,1),5), Delta(Close,1), "
            "IF(Ts_Max(Delta(Close,1),5) < 0, Delta(Close,1), -Delta(Close,1)))"
            "(短期动量方向确认)"
        ),
        window=5,
        compute=_compute_alpha101_9,
        cross_section=False,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        10,
        title=(
            "Alpha101#10: Rank(IF(0 < Ts_Min(Delta(Close,1),4), Delta(Close,1), "
            "IF(Ts_Max(Delta(Close,1),4) < 0, Delta(Close,1), -Delta(Close,1))))"
            "(#9 的截面排名版,4 日窗)"
        ),
        window=4,
        compute=_compute_alpha101_10,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        11,
        title=(
            "Alpha101#11: (Rank(Ts_Max(VWAP-Close,3)) + Rank(Ts_Min(VWAP-Close,3))) "
            "* Rank(Delta(Volume,3))(VWAP 偏离极值 x 量变排名)"
        ),
        window=3,
        compute=_compute_alpha101_11,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        12,
        title=(
            "Alpha101#12: Sign(Delta(Volume,1)) * (-1 * Delta(Close,1))"
            "(量变方向加权的价格反转)"
        ),
        window=1,
        compute=_compute_alpha101_12,
        cross_section=False,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        13,
        title=(
            "Alpha101#13: -1 * Rank(Covariance(Rank(Close), Rank(Volume), 5))"
            "(量价排名协方差的负值)"
        ),
        window=5,
        compute=_compute_alpha101_13,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        14,
        title=(
            "Alpha101#14: -1 * Rank(Delta(Returns,3)) * Corr(Open, Volume, 10)"
            "(收益加速反转 x 量价相关)"
        ),
        window=10,
        compute=_compute_alpha101_14,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open", "bars.volume"),
    ),
    _alpha_definition(
        15,
        title=(
            "Alpha101#15: -1 * Sum(Rank(Corr(Rank(High), Rank(Volume), 3)), 3)"
            "(高价-量相关排名的 3 日和)"
        ),
        window=3,
        compute=_compute_alpha101_15,
        cross_section=True,
        data_dependencies=("bars.high", "bars.volume"),
    ),
    _alpha_definition(
        16,
        title=(
            "Alpha101#16: -1 * Rank(Covariance(Rank(High), Rank(Volume), 5))"
            "(高价-量排名协方差的负值)"
        ),
        window=5,
        compute=_compute_alpha101_16,
        cross_section=True,
        data_dependencies=("bars.high", "bars.volume"),
    ),
    _alpha_definition(
        17,
        title=(
            "Alpha101#17: -Rank(Ts_Rank(Close,10)) * Rank(Delta(Delta(Close,1),1)) "
            "* Rank(Ts_Rank(Volume/ADV20,5))(价格时序位置 x 二阶动量 x 相对量)"
        ),
        window=10,
        compute=_compute_alpha101_17,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        18,
        title=(
            "Alpha101#18: -1 * Rank(StdDev(Abs(Close-Open),5) + (Close-Open) "
            "+ Corr(Close,Open,10))(日内波动与动量组合的负值)"
        ),
        window=10,
        compute=_compute_alpha101_18,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        19,
        title=(
            "Alpha101#19: -Sign((Close-Delay(Close,7))+Delta(Close,7)) * "
            "(1+Rank(1+Sum(Returns,250))) - Rank(X)^2, X = Corr(Rank(VWAP-Close), "
            "Rank(Volume), 12) * Corr(Rank(Close), Rank(ADV20), 12)"
            "(长周期动量符号 x VWAP 偏离-量相关联合排名)"
        ),
        window=250,
        compute=_compute_alpha101_19,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        20,
        title=(
            "Alpha101#20: -Rank(Open-Delay(High,1)) * Rank(Open-Delay(Close,1)) "
            "* Rank(Open-Delay(Low,1))(开盘跳空三重排名反转)"
        ),
        window=1,
        compute=_compute_alpha101_20,
        cross_section=True,
        data_dependencies=(
            "bars.close",
            "bars.open",
            "bars.high",
            "bars.low",
        ),
    ),
    _alpha_definition(
        22,
        title=(
            "Alpha101#22: -Delta(Corr(High, Volume, 5), 5) * Rank(StdDev(Close, 20))"
            "(量价相关变化 x 波动排名)"
        ),
        window=20,
        compute=_compute_alpha101_22,
        cross_section=True,
        data_dependencies=("bars.close", "bars.high", "bars.volume"),
    ),
    _alpha_definition(
        23,
        title=(
            "Alpha101#23: IF(Sum(High,20)/20 < High, -Delta(High,2), 0)"
            "(突破 20 日高价均线后的短期回落)"
        ),
        window=20,
        compute=_compute_alpha101_23,
        cross_section=False,
        data_dependencies=("bars.high",),
    ),
    _alpha_definition(
        25,
        title=(
            "Alpha101#25: Rank(-1 * Returns * ADV20 * VWAP * (High - Close))"
            "(放量长上影反转)"
        ),
        window=20,
        compute=_compute_alpha101_25,
        cross_section=True,
        data_dependencies=("bars.close", "bars.high", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        33,
        title=(
            "Alpha101#33: Rank(-1 * (1 - Open/Close))(日内开盘-收盘反向排名)"
        ),
        window=None,
        compute=_compute_alpha101_33,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        34,
        title=(
            "Alpha101#34: Rank((1 - Rank(StdDev(Returns,2)/StdDev(Returns,5))) "
            "+ (1 - Rank(Delta(Close,1))))(短波动比与价格动量的组合排名)"
        ),
        window=5,
        compute=_compute_alpha101_34,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        41,
        title=(
            "Alpha101#41: (High*Low)^0.5 - VWAP(几何均价对 VWAP 的偏离;"
            "量纲敏感因子,方向待 IC 确认)"
        ),
        window=None,
        compute=_compute_alpha101_41,
        cross_section=False,
        data_dependencies=("bars.high", "bars.low", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        52,
        title=(
            "Alpha101#52: (-Ts_Min(Low,5) + Delay(Ts_Min(Low,5),5)) * "
            "Rank((Sum(Returns,240)-Sum(Returns,20))/220) * Ts_Rank(Volume,5)"
            "(低价改善 x 长短期动量差 x 量时序位置)"
        ),
        window=240,
        compute=_compute_alpha101_52,
        cross_section=True,
        data_dependencies=("bars.close", "bars.low", "bars.volume"),
    ),
    _alpha_definition(
        53,
        title=(
            "Alpha101#53: -Delta(((Close-Low)-(High-Close))/(Close-Low), 9)"
            "(日内位置指标的 9 日反转)"
        ),
        window=9,
        compute=_compute_alpha101_53,
        cross_section=False,
        data_dependencies=("bars.close", "bars.high", "bars.low"),
    ),
    _alpha_definition(
        54,
        title=(
            "Alpha101#54: (-1 * (Low-Close) * Open^5) / ((Low-High) * Close^5)"
            "(日内位置与开收盘比的极值组合;量纲敏感,方向待 IC 确认)"
        ),
        window=None,
        compute=_compute_alpha101_54,
        cross_section=False,
        data_dependencies=("bars.close", "bars.open", "bars.high", "bars.low"),
    ),
    _alpha_definition(
        57,
        title=(
            "Alpha101#57: -(Close - VWAP) / Decay_Linear(Rank(Ts_ArgMax(Close,30)), 2)"
            "(收盘-VWAP 偏离除以极值位置线性衰减;量纲敏感,方向待 IC 确认)"
        ),
        window=30,
        compute=_compute_alpha101_57,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        101,
        title=(
            "Alpha101#101: (Close - Open) / ((High - Low) + 0.001)(日内动量归一)"
        ),
        window=None,
        compute=_compute_alpha101_101,
        cross_section=False,
        data_dependencies=("bars.close", "bars.open", "bars.high", "bars.low"),
    ),
)


#: 目录(裸名 → 条目)。批次 0 样板:return_Nd 动量族(4 窗口变体);
#: 批次 2(#400):Alpha101 量价 31 因子;后续批次按同一模式追加注册。
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
        # ---------------- 批次 1(#399):量价 74 个 ----------------
        # momentum 族(回归 alpha / 残差动量 / MACD / RSRS / 位置 / 相对强弱)
        _entry(
            "reg_alpha_63d",
            title="reg_alpha_63d = 日收益对市场收益(000300.SH)trailing 63 根"
            " bar OLS 截距(市场模型 Jensen's alpha,日频)",
            family="momentum",
            compute=_reg_alpha(63),
            window=63,
            data_dependencies=("bars.close", "index_bars.close"),
        ),
        _entry(
            "reg_alpha_120d",
            title="reg_alpha_120d = 日收益对市场收益 trailing 120 根 bar OLS 截距",
            family="momentum",
            compute=_reg_alpha(120),
            window=120,
            data_dependencies=("bars.close", "index_bars.close"),
        ),
        _entry(
            "reg_alpha_250d",
            title="reg_alpha_250d = 日收益对市场收益 trailing 250 根 bar OLS 截距",
            family="momentum",
            compute=_reg_alpha(250),
            window=250,
            data_dependencies=("bars.close", "index_bars.close"),
        ),
        _entry(
            "resid_momentum_120d",
            title="resid_momentum_120d = 市场模型日残差(120 根 bar 滚动 OLS)"
            " 在 120 根 bar 内求和(残差动量;需 239 根 bar 历史)",
            family="momentum",
            compute=_resid_momentum(120),
            window=120,
            data_dependencies=("bars.close", "index_bars.close"),
            min_history_bars=239,
        ),
        _entry(
            "resid_momentum_250d",
            title="resid_momentum_250d = 市场模型日残差(250 根 bar 滚动 OLS)"
            " 在 250 根 bar 内求和(需 499 根 bar 历史)",
            family="momentum",
            compute=_resid_momentum(250),
            window=250,
            data_dependencies=("bars.close", "index_bars.close"),
            min_history_bars=499,
        ),
        _entry(
            "macd_hist_norm",
            title="macd_hist_norm = (DIF - DEA) * 2 / close;DIF = EMA12 - EMA26,"
            " DEA = EMA9(DIF)(MACD 柱,收盘价归一)",
            family="momentum",
            compute=_macd_hist_norm(),
            window=26,
        ),
        _entry(
            "rsrs_beta_600d",
            title="rsrs_beta_600d = high 对 low 的 trailing 600 根 bar OLS 斜率"
            "(RSRS 阻力/支撑相对强度)",
            family="momentum",
            compute=_rsrs(600, r2_weighted=False),
            window=600,
            data_dependencies=("bars.high", "bars.low"),
            min_history_bars=600,
        ),
        _entry(
            "rsrs_r2_600d",
            title="rsrs_r2_600d = RSRS 斜率 x 拟合 R²(斜率置信度加权的标准"
            " RSRS 指标)",
            family="momentum",
            compute=_rsrs(600, r2_weighted=True),
            window=600,
            data_dependencies=("bars.high", "bars.low"),
            min_history_bars=600,
        ),
        _entry(
            "momentum_12_1d",
            title="momentum_12_1d = return_252d - return_21d(12-1 动量:剔除"
            "近月反转效应)",
            family="momentum",
            compute=_momentum_skip_month(252, 21),
            window=252,
        ),
        _entry(
            "price_pos_252d",
            title="price_pos_252d = (close - min_252) / (max_252 - min_252)"
            "(52 周价格位置)",
            family="momentum",
            compute=_price_position(252),
            window=252,
        ),
        _entry(
            "price_pos_120d",
            title="price_pos_120d = (close - min_120) / (max_120 - min_120)",
            family="momentum",
            compute=_price_position(120),
            window=120,
        ),
        _entry(
            "ema_ratio_20_60d",
            title="ema_ratio_20_60d = EMA20 / EMA60 - 1(快慢趋势强度)",
            family="momentum",
            compute=_ema_ratio(20, 60),
            window=60,
        ),
        _entry(
            "rs_vs_index_252d",
            title="rs_vs_index_252d = 股票 252 根 bar 区间收益 - 000300.SH 同窗"
            "区间收益(相对强弱)",
            family="momentum",
            compute=_rs_vs_index(252),
            window=252,
            data_dependencies=("bars.close", "index_bars.close"),
        ),
        # reversal 族
        _entry(
            "rsi_14d",
            title="rsi_14d = 100 * MA(涨幅,14) / (MA(涨幅,14) + MA(跌幅,14))"
            "(超买看空,direction LOWER)",
            family="reversal",
            compute=_rsi(14),
            window=14,
            direction=FactorPreference.LOWER,
        ),
        _entry(
            "bias_20d",
            title="bias_20d = close / MA20 - 1(20 根 bar 乖离率,高乖离看空)",
            family="reversal",
            compute=_bias(20),
            window=20,
            direction=FactorPreference.LOWER,
        ),
        # risk 族(波动 / beta / 特异波动 / 市场相关 / Sharpe / 高阶矩 / 回撤;
        # 风险暴露定位 signal_eligible=False,direction 记录弱研究先验)
        *_vol_entries(),
        *_liquidity_entries(),
        *_size_entries(),
        *_ALPHA101_FACTORS,
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
    if item.min_history_bars is not None:
        # 覆盖起点声明是语义字段(#399):None 省略键,批次 0 因子锚逐字节稳定
        payload["min_history_bars"] = item.min_history_bars
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
    "MARKET_INDEX_SYMBOL",
    "PREDEFINED_FACTORS",
    "PREDEFINED_FACTORS_SCHEMA_VERSION",
    "PredefinedFactorCompute",
    "PredefinedFactorDefinition",
    "get_predefined_factor",
    "industry_neutralize",
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
]
