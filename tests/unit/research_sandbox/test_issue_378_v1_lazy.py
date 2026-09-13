"""issue #378:v1 因子序列回退路径惰性化(kit 0.3.2)。

#374 的 Arrow 常驻 + 按需截面只覆盖 v2 访问器路径;v1 因子在序列模式下的
逐日回退(``_invoke_v1_per_day``)仍每日**急切物化全部三个数据集**的当日
视图 —— 真实挂载(705 万行 daily_metrics)据此把不碰 daily 的因子也
撞穿容器限额(BJ-0C7981C96AF4427,峰值 3760MB / 26 秒 OOM)。守卫面:

* **等值**——v1 回退(bars-only 与 daily-using 各一)与 0.3.1 急切
  构造的 pandas 参照,对同一份真实 v3 挂载产出逐值一致的 factor_series.json;
* **惰性**——不触碰的数据集连 parquet 都不读(loader 计数为零);loader
  只读一次(跨决策日复用 Arrow 底座);factory 只调一次并缓存;
* **边界**——帧与 factory / 表与 loader 混装拒绝;只读;缺数据集 None;
  bars 缺省空帧。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from pandas.testing import assert_frame_equal

import finboard_research_kit.context as context_module
import finboard_research_kit.harness as harness_module
from finboard_research_kit.context import (
    FactorContext,
    FactorSeriesContext,
    end_of_day,
)
from finboard_research_kit.harness import EXIT_OK, main
from finboard_research_kit.result import normalize_result
from tests.unit.research_sandbox.test_data_mount import (
    _DailyRecord,
    _Inst,
    _Kind,
    _PITBar,
    _Release,
    _StubProvider,
)

_ALL_DAYS = tuple(date(2024, 6, day) for day in (3, 4, 5, 6, 7))
#: 决策窗口 = 后 3 日(前 2 日为回看历史,让每日视图前缀有形状)
_DATES = _ALL_DAYS[2:]
_SYMBOLS = ("600000.SH", "000001.SZ")
_PARAMS = {"window": 2}


async def _build_mount(out_root: Path) -> None:
    """真实 #372 流式写出的 v3 挂载:两标的 x 5 业务日 bars + daily。"""
    bars_provider = _StubProvider(
        release=_Release(
            "DR-bars",
            _Kind("bars"),
            [_Inst(s) for s in _SYMBOLS],
            start_date=_ALL_DAYS[0],
            end_date=_ALL_DAYS[-1],
        ),
        bars=[
            _PITBar(code=symbol, day=day, close=10.0 + i * 1.5 + j * 100)
            for j, symbol in enumerate(_SYMBOLS)
            for i, day in enumerate(_ALL_DAYS)
        ],
    )
    daily_provider = _StubProvider(
        release=_Release(
            "DR-daily",
            _Kind("daily_metrics"),
            [_Inst(s) for s in _SYMBOLS],
            start_date=_ALL_DAYS[0],
            end_date=_ALL_DAYS[-1],
        ),
        daily=[
            _DailyRecord(
                symbol=symbol,
                trade_date=day,
                pb=Decimal("1.5") + i + j,
                available_at=datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC),
            )
            for j, symbol in enumerate(_SYMBOLS)
            for i, day in enumerate(_ALL_DAYS)
        ],
    )
    from finboard_backtest.research_sandbox.data_mount import (
        build_window_data_mount,
    )

    await build_window_data_mount(
        providers=[bars_provider, daily_provider],
        window_start=_DATES[0],
        window_end=_DATES[-1],
        dates=list(_DATES),
        out_root=out_root,
        code_artifact="rev5",
        code_commit="c" * 40,
        release_id="DR-bars",
        dataset_release_ids=["DR-daily"],
    )


def _make_code_dir(root: Path, factor_src: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "factor.py").write_text(factor_src, encoding="utf-8")
    params = "\n".join(f"{key} = {json.dumps(value)}" for key, value in _PARAMS.items())
    (root / "manifest.toml").write_text(
        f'[manifest]\nentry = "factor.compute"\n\n[manifest.params]\n{params}\n',
        encoding="utf-8",
    )
    return root


async def _run_harness(tmp_path: Path, factor_src: str) -> dict[str, Any]:
    """harness --mode factor_series(v1 逐日回退),返回 canonical payload。"""
    await _build_mount(tmp_path / "data")
    _make_code_dir(tmp_path / "code", factor_src)
    out = tmp_path / "out"
    code = main(
        [
            "--code-dir",
            str(tmp_path / "code"),
            "--data-dir",
            str(tmp_path / "data"),
            "--out-dir",
            str(out),
            "--mode",
            "factor_series",
        ]
    )
    assert code == EXIT_OK, (out / "error.json").read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads((out / "factor_series.json").read_text(encoding="utf-8"))
    metrics: dict[str, Any] = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert "v1 逐日回退" in metrics["entry_used"]
    return payload


