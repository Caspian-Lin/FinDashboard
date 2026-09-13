"""issue #465:research_run 协方差估计统一走 Ledoit-Wolf + PD 修复路径。

RR-bfce655159c976931cca8c10 事故(556 期全市场小市值 weekly):加载 1h43m
后第 1 期组合构建即被拒 ``hard_constraint_rejected``——「协方差矩阵奇异或
非正定: min_eigenvalue=-3.741e-17」。-3.7e-17 是浮点零,结构性秩亏而非坏
数据,三层根因:

1. signal_engine 的 ``_estimate_covariance`` 是手写裸 ``np.cov`` 样本协方差
   (无收缩、无 PD 修复),回测选股路径的
   ``portfolio.covariance.estimate_covariance``(Ledoit-Wolf + 特征值 clip)
   从未覆盖 research_run;
2. 窗口取最短序列:2015 年初池子含大量次新股,最短序列只有几十根 → T≪N,
   样本协方差秩 ≤ T-1,min 特征值 ≈ 0⁻ 是必然;
3. builder 的 PSD 校验无条件 fail-closed,Σ 只用于风险报告也照样杀 run。

本组测试锁定:T≪N 估计产物过 PSD 门、退化(零方差)序列 fail-visible 且
保留在估计域、次新股短序列不被剔除(对齐陷阱回归)、零价跳点 ragged 修复、
容差与 clip 下限对齐(修复矩阵恒过校验)、真坏矩阵照旧被拦。
纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
import structlog

from finboard_backtest.portfolio import PSD_MIN_EIGENVALUE, CovarianceEstimate
from finboard_backtest.portfolio.builder import _covariance_problem
from finboard_backtest.research_run import signal_engine as signal_engine_module
from finboard_backtest.research_run.signal_engine import _estimate_covariance


@pytest.fixture(autouse=True)
def _uncached_signal_engine_logger():
    """隔离 ``setup_logging`` 对 structlog 的全局污染(#452 同款)。

    CI 从仓库根跑全量,任何先行的测试调用 ``setup_logging`` 后 signal_engine
    的模块 logger 已缓存,``capture_logs`` 永远抓空。测试期重置为不缓存并
    重建模块 logger,结束恢复。
    """
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


def _random_walk_prices(
    n_prices: int,
    *,
    seed: int,
    start: float = 10.0,
    scale: float = 0.02,
) -> npt.NDArray[np.float64]:
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1.0 + rng.standard_normal(n_prices) * scale)


class TestTMuchLessThanN:
    def test_estimate_passes_psd_gate(self) -> None:
        """300 标的 x 20 观测(T≪N):估计产物 PD 且过 builder PSD 门。

        旧实现(裸 np.cov 样本协方差)同输入结构性秩亏:秩 ≤ T-1 = 19,
        min 特征值 ≈ 0⁻(事故形态 -3.7e-17),fail-closed 必然拒绝 —— 下方
        对照断言锁定该前提仍然成立。
        """
        n_symbols, n_prices = 300, 21
        price_series = {
            f"{index:06d}.SZ": _random_walk_prices(n_prices, seed=index).tolist()
            for index in range(n_symbols)
        }

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert set(estimate.tickers) == set(price_series)
        assert estimate.n_observations == n_prices - 1
        assert estimate.method == "ledoit_wolf"
        matrix = estimate.matrix
        assert matrix.shape == (n_symbols, n_symbols)
        min_eigenvalue = float(np.linalg.eigvalsh((matrix + matrix.T) / 2.0).min())
        # PD 修复下限:clip 到 PSD_MIN_EIGENVALUE(重构误差远小于 1e-15)。
        assert min_eigenvalue >= PSD_MIN_EIGENVALUE - 1e-15
        assert _covariance_problem(estimate, set(price_series)) is None

        # 对照:同输入的旧实现样本协方差 min 特征值 <= 0(结构性秩亏),
        # 旧口径下 _covariance_problem 必然返回非 None。
        returns = np.stack(
            [
                np.asarray(values[1:], dtype=np.float64)
                / np.asarray(values[:-1], dtype=np.float64)
                - 1.0
                for values in price_series.values()
            ]
        )
        sample_cov = np.cov(returns)
        old_min_eigenvalue = float(
            np.linalg.eigvalsh((sample_cov + sample_cov.T) / 2.0).min()
        )
        assert old_min_eigenvalue <= 0.0

    def test_shrinkage_active_for_short_window(self) -> None:
        """T≪N 时 Ledoit-Wolf 收缩生效(不再是 shrinkage=0.0 的纯样本)。"""
        price_series = {
            f"{index:06d}.SZ": _random_walk_prices(21, seed=index).tolist()
            for index in range(50)
        }

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert estimate.shrinkage > 0.0


class TestDegenerateSymbols:
    def test_constant_series_warns_and_stays_pd(self) -> None:
        """零方差(全常数)序列:具名 warning 计数 + 样例,标的保留且矩阵 PD。"""
        price_series = {
            "000001.SZ": _random_walk_prices(30, seed=1).tolist(),
            "000002.SZ": _random_walk_prices(30, seed=2).tolist(),
            "600000.SH": [10.0] * 30,
        }

        with structlog.testing.capture_logs() as logs:
            estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert "600000.SH" in estimate.tickers
        min_eigenvalue = float(
            np.linalg.eigvalsh((estimate.matrix + estimate.matrix.T) / 2.0).min()
        )
        assert min_eigenvalue >= PSD_MIN_EIGENVALUE - 1e-15
        assert _covariance_problem(estimate, set(price_series)) is None

        events = [
            event
            for event in logs
            if event.get("event") == "research_run.covariance_degenerate_symbols"
        ]
        assert len(events) == 1
        event = events[0]
        assert event["log_level"] == "warning"
        assert event["count"] == 1
        assert event["samples"] == ["600000.SH"]

    def test_no_warning_for_healthy_series(self) -> None:
        """正常序列不打退化 warning。"""
        price_series = {
            "000001.SZ": _random_walk_prices(30, seed=3).tolist(),
            "000002.SZ": _random_walk_prices(30, seed=4).tolist(),
        }

        with structlog.testing.capture_logs() as logs:
            estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert not [
            event
            for event in logs
            if event.get("event") == "research_run.covariance_degenerate_symbols"
        ]


class TestAlignmentCoverage:
    def test_recently_listed_short_series_kept(self) -> None:
        """次新股短序列仍在产物 tickers 内(对齐陷阱回归)。

        ``pairwise_aligned_returns`` 默认 ``min_overlap=30`` 会剔除观测不足
        的标的,而 builder 对「协方差缺少信号标的」同样 fail-closed —— 直接
        透传会把秩亏拒绝变成缺标的拒绝。预对齐 + ``min_observations=2``
        后全部保留。
        """
        price_series: dict[str, list[float]] = {
            f"{index:06d}.SZ": _random_walk_prices(60, seed=index).tolist()
            for index in range(5)
        }
        price_series["300999.SZ"] = _random_walk_prices(10, seed=99).tolist()

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert set(estimate.tickers) == set(price_series)
        assert "300999.SZ" in estimate.tickers
        # 全池最短公共窗口语义保持:10 个价点 → 9 期收益。
        assert estimate.n_observations == 9
        assert _covariance_problem(estimate, set(price_series)) is None

    def test_zero_price_jumps_equal_length(self) -> None:
        """价格含 0 值跳点:不炸、全序列等长、矩阵有限且过 PSD 门。

        旧实现按 ``values[index-1] != 0`` 跳过零前价跳点,各标的 returns
        长度不齐(ragged → ``np.asarray`` 出 object 数组)。
        """
        rng = np.random.default_rng(7)
        jagged = _random_walk_prices(40, seed=11)
        jagged[17] = 0.0
        price_series: dict[str, list[float]] = {
            f"{index:06d}.SZ": (10.0 * np.cumprod(1.0 + rng.standard_normal(40) * 0.02)).tolist()
            for index in range(5)
        }
        price_series["000004.SZ"] = jagged.tolist()

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert set(estimate.tickers) == set(price_series)
        assert estimate.n_observations == 39
        assert np.isfinite(estimate.matrix).all()
        assert _covariance_problem(estimate, set(price_series)) is None


class TestInsufficientInputs:
    def test_fewer_than_two_symbols_returns_none(self) -> None:
        price_series = {"000001.SZ": _random_walk_prices(30, seed=5).tolist()}
        assert _estimate_covariance(price_series) is None

    def test_fewer_than_two_observations_returns_none(self) -> None:
        # 全池最长序列只有 2 个价点 → 公共窗口 1 期收益,不足估计。
        price_series = {
            "000001.SZ": [10.0, 10.5],
            "000002.SZ": [20.0, 20.3],
        }
        assert _estimate_covariance(price_series) is None

    def test_single_price_symbol_excluded_from_domain(self) -> None:
        """只有 1 个价点的标的不进估计域(<2 价点),其余照常估计。"""
        price_series = {
            "000001.SZ": _random_walk_prices(30, seed=6).tolist(),
            "000002.SZ": _random_walk_prices(30, seed=7).tolist(),
            "688001.SH": [9.0],
        }

        estimate = _estimate_covariance(price_series)

        assert estimate is not None
        assert set(estimate.tickers) == {"000001.SZ", "000002.SZ"}


class TestCovarianceProblemGate:
    @staticmethod
    def _estimate(matrix: npt.NDArray[np.float64]) -> CovarianceEstimate:
        return CovarianceEstimate(
            matrix=matrix,
            tickers=["A", "B"],
            shrinkage=0.0,
            n_observations=10,
        )

    def test_indefinite_matrix_rejected(self) -> None:
        matrix = np.array([[1.0, 0.0], [0.0, -1.0]])
        problem = _covariance_problem(self._estimate(matrix), {"A", "B"})
        assert problem is not None
        assert "奇异或非正定" in problem

    def test_nan_matrix_rejected(self) -> None:
        matrix = np.array([[1.0, float("nan")], [float("nan"), 1.0]])
        problem = _covariance_problem(self._estimate(matrix), {"A", "B"})
        assert problem == "协方差矩阵包含非有限值"

    def test_asymmetric_matrix_rejected(self) -> None:
        matrix = np.array([[1.0, 0.5], [0.3, 1.0]])
        problem = _covariance_problem(self._estimate(matrix), {"A", "B"})
        assert problem == "协方差矩阵不对称"

    def test_missing_symbol_rejected(self) -> None:
        matrix = np.eye(2)
        problem = _covariance_problem(self._estimate(matrix), {"A", "B", "C"})
        assert problem is not None
        assert "协方差缺少信号标的" in problem

    def test_repaired_min_eigenvalue_passes_aligned_tolerance(self) -> None:
        """容差对齐:修复矩阵(min_eig = clip 下限)在大尺度下恒过校验。

        旧容差公式 ``eps * max(1, ‖A‖∞) * N`` 在大尺度矩阵上会超过 clip
        下限(diag(2000, 2000, 1e-12):旧容差 ≈ 1.33e-12 ≥ 1e-12,修复过的
        矩阵也会被拒 ——「修了也会被拒」边界);新口径
        ``min(旧公式, PSD_MIN_EIGENVALUE / 2)`` = 5e-13,恒过。
        """
        matrix = np.diag([2000.0, 2000.0, PSD_MIN_EIGENVALUE])
        old_tolerance = (
            np.finfo(np.float64).eps * float(np.abs(matrix).max()) * 3
        )
        # 前提:该用例确实落在旧口径会拒绝的区间。
        assert old_tolerance >= PSD_MIN_EIGENVALUE
        estimate = CovarianceEstimate(
            matrix=matrix,
            tickers=["A", "B", "C"],
            shrinkage=0.0,
            n_observations=30,
        )
        assert _covariance_problem(estimate, {"A", "B", "C"}) is None

    def test_zero_matrix_still_rejected(self) -> None:
        """全零矩阵(min_eig = 0 ≤ 容差)照旧拒绝,对齐不放松真坏矩阵防线。"""
        matrix = np.zeros((2, 2))
        problem = _covariance_problem(self._estimate(matrix), {"A", "B"})
        assert problem is not None
        assert "奇异或非正定" in problem


class TestEstimatorDelegation:
    def test_matches_engine_path_estimator(self) -> None:
        """research_run 估计产物与直接调用 portfolio 引擎路径估计器一致。

        同一批 returns 喂 ``portfolio.covariance.estimate_covariance``
        (min_observations=2)应与 ``_estimate_covariance`` 产物逐值一致
        (统一估计器,非另一份实现)。
        """
        from finboard_backtest.portfolio import estimate_covariance

        price_series = {
            "000001.SZ": _random_walk_prices(40, seed=21).tolist(),
            "000002.SZ": _random_walk_prices(40, seed=22).tolist(),
            "000003.SZ": _random_walk_prices(40, seed=23).tolist(),
        }
        research_estimate = _estimate_covariance(price_series)

        returns_by_ticker = {}
        for symbol, values in price_series.items():
            arr = np.asarray(values, dtype=np.float64)
            returns_by_ticker[symbol] = arr[1:] / arr[:-1] - 1.0
        engine_estimate = estimate_covariance(returns_by_ticker, min_observations=2)

        assert research_estimate is not None
        assert research_estimate.tickers == engine_estimate.tickers
        assert research_estimate.shrinkage == pytest.approx(
            engine_estimate.shrinkage
        )
        assert np.array_equal(research_estimate.matrix, engine_estimate.matrix)
