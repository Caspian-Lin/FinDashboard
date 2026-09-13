"""协方差窗口塌缩与目标覆盖缺口修复(2026-09-13 全历史 run 双事故)。

两条同族的生产事故(RR-bff0 / RR-0c2aa,556 期全市场小市值周频 replay)在
早期决策期被 ``hard_constraint_rejected`` 杀死:

1. **窗口塌缩**(RR-0c2aa 决策 6,2015-03-20):universe 选股是
   ``ranking=market_cap bottom + selection_limit=200``,小市值池天然纳入
   次新股;池内只要出现一只「恰好 2 个价点」的新上市标的,全池最短窗口就
   塌缩到 2 → 观测 1 期 → ``_estimate_covariance`` 返回 ``None`` →
   builder「风险贡献硬约束缺少可用协方差矩阵」fail-closed 拒整条 run。
   修复:窗口只在**可估计标的**(≥3 价点)上表决,2 价点标的以零方差/
   零协方差行保留在估计域内(矩阵扩展 + PD clip),具名 warning 可见。

2. **目标覆盖缺口**(RR-bff0 决策 3,2015-02-27):组合阶段的再平衡带会保留
   已掉出当期选股池的持仓(市值排名轮换 → ``included=False``),
   ``price_series`` / 协方差只覆盖选股池 → builder「协方差缺少目标标的:
   ['000815.SZ']」fail-closed 拒整条 run。修复:组合构建前按「信号标的与
   当前持仓」补齐覆盖 —— ``close_series_provider``(close 矩阵切片)可取到
   全部目标标的序列时用同一估计器重建协方差并具名 warning;取不到则保持
   原样(矩阵缺失照旧 fail-closed,语义零变化)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
import structlog

from finboard_backtest.portfolio import (
    PSD_MIN_EIGENVALUE,
    AssetLotInfo,
    CovarianceEstimate,
)
from finboard_backtest.portfolio.builder import _covariance_problem
from finboard_backtest.research_run import (
    NormalizedSignal,
    UniverseCandidate,
)
from finboard_backtest.research_run import (
    signal_engine as signal_engine_module,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _covariance_with_target_coverage,
)
from finboard_backtest.research_run.signal_engine import _estimate_covariance


@pytest.fixture(autouse=True)
def _uncached_signal_engine_logger():
    """隔离 ``setup_logging`` 对 structlog 的全局污染(#465 同款)。"""
    saved_config = structlog.get_config()
    saved_logger = signal_engine_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    signal_engine_module.logger = structlog.get_logger(
        "finboard_backtest.research_run.signal_engine"
    )
    yield
    signal_engine_module.logger = saved_logger
    structlog.configure(**saved_config)


def _prices(n: int, *, seed: int, start: float = 10.0, scale: float = 0.02) -> list[float]:
    rng = np.random.default_rng(seed)
    values: list[float] = [
        float(value)
        for value in start * np.cumprod(1.0 + rng.standard_normal(n) * scale)
    ]
    return values


def _min_eigenvalue(matrix: npt.NDArray[np.float64]) -> float:
    return float(np.linalg.eigvalsh((matrix + matrix.T) / 2.0).min())


class TestWindowCollapse:
    """C1:池内 2 价点标的不再塌缩全池窗口。"""

    def test_two_point_symbol_does_not_collapse_window(self) -> None:
        """1 只 2 价点新上市标的 + 5 只长历史:估计非 None,窗口取长历史组。"""
        price_series: dict[str, list[float]] = {
            f"{index:06d}.SZ": _prices(40, seed=index) for index in range(5)
        }
        price_series["300999.SZ"] = _prices(2, seed=99)  # 新上市:2 个价点

        with structlog.testing.capture_logs() as logs:
            estimate = _estimate_covariance(price_series)

        assert estimate is not None
        # 旧口径对照:全池最短序列 = 2 → n_obs = 1 → None(事故形态)。
        assert min(len(values) for values in price_series.values()) == 2
        assert min(len(values) for values in price_series.values()) - 1 < 2
        # 覆盖完整:信号标的 / 目标标的覆盖校验都按 tickers 判定。
        assert set(estimate.tickers) == set(price_series)
        assert estimate.n_observations == 39
        assert _covariance_problem(estimate, set(price_series)) is None
        events = [
            event
            for event in logs
            if event.get("event") == "research_run.covariance_short_history_symbols"
        ]
        assert len(events) == 1
        assert events[0]["count"] == 1
        assert events[0]["samples"] == ["300999.SZ"]
        assert events[0]["window_points"] == 40

    def test_two_point_symbol_row_is_zero_variance(self) -> None:
        """历史不足标的以零方差/零协方差行并入,矩阵仍正定过校验。"""
        price_series: dict[str, list[float]] = {
            f"{index:06d}.SZ": _prices(30, seed=index) for index in range(4)
        }
        price_series["300999.SZ"] = _prices(2, seed=5)

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        index = list(estimate.tickers).index("300999.SZ")
        row = estimate.matrix[index]
        assert abs(float(row[index])) <= PSD_MIN_EIGENVALUE * 2.0
        assert float(np.abs(np.delete(row, index)).max()) <= PSD_MIN_EIGENVALUE * 2.0
        assert _min_eigenvalue(estimate.matrix) >= PSD_MIN_EIGENVALUE - 1e-15
        assert _covariance_problem(estimate, {"300999.SZ"}) is None

    def test_all_short_history_returns_none(self) -> None:
        """可估计标的不足 2 只:仍返回 None(fail-closed 语义不变)。"""
        price_series = {"000001.SZ": _prices(2, seed=1), "000002.SZ": _prices(2, seed=2)}

        assert _estimate_covariance(price_series) is None

    def test_single_point_symbol_still_excluded_from_estimation(self) -> None:
        """1 价点标的(收益不可得)照旧不进估计域,不影响窗口。"""
        price_series: dict[str, list[float]] = {
            f"{index:06d}.SZ": _prices(30, seed=index) for index in range(3)
        }
        price_series["300999.SZ"] = [10.0]

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert "300999.SZ" not in estimate.tickers
        assert estimate.n_observations == 29


def _item(
    *,
    covariance: CovarianceEstimate | None,
    symbols: tuple[str, ...] = ("A.SH", "B.SH"),
    signal_symbols: tuple[str, ...] | None = None,
    provider: Any = None,
    day: int = 2,
) -> PortfolioDecisionInput:
    signals = signal_symbols or symbols
    decision_at = datetime(2025, 1, day, 15, tzinfo=UTC)
    execution_at = datetime(2025, 1, day + 1, 9, 30, tzinfo=UTC)
    prices = dict.fromkeys(symbols, 10.0)
    return PortfolioDecisionInput(
        business_date=date(2025, 1, day),
        decision_at=decision_at,
        execution_at=execution_at,
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("通过冻结候选池规则",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in symbols
        ),
        features=(),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="fixed",
                factor_snapshot_id=None,
                rationale="测试信号",
            )
            for symbol in signals
        ),
        prices=prices,
        execution_prices=prices,
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in symbols},
        input_artifact_ids=("release-v1",),
        covariance=covariance,
        close_series_provider=provider,
    )


