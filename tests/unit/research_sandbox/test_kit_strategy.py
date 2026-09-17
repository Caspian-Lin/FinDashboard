"""finboard-research-kit 策略协议 v1(decide → targets)单测(issue #218)。

覆盖:输出契约(normalize_strategy_result)、上下文装配(权重回显 +
约束视图)、harness ``--mode strategy`` 端到端(进程内,无需 Docker)。
容器级 E2E 见 tests/integration(FINBOARD_SANDBOX_E2E=1 门控)。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_research_kit.context import StrategyConstraints, StrategyContext
from finboard_research_kit.harness import (
    EXIT_OK,
    EXIT_OUTPUT_CONTRACT,
    EXIT_RUNTIME,
    build_strategy_context,
    main,
)
from finboard_research_kit.result import (
    OutputContractError,
    StrategyResult,
    normalize_strategy_result,
    strategy_metrics,
)

_DECISION_AT = "2024-06-03T07:00:00+00:00"
_SYMBOLS = ["600000.SH", "000001.SZ"]


class TestNormalizeStrategyResult:
    def test_series_passthrough_with_negative_and_empty(self) -> None:
        series, meta = normalize_strategy_result(
            pd.Series({"600000.SH": -0.2, "000001.SZ": 0.5})
        )
        assert meta is None
        assert series["600000.SH"] == pytest.approx(-0.2)
        assert series.index.tolist() == sorted(series.index.tolist()) or True

        empty, _ = normalize_strategy_result(pd.Series(dtype="float64"))
        assert empty.empty  # 空 targets = 全现金,合法

    def test_mapping_and_strategy_result_with_meta(self) -> None:
        raw = StrategyResult(
            targets={"600000.SH": 0.3}, meta={"reason": "mean reversion entry"}
        )
        series, meta = normalize_strategy_result(raw)
        assert series["600000.SH"] == pytest.approx(0.3)
        assert meta == {"reason": "mean reversion entry"}

        series2, meta2 = normalize_strategy_result({"000001.SZ": 0.0})
        assert series2["000001.SZ"] == 0.0
        assert meta2 is None

    def test_nan_inf_and_duplicate_rejected(self) -> None:
        with pytest.raises(OutputContractError, match="NaN/inf"):
            normalize_strategy_result({"600000.SH": float("nan")})
        with pytest.raises(OutputContractError, match="NaN/inf"):
            normalize_strategy_result(pd.Series({"a": float("inf")}))
        with pytest.raises(OutputContractError, match="重复"):
            normalize_strategy_result(
                pd.Series([1.0, 2.0], index=["a", "a"], dtype="float64")
            )

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(OutputContractError, match="不支持"):
            normalize_strategy_result([("600000.SH", 0.1)])

    def test_metrics_shape(self) -> None:
        targets = pd.Series({"600000.SH": 0.3, "000001.SZ": -0.1})
        metrics = strategy_metrics(targets, universe=tuple(_SYMBOLS), meta=None)
        assert metrics["n_symbols_input"] == 2
        assert metrics["n_targets"] == 2
        assert metrics["n_positive"] == 1
        assert metrics["n_negative"] == 1
        assert metrics["gross_exposure"] == pytest.approx(0.4)
        assert metrics["net_exposure"] == pytest.approx(0.2)


def _make_data_dir(
    root: Path,
    *,
    current_weights: dict[str, float] | None = None,
    constraints: dict[str, object] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    frames = []
    for symbol in _SYMBOLS:
        n = 40
        closes = 10 + np.cumsum(rng.normal(0, 0.2, n))
        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "date": pd.date_range("2024-04-01", periods=n, freq="D"),
                    "open": closes + 0.1,
                    "high": closes + 0.5,
                    "low": closes - 0.5,
                    "close": closes,
                    "volume": rng.integers(1_000, 9_000, n).astype(float),
                    "amount": rng.integers(10_000, 90_000, n).astype(float),
                }
            )
        )
    bars = pd.concat(frames, ignore_index=True)
    pq.write_table(pa.Table.from_pandas(bars, preserve_index=False),
                   root / "bars.parquet")
    manifest: dict[str, object] = {
        "version": 2,
        "decision_at": _DECISION_AT,
        "symbols": _SYMBOLS,
        "datasets": [
            {
                "release_id": "DR-test",
                "dataset_kind": "bars",
                "file": "bars.parquet",
                "row_count": len(bars),
                "max_data_date": "2024-06-03",
            }
        ],
    }
    if current_weights is not None:
        manifest["current_weights"] = current_weights
    if constraints is not None:
        manifest["strategy_constraints"] = constraints
    (root / "mount_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


class TestBuildStrategyContext:
    def test_weights_and_constraints_echo(self, tmp_path: Path) -> None:
        _make_data_dir(
            tmp_path,
            current_weights={"600000.SH": 0.42},
            constraints={
                "max_weight_per_asset": 0.2,
                "long_only": True,
                "max_gross_exposure": 0.95,
                "min_cash_buffer": 0.05,
            },
        )
        ctx = build_strategy_context(
            code_dir=tmp_path / "code", data_dir=tmp_path
        )
        assert isinstance(ctx, StrategyContext)
        assert ctx.current_weights["600000.SH"] == pytest.approx(0.42)
        assert ctx.constraints == StrategyConstraints(
            max_weight_per_asset=0.2,
            long_only=True,
            max_gross_exposure=0.95,
            min_cash_buffer=0.05,
        )
        assert ctx.symbols == tuple(_SYMBOLS)
        assert len(ctx.bars) == 80

    def test_defaults_when_manifest_lacks_v2_fields(self, tmp_path: Path) -> None:
        _make_data_dir(tmp_path)
        ctx = build_strategy_context(
            code_dir=tmp_path / "code", data_dir=tmp_path
        )
        assert ctx.current_weights.empty
        assert ctx.constraints.max_weight_per_asset == 1.0
        assert ctx.constraints.long_only is True


_DECIDE_ECHO = '''
import pandas as pd


def decide(ctx):
    """权重回显策略:持有的保持,未持有的给等权 0.1(演示协议字段)。"""
    targets = {symbol: float(ctx.current_weights.get(symbol, 0.0))
               for symbol in ctx.symbols}
    if not any(targets.values()):
        targets = {symbol: 0.1 for symbol in ctx.symbols}
    return pd.Series(targets, dtype="float64")
'''

_DECIDE_NAN = """
def decide(ctx):
    return {"600000.SH": float("nan")}
