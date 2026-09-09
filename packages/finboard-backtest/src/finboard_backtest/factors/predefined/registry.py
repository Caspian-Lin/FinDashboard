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

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
)
from finboard_backtest.factors.predefined.operators import (
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
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
]