def _estimate(symbols: tuple[str, ...], *, seed: int = 0) -> CovarianceEstimate:
    price_series = {
        symbol: _prices(40, seed=seed + index) for index, symbol in enumerate(symbols)
    }
    estimate = _estimate_covariance(price_series)
    assert estimate is not None
    return estimate


def _diagonal(symbols: tuple[str, ...], *, variance: float = 0.0004) -> CovarianceEstimate:
    """直接构造对角协方差(单标的场景:估计器要求 >=2 标的)。"""
    return CovarianceEstimate(
        matrix=np.diag([variance] * len(symbols)),
        tickers=list(symbols),
        shrinkage=0.0,
        n_observations=252,
    )


class TestTargetCoverageFallback:
    """C2:池内估计未覆盖目标标的(持仓掉出选股池)时按目标集合重建。"""

    def test_full_coverage_returns_original(self) -> None:
        """覆盖完整:原对象原样返回,零重建零 warning。"""
        covariance = _estimate(("A.SH", "B.SH"))
        item = _item(covariance=covariance, provider=lambda codes: {})

        with structlog.testing.capture_logs() as logs:
            result = _covariance_with_target_coverage(
                item, current_weights={"A.SH": 0.4, "B.SH": 0.4}
            )

        assert result is covariance
        assert not [e for e in logs if "covariance_coverage" in str(e.get("event"))]

    def test_missing_holding_triggers_rebuild(self) -> None:
        """持仓掉出选股池 → 用目标集合序列重建(tickers 覆盖目标)。"""
        covariance = _diagonal(("A.SH",))
        series = {
            "A.SH": _prices(40, seed=0),
            "B.SH": _prices(40, seed=3),
            "Z.SH": _prices(40, seed=7),
        }
        item = _item(
            covariance=covariance,
            provider=lambda codes: {c: series[c] for c in codes if c in series},
        )

        with structlog.testing.capture_logs() as logs:
            result = _covariance_with_target_coverage(
                item, current_weights={"Z.SH": 0.02}
            )

        assert result is not None
        assert set(result.tickers) == {"A.SH", "B.SH", "Z.SH"}
        assert _covariance_problem(result, {"A.SH"}) is None
        events = [
            e for e in logs if e.get("event") == "research_run.covariance_coverage_rebuilt"
        ]
        assert len(events) == 1
        # 信号标的 B.SH 与持仓 Z.SH 都未被池内估计覆盖。
        assert events[0]["missing"] == ["B.SH", "Z.SH"]
        assert events[0]["target_symbols"] == 3

    def test_partial_provider_keeps_original(self) -> None:
        """序列不全:保持原样(矩阵缺失照旧 fail-closed,不静默补零)。"""
        covariance = _diagonal(("A.SH",))
        item = _item(
            covariance=covariance,
            provider=lambda codes: {"A.SH": _prices(40, seed=0)},
        )

        with structlog.testing.capture_logs() as logs:
            result = _covariance_with_target_coverage(
                item, current_weights={"Z.SH": 0.02}
            )

        assert result is covariance
        events = [
            e
            for e in logs
            if e.get("event") == "research_run.covariance_coverage_unavailable"
        ]
        assert len(events) == 1
        assert events[0]["missing"] == ["B.SH", "Z.SH"]

    def test_none_covariance_rebuilt_from_targets(self) -> None:
        """池内估计为 None(窗口塌缩消费端):目标集合可估时重建。"""
        series = {"A.SH": _prices(30, seed=1), "B.SH": _prices(30, seed=2)}
        item = _item(covariance=None, provider=lambda codes: series)

        with structlog.testing.capture_logs() as logs:
            result = _covariance_with_target_coverage(item, current_weights={})

        assert result is not None
        assert set(result.tickers) == {"A.SH", "B.SH"}
        assert [
            e for e in logs if e.get("event") == "research_run.covariance_coverage_rebuilt"
        ]

    def test_none_covariance_without_provider_unchanged(self) -> None:
        """无 provider(矩阵未启用 / stub):行为与历史一致(None 透传)。"""
        item = _item(covariance=None, provider=None)

        assert _covariance_with_target_coverage(item, current_weights={}) is None


