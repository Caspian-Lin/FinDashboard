"""finboard-research-kit 协议 v2 —— compute_series 区间执行(issue #359)。

进程内覆盖(无需 Docker):

* harness ``--mode factor_series`` 端到端:``factor.compute_series`` 被
  正确解析调用、canonical ``factor_series.json`` 结构钉死(protocol_version/
  code_artifact/code_commit/kind/release_id/dataset_release_ids/params/
  window_start/window_end/dates/values)、metrics 质量指标;
* ``bars_view(as_of)`` / ``dataset_view(kind, as_of)`` PIT 过滤边界:
  available_at > as_of 日终的数据不可见(挂载清单 v3 的窗口全量数据里
  只能看到前缀);
* v1 回退:manifest 声明 ``factor.compute`` 时逐日构造单日
  FactorContext 重放,与 v1 单日参照逐值一致(双轨完整保留);
* 输出契约:日期集不一致 / 候选池外 symbol / 非法返回形态 → exit 3。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_research_kit.context import (
    FactorSeriesContext,
    end_of_day,
)
from finboard_research_kit.harness import (
    EXIT_OK,
    EXIT_OUTPUT_CONTRACT,
    EXIT_RUNTIME,
    main,
)
from finboard_research_kit.result import (
    FactorSeries,
    OutputContractError,
    normalize_series_result,
    series_metrics,
)

_SYMBOLS = ["600000.SH", "000001.SZ"]
_DATES = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
_DECISION_AT = "2024-06-05T07:00:00+00:00"


def _write_window_data(root: Path, *, with_available_at: bool = True) -> pd.DataFrame:
    """窗口挂载 v3 数据面:两标的 x 5 个业务日的 bars。

    ``available_at`` = 业务日 15:30 UTC(模拟 D1 收盘后可见)。
    """
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    days = pd.date_range("2024-06-01", periods=5, freq="D")
    frames = []
    for symbol in _SYMBOLS:
        closes = 10 + np.cumsum(rng.normal(0, 0.2, len(days)))
        frame = pd.DataFrame(
            {
                "symbol": symbol,
                "date": days,
                "open": closes + 0.1,
                "high": closes + 0.5,
                "low": closes - 0.5,
                "close": closes,
                "volume": np.linspace(1_000, 5_000, len(days)),
                "amount": np.linspace(10_000, 50_000, len(days)),
            }
        )
        if with_available_at:
            frame["available_at"] = [
                d.tz_localize(UTC) + pd.Timedelta(hours=15, minutes=30)
                for d in days
            ]
        frames.append(frame)
    bars = pd.concat(frames, ignore_index=True)
    pq.write_table(pa.Table.from_pandas(bars, preserve_index=False), root / "bars.parquet")
    return bars


def _write_window_manifest(
    root: Path,
    *,
    entry_hint: str = "factor",
    code_artifact: str = "mom_series",
    code_commit: str = "a" * 40,
    release_id: str = "DR-bars",
) -> None:
    manifest = {
        "version": 3,
        "window": {
            "window_start": "2024-06-03",
            "window_end": "2024-06-05",
            "dates": [d.isoformat() for d in _DATES],
        },
        "symbols": _SYMBOLS,
        "code_artifact": code_artifact,
        "code_commit": code_commit,
        "release_id": release_id,
        "dataset_release_ids": [release_id],
        "datasets": [],
    }
    (root / "mount_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_SERIES_FACTOR = '''
import numpy as np
import pandas as pd


def compute_series(ctx):
    values = {}
    for day in ctx.dates:
        view = ctx.bars_view(day)
        cross = {}
        for symbol in ctx.symbols:
            closes = view.for_symbol(symbol)["close"].to_numpy()
            window = int(ctx.params.get("window", 2))
            if len(closes) < window + 1:
                cross[symbol] = None
                continue
            cross[symbol] = float(closes[-1] / closes[-1 - window] - 1.0)
        values[day] = cross
    return pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "score": score}
            for day, cross in values.items()
            for symbol, score in cross.items()
        ]
    )
'''


def _make_code_dir(
    root: Path,
    factor_src: str,
    entry: str = "factor.compute_series",
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


class TestFactorSeriesHarness:
    def test_compute_series_end_to_end(self, tmp_path: Path) -> None:
        bars = _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        _make_code_dir(tmp_path / "code", _SERIES_FACTOR, params={"window": 2})
        out = tmp_path / "out"

        code = main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        )
        assert code == EXIT_OK
        payload = json.loads(
            (out / "factor_series.json").read_text(encoding="utf-8")
        )
        # canonical 结构钉死(issue #359)
        assert payload["protocol_version"] == 2
        assert payload["code_artifact"] == "mom_series"
        assert payload["code_commit"] == "a" * 40
        assert payload["kind"] == "factor"
        assert payload["release_id"] == "DR-bars"
        assert payload["dataset_release_ids"] == ["DR-bars"]
        assert payload["params"] == {"window": 2}
        assert payload["window_start"] == "2024-06-03"
        assert payload["window_end"] == "2024-06-05"
        assert payload["dates"] == [d.isoformat() for d in _DATES]
        assert set(payload["values"]) == {d.isoformat() for d in _DATES}
        # 逐日 PIT:6/3 的截面只依赖 available_at <= 6/3 日终的数据
        for day in _DATES:
            for symbol in _SYMBOLS:
                closes = (
                    bars[bars["symbol"] == symbol]
                    .set_index("date")["close"]
                    .loc[:pd.Timestamp(day)]
                    .to_numpy()
                )
                expected = closes[-1] / closes[-3] - 1.0
                got = payload["values"][day.isoformat()][symbol]
                assert got == pytest.approx(expected, rel=1e-9)
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["mode"] == "factor_series"
        assert metrics["protocol_version"] == 2
        assert metrics["coverage"] == 1.0
        assert metrics["nan_ratio"] == 0.0
        assert metrics["entry"] == "factor.compute_series"
        assert metrics["entry_used"] == "factor.compute_series"
        assert not (out / "error.json").exists()

    def test_pit_view_boundary_at_day_end(self, tmp_path: Path) -> None:
        """bars_view(as_of) 看不到 available_at > as_of 日终的数据。"""
        _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        probe = (
            "def compute_series(ctx):\n"
            "    return {d: {s: len(ctx.bars_view(d).frame) for s in ctx.symbols}\n"
            "            for d in ctx.dates}\n"
        )
        _make_code_dir(tmp_path / "code", probe)
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OK
        payload = json.loads(
            (out / "factor_series.json").read_text(encoding="utf-8")
        )
        # 数据面 5 个业务日(6/1..6/5),available_at = 当日 15:30;
        # 决策日 6/3 的可见前缀 = 6/1、6/2、6/3 三日 x 两标的。
        assert payload["values"]["2024-06-03"]["600000.SH"] == 6
        assert payload["values"]["2024-06-04"]["600000.SH"] == 8
        assert payload["values"]["2024-06-05"]["600000.SH"] == 10

    def test_dates_mismatch_exit_3(self, tmp_path: Path) -> None:
        _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        bad = (
            "def compute_series(ctx):\n"
            "    return {d: {s: 1.0 for s in ctx.symbols}\n"
            "            for d in ctx.dates[:-1]}\n"  # 缺最后一天
        )
        _make_code_dir(tmp_path / "code", bad)
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OUTPUT_CONTRACT
        error = json.loads((out / "error.json").read_text(encoding="utf-8"))
        assert error["error_code"] == "output_contract_violation"
        assert "日期集" in error["message"]
        assert not (out / "factor_series.json").exists()

    def test_outside_universe_exit_3(self, tmp_path: Path) -> None:
        _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        bad = (
            "def compute_series(ctx):\n"
            "    return {d: {'600000.SH': 1.0, '000001.SZ': 1.0,\n"
            "                '999999.SZ': 2.0} for d in ctx.dates}\n"
        )
        _make_code_dir(tmp_path / "code", bad)
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OUTPUT_CONTRACT

    def test_runtime_error_exit_4(self, tmp_path: Path) -> None:
        _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        _make_code_dir(
            tmp_path / "code",
            "def compute_series(ctx):\n    raise ValueError('boom')\n",
        )
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_RUNTIME
        error = json.loads((out / "error.json").read_text(encoding="utf-8"))
        assert error["error_code"] == "runtime_error"

    def test_manifest_v2_rejected_in_series_mode(self, tmp_path: Path) -> None:
        """v3 挂载清单强制:旧版清单在 factor_series 模式下拒绝。"""
        _write_window_data(tmp_path / "data")
        manifest = {
            "version": 2,
            "decision_at": _DECISION_AT,
            "symbols": _SYMBOLS,
            "datasets": [],
        }
        (tmp_path / "data" / "mount_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        _make_code_dir(tmp_path / "code", _SERIES_FACTOR)
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OUTPUT_CONTRACT
        error = json.loads((out / "error.json").read_text(encoding="utf-8"))
        assert "v3" in error["message"]


class TestV1Fallback:
    def test_compute_fallback_per_day_replay(self, tmp_path: Path) -> None:
        """v1 双轨:manifest 声明 factor.compute 时逐日重放,与 v1 参照一致。"""
        bars = _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        v1_factor = (
            "def compute(ctx):\n"
            "    scores = {}\n"
            "    for symbol in ctx.symbols:\n"
            "        closes = ctx.bars_for(symbol)['close'].to_numpy()\n"
            "        if len(closes) < 3:\n"
            "            scores[symbol] = float('nan')\n"
            "        else:\n"
            "            scores[symbol] = float(closes[-1] / closes[-3] - 1.0)\n"
            "    return scores\n"
        )
        _make_code_dir(tmp_path / "code", v1_factor, entry="factor.compute")
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OK
        payload = json.loads(
            (out / "factor_series.json").read_text(encoding="utf-8")
        )
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        assert "v1 逐日回退" in metrics["entry_used"]
        for day in _DATES:
            for symbol in _SYMBOLS:
                closes = (
                    bars[bars["symbol"] == symbol]
                    .set_index("date")["close"]
                    .loc[:pd.Timestamp(day)]
                    .to_numpy()
                )
                expected = closes[-1] / closes[-3] - 1.0
                got = payload["values"][day.isoformat()][symbol]
                assert got == pytest.approx(expected, rel=1e-9)

    def test_unsupported_entry_rejected(self, tmp_path: Path) -> None:
        _write_window_data(tmp_path / "data")
        _write_window_manifest(tmp_path / "data")
        _make_code_dir(
            tmp_path / "code",
            "def decide(ctx):\n    return 1.0\n",
            entry="factor.decide",
        )
        out = tmp_path / "out"
        assert main(
            [
                "--code-dir", str(tmp_path / "code"),
                "--data-dir", str(tmp_path / "data"),
                "--out-dir", str(out),
                "--mode", "factor_series",
            ]
        ) == EXIT_OUTPUT_CONTRACT


class TestPitViews:
    """FactorSeriesContext 访问器的 PIT 过滤边界(纯单元,不走 harness)。"""

    def _ctx(self, with_available_at: bool = True) -> FactorSeriesContext:
        days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
        rows = []
        for i, day in enumerate(days):
            for symbol in _SYMBOLS:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": pd.Timestamp(day),
                        "close": 10.0 + i,
                        "available_at": datetime(
                            day.year, day.month, day.day, 15, 30, tzinfo=UTC
                        )
                        if with_available_at
                        else None,
                    }
                )
        bars = pd.DataFrame(rows)
        if not with_available_at:
            bars = bars.drop(columns=["available_at"])
        daily = pd.DataFrame(
            [
                {
                    "symbol": _SYMBOLS[0],
                    "trade_date": pd.Timestamp(day),
                    "pb": 1.0 + i,
                    "available_at": datetime(
                        day.year, day.month, day.day, 17, 0, tzinfo=UTC
                    ),
                }
                for i, day in enumerate(days)
            ]
        )
        return FactorSeriesContext(
            dates=tuple(days), symbols=tuple(_SYMBOLS), bars=bars, daily_metrics=daily
        )

    def test_bars_view_excludes_future_rows(self) -> None:
        ctx = self._ctx()
        view = ctx.bars_view(date(2024, 6, 4))
        assert view.as_of == date(2024, 6, 4)
        dates = sorted(view.frame["date"].dt.date.unique())
        assert dates == [date(2024, 6, 3), date(2024, 6, 4)]
        # 单标的子集按日期升序
        subset = view.for_symbol("600000.SH")
        assert list(subset["close"]) == [10.0, 11.0]

    def test_bars_view_day_end_boundary_inclusive(self) -> None:
        """available_at 恰为 as_of 当日(15:30)的行可见(日终边界含)。"""
        ctx = self._ctx()
        view = ctx.bars_view(date(2024, 6, 3))
        assert len(view.frame) == 2

    def test_dataset_view_filters_and_unknown_kind(self) -> None:
        ctx = self._ctx()
        view = ctx.dataset_view("daily_metrics", date(2024, 6, 4))
        assert view.frame is not None
        assert len(view.frame) == 2
        assert ctx.dataset_view("financial_indicators", date(2024, 6, 4)).frame is None
        with pytest.raises(ValueError, match="未知数据集"):
            ctx.dataset_view("convertible_metrics", date(2024, 6, 4))

    def test_fallback_by_trade_date_without_available_at(self) -> None:
        """无 available_at 列时按业务日期回退(D1 语义等值)。"""
        ctx = self._ctx(with_available_at=False)
        view = ctx.bars_view(date(2024, 6, 4))
        assert len(view.frame) == 4

    def test_end_of_day_is_utc(self) -> None:
        ceiling = end_of_day(date(2024, 6, 4))
        assert ceiling.tzinfo is UTC
        assert ceiling.hour == 23
        assert ceiling.minute == 59


class TestNormalizeSeriesResult:
    _DATES = (date(2024, 6, 3), date(2024, 6, 4))

    def test_mapping_input_pads_universe(self) -> None:
        raw = {
            date(2024, 6, 3): {"600000.SH": 1.0},
            date(2024, 6, 4): {"600000.SH": 2.0, "000001.SZ": None},
        }
        series = normalize_series_result(
            raw, expected_dates=self._DATES, universe=("600000.SH", "000001.SZ")
        )
        assert series.dates == self._DATES
        assert series.values[date(2024, 6, 3)]["000001.SZ"] is None
        assert series.values[date(2024, 6, 4)]["000001.SZ"] is None
        assert series.values[date(2024, 6, 4)]["600000.SH"] == 2.0

    def test_factor_series_passthrough_and_date_keys(self) -> None:
        # 字符串日期键在运行时合法(normalize 做宽松收敛;agent 代码无类型约束)
        raw: dict[object, object] = {
            "2024-06-03": {"600000.SH": 1.0},
            "2024-06-04": {"600000.SH": float("nan"), "000001.SZ": 3.0},
        }
        series = normalize_series_result(
            raw, expected_dates=self._DATES, universe=("600000.SH", "000001.SZ")
        )
        assert series.dates == self._DATES
        assert series.values[date(2024, 6, 3)]["000001.SZ"] is None
        assert series.values[date(2024, 6, 4)]["600000.SH"] != series.values[
            date(2024, 6, 4)
        ]["600000.SH"]  # NaN 保留(质量门口径),不抛契约错

    def test_factor_series_wrapper_passthrough(self) -> None:
        raw = FactorSeries(
            dates=self._DATES,
            values={
                date(2024, 6, 3): {"600000.SH": 1.0},
                date(2024, 6, 4): {"600000.SH": float("nan"), "000001.SZ": 3.0},
            },
        )
        series = normalize_series_result(
            raw, expected_dates=self._DATES, universe=("600000.SH", "000001.SZ")
        )
        assert series.values[date(2024, 6, 3)]["600000.SH"] == 1.0
        assert series.values[date(2024, 6, 4)]["000001.SZ"] == 3.0

    def test_rejects_missing_and_extra_dates(self) -> None:
        with pytest.raises(OutputContractError, match="日期集"):
            normalize_series_result(
                {date(2024, 6, 3): {"600000.SH": 1.0}},
                expected_dates=self._DATES,
                universe=("600000.SH",),
            )
        with pytest.raises(OutputContractError, match="日期集"):
            normalize_series_result(
                {
                    date(2024, 6, 3): {"600000.SH": 1.0},
                    date(2024, 6, 4): {"600000.SH": 1.0},
                    date(2024, 6, 5): {"600000.SH": 1.0},
                },
                expected_dates=self._DATES,
                universe=("600000.SH",),
            )

    def test_rejects_outside_symbol_and_bad_type(self) -> None:
        with pytest.raises(OutputContractError, match="候选池外"):
            normalize_series_result(
                {d: {"BAD": 1.0} for d in self._DATES},
                expected_dates=self._DATES,
                universe=("600000.SH",),
            )
        with pytest.raises(OutputContractError, match="非数值"):
            normalize_series_result(
                {d: {"600000.SH": "x"} for d in self._DATES},
                expected_dates=self._DATES,
                universe=("600000.SH",),
            )

    def test_rejects_unsupported_type(self) -> None:
        with pytest.raises(OutputContractError, match="不支持"):
            normalize_series_result(
                [1, 2, 3], expected_dates=self._DATES, universe=("600000.SH",)
            )

    def test_series_metrics(self) -> None:
        series = normalize_series_result(
            {
                date(2024, 6, 3): {"600000.SH": 1.0, "000001.SZ": None},
                date(2024, 6, 4): {"600000.SH": float("inf"), "000001.SZ": 2.0},
            },
            expected_dates=self._DATES,
            universe=("600000.SH", "000001.SZ"),
        )
        metrics = series_metrics(series, universe=("600000.SH", "000001.SZ"))
        assert metrics["n_dates"] == 2
        assert metrics["n_cells"] == 4
        assert metrics["n_cells_finite"] == 2
        assert metrics["coverage"] == 0.5
        # 4 格产出 3 个非 None,其中 1 个 inf → nan_ratio = 1/3
        assert metrics["nan_ratio"] == pytest.approx(1 / 3)