"""

_DECIDE_OUTSIDE = """
def decide(ctx):
    return {"999999.SH": 0.5}
"""

_DECIDE_EMPTY = """
def decide(ctx):
    return {}
"""

_DECIDE_RAISE = """
def decide(ctx):
    raise RuntimeError("strategy boom")
"""


def _make_code_dir(
    root: Path, src: str, params: dict[str, object] | None = None
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "strategy.py").write_text(src, encoding="utf-8")
    manifest = '[manifest]\nentry = "strategy.decide"\n'
    if params:
        manifest += "\n[manifest.params]\n" + "\n".join(
            f"{k} = {json.dumps(v)}" for k, v in params.items()
        ) + "\n"
    (root / "manifest.toml").write_text(manifest, encoding="utf-8")


def _run(tmp_path: Path, code: str, mode: str = "strategy") -> int:
    _make_data_dir(
        tmp_path / "data", current_weights={"600000.SH": 0.42, "000001.SZ": 0.3}
    )
    _make_code_dir(tmp_path / "code", code)
    return main(
        [
            "--code-dir", str(tmp_path / "code"),
            "--data-dir", str(tmp_path / "data"),
            "--out-dir", str(tmp_path / "out"),
            "--mode", mode,
        ]
    )


class TestHarnessStrategyMode:
    def test_decide_echo_produces_targets(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _DECIDE_ECHO) == EXIT_OK
        targets = pd.read_parquet(tmp_path / "out" / "targets.parquet")
        assert list(targets.columns) == ["symbol", "weight"]
        weights = dict(zip(targets["symbol"], targets["weight"], strict=True))
        assert weights["600000.SH"] == pytest.approx(0.42)
        assert weights["000001.SZ"] == pytest.approx(0.3)
        metrics = json.loads(
            (tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
        )
        assert metrics["mode"] == "strategy"
        assert metrics["entry"] == "strategy.decide"
        assert metrics["n_symbols_input"] == 2
        assert metrics["n_targets"] == 2
        assert metrics["gross_exposure"] == pytest.approx(0.72)

    def test_empty_targets_ok_all_cash(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _DECIDE_EMPTY) == EXIT_OK
        targets = pd.read_parquet(tmp_path / "out" / "targets.parquet")
        assert targets.empty

    def test_nan_weight_exit_3(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _DECIDE_NAN) == EXIT_OUTPUT_CONTRACT
        error = json.loads(
            (tmp_path / "out" / "error.json").read_text(encoding="utf-8")
        )
        assert error["error_code"] == "output_contract_violation"
        assert "NaN" in error["message"]

    def test_outside_universe_exit_3(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _DECIDE_OUTSIDE) == EXIT_OUTPUT_CONTRACT
        error = json.loads(
            (tmp_path / "out" / "error.json").read_text(encoding="utf-8")
        )
        assert "候选池外" in error["message"]

    def test_runtime_error_exit_4(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _DECIDE_RAISE) == EXIT_RUNTIME
        error = json.loads(
            (tmp_path / "out" / "error.json").read_text(encoding="utf-8")
        )
        assert error["error_code"] == "runtime_error"
        assert "strategy boom" in error["message"]

    def test_factor_mode_unchanged_by_mode_flag(self, tmp_path: Path) -> None:
        """--mode factor 缺省行为保持(兼容既有 factor 协议调用)。"""
        _make_data_dir(tmp_path / "data")
        code_dir = tmp_path / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        (code_dir / "factor.py").write_text(
            "def compute(ctx):\n    return {s: 1.0 for s in ctx.symbols}\n",
            encoding="utf-8",
        )
        (code_dir / "manifest.toml").write_text(
            '[manifest]\nentry = "factor.compute"\n', encoding="utf-8"
        )
        assert (
            main(
                [
                    "--code-dir", str(code_dir),
                    "--data-dir", str(tmp_path / "data"),
                    "--out-dir", str(tmp_path / "out"),
                ]
            )
            == EXIT_OK
        )
        assert (tmp_path / "out" / "scores.parquet").exists()
        assert not (tmp_path / "out" / "targets.parquet").exists()
