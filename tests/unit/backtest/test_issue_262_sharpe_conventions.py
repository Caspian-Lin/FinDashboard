"""Sharpe 口径统一与 rf 标注测试(issue #262)。

研究域各 Sharpe 实现的口径映射回归锁定:

===========================  ============  ==========  ==========================
实现                          rf 默认        ddof        输入
===========================  ============  ==========  ==========================
metrics.sharpe_ratio         0.03(引擎)    0(总体)     curve[(date, Decimal)]
metrics.sharpe_ratio_rf0     0.0           1(样本)     curve[(date, Decimal)]
research_run._curve_metrics  0.0           1           EquityPoint 曲线
mean_reversion._compute_..   0.0           1           权益数值序列
futures_tsmom._compute_..    0.03          1           权益数值序列
validation.sharpe_from_ret.  0.03          0           日收益率序列
===========================  ============  ==========  ==========================

全部实现统一委托 ``metrics._sharpe_core``;引擎报告额外序列化 ``sharpe_rf0``
(rf=0 对照口径)与 ``risk_free_annual``(实际 rf),研究报告序列化
``risk_free_annual=0.0``。跨报告同屏比较 Sharpe 的规则:引擎侧取
``sharpe_rf0``、研究侧取 ``sharpe_ratio``(同口径 rf=0/ddof=1/√252)。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from finboard_backtest.futures_tsmom.analysis import _compute_sharpe as tsmom_sharpe
from finboard_backtest.mean_reversion.analysis import _compute_sharpe as mr_sharpe
from finboard_backtest.metrics import (
    DEFAULT_RISK_FREE_ANNUAL,
    sharpe_from_daily_returns,
    sharpe_from_equity_values,
    sharpe_ratio,
    sharpe_ratio_rf0,
)
from finboard_backtest.research_run.contracts import report_from_json
from finboard_backtest.research_run.portfolio_pipeline import _curve_metrics
from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.statistics import sharpe_from_returns

_BASE = date(2024, 1, 1)


def _curve_from_values(values: list[float]) -> list[tuple[date, Decimal]]:
    return [
        (_BASE + timedelta(days=i), Decimal(str(round(v, 10))))
        for i, v in enumerate(values)
    ]


def _equity_values(n: int = 120, drift: float = 0.0005) -> list[float]:
    """带正弦波动的上行权益(全正,相邻日收益率非零)。"""
    values = [100.0]
    for k in range(1, n):
        values.append(values[-1] * (1 + drift + 0.008 * math.sin(k * 2.399)))
    return values


def _returns_of(values: list[float]) -> list[float]:
    return [
        values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0
    ]


class TestDualConventionValues:
    """引擎双口径的精确数值断言(numpy 独立路径参照)。"""

    @pytest.mark.unit
    def test_engine_convention_rf3_ddof0(self) -> None:
        values = _equity_values()
        curve = _curve_from_values(values)
        rets = _returns_of(values)
        expected = float(
            (np.mean(rets) - DEFAULT_RISK_FREE_ANNUAL / 252)
            / np.std(rets, ddof=0)
            * np.sqrt(252)
        )
        assert sharpe_ratio(curve) == pytest.approx(expected)

    @pytest.mark.unit
    def test_rf0_convention_ddof1(self) -> None:
        values = _equity_values()
        curve = _curve_from_values(values)
        rets = _returns_of(values)
        expected = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252))
        assert sharpe_ratio_rf0(curve) == pytest.approx(expected)
        assert sharpe_ratio_rf0(curve) == pytest.approx(
            sharpe_from_equity_values(values, risk_free_annual=0.0, ddof=1)
        )

    @pytest.mark.unit
    def test_run277_low_return_scenario(self) -> None:
        """run 277 误读场景:年化 ~3% 低波动策略,rf=3% 主口径近 0、rf0 明显为正。"""
        # 日漂移 ≈ 3%/252,波动由正弦提供 → rf 口径超额均值 ≈ 0。
        values = _equity_values(n=250, drift=0.03 / 252)
        curve = _curve_from_values(values)
        sr_rf3 = sharpe_ratio(curve)
        sr_rf0 = sharpe_ratio_rf0(curve)
        assert abs(sr_rf3) < 0.1, "rf=3% 口径应把 ~3% 年化策略拖近 0(run 277 误读)"
        assert sr_rf0 > 0.2, "rf=0 口径应保留风险调整收益信号"
        assert sr_rf0 > sr_rf3

    @pytest.mark.unit
    def test_boundary_cases(self) -> None:
        short = [(date(2024, 1, 1), Decimal(100)), (date(2024, 1, 2), Decimal(101))]
        assert sharpe_ratio(short) == 0.0
        assert sharpe_ratio_rf0(short) == 0.0
        flat = [(date(2024, 1, 1) + timedelta(days=i), Decimal(100)) for i in range(5)]
        assert sharpe_ratio(flat) == 0.0
        assert sharpe_ratio_rf0(flat) == 0.0


class TestConventionAlignmentAcrossImplementations:
    """五套实现口径映射回归锁定(统一委托 metrics._sharpe_core 后恒等)。"""

    @pytest.mark.unit
    def test_mean_reversion_equals_rf0(self) -> None:
        values = _equity_values()
        assert mr_sharpe(values) == pytest.approx(sharpe_ratio_rf0(_curve_from_values(values)))

    @pytest.mark.unit
    def test_futures_tsmom_rf3_ddof1(self) -> None:
        values = _equity_values()
        assert tsmom_sharpe(values) == pytest.approx(
            sharpe_from_equity_values(values, risk_free_annual=DEFAULT_RISK_FREE_ANNUAL, ddof=1)
        )

    @pytest.mark.unit
    def test_validation_default_equals_engine_convention(self) -> None:
        values = _equity_values()
        curve = _curve_from_values(values)
        rets = _returns_of(values)
        assert sharpe_from_returns(rets) == pytest.approx(sharpe_ratio(curve))

    @pytest.mark.unit
    def test_validation_pbo_rf0_call(self) -> None:
        values = _equity_values()
        rets = _returns_of(values)
        assert sharpe_from_returns(rets, risk_free_annual=0.0) == pytest.approx(
            sharpe_from_daily_returns(rets, risk_free_annual=0.0, ddof=0)
        )

    @pytest.mark.unit
    def test_research_run_curve_metrics_equals_rf0(self) -> None:
        from finboard_backtest.research_run.contracts import EquityPoint

        values = _equity_values()
        curve = tuple(
            EquityPoint(trade_date=_BASE + timedelta(days=i), equity=Decimal(str(round(v, 10))))
            for i, v in enumerate(values)
        )
        strategy_return, _annualized, sharpe, _mdd = _curve_metrics(curve, Decimal("100"))
        assert strategy_return == pytest.approx(values[-1] / values[0] - 1)
        assert sharpe == pytest.approx(sharpe_ratio_rf0(_curve_from_values(values)))
        assert sharpe == pytest.approx(sharpe_from_equity_values(values))


class TestBacktestResultSharpeAnnotations:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        result = BacktestResult()
        assert result.sharpe_rf0 == 0.0
        assert result.risk_free_annual == pytest.approx(DEFAULT_RISK_FREE_ANNUAL)

    @pytest.mark.unit
    async def test_engine_fills_both_conventions(self) -> None:
        """引擎 run 填充双口径:主口径 rf=3%/ddof=0,sharpe_rf0 与研究口径恒等。"""
        from datetime import UTC, datetime

        from finboard_backtest.config import BacktestConfig
        from finboard_backtest.engine import BacktestEngine
        from finboard_core import Strategy
        from finboard_shared.identifiers import StrategyId
        from finboard_shared.models import Bar, Symbol
        from finboard_shared.types import BarPeriod, Market

        code = "600519.SH"
        symbol = Symbol(code, Market.A_SHARE)

        class NoopStrategy(Strategy):
            @property
            def strategy_id(self) -> StrategyId:
                return StrategyId("noop")

            async def on_market_data(self, event: object, ctx: object) -> None:
                del event, ctx

        def _bar(day: int, close: str) -> Bar:
            c = Decimal(close)
            return Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime(2024, 1, day, tzinfo=UTC),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("1000000"),
                amount=Decimal("0"),
                source="test",
            )

        closes = [(2, "100"), (3, "102"), (4, "105"), (5, "103"), (6, "107")]

        class MemoryProvider:
            async def fetch_bars(self, sym: Symbol, period: object, start: object, end: object, *, adjust: str = "qfq") -> list[Bar]:
                del period, start, end, adjust
                return [_bar(d, c) for d, c in closes]

        engine = BacktestEngine(
            strategy=NoopStrategy(),
            data_provider=MemoryProvider(),
            config=BacktestConfig(
                symbols=[code],
                start=date(2024, 1, 1),
                end=date(2024, 1, 31),
            ),
        )
        result = await engine.run()
        assert result.risk_free_annual == pytest.approx(DEFAULT_RISK_FREE_ANNUAL)
        assert result.sharpe_ratio == pytest.approx(sharpe_ratio(result.equity_curve))
        assert result.sharpe_rf0 == pytest.approx(sharpe_ratio_rf0(result.equity_curve))


class TestResearchReportSharpeAnnotation:
    def _report_payload(self) -> dict[str, object]:
        return {
            "strategy_kind": "multi_factor",
            "strategy_return": 0.05,
            "benchmark_symbol": "000300.SH",
            "benchmark_return": 0.04,
            "excess_return": 0.01,
            "sharpe_ratio": 0.87,
            "max_drawdown": -0.08,
            "final_equity": "105000",
            "final_cash": "5000",
            "commission_paid": "10",
            "tax_paid": "10",
            "slippage_paid": "5",
            "fill_shortfall": "0",
            "constraint_impact": {},
            "decision_count": 3,
            "order_count": 6,
            "fill_count": 6,
        }

    @pytest.mark.unit
    def test_legacy_payload_without_annotation_defaults_to_zero(self) -> None:
        report = report_from_json(self._report_payload())
        assert report.risk_free_annual == 0.0

    @pytest.mark.unit
    def test_annotation_roundtrip(self) -> None:
        payload = self._report_payload()
        payload["risk_free_annual"] = 0.0
        report = report_from_json(payload)
        assert report.risk_free_annual == 0.0
        assert report.sharpe_ratio == pytest.approx(0.87)


class TestSerializationSurfaces:
    @pytest.mark.unit
    def test_rest_metrics_schema_has_both_conventions(self) -> None:
        from finboard_api.schemas import BacktestMetricsOut

        out = BacktestMetricsOut()
        assert out.sharpe_rf0 == 0.0
        assert out.risk_free_annual == pytest.approx(DEFAULT_RISK_FREE_ANNUAL)
        assert "sharpe_rf0" in BacktestMetricsOut.model_fields
        assert "risk_free_annual" in BacktestMetricsOut.model_fields

    @pytest.mark.unit
    def test_grid_metric_fields_include_rf0(self) -> None:
        from finboard_mcp.tools.grid import _METRIC_FIELDS

        assert "sharpe_rf0" in _METRIC_FIELDS
        assert "sharpe_ratio" in _METRIC_FIELDS
