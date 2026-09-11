"""因子批次 1(#399):量价 74 个的目录不变量与逐值单测。

* 目录不变量:家族计数(momentum 17 / reversal 2 / risk 24 /
  liquidity 32 / size 3)、指数依赖与 ``min_history_bars`` 声明、
  signal_eligible 口径、批次 0 commit 锚逐字节稳定;
* 逐族 compute:合成行情下与**独立 pandas 参考**逐值对照
  (reg_alpha/beta/RSRS 用 pandas rolling cov/var/corr 独立重算,
  残差动量 / Amihud / RSI / 下行波动用逐窗循环参照);
* 边界:窗口不足 → None、停牌缺行延续、指数缺失 → 全缺测、
  bars/daily_metrics 日期交集对齐、daily 未挂载 → 空截面。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import functools
import math
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
    MARKET_INDEX_SYMBOL,
    PREDEFINED_FACTORS,
    get_predefined_factor,
    is_registered_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)

#: 批次 1 新注册的 74 个裸名(与 PR 注册清单总表一致)
BATCH1_NAMES = frozenset(
    {
        # momentum(13)
        "reg_alpha_63d",
        "reg_alpha_120d",
        "reg_alpha_250d",
        "resid_momentum_120d",
        "resid_momentum_250d",
        "macd_hist_norm",
        "rsrs_beta_600d",
        "rsrs_r2_600d",
        "momentum_12_1d",
        "price_pos_252d",
        "price_pos_120d",
        "ema_ratio_20_60d",
        "rs_vs_index_252d",
        # reversal(2)
        "rsi_14d",
        "bias_20d",
        # risk(24)
        "vol_20d",
        "vol_60d",
        "vol_120d",
        "vol_250d",
        "vol_ratio_20_60d",
        "beta_60d",
        "beta_120d",
        "beta_250d",
        "beta_1320d",
        "specific_vol_60d",
        "specific_vol_120d",
        "specific_vol_250d",
        "corr_market_60d",
        "corr_market_120d",
        "corr_market_250d",
        "corr_market_1320d",
        "sharpe_60d",
        "sharpe_120d",
        "sharpe_250d",
        "sharpe_1320d",
        "skew_250d",
        "kurt_250d",
        "downside_vol_250d",
        "drawdown_250d",
        # liquidity(32)
        "turnover_ma_5d",
        "turnover_ma_10d",
        "turnover_ma_20d",
        "turnover_ma_60d",
        "turnover_ma_120d",
        "turnover_ma_250d",
        "turnover_std_10d",
        "turnover_std_20d",
        "turnover_std_60d",
        "turnover_std_120d",
        "turnover_std_250d",
        "turnover_bias_20d",
        "turnover_bias_60d",
        "turnover_bias_250d",
        "turnover_z_60d",
        "turnover_z_250d",
        "turnover_ratio_5_20d",
        "turnover_ratio_20_60d",
        "amount_ma_20d",
        "amount_ma_60d",
        "amount_ma_250d",
        "volume_ma_20d",
        "volume_ma_60d",
        "volume_ma_250d",
        "amihud_20d",
        "amihud_60d",
        "amihud_120d",
        "amihud_250d",
        "vwap_dev_20d",
        "vwap_dev_60d",
        "turnover_ret_corr_20d",
        "turnover_ret_corr_60d",
        # size(3)
        "log_total_market_cap",
        "log_circulating_market_cap",
        "float_share_ratio",
    }
)

STOCKS = ("600000.SH", "000001.SZ", "300750.SZ")


class _BatchInput(PredefinedFactorInput):
    """批次 1 测试输入面:多字段 bars / daily_metrics 显式注入。"""

    def __init__(
        self,
        bars_by_field: Mapping[str, Mapping[str, SymbolSeries]],
        *,
        decision_dates: tuple[date, ...],
        daily_by_field: Mapping[str, Mapping[str, SymbolSeries]] | None = None,
        benchmark: frozenset[str] = frozenset({MARKET_INDEX_SYMBOL}),
    ) -> None:
        super().__init__(
            factor_name="batch1_test",
            decision_dates=decision_dates,
            tradable_symbols=tuple(
                symbol
                for symbol in bars_by_field.get("close", {})
                if symbol not in benchmark
            ),
            benchmark_only_symbols=benchmark,
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
        # 批次 1 因子不消费公告序列;#402 契约「未挂载 → 空映射」。
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
def _universe() -> tuple[list[date], dict[str, dict[str, SymbolSeries]], dict[str, dict[str, SymbolSeries]]]:
    """合成宇宙:市场指数 + 3 只股票(720 根 bar),返回 dates/bars/daily。"""
    n = 720
    dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(n)]
    rng = np.random.default_rng(399)
    market_close = 4000.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.008, n))
    market = pd.Series(market_close, index=dates)
    bars_by_field: dict[str, dict[str, SymbolSeries]] = {
        field: {MARKET_INDEX_SYMBOL: _series(market)} for field in ("close", "high", "low")
    }
    bars_by_field["volume"] = {
        MARKET_INDEX_SYMBOL: _series(pd.Series(rng.uniform(1e8, 3e8, n), index=dates))
    }
    bars_by_field["amount"] = {
        MARKET_INDEX_SYMBOL: _series(pd.Series(rng.uniform(1e10, 3e10, n), index=dates))
    }
    daily_by_field: dict[str, dict[str, SymbolSeries]] = {}
    profiles = {
        "600000.SH": (0.0006, 0.018, 12.0),
        "000001.SZ": (-0.0002, 0.025, 8.0),
        "300750.SZ": (0.0010, 0.030, 40.0),
    }
    for symbol, (drift, vol, base) in profiles.items():
        close = pd.Series(
            base * np.cumprod(1.0 + rng.normal(drift, vol, n)), index=dates
        )
        high = close * (1.0 + np.abs(rng.normal(0.0, 0.008, n)))
        low = close * (1.0 - np.abs(rng.normal(0.0, 0.008, n)))
        volume = pd.Series(rng.uniform(5e6, 2e7, n), index=dates)
        amount = volume * close * (1.0 + rng.normal(0.0, 0.01, n))
        bars_by_field["close"][symbol] = _series(close)
        bars_by_field["high"][symbol] = _series(pd.Series(high, index=dates))
        bars_by_field["low"][symbol] = _series(pd.Series(low, index=dates))
        bars_by_field["volume"][symbol] = _series(volume)
        bars_by_field["amount"][symbol] = _series(amount)
        turnover = pd.Series(rng.uniform(0.5, 8.0, n), index=dates)
        total_shares = pd.Series(1.0e9, index=dates)
        float_shares = pd.Series(6.0e8, index=dates)
        total_cap = close * total_shares
        circ_cap = close * float_shares
        daily_by_field["turnover_rate"] = daily_by_field.get("turnover_rate", {})
        daily_by_field["turnover_rate"][symbol] = _series(turnover)
        daily_by_field["total_shares"] = daily_by_field.get("total_shares", {})
        daily_by_field["total_shares"][symbol] = _series(total_shares)
        daily_by_field["float_shares"] = daily_by_field.get("float_shares", {})
        daily_by_field["float_shares"][symbol] = _series(float_shares)
        daily_by_field["total_market_cap"] = daily_by_field.get("total_market_cap", {})
        daily_by_field["total_market_cap"][symbol] = _series(total_cap)
        daily_by_field["circulating_market_cap"] = daily_by_field.get(
            "circulating_market_cap", {}
        )
        daily_by_field["circulating_market_cap"][symbol] = _series(circ_cap)
    return dates, bars_by_field, daily_by_field


def _stock_references() -> dict[str, pd.DataFrame]:
    """逐股票的 pandas 参考列(从合成宇宙的 bars/daily 重算)。"""
    _, bars, daily = _universe()
    references: dict[str, pd.DataFrame] = {}
    for symbol in STOCKS:
        close = pd.Series(
            bars["close"][symbol].values, index=bars["close"][symbol].dates
        )
        market = pd.Series(
            bars["close"][MARKET_INDEX_SYMBOL].values,
            index=bars["close"][MARKET_INDEX_SYMBOL].dates,
        )
        stock_ret = close.pct_change()
        market_ret = market.pct_change()
        turnover = pd.Series(
            daily["turnover_rate"][symbol].values,
            index=daily["turnover_rate"][symbol].dates,
        )
        references[symbol] = pd.DataFrame(
            {
                "close": close,
                "stock_ret": stock_ret,
                "market_ret": market_ret,
                "turnover": turnover,
            }
        )
    return references


def _decision_dates() -> tuple[date, ...]:
    dates, _, _ = _universe()
    return tuple(dates[-4:])


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


def _rolling_cov(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    return a.rolling(window).cov(b)


class TestCatalogBatch1:
    """目录不变量:家族计数 / 声明 / 批次 0 锚稳定。"""

    def test_family_counts_and_total(self) -> None:
        """家族计数按批次 scope 断言(目录并集后含批次 2-4,#403 收口)。"""
        batch0 = {"return_21d", "return_63d", "return_126d", "return_252d"}
        batch_scope = BATCH1_NAMES | batch0
        families: dict[str, int] = {}
        for name, item in PREDEFINED_FACTORS.items():
            if name in batch_scope:
                families[item.family] = families.get(item.family, 0) + 1
        assert families == {
            "momentum": 17,
            "reversal": 2,
            "risk": 24,
            "liquidity": 32,
            "size": 3,
        }
        registered = set(predefined_factor_names())
        assert len(batch_scope & registered) == 78
        assert registered >= BATCH1_NAMES

    def test_index_dependencies_declared(self) -> None:
        expected = {
            "reg_alpha_63d",
            "reg_alpha_120d",
            "reg_alpha_250d",
            "resid_momentum_120d",
            "resid_momentum_250d",
            "rs_vs_index_252d",
            "beta_60d",
            "beta_120d",
            "beta_250d",
            "beta_1320d",
            "specific_vol_60d",
            "specific_vol_120d",
            "specific_vol_250d",
            "corr_market_60d",
            "corr_market_120d",
            "corr_market_250d",
            "corr_market_1320d",
        }
        declared = {
            name
            for name, item in PREDEFINED_FACTORS.items()
            if any(dep.startswith("index_bars.") for dep in item.data_dependencies)
        }
        assert declared == expected
        # 指数依赖因子同时声明 bars.close(标的行情)
        assert all(
            "bars.close" in PREDEFINED_FACTORS[name].data_dependencies
            for name in expected
        )

    def test_min_history_bars_declarations(self) -> None:
        declared = {
            name: item.min_history_bars
            for name, item in PREDEFINED_FACTORS.items()
            if item.min_history_bars is not None
        }
        # 批次 1 基线 7 个长窗声明精确到值;后续批次(#429 等)的声明集合在
        # 各自测试文件内做批次 scope 精确断言,此处只锁定基线与通用不变量
        # (声明覆盖起点 >= window),避免每加一批就改历史断言。
        assert {
            name: declared.get(name)
            for name in (
                "resid_momentum_120d",
                "resid_momentum_250d",
                "rsrs_beta_600d",
                "rsrs_r2_600d",
                "beta_1320d",
                "corr_market_1320d",
                "sharpe_1320d",
            )
        } == {
            "resid_momentum_120d": 239,
            "resid_momentum_250d": 499,
            "rsrs_beta_600d": 600,
            "rsrs_r2_600d": 600,
            "beta_1320d": 1320,
            "corr_market_1320d": 1320,
            "sharpe_1320d": 1320,
        }
        for name, warmup in declared.items():
            item = PREDEFINED_FACTORS[name]
            assert item.window is not None, name
            assert warmup >= item.window, name

    def test_min_history_bars_validation(self) -> None:
        from dataclasses import replace


        base = get_predefined_factor("beta_1320d")
        with pytest.raises(ValueError, match="min_history_bars"):
            replace(base, min_history_bars=0)
        with pytest.raises(ValueError, match="min_history_bars"):
            replace(base, window=2000)

    def test_signal_eligible_policy(self) -> None:
        for name in BATCH1_NAMES:
            item = PREDEFINED_FACTORS[name]
            if item.family in {"momentum", "reversal"}:
                assert item.signal_eligible, name
            elif item.family in {"risk", "size"} or item.family == "liquidity":
                assert not item.signal_eligible, name
        # direction 口径:风险暴露族记录弱先验(波动/回撤 LOWER,Sharpe HIGHER)
        assert PREDEFINED_FACTORS["vol_60d"].direction.value == "lower"
        assert PREDEFINED_FACTORS["sharpe_120d"].direction.value == "higher"
        assert PREDEFINED_FACTORS["beta_120d"].direction.value == "lower"
        assert PREDEFINED_FACTORS["log_total_market_cap"].direction.value == "lower"
        assert PREDEFINED_FACTORS["rsi_14d"].direction.value == "lower"

    def test_batch0_anchor_byte_stable(self) -> None:
        """批次 0 因子(min_history_bars=None)锚不含新键,与 C0 逐字节一致。"""
        import hashlib
        import json

        item = get_predefined_factor("return_21d")
        payload = {
            "schema_version": "v1",
            "name": item.name,
            "data_dependencies": sorted(item.data_dependencies),
            "window": item.window,
            "cross_section": item.cross_section,
            "implementation_version": item.implementation_version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert predefined_factor_commit("return_21d") == f"predefined-{digest[:12]}"

    def test_declared_anchor_includes_min_history(self) -> None:
        import hashlib
        import json

        item = get_predefined_factor("beta_1320d")
        payload = {
            "schema_version": "v1",
            "name": item.name,
            "data_dependencies": sorted(item.data_dependencies),
            "window": item.window,
            "cross_section": item.cross_section,
            "implementation_version": item.implementation_version,
            "min_history_bars": item.min_history_bars,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert predefined_factor_commit("beta_1320d") == f"predefined-{digest[:12]}"


class TestMomentumBatch1:
    """momentum 新族:回归 alpha / 残差动量 / MACD / RSRS / 位置 / 相对强弱。"""

    @pytest.mark.parametrize("window", [63, 120, 250])
    def test_reg_alpha_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"reg_alpha_{window}d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            beta = _rolling_cov(ref["stock_ret"], ref["market_ret"], window) / ref[
                "market_ret"
            ].rolling(window).var(ddof=1)
            expected = (
                ref["stock_ret"].rolling(window).mean()
                - beta * ref["market_ret"].rolling(window).mean()
            )
            _assert_matches(frame, expected, symbol)

    def test_resid_momentum_matches_polyfit_reference(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("resid_momentum_120d", inp)
        window = 120
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            stock_ret = ref["stock_ret"].to_numpy()
            market_ret = ref["market_ret"].to_numpy()
            index = ref.index
            resid = pd.Series(np.nan, index=index)
            # r[0] 为 NaN(pct_change 首位)→ 含它的窗口无残差,从 i=window 起
            for i in range(window, len(index)):
                y = stock_ret[i - window + 1 : i + 1]
                x = market_ret[i - window + 1 : i + 1]
                slope, intercept = np.polyfit(x, y, 1)
                resid.iloc[i] = y[-1] - (slope * x[-1] + intercept)
            expected = resid.rolling(window).sum()
            _assert_matches(frame, expected, symbol)

    def test_macd_hist_norm_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("macd_hist_norm", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            dif = close.ewm(span=12, adjust=False).mean() - close.ewm(
                span=26, adjust=False
            ).mean()
            dea = dif.ewm(span=9, adjust=False).mean()
            _assert_matches(frame, (dif - dea) * 2.0 / close, symbol)

    def test_rsrs_matches_pandas_and_hand_anchor(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame_beta = _compute("rsrs_beta_600d", inp)
        frame_r2 = _compute("rsrs_r2_600d", inp)
        window = 600
        for symbol in STOCKS:
            _, bars_, _ = _universe()
            high = pd.Series(bars_["high"][symbol].values, index=bars_["high"][symbol].dates)
            low = pd.Series(bars_["low"][symbol].values, index=bars_["low"][symbol].dates)
            slope = _rolling_cov(high, low, window) / low.rolling(window).var(ddof=1)
            rho = high.rolling(window).corr(low)
            _assert_matches(frame_beta, slope, symbol)
            _assert_matches(frame_r2, slope * rho * rho, symbol)
        # 手算锚点:窗口 [h]=(2,4,6),[l]=(1,2,3) → slope = cov/var:
        # 高对低完全线性(h = 2l)→ slope=2,rho=1 → r2 版本 = 2
        from finboard_backtest.factors.predefined.operators import ts_corr, ts_cov

        h_arr = np.array([2.0, 4.0, 6.0])
        l_arr = np.array([1.0, 2.0, 3.0])
        slope_hand = ts_cov(h_arr, l_arr, 3) / ts_cov(l_arr, l_arr, 3)
        assert slope_hand[2] == pytest.approx(2.0)
        assert (ts_corr(h_arr, l_arr, 3) ** 2)[2] == pytest.approx(1.0)

    def test_momentum_12_1d_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("momentum_12_1d", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            expected = close.pct_change(252) - close.pct_change(21)
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize("window", [120, 252])
    def test_price_pos_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"price_pos_{window}d", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            lo = close.rolling(window).min()
            hi = close.rolling(window).max()
            _assert_matches(frame, (close - lo) / (hi - lo), symbol)

    def test_ema_ratio_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("ema_ratio_20_60d", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            expected = close.ewm(span=20, adjust=False).mean() / close.ewm(
                span=60, adjust=False
            ).mean() - 1.0
            _assert_matches(frame, expected, symbol)

    def test_rs_vs_index_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("rs_vs_index_252d", inp)
        market = pd.Series(
            bars["close"][MARKET_INDEX_SYMBOL].values,
            index=bars["close"][MARKET_INDEX_SYMBOL].dates,
        )
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            expected = (close / close.shift(252) - 1.0) - (
                market / market.shift(252) - 1.0
            )
            _assert_matches(frame, expected, symbol)


class TestReversalBatch1:
    """reversal 族:RSI(手算 + pandas)/ 乖离率。"""

    def test_rsi_hand_anchor(self) -> None:
        # 手算:14 日窗口缩到 3 日演示口径——涨幅 [1,0,1],跌幅 [0,1,0]:
        # avg_gain=2/3, avg_loss=1/3 → RSI = 100*(2/3)/(2/3+1/3) = 66.67
        closes = pd.Series([10.0, 11.0, 10.0, 11.0])
        change = closes.diff()
        gain = change.clip(lower=0.0)
        loss = (-change).clip(lower=0.0)
        rsi = 100.0 * gain.rolling(3).mean() / (
            gain.rolling(3).mean() + loss.rolling(3).mean()
        )
        assert rsi.iloc[-1] == pytest.approx(100.0 * (2.0 / 3.0), rel=1e-12)

    def test_rsi_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("rsi_14d", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            change = close.diff()
            gain = change.clip(lower=0.0)
            loss = (-change).clip(lower=0.0)
            expected = 100.0 * gain.rolling(14).mean() / (
                gain.rolling(14).mean() + loss.rolling(14).mean()
            )
            _assert_matches(frame, expected, symbol)

    def test_bias_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("bias_20d", inp)
        for symbol in STOCKS:
            close = _stock_references()[symbol]["close"]
            _assert_matches(frame, close / close.rolling(20).mean() - 1.0, symbol)


class TestRiskBatch1:
    """risk 族:波动 / beta / 特异波动 / 市场相关 / Sharpe / 高阶矩 / 回撤。"""

    @pytest.mark.parametrize("window", [20, 60, 120, 250])
    def test_vol_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"vol_{window}d", inp)
        for symbol in STOCKS:
            ret = _stock_references()[symbol]["stock_ret"]
            _assert_matches(frame, ret.rolling(window).std(ddof=1), symbol)

    def test_vol_ratio_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("vol_ratio_20_60d", inp)
        for symbol in STOCKS:
            ret = _stock_references()[symbol]["stock_ret"]
            expected = ret.rolling(20).std(ddof=1) / ret.rolling(60).std(ddof=1)
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize("window", [60, 120, 250])
    def test_beta_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"beta_{window}d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            expected = _rolling_cov(ref["stock_ret"], ref["market_ret"], window) / ref[
                "market_ret"
            ].rolling(window).var(ddof=1)
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize("window", [60, 120, 250])
    def test_specific_vol_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"specific_vol_{window}d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            total = ref["stock_ret"].rolling(window).std(ddof=1)
            rho = ref["stock_ret"].rolling(window).corr(ref["market_ret"])
            expected = total * np.sqrt((1.0 - rho * rho).clip(lower=0.0))
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize("window", [60, 120, 250])
    def test_corr_market_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"corr_market_{window}d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            _assert_matches(
                frame, ref["stock_ret"].rolling(window).corr(ref["market_ret"]), symbol
            )

    @pytest.mark.parametrize("window", [60, 120, 250])
    def test_sharpe_matches_pandas(self, window: int) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute(f"sharpe_{window}d", inp)
        for symbol in STOCKS:
            ret = _stock_references()[symbol]["stock_ret"]
            expected = (
                ret.rolling(window).mean()
                / ret.rolling(window).std(ddof=1)
                * math.sqrt(250.0)
            )
            _assert_matches(frame, expected, symbol)

    def test_skew_kurt_downside_drawdown_match_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame_skew = _compute("skew_250d", inp)
        frame_kurt = _compute("kurt_250d", inp)
        frame_downside = _compute("downside_vol_250d", inp)
        frame_drawdown = _compute("drawdown_250d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            ret = ref["stock_ret"]
            close = ref["close"]
            _assert_matches(frame_skew, ret.rolling(250).skew(), symbol)
            _assert_matches(frame_kurt, ret.rolling(250).kurt(), symbol)
            negatives = ret.to_numpy()
            downside = pd.Series(np.nan, index=ref.index)
            for i in range(249, len(negatives)):
                window_values = negatives[i - 249 : i + 1]
                neg = window_values[window_values < 0]
                if neg.size > 1:
                    downside.iloc[i] = float(np.std(neg, ddof=1))
            _assert_matches(frame_downside, downside, symbol)
            hi = close.rolling(250).max()
            _assert_matches(frame_drawdown, (hi - close) / hi, symbol)

    def test_long_window_short_history_yields_none(self) -> None:
        """1320d 长窗口在 720 根 bar 上全缺测(覆盖起点晚于窗口)。"""
        dates, bars, _ = _universe()
        day = dates[-1]
        for name in ("beta_1320d", "corr_market_1320d", "sharpe_1320d"):
            frame = _compute(name, _BatchInput(bars, decision_dates=(day,)))
            for symbol in STOCKS:
                assert frame[day][symbol] is None, (name, symbol)

    def test_index_missing_yields_none(self) -> None:
        """发布不含基准指数 → 市场收益因子全缺测(fail-visible)。"""
        dates, bars, _ = _universe()
        day = dates[-1]
        without_index = {
            field: {
                symbol: series
                for symbol, series in series_by_field.items()
                if symbol != MARKET_INDEX_SYMBOL
            }
            for field, series_by_field in bars.items()
        }
        for name in ("beta_120d", "reg_alpha_63d", "rs_vs_index_252d", "corr_market_60d"):
            frame = _compute(name, _BatchInput(without_index, decision_dates=(day,)))
            for symbol in STOCKS:
                assert frame[day][symbol] is None, (name, symbol)

    def test_short_window_shortage_yields_none(self) -> None:
        dates, bars, _ = _universe()
        day = dates[30]  # 窗口 60/120/250 均不足
        for name in ("vol_60d", "beta_120d", "sharpe_250d", "skew_250d"):
            frame = _compute(name, _BatchInput(bars, decision_dates=(day,)))
            for symbol in STOCKS:
                assert frame[day][symbol] is None, (name, symbol)


class TestLiquidityBatch1:
    """liquidity 族:换手率窗口展开 / 成交额量均值 / Amihud / VWAP / 量价相关。"""

    @pytest.mark.parametrize("window", [5, 20, 120])
    def test_turnover_ma_matches_pandas(self, window: int) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(f"turnover_ma_{window}d", inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            _assert_matches(frame, turnover.rolling(window).mean(), symbol)

    @pytest.mark.parametrize("window", [10, 60, 250])
    def test_turnover_std_matches_pandas(self, window: int) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(f"turnover_std_{window}d", inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            _assert_matches(frame, turnover.rolling(window).std(ddof=1), symbol)

    @pytest.mark.parametrize("window", [20, 60, 250])
    def test_turnover_bias_matches_pandas(self, window: int) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(f"turnover_bias_{window}d", inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            expected = turnover / turnover.rolling(window).mean() - 1.0
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize("window", [60, 250])
    def test_turnover_z_matches_pandas(self, window: int) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(f"turnover_z_{window}d", inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            expected = (turnover - turnover.rolling(window).mean()) / turnover.rolling(
                window
            ).std(ddof=1)
            _assert_matches(frame, expected, symbol)

    @pytest.mark.parametrize(
        ("name", "fast", "slow"), [("turnover_ratio_5_20d", 5, 20), ("turnover_ratio_20_60d", 20, 60)]
    )
    def test_turnover_ratio_matches_pandas(
        self, name: str, fast: int, slow: int
    ) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute(name, inp)
        for symbol in STOCKS:
            turnover = _stock_references()[symbol]["turnover"]
            expected = turnover.rolling(fast).mean() / turnover.rolling(slow).mean()
            _assert_matches(frame, expected, symbol)

    def test_amount_volume_ma_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame_amount = _compute("amount_ma_60d", inp)
        frame_volume = _compute("volume_ma_20d", inp)
        for symbol in STOCKS:
            _, bars_, _ = _universe()
            amount = pd.Series(
                bars_["amount"][symbol].values, index=bars_["amount"][symbol].dates
            )
            volume = pd.Series(
                bars_["volume"][symbol].values, index=bars_["volume"][symbol].dates
            )
            _assert_matches(frame_amount, amount.rolling(60).mean(), symbol)
            _assert_matches(frame_volume, volume.rolling(20).mean(), symbol)

    def test_amihud_matches_loop_reference(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("amihud_60d", inp)
        for symbol in STOCKS:
            _, bars_, _ = _universe()
            close = pd.Series(
                bars_["close"][symbol].values, index=bars_["close"][symbol].dates
            )
            amount = pd.Series(
                bars_["amount"][symbol].values, index=bars_["amount"][symbol].dates
            )
            expected = (close.pct_change().abs() / amount).rolling(60).mean()
            _assert_matches(frame, expected, symbol)

    def test_vwap_dev_matches_pandas(self) -> None:
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("vwap_dev_20d", inp)
        for symbol in STOCKS:
            _, bars_, _ = _universe()
            close = pd.Series(
                bars_["close"][symbol].values, index=bars_["close"][symbol].dates
            )
            amount = pd.Series(
                bars_["amount"][symbol].values, index=bars_["amount"][symbol].dates
            )
            volume = pd.Series(
                bars_["volume"][symbol].values, index=bars_["volume"][symbol].dates
            )
            vwap = amount.rolling(20).sum() / volume.rolling(20).sum()
            _assert_matches(frame, close / vwap - 1.0, symbol)

    def test_turnover_ret_corr_matches_pandas(self) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame = _compute("turnover_ret_corr_60d", inp)
        for symbol in STOCKS:
            ref = _stock_references()[symbol]
            _assert_matches(
                frame, ref["turnover"].rolling(60).corr(ref["stock_ret"]), symbol
            )

    def test_daily_metrics_unmounted_yields_empty_frame(self) -> None:
        """daily 未挂载 → 换手率因子全日期空截面(不炸构建)。"""
        _, bars, _ = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates())
        frame = _compute("turnover_ma_20d", inp)
        assert set(frame) == set(_decision_dates())
        assert all(frame[day] == {} for day in frame)


class TestSizeBatch1:
    """size 族:对数市值 / 流通占比(window=None,逐日截面原料变换)。"""

    def test_log_market_cap_matches_reference(self) -> None:
        _, bars, daily = _universe()
        inp = _BatchInput(bars, decision_dates=_decision_dates(), daily_by_field=daily)
        frame_total = _compute("log_total_market_cap", inp)
        frame_circ = _compute("log_circulating_market_cap", inp)
        frame_ratio = _compute("float_share_ratio", inp)
        for symbol in STOCKS:
            _, _, daily_ = _universe()
            total = pd.Series(
                daily_["total_market_cap"][symbol].values,
                index=daily_["total_market_cap"][symbol].dates,
            )
            circ = pd.Series(
                daily_["circulating_market_cap"][symbol].values,
                index=daily_["circulating_market_cap"][symbol].dates,
            )
            float_shares = pd.Series(
                daily_["float_shares"][symbol].values,
                index=daily_["float_shares"][symbol].dates,
            )
            total_shares = pd.Series(
                daily_["total_shares"][symbol].values,
                index=daily_["total_shares"][symbol].dates,
            )
            _assert_matches(frame_total, pd.Series(np.log(total), index=total.index), symbol)
            _assert_matches(frame_circ, pd.Series(np.log(circ), index=circ.index), symbol)
            _assert_matches(frame_ratio, float_shares / total_shares, symbol)
            # 市值口径量纲检查:对数市值随价格单调
            assert frame_total[_decision_dates()[-1]][symbol] is not None

    def test_window_is_none_for_size_factors(self) -> None:
        for name in ("log_total_market_cap", "log_circulating_market_cap", "float_share_ratio"):
            assert PREDEFINED_FACTORS[name].window is None


class TestIndexBarsAccessor:
    """``PredefinedFactorInput.index_bars`` 默认实现(v1 派生自 bars)。"""

    def test_default_filters_benchmark_only(self) -> None:
        dates, bars, _ = _universe()
        # 注入一只期货主连(benchmark-only)验证子集口径
        bars = {**bars, "close": {**bars["close"], "IF.CFFEX": bars["close"][MARKET_INDEX_SYMBOL]}}
        inp = _BatchInput(
            bars,
            decision_dates=(dates[-1],),
            benchmark=frozenset({MARKET_INDEX_SYMBOL, "IF.CFFEX"}),
        )
        index_bars = inp.index_bars("close")
        assert set(index_bars) == {MARKET_INDEX_SYMBOL, "IF.CFFEX"}
        # 非 benchmark 字段返回空
        assert inp.index_bars("nonexistent") == {}

    def test_missing_index_returns_empty(self) -> None:
        dates, bars, _ = _universe()
        without_index = {
            field: {
                symbol: series
                for symbol, series in series_by_field.items()
                if symbol != MARKET_INDEX_SYMBOL
            }
            for field, series_by_field in bars.items()
        }
        inp = _BatchInput(without_index, decision_dates=(dates[-1],))
        assert inp.index_bars("close") == {}


class TestAlignmentAndSuspension:
    """跨数据集日期交集对齐与停牌缺行语义。"""

    def test_turnover_ret_corr_with_bars_gaps(self) -> None:
        """bars 缺行(停牌)→ 与 daily 按交集对齐,值与 gappy 序列参考一致。"""
        dates, bars, daily = _universe()
        day = dates[-1]
        # 600000.SH 在窗口中段抹去 40 根 bar(模拟长期停牌 = 行缺失)
        gapped = dict(bars["close"])
        original = gapped["600000.SH"]
        keep = list(range(300)) + list(range(340, len(original.dates)))
        gapped["600000.SH"] = SymbolSeries(
            dates=tuple(original.dates[i] for i in keep),
            values=original.values[keep],
            available_at=tuple(original.available_at[i] for i in keep),
        )
        inp = _BatchInput(
            {"close": gapped},
            decision_dates=(day,),
            daily_by_field=daily,
        )
        frame = _compute("turnover_ret_corr_60d", inp)
        # 参考在 gappy close(交集)上重算
        turnover_series = daily["turnover_rate"]["600000.SH"]
        turnover = pd.Series(turnover_series.values, index=turnover_series.dates)
        close = pd.Series(gapped["600000.SH"].values, index=gapped["600000.SH"].dates)
        expected = turnover.rolling(60).corr(close.pct_change())
        got = frame[day]["600000.SH"]
        assert got is not None
        assert got == pytest.approx(float(expected.loc[:day].iloc[-1]), rel=1e-9)

    def test_suspension_extends_to_last_available_bar(self) -> None:
        """停牌缺行 = 序列行缺失:决策日取最近可得 bar(与 C0 return 族同语义)。"""
        dates, bars, _ = _universe()
        day = dates[-1]
        missing_tail = dict(bars["close"])
        original = missing_tail["600000.SH"]
        keep = list(range(len(original.dates) - 30))
        missing_tail["600000.SH"] = SymbolSeries(
            dates=tuple(original.dates[i] for i in keep),
            values=original.values[keep],
            available_at=tuple(original.available_at[i] for i in keep),
        )
        inp = _BatchInput({"close": missing_tail}, decision_dates=(day,))
        frame = _compute("bias_20d", inp)
        # 决策日在最后 30 根 bar 之后 → 取倒数第 31 根 bar 的因子值
        close_values = original.values
        tail_position = len(keep) - 1
        ma20 = float(np.mean(close_values[tail_position - 19 : tail_position + 1]))
        expected = close_values[tail_position] / ma20 - 1.0
        assert frame[day]["600000.SH"] == pytest.approx(float(expected), rel=1e-12)

    def test_market_shorter_than_stock_intersects(self) -> None:
        """指数序列更短 → 交集对齐(取共同日期),值在交集窗口上可复算。"""
        dates, bars, _ = _universe()
        day = dates[-1]
        short_market = dict(bars["close"])
        original = short_market[MARKET_INDEX_SYMBOL]
        keep = list(range(500, len(original.dates)))  # 只保留后 220 根
        short_market[MARKET_INDEX_SYMBOL] = SymbolSeries(
            dates=tuple(original.dates[i] for i in keep),
            values=original.values[keep],
            available_at=tuple(original.available_at[i] for i in keep),
        )
        inp = _BatchInput({"close": short_market}, decision_dates=(day,))
        frame = _compute("beta_60d", inp)
        market = pd.Series(short_market[MARKET_INDEX_SYMBOL].values,
                           index=short_market[MARKET_INDEX_SYMBOL].dates)
        close = pd.Series(bars["close"]["600000.SH"].values,
                          index=bars["close"]["600000.SH"].dates)
        aligned_close = close.reindex(market.index)  # 交集 = 指数日期
        stock_ret = aligned_close.pct_change()
        market_ret = market.pct_change()
        expected = stock_ret.rolling(60).cov(market_ret) / market_ret.rolling(60).var(ddof=1)
        got = frame[day]["600000.SH"]
        assert got is not None
        assert got == pytest.approx(float(expected.loc[:day].iloc[-1]), rel=1e-9)


def test_sample_rejects_length_mismatch_between_axis_and_values() -> None:
    """对齐轴与值数组长度不一致 fail-fast(契约防回归)。"""
    series = _series(pd.Series([1.0, 2.0, 3.0], index=[date(2024, 1, 2 + i) for i in range(3)]))
    with pytest.raises(ValueError, match="不一致"):
        sample_series_frame(
            {"s": series},
            {"s": np.array([1.0, 2.0])},
            decision_dates=(series.dates[-1],),
        )


def test_predefined_factor_names_include_all_batch1() -> None:
    names = set(predefined_factor_names())
    assert names >= BATCH1_NAMES
    for name in BATCH1_NAMES:
        assert get_predefined_factor(name).name == name
        assert is_registered_predefined_factor(f"p_{name}")
