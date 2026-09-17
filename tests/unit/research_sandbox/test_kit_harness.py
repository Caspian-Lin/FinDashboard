"""finboard-research-kit harness —— 协议 v1 端到端(进程内,无需 Docker)。

覆盖:样例因子产出与内置参照一致(数值容差)、输出契约违规(exit 3 +
error.json)、运行时异常(exit 4)、params 注入(payload 覆盖 manifest)。
容器级加固(断网 / 只读 / 超时 / OOM)见 tests/integration/
test_research_sandbox_docker_e2e.py(FINBOARD_SANDBOX_E2E=1 门控)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_research_kit import __version__
from finboard_research_kit.harness import (
    EXIT_OK,
    EXIT_OUTPUT_CONTRACT,
    EXIT_RUNTIME,
    main,
)

_DECISION_AT = "2024-06-03T07:00:00+00:00"
_SYMBOLS = ["600000.SH", "000001.SZ"]


def _write_bars(path: Path) -> pd.DataFrame:
    rng = np.random.default_rng(42)
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
    pq.write_table(
        pa.Table.from_pandas(bars, preserve_index=False), path
    )
    return bars


def _make_data_dir(root: Path) -> pd.DataFrame:
    root.mkdir(parents=True, exist_ok=True)
    bars = _write_bars(root / "bars.parquet")
    (root / "mount_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
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
        ),
        encoding="utf-8",
    )
    return bars


_MOMENTUM_FACTOR = '''
import numpy as np
import pandas as pd


def compute(ctx):
    scores = {}
    window = int(ctx.params.get("window", 5))
    for symbol in ctx.symbols:
        closes = ctx.bars_for(symbol)["close"].to_numpy()
        if len(closes) < window + 1:
            scores[symbol] = float("nan")
            continue
        scores[symbol] = float(closes[-1] / closes[-1 - window] - 1.0)
    return pd.Series(scores)
'''


def _make_code_dir(
    root: Path,
    factor_src: str,
    entry: str = "factor.compute",
    params: dict[str, Any] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "factor.py").write_text(factor_src, encoding="utf-8")
    manifest = f'[manifest]\nentry = "{entry}"\n'
    if params:
        manifest += "\n[manifest.params]\n" + "\n".join(
            f"{k} = {json.dumps(v)}" for k, v in params.items()
        ) + "\n"
    (root / "manifest.toml").write_text(manifest, encoding="utf-8")


def _reference_momentum(bars: pd.DataFrame, window: int) -> pd.Series:
    out = {}
    for symbol in _SYMBOLS:
        closes = bars[bars["symbol"] == symbol]["close"].to_numpy()
        out[symbol] = closes[-1] / closes[-1 - window] - 1.0
    return pd.Series(out)


class TestHarness:
    def test_sample_factor_matches_reference(self, tmp_path: Path) -> None:
        bars = _make_data_dir(tmp_path / "data")
        _make_code_dir(
            tmp_path / "code", _MOMENTUM_FACTOR, params={"window": 20}
        )
        out = tmp_path / "out"

        code = main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
            ]
        )
        assert code == EXIT_OK

        scores = pd.read_parquet(out / "scores.parquet")
        assert list(scores.columns) == ["symbol", "score"]
        reference = _reference_momentum(bars, window=20)
        for row in scores.itertuples(index=False):
            assert row.score == pytest.approx(
                float(reference[row.symbol]), rel=1e-9, abs=1e-12
            )
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["coverage"] == 1.0
        assert metrics["nan_ratio"] == 0.0
        assert metrics["kit_version"] == __version__
        assert metrics["entry"] == "factor.compute"
        assert metrics["n_symbols_input"] == len(_SYMBOLS)
        assert not (out / "error.json").exists()

    def test_params_json_overrides_manifest(self, tmp_path: Path) -> None:
        bars = _make_data_dir(tmp_path / "data")
        _make_code_dir(
            tmp_path / "code", _MOMENTUM_FACTOR, params={"window": 20}
        )
        # 服务端注入的运行参数覆盖 manifest.params
        (tmp_path / "code" / "params.json").write_text(
            json.dumps({"window": 5}), encoding="utf-8"
        )
        out = tmp_path / "out"
        assert (
            main(
                [
                    "--code-dir", str(tmp_path / "code"),
                    "--data-dir", str(tmp_path / "data"),
                    "--out-dir", str(out),
                ]
            )
            == EXIT_OK
        )
        scores = pd.read_parquet(out / "scores.parquet")
        reference = _reference_momentum(bars, window=5)
        for row in scores.itertuples(index=False):
            assert row.score == pytest.approx(
                float(reference[row.symbol]), rel=1e-9
            )

    def test_output_contract_violation_exit_3(self, tmp_path: Path) -> None:
        _make_data_dir(tmp_path / "data")
        bad = (
                "def compute(ctx):\n"
                "    return {'NOT_A_SYMBOL': 1.0}\n"
        )
        _make_code_dir(tmp_path / "code", bad)
        out = tmp_path / "out"
        assert (
            main(
                [
                    "--code-dir", str(tmp_path / "code"),
                    "--data-dir", str(tmp_path / "data"),
                    "--out-dir", str(out),
                ]
            )
            == EXIT_OUTPUT_CONTRACT
        )
        error = json.loads((out / "error.json").read_text(encoding="utf-8"))
        assert error["error_code"] == "output_contract_violation"
        assert "候选池外" in error["message"]
        assert not (out / "scores.parquet").exists()

    def test_runtime_error_exit_4(self, tmp_path: Path) -> None:
        _make_data_dir(tmp_path / "data")
        boom = "def compute(ctx):\n    raise ValueError('boom')\n"
        _make_code_dir(tmp_path / "code", boom)
        out = tmp_path / "out"
        assert (
            main(
                [
                    "--code-dir", str(tmp_path / "code"),
                    "--data-dir", str(tmp_path / "data"),
                    "--out-dir", str(out),
                ]
            )
            == EXIT_RUNTIME
        )
        error = json.loads((out / "error.json").read_text(encoding="utf-8"))
        assert error["error_code"] == "runtime_error"
        assert "ValueError" in error["message"]
        assert "traceback" in error

    def test_daily_metrics_and_financial_views(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        _make_data_dir(root)
        pq.write_table(
            pa.table(
                {
                    "symbol": ["600000.SH"],
                    "trade_date": pa.array(
                        [pd.Timestamp("2024-06-03").date()], pa.date32()
                    ),
                    "pb": pa.array([1.25], pa.float64()),
                }
            ),
            root / "daily_metrics.parquet",
        )
        factor = (
            "def compute(ctx):\n"
            "    assert ctx.daily_metrics is not None\n"
            "    assert ctx.daily_metrics['pb'].iloc[0] == 1.25\n"
            "    assert ctx.financial_indicators is None\n"
            "    return {s: 0.5 for s in ctx.symbols}\n"
        )
        _make_code_dir(tmp_path / "code", factor)
        out = tmp_path / "out"
        assert (
            main(
                [
                    "--code-dir", str(tmp_path / "code"),
                    "--data-dir", str(root),
                    "--out-dir", str(out),
                ]
            )
            == EXIT_OK
        )