def _v1_pandas_reference(code_dir: Path, data_dir: Path) -> dict[date, dict[str, float | None]]:
    """0.3.1 参照路径:整表 pandas + 每日急切构造 FactorContext 跑同一因子。"""
    bars = pd.read_parquet(data_dir / "bars.parquet")
    bars["date"] = pd.to_datetime(bars["date"])
    daily_path = data_dir / "daily_metrics.parquet"
    daily = pd.read_parquet(daily_path) if daily_path.exists() else None
    manifest = json.loads((data_dir / "mount_manifest.json").read_text(encoding="utf-8"))
    dates = tuple(date.fromisoformat(d) for d in manifest["window"]["dates"])
    symbols = tuple(manifest["symbols"])
    spec = importlib.util.spec_from_file_location("_ref_v1_factor", code_dir / "factor.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values: dict[date, dict[str, float | None]] = {}
    for day in dates:
        day_ctx = FactorContext(
            decision_at=end_of_day(day),
            symbols=symbols,
            bars=context_module._pit_frame(bars, as_of=day, fallback_date_col="date"),
            daily_metrics=context_module._pit_frame(
                daily, as_of=day, fallback_date_col="trade_date"
            ),
            financial_indicators=None,
            params=MappingProxyType(dict(_PARAMS)),
        )
        normalized = normalize_result(module.compute(day_ctx))
        values[day] = {
            str(symbol): (None if pd.isna(value) else float(value))
            for symbol, value in normalized.items()
        }
    return values


def _canonical(
    values: Any,
) -> dict[str, dict[str, float | None]]:
    """归一到 canonical JSON 口径(非有限值 → None,date → ISO)。"""
    return {
        day.isoformat(): {
            symbol: (value if value is not None and math.isfinite(value) else None)
            for symbol, value in sorted(cross.items())
        }
        for day, cross in values.items()
    }


#: rev5 同款:只用 ctx.bars 的横截面反转因子(v1 单日入口)
_V1_BARS_ONLY = """
import pandas as pd


def compute(ctx):
    close = ctx.bars.pivot(index="date", columns="symbol", values="close").sort_index()
    window = int(ctx.params.get("window", 2))
    if len(close) < window + 1:
        raise ValueError("bars 历史不足: %d 行" % len(close))
    mom = close.pct_change(window).iloc[-1]
    score = (-mom).dropna()
    std = score.std(ddof=0)
    if std and std > 0:
        score = (score - score.mean()) / std
    return score
"""

#: 触碰 ctx.daily_metrics 的 v1 因子(每日可见 pb 截面)
_V1_DAILY = """
import pandas as pd


def compute(ctx):
    daily = ctx.daily_metrics
    if daily is None or daily.empty:
        return pd.Series(dtype="float64")
    last = daily.sort_values("trade_date").groupby("symbol")["pb"].last()
    return last.astype(float)
"""


class TestV1FallbackEquivalence:
    """v1 回退路径与 0.3.1 急切构造的 pandas 参照逐值等值。"""

    async def test_bars_only_v1_factor_equal_pandas_reference(self, tmp_path: Path) -> None:
        payload = await _run_harness(tmp_path, _V1_BARS_ONLY)
        reference = _v1_pandas_reference(tmp_path / "code", tmp_path / "data")
        assert payload["protocol_version"] == 2
        assert payload["values"] == _canonical(reference)

    async def test_daily_using_v1_factor_equal_pandas_reference(self, tmp_path: Path) -> None:
        payload = await _run_harness(tmp_path, _V1_DAILY)
        reference = _v1_pandas_reference(tmp_path / "code", tmp_path / "data")
        assert payload["values"] == _canonical(reference)


class TestLazyContract:
    """不触碰的数据集零 IO;loader / factory 只触发一次。"""

    async def test_untouched_datasets_never_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bars-only v1 因子:daily/fin parquet 从不读,bars 只读一次。"""
        optional_calls: list[Path] = []
        bars_calls: list[Path] = []
        real_optional = harness_module._read_mount_table_optional
        real_bars = harness_module._read_mount_table

        def counting_optional(path: Path) -> pa.Table | None:
            optional_calls.append(path)
            return real_optional(path)

        def counting_bars(path: Path) -> pa.Table:
            bars_calls.append(path)
            return real_bars(path)

        monkeypatch.setattr(harness_module, "_read_mount_table_optional", counting_optional)
        monkeypatch.setattr(harness_module, "_read_mount_table", counting_bars)
        await _run_harness(tmp_path, _V1_BARS_ONLY)
        assert optional_calls == []
        # Arrow 底座跨决策日复用:首个决策日读一次,其后 loader 已清空
        assert len(bars_calls) == 1

    async def test_daily_using_factor_reads_daily_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """daily-using 因子:daily loader 也只读一次(不是每日一次)。"""
        optional_calls: list[Path] = []
        real_optional = harness_module._read_mount_table_optional

        def counting_optional(path: Path) -> pa.Table | None:
            optional_calls.append(path)
            return real_optional(path)

        monkeypatch.setattr(harness_module, "_read_mount_table_optional", counting_optional)
        await _run_harness(tmp_path, _V1_DAILY)
        assert len(optional_calls) == 1

    def test_factor_context_factory_cached_once(self) -> None:
        calls: list[int] = []

        def factory() -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"symbol": ["600000.SH"], "pb": [1.5]})

        ctx = FactorContext(
            decision_at=datetime(2024, 6, 5, tzinfo=UTC),
            symbols=("600000.SH",),
            daily_metrics_factory=factory,
        )
        first = ctx.daily_metrics
        assert first is not None
        assert ctx.daily_metrics is first
        assert len(calls) == 1

    def test_factor_context_frame_and_factory_rejected(self) -> None:
        with pytest.raises(ValueError, match="二选一"):
            FactorContext(
                decision_at=datetime(2024, 6, 5, tzinfo=UTC),
                symbols=("600000.SH",),
                bars=pd.DataFrame(),
                bars_factory=lambda: pd.DataFrame(),
            )

    def test_factor_context_missing_dataset_none_and_default_bars(
        self,
    ) -> None:
        ctx = FactorContext(
            decision_at=datetime(2024, 6, 5, tzinfo=UTC),
            symbols=("600000.SH",),
            daily_metrics_factory=lambda: None,
        )
        assert ctx.daily_metrics is None
        assert ctx.financial_indicators is None
        bars = ctx.bars
        assert isinstance(bars, pd.DataFrame)
        assert bars.empty
        assert ctx.bars is bars  # 缺省空帧同样缓存,不重复构造

    def test_factor_context_readonly(self) -> None:
        ctx = FactorContext(
            decision_at=datetime(2024, 6, 5, tzinfo=UTC),
            symbols=("600000.SH",),
        )
        with pytest.raises(AttributeError, match="只读上下文"):
            ctx.symbols = ()
        with pytest.raises(AttributeError, match="只读上下文"):
            del ctx.params

    def test_factor_context_bars_for_with_factory(self) -> None:
        frame = pd.DataFrame(
            {
                "symbol": ["600000.SH", "600000.SH"],
                "date": pd.to_datetime(["2024-06-03", "2024-06-04"]),
                "close": [10.0, 11.0],
            }
        )
        ctx = FactorContext(
            decision_at=datetime(2024, 6, 4, tzinfo=UTC),
            symbols=("600000.SH",),
            bars_factory=lambda: frame,
        )
        subset = ctx.bars_for("600000.SH")
        assert subset["close"].to_list() == [10.0, 11.0]


class TestSeriesLoaderContract:
    """FactorSeriesContext 的 loader 惰性读取。"""

    def _bars_table(self) -> pa.Table:
        days = list(_ALL_DAYS)
        return pa.table(
            {
                "symbol": ["600000.SH"] * len(days),
                "date": pa.array(days, type=pa.date32()),
                "close": [10.0 + i for i in range(len(days))],
                "available_at": pa.array(
                    [datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC) for d in days],
                    type=pa.timestamp("us", tz="UTC"),
                ),
            }
        )

    def test_loader_once_and_views_equal_table_source(self) -> None:
        table = self._bars_table()
        calls: list[int] = []

        def loader() -> pa.Table:
            calls.append(1)
            return table

        lazy_ctx = FactorSeriesContext(dates=_DATES, symbols=_SYMBOLS, bars_loader=loader)
        eager_ctx = FactorSeriesContext(dates=_DATES, symbols=_SYMBOLS, bars_table=table)
        for day in _DATES:
            assert_frame_equal(lazy_ctx.bars_view(day).frame, eager_ctx.bars_view(day).frame)
        assert_frame_equal(lazy_ctx.bars, eager_ctx.bars)
        assert len(calls) == 1

    def test_loader_with_existing_sources_rejected(self) -> None:
        with pytest.raises(ValueError, match="二选一"):
            FactorSeriesContext(
                dates=_DATES,
                symbols=_SYMBOLS,
                bars_table=self._bars_table(),
                bars_loader=lambda: self._bars_table(),
            )
        with pytest.raises(ValueError, match="二选一"):
            FactorSeriesContext(
                dates=_DATES,
                symbols=_SYMBOLS,
                bars=pd.DataFrame(),
                bars_loader=lambda: self._bars_table(),
            )

    def test_loader_returning_none_keeps_missing_semantics(self) -> None:
        ctx = FactorSeriesContext(
            dates=_DATES,
            symbols=_SYMBOLS,
            bars_table=self._bars_table(),
            daily_metrics_loader=lambda: None,
        )
        assert ctx.daily_metrics is None
        assert ctx.dataset_view("daily_metrics", _DATES[0]).frame is None

    def test_repr_does_not_trigger_load(self) -> None:
        def loader() -> pa.Table:
            raise AssertionError("repr 不得触发读取")

        ctx = FactorSeriesContext(dates=_DATES, symbols=_SYMBOLS, bars_loader=loader)
        assert "lazy" in repr(ctx)
