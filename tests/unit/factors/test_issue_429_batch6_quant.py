"""因子批次 6(#429):P1 量价 28 个的目录不变量与逐值单测。

* 目录不变量(批次 scope,无全局 exact-total 断言):28 个全部注册、
  家族计数(liquidity 17 / risk 6 / momentum 5)、direction 与
  signal_eligible 口径、窗口 / ``min_history_bars``(252/504 长窗)/
  data_dependencies 精确、commit 锚互异;
* 逐族 compute:手工小样本字面锚点(换手乖离 11/13 构造、换手波动
  乖离 √12 构造、净值高低比 2.0 构造、return/std/单位成交额波动、
  日内位置 IR ±1 构造)+ 合成行情下独立 pandas 参考逐值对照
  (rolling mean/std/cumprod/ewm);
* 边界:长窗不足 → None、daily_metrics 未挂载 → 空截面、净值恒定 →
  比值 1、净值缩放不变、一字板(high==low)→ 缺测、成交额窗口合计
  0 → None、常数价格 → DIF/DEA = 0。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import functools
import math
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
    sample_series_frame,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    get_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)

#: 批次 6 新注册的 28 个裸名(与 tushare factor_list 2026-09-11 快照名一致)
BATCH6_NAMES = frozenset(
    {
        # liquidity(17):换手乖离 x8 + 换手波动乖离 x8 + 单位成交额波动
        *(
            f"bias_turn_{short}d_{long}d"
            for short in (21, 42, 63, 126)
            for long in (252, 504)
        ),
        *(
            f"bias_std_turn_{short}d_{long}d"
            for short in (21, 42, 63, 126)
            for long in (252, 504)
        ),
        "sum_abs_rtn_amount_20d",
        # risk(6):净值高低比 x5 + 收益波动窗口变体
        *(f"high_low_{window}d" for window in (21, 42, 63, 126, 252)),
        "return_std_42d",
        # momentum(6):窗口变体 x2 + 日内位置 IR + MACD 分量 x2
        "return_5d",
        "return_42d",
        "price_position_ir_60d",
        "dif",
        "dea",
    }
)

STOCKS = ("600000.SH", "000001.SZ")


class _Batch6Input(PredefinedFactorInput):
    """批次 6 测试输入面:多字段 bars / daily_metrics 显式注入。"""

    def __init__(
        self,
        bars_by_field: Mapping[str, Mapping[str, SymbolSeries]],
        *,
        decision_dates: tuple[date, ...],
        daily_by_field: Mapping[str, Mapping[str, SymbolSeries]] | None = None,
    ) -> None:
        super().__init__(
            factor_name="batch6_test",
            decision_dates=decision_dates,
            tradable_symbols=tuple(bars_by_field.get("close", {})),
            benchmark_only_symbols=frozenset(),
        )
        self._bars = {field: dict(series) for field, series in bars_by_field.items()}
        self._daily = {
            field: dict(series) for field, series in (daily_by_field or {}).items()
        }

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return dict(self._bars.get(field, {}))

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._daily.get(field, {}))

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        # 批次 6 因子不消费公告序列;#402 契约「未挂载 → 空映射」。
        return {}

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        if kind == "daily_metrics":
            return self.daily_metrics(field)
        return {}

    def dividend_events(self) -> dict[str, Any]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
        )


def _series(values: pd.Series, *, available_hour: int = 15) -> SymbolSeries:
    """pandas 序列(业务日索引)→ SymbolSeries。"""
    dates = tuple(values.index)
    return SymbolSeries(
        dates=dates,
        values=values.to_numpy(dtype=np.float64),
        available_at=tuple(
            datetime(d.year, d.month, d.day, available_hour, tzinfo=UTC)
            for d in dates
        ),
    )


@functools.lru_cache(maxsize=1)
def _universe() -> (
    tuple[list[date], dict[str, dict[str, SymbolSeries]], dict[str, dict[str, SymbolSeries]]]
):
    """合成宇宙:2 只股票(540 根 bar,覆盖 504 长窗),返回 dates/bars/daily。

    OHLC 满足 high > max(close, open) >= min(close, open) > low(日内
    区间恒正,(close-open)/(high-low) 无零分母),换手率独立均匀分布。
    """
    n = 540
    dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(n)]
    rng = np.random.default_rng(429)
    bars_by_field: dict[str, dict[str, SymbolSeries]] = {
        field: {} for field in ("close", "open", "high", "low", "amount")
    }
    daily_by_field: dict[str, dict[str, SymbolSeries]] = {"turnover_rate": {}}
    profiles = {
        "600000.SH": (0.0006, 0.018, 12.0),
        "000001.SZ": (-0.0002, 0.025, 8.0),
    }
    for symbol, (drift, vol, base) in profiles.items():
        close = pd.Series(
            base * np.cumprod(1.0 + rng.normal(drift, vol, n)), index=dates
        )
        open_ = close * (1.0 + rng.normal(0.0, 0.004, n))
        span = np.abs(rng.normal(0.01, 0.004, n))
        high = np.maximum(close, open_) * (1.0 + span)
        low = np.minimum(close, open_) * (1.0 - span)
        amount = pd.Series(np.abs(rng.normal(2.0e8, 5.0e7, n)), index=dates)
        turnover = pd.Series(rng.uniform(0.5, 8.0, n), index=dates)
        bars_by_field["close"][symbol] = _series(close)
        bars_by_field["open"][symbol] = _series(open_)
        bars_by_field["high"][symbol] = _series(pd.Series(high, index=dates))
        bars_by_field["low"][symbol] = _series(pd.Series(low, index=dates))
        bars_by_field["amount"][symbol] = _series(amount)
        daily_by_field["turnover_rate"][symbol] = _series(turnover)
    return dates, bars_by_field, daily_by_field


def _stock_references() -> dict[str, pd.DataFrame]:
    """逐股票的 pandas 参考列(从合成宇宙的 bars/daily 重算)。"""
    _, bars, daily = _universe()
    references: dict[str, pd.DataFrame] = {}
    for symbol in STOCKS:
        close = pd.Series(
            bars["close"][symbol].values, index=bars["close"][symbol].dates
        )
        open_ = pd.Series(
            bars["open"][symbol].values, index=bars["open"][symbol].dates
        )
        high = pd.Series(
            bars["high"][symbol].values, index=bars["high"][symbol].dates
        )
        low = pd.Series(bars["low"][symbol].values, index=bars["low"][symbol].dates)
        amount = pd.Series(
            bars["amount"][symbol].values, index=bars["amount"][symbol].dates
        )
        turnover = pd.Series(
            daily["turnover_rate"][symbol].values,
            index=daily["turnover_rate"][symbol].dates,
        )
        references[symbol] = pd.DataFrame(
            {
                "close": close,
                "open": open_,
                "high": high,
                "low": low,
                "amount": amount,
                "turnover": turnover,
                "ret": close.pct_change(),
            }
        )
    return references


def _decision_dates() -> tuple[date, ...]:
    dates, _, _ = _universe()
    return tuple(dates[-3:])


def _compute(name: str, inp: PredefinedFactorInput) -> FactorSeriesFrame:
    return get_predefined_factor(name).compute(inp)


def _assert_matches(
    frame: FactorSeriesFrame,
    reference: pd.Series,
    symbol: str,
    *,
    rel: float = 1e-9,
) -> None:
    """逐决策日对照:参考缺测 → None;参考有限 → approx 相等。"""
    for day in _decision_dates():
        expected = reference.loc[:day].iloc[-1]
        got = frame[day][symbol]
        if not math.isfinite(float(expected)):
            assert got is None, (day, symbol, expected, got)
        else:
            assert got is not None, (day, symbol, expected, got)
            assert got == pytest.approx(float(expected), rel=rel, abs=1e-15)


def _literal_input(
    *,
    close: np.ndarray,
    turnover: np.ndarray | None = None,
    open_: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    amount: np.ndarray | None = None,
) -> tuple[_Batch6Input, tuple[date, ...]]:
    """手工小样本 → 输入(单标的,日期轴按数组长度生成)。"""
    n = close.size
    dates = tuple(date(2023, 1, 2) + timedelta(days=i) for i in range(n))
    bars_by_field: dict[str, dict[str, SymbolSeries]] = {
        "close": {"S": _series(pd.Series(close, index=dates))}
    }
    daily_by_field: dict[str, dict[str, SymbolSeries]] = {}
    if open_ is not None:
        bars_by_field["open"] = {"S": _series(pd.Series(open_, index=dates))}
    if high is not None:
        bars_by_field["high"] = {"S": _series(pd.Series(high, index=dates))}
    if low is not None:
        bars_by_field["low"] = {"S": _series(pd.Series(low, index=dates))}
    if amount is not None:
        bars_by_field["amount"] = {"S": _series(pd.Series(amount, index=dates))}
    if turnover is not None:
        daily_by_field["turnover_rate"] = {
            "S": _series(pd.Series(turnover, index=dates))
        }
    return _Batch6Input(
        bars_by_field, decision_dates=(dates[-1],), daily_by_field=daily_by_field
    ), (dates[-1],)


class TestCatalogBatch6:
    """目录不变量(批次 scope):注册 / 家族 / 元数据 / 锚。"""

    def test_batch6_exactly_28_registered(self) -> None:
        assert len(BATCH6_NAMES) == 28
        registered = set(predefined_factor_names())
        assert BATCH6_NAMES.issubset(registered)
        # 字典推导对重名静默覆盖 —— 28 个条目对象逐一可按名取回且 name 一致
        for name in BATCH6_NAMES:
            assert get_predefined_factor(name).name == name

    def test_family_counts_scoped_to_batch(self) -> None:
        families: Counter[str] = Counter(
            PREDEFINED_FACTORS[name].family for name in BATCH6_NAMES
        )
        assert families == Counter({"liquidity": 17, "risk": 6, "momentum": 5})

    def test_direction_and_signal_eligible_policy(self) -> None:
        for name in BATCH6_NAMES:
            item = PREDEFINED_FACTORS[name]
            if item.family == "momentum":
                assert item.signal_eligible, name
                assert item.direction.value == "higher", name
            elif item.family == "risk":
                # 风险暴露定位:高/低比与已实现波动均 direction LOWER
                assert not item.signal_eligible, name
                assert item.direction.value == "lower", name
            else:  # liquidity:相对活跃度口径 HIGHER(turnover_ratio/bias 族约定)
                assert not item.signal_eligible, name
                assert item.direction.value == "higher", name

    def test_windows_follow_names(self) -> None:
        for name in BATCH6_NAMES:
            item = PREDEFINED_FACTORS[name]
            if name.startswith(("bias_turn_", "bias_std_turn_")):
                # 双窗因子:window = 长窗(tail),与 turnover_ratio 族口径一致
                assert item.window == int(name.rsplit("_", 1)[1].removesuffix("d")), name
            elif name.startswith("high_low_"):
                assert item.window == int(
                    name.removeprefix("high_low_").removesuffix("d")
                ), name
            elif name == "return_5d":
                assert item.window == 5
            elif name in ("return_42d", "return_std_42d"):
                assert item.window == 42, name
            elif name == "price_position_ir_60d":
                assert item.window == 60
            elif name in ("dif", "dea"):
                # 与 macd_hist_norm 的声明窗口一致(DIF 需要 EMA26)
                assert item.window == 26, name
            else:
                assert item.window == 20, name  # sum_abs_rtn_amount_20d

    def test_min_history_bars_on_long_windows(self) -> None:
        """252/504 长窗声明覆盖起点(#361 入队具名拒绝短历史全缺测)。"""
        declared = {
            name: PREDEFINED_FACTORS[name].min_history_bars
            for name in BATCH6_NAMES
            if PREDEFINED_FACTORS[name].min_history_bars is not None
        }
        assert declared == {
            **{
                f"bias_turn_{short}d_{long}d": long
                for short in (21, 42, 63, 126)
                for long in (252, 504)
            },
            **{
                f"bias_std_turn_{short}d_{long}d": long
                for short in (21, 42, 63, 126)
                for long in (252, 504)
            },
            "high_low_252d": 252,
        }
        # 其余(短窗/中窗)不声明 —— 与 vol_20d 等既有口径一致
        assert all(
            PREDEFINED_FACTORS[name].min_history_bars is None
            for name in BATCH6_NAMES - set(declared)
        )

    def test_data_dependencies_exact(self) -> None:
        for name in BATCH6_NAMES:
            item = PREDEFINED_FACTORS[name]
            if name.startswith(("bias_turn_", "bias_std_turn_")):
                assert item.data_dependencies == ("daily_metrics.turnover_rate",), name
            elif name == "sum_abs_rtn_amount_20d":
                assert item.data_dependencies == ("bars.close", "bars.amount"), name
            elif name == "price_position_ir_60d":
                assert item.data_dependencies == (
                    "bars.close",
                    "bars.open",
                    "bars.high",
                    "bars.low",
                ), name
            else:
                assert item.data_dependencies == ("bars.close",), name

    def test_anchors_distinct_and_shape(self) -> None:
        anchors = {predefined_factor_commit(name) for name in BATCH6_NAMES}
        assert len(anchors) == 28
        for name in BATCH6_NAMES:
            item = PREDEFINED_FACTORS[name]
            assert item.implementation_version == "1", name
            assert item.cross_section is False, name
            assert predefined_factor_commit(name).startswith("predefined-"), name


class TestTurnoverBias:
    """换手乖离 bias_turn_*(liquidity):手工小样本 + pandas 参考。"""

    def test_hand_anchor_21_252(self) -> None:
        # 252 根换手:前 231 根 1.0、后 21 根 2.0 →
        # MA_short = 2.0,MA_long = (231*1 + 21*2)/252 = 273/252 →
        # bias = 2/(273/252) - 1 = 231/273 = 11/13
        turnover = np.array([1.0] * 231 + [2.0] * 21)
        inp, (day,) = _literal_input(close=np.ones(252), turnover=turnover)
        frame = _compute("bias_turn_21d_252d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(11.0 / 13.0, rel=1e-12)

    @pytest.mark.parametrize(
        ("name", "short", "long"),
        [
            ("bias_turn_21d_252d", 21, 252),
            ("bias_turn_63d_252d", 63, 252),
            ("bias_turn_126d_504d", 126, 504),
        ],
    )
    def test_matches_pandas(self, name: str, short: int, long: int) -> None:
        _, bars, daily = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(name, inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            expected = (
                turnover.rolling(short).mean() / turnover.rolling(long).mean() - 1.0
            )
            _assert_matches(frame, expected, symbol)

    def test_long_window_shortage_yields_none(self) -> None:
        dates, bars, daily = _universe()
        day = dates[100]  # 长窗 252/504 均不足
        inp = _Batch6Input(bars, decision_dates=(day,), daily_by_field=daily)
        for name in ("bias_turn_21d_252d", "bias_std_turn_21d_252d", "bias_turn_21d_504d"):
            frame = _compute(name, inp)
            for symbol in STOCKS:
                assert frame[day][symbol] is None, (name, symbol)

    def test_daily_metrics_unmounted_yields_empty_frame(self) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        frame = _compute("bias_turn_42d_252d", inp)
        assert set(frame) == set(_decision_dates())
        assert all(frame[day] == {} for day in frame)


class TestTurnoverStdBias:
    """换手波动乖离 bias_std_turn_*:手工小样本 + pandas 参考。"""

    def test_hand_anchor_21_252(self) -> None:
        # 252 根换手:前 251 根 0、末根 1 →
        # 短窗(21)样本标准差 = 1/√21,长窗(252)= 1/√252 →
        # bias = √(252/21) - 1 = √12 - 1
        turnover = np.array([0.0] * 251 + [1.0])
        inp, (day,) = _literal_input(close=np.ones(252), turnover=turnover)
        frame = _compute("bias_std_turn_21d_252d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(math.sqrt(12.0) - 1.0, rel=1e-12)

    def test_constant_turnover_yields_none(self) -> None:
        # 常数换手 → 两个窗口标准差均为 0 → 0/0 - 1 → 缺测
        turnover = np.full(300, 3.0)
        inp, (day,) = _literal_input(close=np.ones(300), turnover=turnover)
        frame = _compute("bias_std_turn_21d_252d", inp)
        assert frame[day]["S"] is None

    @pytest.mark.parametrize(
        ("name", "short", "long"),
        [
            ("bias_std_turn_21d_252d", 21, 252),
            ("bias_std_turn_63d_504d", 63, 504),
        ],
    )
    def test_matches_pandas(self, name: str, short: int, long: int) -> None:
        _, bars, daily = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(name, inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            expected = (
                turnover.rolling(short).std(ddof=1)
                / turnover.rolling(long).std(ddof=1)
                - 1.0
            )
            _assert_matches(frame, expected, symbol)


class TestHighLow:
    """净值高低比 high_low_*(risk):手工小样本 + pandas 参考 + 缩放不变。"""

    def test_hand_anchor_21d(self) -> None:
        # 23 根 close:[100]*9 + [50] + [100]*13 → 净值在 pos9 = 0.5、其余 1;
        # 末位 trailing 21(pos2..22)含 0.5 → max/min = 1/0.5 = 2.0
        close = np.array([100.0] * 9 + [50.0] + [100.0] * 13)
        inp, (day,) = _literal_input(close=close)
        frame = _compute("high_low_21d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(2.0, rel=1e-12)

    def test_flat_window_yields_one(self) -> None:
        # 净值恒定 → max = min > 0 → 比值 1(区间无运动,有效值)
        close = np.full(30, 77.0)
        inp, (day,) = _literal_input(close=close)
        frame = _compute("high_low_21d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(1.0, rel=1e-12)

    def test_nav_scale_invariance(self) -> None:
        # 因子只依赖日收益:价格整体缩放不改变净值比值(锚点缩放消去)
        close = np.array([100.0] * 9 + [50.0] + [100.0] * 13)
        inp_a, (day_a,) = _literal_input(close=close)
        inp_b, (day_b,) = _literal_input(close=close * 7.3)
        got_a = _compute("high_low_21d", inp_a)[day_a]["S"]
        got_b = _compute("high_low_21d", inp_b)[day_b]["S"]
        assert got_a is not None
        assert got_b is not None
        assert got_a == pytest.approx(got_b, rel=1e-12)

    @pytest.mark.parametrize(("name", "window"), [("high_low_21d", 21), ("high_low_252d", 252)])
    def test_matches_pandas(self, name: str, window: int) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        frame = _compute(name, inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            nav = (close.pct_change().fillna(0.0) + 1.0).cumprod()
            expected = nav.rolling(window).max() / nav.rolling(window).min()
            _assert_matches(frame, expected, symbol)

    def test_long_window_shortage_yields_none(self) -> None:
        dates, bars, _ = _universe()
        day = dates[100]
        frame = _compute("high_low_252d", _Batch6Input(bars, decision_dates=(day,)))
        for symbol in STOCKS:
            assert frame[day][symbol] is None, symbol


class TestReturnVariants:
    """窗口变体:return_5d / return_42d / return_std_42d。"""

    def test_return_5d_hand_anchor(self) -> None:
        close = np.array([100.0] * 5 + [110.0])
        inp, (day,) = _literal_input(close=close)
        frame = _compute("return_5d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(0.10, rel=1e-12)

    def test_return_42d_hand_anchor(self) -> None:
        close = np.array([100.0] * 42 + [200.0])
        inp, (day,) = _literal_input(close=close)
        frame = _compute("return_42d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(1.0, rel=1e-12)

    def test_return_std_42d_hand_anchor(self) -> None:
        # 43 根 close:日收益 [0, +1%, -1%, ...](42 个 ±1%)→
        # 末位 trailing 42 日收益样本标准差(ddof=1)= 0.01·sqrt(42/41)
        # (与 vol 族同口径:ts_std 默认样本标准差)
        returns = np.array([0.0] + [0.01, -0.01] * 21)
        close = 100.0 * np.cumprod(1.0 + returns)
        inp, (day,) = _literal_input(close=close)
        frame = _compute("return_std_42d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(0.01 * math.sqrt(42 / 41), rel=1e-12)

    def test_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            _assert_matches(
                _compute("return_5d", inp), ref["close"].pct_change(5), symbol
            )
            _assert_matches(
                _compute("return_42d", inp), ref["close"].pct_change(42), symbol
            )
            _assert_matches(
                _compute("return_std_42d", inp), ref["ret"].rolling(42).std(ddof=1), symbol
            )


class TestPricePositionIr:
    """日内位置信息比率 price_position_ir_60d(momentum)。"""

    def test_hand_anchor_alternating_sign(self) -> None:
        # 61 根:ratio = (close-open)/(high-low) 交替 ±1(l=10,h=12,o=11,
        # c 交替 12/10)→ 末位 60 日 mean = 0、std = 1 → IR = 0
        n = 61
        close = np.array([11.0] + [12.0, 10.0] * 30)
        open_ = np.full(n, 11.0)
        high = np.full(n, 12.0)
        low = np.full(n, 10.0)
        inp, (day,) = _literal_input(close=close, open_=open_, high=high, low=low)
        frame = _compute("price_position_ir_60d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(0.0, abs=1e-12)

    def test_constant_ratio_yields_none(self) -> None:
        # ratio 恒 0.5(c = o + 0.5*(h-l))→ std = 0 → 0/0 → 缺测
        n = 61
        open_ = np.full(n, 10.0)
        high = np.full(n, 13.0)
        low = np.full(n, 9.0)
        close = open_ + 0.5 * (high - low)
        inp, (day,) = _literal_input(close=close, open_=open_, high=high, low=low)
        frame = _compute("price_position_ir_60d", inp)
        assert frame[day]["S"] is None

    def test_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        frame = _compute("price_position_ir_60d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            ratio = (ref["close"] - ref["open"]) / (ref["high"] - ref["low"])
            expected = ratio.rolling(60).mean() / ratio.rolling(60).std(ddof=1)
            _assert_matches(frame, expected, symbol)

    def test_window_shortage_yields_none(self) -> None:
        dates, bars, _ = _universe()
        day = dates[30]
        frame = _compute("price_position_ir_60d", _Batch6Input(bars, decision_dates=(day,)))
        for symbol in STOCKS:
            assert frame[day][symbol] is None, symbol


class TestSumAbsReturnAmount:
    """单位成交额波动 sum_abs_rtn_amount_20d(liquidity,Amihud 求和形态)。"""

    def test_hand_anchor(self) -> None:
        # 22 根:仅末日 +10%(|Σret| = 0.1),成交额恒 1000(Σ = 20000)→ 5e-6
        close = np.array([100.0] * 21 + [110.0])
        amount = np.full(22, 1000.0)
        inp, (day,) = _literal_input(close=close, amount=amount)
        frame = _compute("sum_abs_rtn_amount_20d", inp)
        got = frame[day]["S"]
        assert got is not None
        assert got == pytest.approx(0.1 / 20000.0, rel=1e-12)

    def test_zero_amount_window_yields_none(self) -> None:
        # 窗口内成交额合计 0 → 分母 0 → 缺测(采样归一 None,fail-visible)
        close = np.array([100.0] * 21 + [110.0])
        amount = np.array([1000.0] * 2 + [0.0] * 20)
        inp, (day,) = _literal_input(close=close, amount=amount)
        frame = _compute("sum_abs_rtn_amount_20d", inp)
        assert frame[day]["S"] is None

    def test_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        frame = _compute("sum_abs_rtn_amount_20d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            expected = ref["ret"].abs().rolling(20).sum() / ref["amount"].rolling(20).sum()
            _assert_matches(frame, expected, symbol)


class TestMacdComponents:
    """MACD 分量 dif / dea(momentum,原始价格量纲,与 macd_hist_norm 同口径)。"""

    def test_constant_close_yields_zero(self) -> None:
        # 常数价格 → EMA12 = EMA26 = close → DIF = 0,DEA = EMA9(0) = 0
        close = np.full(40, 100.0)
        inp, (day,) = _literal_input(close=close)
        dif = _compute("dif", inp)[day]["S"]
        dea = _compute("dea", inp)[day]["S"]
        assert dif is not None
        assert dea is not None
        assert dif == pytest.approx(0.0, abs=1e-12)
        assert dea == pytest.approx(0.0, abs=1e-12)

    def test_matches_pandas_ewm(self) -> None:
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        frame_dif = _compute("dif", inp)
        frame_dea = _compute("dea", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            dif = close.ewm(span=12, adjust=False).mean() - close.ewm(
                span=26, adjust=False
            ).mean()
            dea = dif.ewm(span=9, adjust=False).mean()
            _assert_matches(frame_dif, dif, symbol)
            _assert_matches(frame_dea, dea, symbol)

    def test_components_reconstruct_macd_hist_numerator(self) -> None:
        # 共享口径:dif - dea == macd_hist_norm * close / 2(同 DIF/DEA 链)
        _, bars, _ = _universe()
        inp = _Batch6Input(bars, decision_dates=_decision_dates())
        hist = _compute("macd_hist_norm", inp)
        dif = _compute("dif", inp)
        dea = _compute("dea", inp)
        for day in _decision_dates():
            for symbol in STOCKS:
                close = _stock_references()[symbol]["close"].loc[:day].iloc[-1]
                hist_value = hist[day][symbol]
                dif_value = dif[day][symbol]
                dea_value = dea[day][symbol]
                assert hist_value is not None
                assert dif_value is not None
                assert dea_value is not None
                reconstructed = (dif_value - dea_value) * 2.0 / close
                assert reconstructed == pytest.approx(hist_value, rel=1e-9)