class TestPipelineCoverageIntegration:
    """E2E:持仓掉出选股池的第二期组合构建不再被目标覆盖校验拒绝。"""

    @staticmethod
    async def _run(manifest_factory, *, provider: Any) -> list[Any]:
        from dataclasses import replace

        manifest = manifest_factory(run_id="RR-coverage", idempotency_key="idem-coverage")
        manifest = replace(
            manifest,
            portfolio_config={
                "overrides": {
                    "max_weight_per_asset": 0.5,
                    # 3 只持仓 x ~1/3 权重 → 阈值 0.40 可行(单资产占比下限 1/3)。
                    "max_risk_contribution": 0.40,
                    # 再平衡带上限 0.5:持仓偏离 0.33 <= 0.5 → 一律保留
                    # (目标 = 持仓与本期信号),复现「掉出选股池的持仓仍是
                    # 目标」形态(#380 带保留语义)。
                    "rebalance_threshold": 0.5,
                }
            },
        )
        pool = ("A.SH", "B.SH", "C.SH")
        first = _item(
            covariance=_estimate(pool), symbols=pool, provider=provider
        )
        second = _item(
            covariance=_diagonal(("A.SH",)),
            symbols=pool,
            signal_symbols=("A.SH",),
            provider=provider,
            day=3,
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross", decision_inputs=(first, second)
        )
        adapter.validate_manifest(manifest)
        return [decision async for decision in adapter.decisions(manifest)]

    async def test_held_symbol_outside_pool_passes_with_provider(
        self, manifest_factory
    ) -> None:
        """提供目标序列 → 重建协方差 → 第二期正常产出(事故形态修复)。"""
        pool = ("A.SH", "B.SH", "C.SH")
        series = {
            symbol: _prices(40, seed=index) for index, symbol in enumerate(pool)
        }
        decisions = await self._run(
            manifest_factory,
            provider=lambda codes: {c: series[c] for c in codes if c in series},
        )

        assert len(decisions) == 2
        second = decisions[1]
        # 掉出选股池的持仓(B/C)经再平衡带保留仍是目标,且覆盖校验通过。
        retained = {
            snapshot.symbol if hasattr(snapshot, "symbol") else str(snapshot)
            for snapshot in second.targets_after_risk
        }
        assert retained <= set(pool)
        assert {"B.SH", "C.SH"} & retained
        risk_outcomes = [
            item
            for item in second.constraints
            if item.constraint == "max_risk_contribution"
        ]
        assert risk_outcomes
        assert risk_outcomes[0].passed

    async def test_held_symbol_outside_pool_still_rejected_without_provider(
        self, manifest_factory
    ) -> None:
        """无 provider 对照:同一形态照旧 fail-closed(证明测试在测修复本身)。"""
        from finboard_backtest.research_run.contracts import (
            ResearchConstraintViolationError,
        )

        with pytest.raises(ResearchConstraintViolationError) as excinfo:
            await self._run(manifest_factory, provider=None)

        assert "协方差缺少目标标的" in str(excinfo.value)
