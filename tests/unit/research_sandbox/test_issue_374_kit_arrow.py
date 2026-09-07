"""issue #374:kit 数据面 Arrow 常驻 + 按需截面(第二层治本)。

容器 OOM 根因是 harness ``pd.read_parquet`` 把整表(705 万行 daily_metrics
≈ 2GB)解压进 pandas。第二层改造后:harness 以 Arrow 表常驻(无对象税),
``as_of`` 过滤在 Arrow compute 内完成,仅当次访问的可见行物化为 pandas
—— 容器内存与窗口长度解耦。守卫面:

* **等值**——Arrow 底座与 0.3.0 pandas 整表路径,对同一份真实 v3 挂载
  (#372 流式写出)产出逐值一致的 factor_series.json(诚实因子 + 前视
  因子各一);``bars_view`` / ``dataset_view`` 逐日帧与 pandas 参照
  ``assert_frame_equal``(dtype 含内);
* **惰性**——harness 装配后零 pandas 物化,views-only 因子只物化当次
  可见行;``ctx.bars`` 等兼容字段惰性物化且缓存;
* **边界**——双来源混装拒绝;只读上下文不可赋值;无 available_at 的
  Arrow 数据面按 date32 业务列回退,其余类型 fail-closed。

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
import pyarrow.parquet as pq
import pytest
from pandas.testing import assert_frame_equal

import finboard_research_kit.context as context_module
from finboard_research_kit.context import FactorSeriesContext
from finboard_research_kit.harness import EXIT_OK, build_series_context, main
from finboard_research_kit.result import normalize_series_result
from tests.unit.research_sandbox.test_data_mount import (
    _DailyRecord,
    _Inst,
    _Kind,
    _PITBar,
    _Release,
    _StubProvider,
)

_ALL_DAYS = tuple(
    date(2024, 6, day) for day in (3, 4, 5, 6, 7)
)
#: 决策窗口 = 后 3 日(前 2 日为回看历史,让 bars_view 前缀有形状)
_DATES = _ALL_DAYS[2:]
_SYMBOLS = ("600000.SH", "000001.SZ")
_PARAMS = {"window": 2}


async def _build_mount(out_root: Path) -> None:
    """真实 #372 流式写出的一份 v3 挂载:两标的 x 5 业务日 bars + daily。"""
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
                available_at=datetime(
                    day.year, day.month, day.day, 17, 0, tzinfo=UTC
                ),
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
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id="DR-bars",
        dataset_release_ids=["DR-daily"],
    )


def _make_code_dir(root: Path, factor_src: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "factor.py").write_text(factor_src, encoding="utf-8")
    params = "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in _PARAMS.items()
    )
    (root / "manifest.toml").write_text(
        f'[manifest]\nentry = "factor.compute_series"\n\n[manifest.params]\n{params}\n',
        encoding="utf-8",
    )
    return root


async def _run_harness(tmp_path: Path, factor_src: str) -> dict[str, Any]:
    """harness --mode factor_series 端到端(Arrow 底座),返回 canonical payload。"""
    await _build_mount(tmp_path / "data")
    _make_code_dir(tmp_path / "code", factor_src)
    out = tmp_path / "out"
    code = main(
        [
            "--code-dir", str(tmp_path / "code"),
            "--data-dir", str(tmp_path / "data"),
            "--out-dir", str(out),
            "--mode", "factor_series",
        ]
    )
    assert code == EXIT_OK, (out / "error.json").read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads(
        (out / "factor_series.json").read_text(encoding="utf-8")
    )
    return payload


def _run_pandas_reference(
    code_dir: Path, data_dir: Path
) -> dict[date, dict[str, float | None]]:
    """0.3.0 参照路径:pd.read_parquet 整表 + pandas 构造上下文跑同一因子。"""
    bars = pd.read_parquet(data_dir / "bars.parquet")
    bars["date"] = pd.to_datetime(bars["date"])
    daily_path = data_dir / "daily_metrics.parquet"
    manifest = json.loads(
        (data_dir / "mount_manifest.json").read_text(encoding="utf-8")
    )
    ctx = FactorSeriesContext(
        dates=tuple(date.fromisoformat(d) for d in manifest["window"]["dates"]),
        symbols=tuple(manifest["symbols"]),
        bars=bars,
        daily_metrics=(
            pd.read_parquet(daily_path) if daily_path.exists() else None
        ),
        params=MappingProxyType(dict(_PARAMS)),
    )
    spec = importlib.util.spec_from_file_location(
        "_ref_factor", code_dir / "factor.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = module.compute_series(ctx)
    series = normalize_series_result(
        raw, expected_dates=ctx.dates, universe=ctx.symbols
    )
    return series.values


def _canonical(
    values: Any,
) -> dict[str, dict[str, float | None]]:
    """归一到 canonical JSON 口径(非有限值 → None,date → ISO)。"""
    return {
        day.isoformat(): {
            symbol: (
                value
                if value is not None and math.isfinite(value)
                else None
            )
            for symbol, value in sorted(cross.items())
        }
        for day, cross in values.items()
    }


_HONEST_FACTOR = '''
def compute_series(ctx):
    values = {}
    for day in ctx.dates:
        view = ctx.bars_view(day)
        daily = ctx.dataset_view("daily_metrics", day)
        cross = {}
        for symbol in ctx.symbols:
            closes = view.for_symbol(symbol)["close"].to_numpy()
            if len(closes) < 3:
                cross[symbol] = None
                continue
            score = float(closes[-1] / closes[-3] - 1.0)
            if daily.frame is not None:
                rows = daily.frame[daily.frame["symbol"] == symbol]
                if len(rows):
                    score += 0.01 * float(
                        rows.sort_values("trade_date")["pb"].iloc[-1]
                    )
            cross[symbol] = score
        values[day] = cross
    return values
'''

_LOOKAHEAD_FACTOR = '''
def compute_series(ctx):
    # 故意整窗可见(每日均读 window_end 收盘)—— 等值对照样本,非契约示范
    full = ctx.bars_view(ctx.dates[-1])
    return {
        day: {
            symbol: float(full.for_symbol(symbol)["close"].iloc[-1])
            for symbol in ctx.symbols
        }
        for day in ctx.dates
    }
'''


class TestArrowPandasEquivalence:
    """Arrow 底座与 0.3.0 pandas 整表路径逐值等值。"""

    async def test_harness_values_equal_pandas_reference(
        self, tmp_path: Path
    ) -> None:
        """诚实因子:factor_series.json 与 pandas 参照路径逐值一致。"""
        payload = await _run_harness(tmp_path, _HONEST_FACTOR)
        reference = _run_pandas_reference(
            tmp_path / "code", tmp_path / "data"
        )
        assert payload["protocol_version"] == 2
        assert payload["release_id"] == "DR-bars"
        assert payload["dataset_release_ids"] == ["DR-daily"]
        assert payload["params"] == _PARAMS
        assert payload["values"] == _canonical(reference)

    async def test_lookahead_factor_values_equal_pandas_reference(
        self, tmp_path: Path
    ) -> None:
        """前视因子:两条路径对同一份挂载产出完全相同的(违规)值。"""
        payload = await _run_harness(tmp_path, _LOOKAHEAD_FACTOR)
        reference = _run_pandas_reference(
            tmp_path / "code", tmp_path / "data"
        )
        assert payload["values"] == _canonical(reference)
        # 前视样本的确读到了整窗:首日均见 window_end 收盘
        first_day = payload["values"][_DATES[0].isoformat()]
        last_day = payload["values"][_DATES[-1].isoformat()]
        assert first_day == last_day

    async def test_views_frames_equal_pandas_reference(
        self, tmp_path: Path
    ) -> None:
        """逐日 bars_view / dataset_view 帧与 pandas 参照逐值逐 dtype 一致。"""
        await _build_mount(tmp_path / "data")
        data_dir = tmp_path / "data"
        manifest = json.loads(
            (data_dir / "mount_manifest.json").read_text(encoding="utf-8")
        )
        legacy_bars = pd.read_parquet(data_dir / "bars.parquet")
        legacy_bars["date"] = pd.to_datetime(legacy_bars["date"])
        legacy_daily = pd.read_parquet(data_dir / "daily_metrics.parquet")
        arrow_ctx = build_series_context(
            code_dir=tmp_path, data_dir=data_dir, manifest=manifest
        )
        legacy_ctx = FactorSeriesContext(
            dates=tuple(date.fromisoformat(d) for d in manifest["window"]["dates"]),
            symbols=tuple(manifest["symbols"]),
            bars=legacy_bars,
            daily_metrics=legacy_daily,
        )
        for day in _DATES:
            assert_frame_equal(
                arrow_ctx.bars_view(day).frame, legacy_ctx.bars_view(day).frame
            )
            arrow_daily = arrow_ctx.dataset_view("daily_metrics", day).frame
            legacy_daily_view = legacy_ctx.dataset_view("daily_metrics", day).frame
            assert arrow_daily is not None
            assert legacy_daily_view is not None
            assert_frame_equal(arrow_daily, legacy_daily_view)
        assert arrow_ctx.dataset_view("financial_indicators", _DATES[0]).frame is None

    async def test_dtype_contract_preserved(self, tmp_path: Path) -> None:
        """dtype 契约:bars.date 为 datetime64,daily.trade_date 为 date 对象。"""
        await _build_mount(tmp_path / "data")
        data_dir = tmp_path / "data"
        manifest = json.loads(
            (data_dir / "mount_manifest.json").read_text(encoding="utf-8")
        )
        ctx = build_series_context(
            code_dir=tmp_path, data_dir=data_dir, manifest=manifest
        )
        view = ctx.bars_view(_DATES[-1])
        assert pd.api.types.is_datetime64_any_dtype(view.frame["date"])
        daily = ctx.dataset_view("daily_metrics", _DATES[-1]).frame
        assert daily is not None
        assert daily["trade_date"].dtype == object
        assert all(isinstance(d, date) for d in daily["trade_date"])


class TestLazyMaterialization:
    """Arrow 底座下 pandas 物化是惰性、按需、且只物化当次可见行。"""

    async def test_harness_views_only_factor_never_materializes_full_frames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """views-only 因子:物化次数 = 访问器调用次数,整表物化不发生。"""
        calls: list[int] = []
        real = context_module._table_to_frame

        def counting(table: pa.Table) -> pd.DataFrame:
            calls.append(table.num_rows)
            return real(table)

        monkeypatch.setattr(context_module, "_table_to_frame", counting)
        probe = (
            "def compute_series(ctx):\n"
            "    values = {}\n"
            "    for d in ctx.dates:\n"
            "        view = ctx.bars_view(d)\n"
            "        values[d] = {s: float(view.for_symbol(s)"
            "['close'].iloc[-1]) for s in ctx.symbols}\n"
            "    return values\n"
        )
        await _run_harness(tmp_path, probe)
        # 3 个决策日 x 每日一次 bars_view = 3 次物化;0.3.0 的整表装配
        # (构造期 1 次 + 视图 N 次)在此形态下是 4 次
        assert len(calls) == len(_DATES)

    async def test_bars_property_lazy_and_cached(self, tmp_path: Path) -> None:
        """ctx.bars 兼容字段首次访问才整表物化,且结果被缓存。"""
        await _build_mount(tmp_path / "data")
        data_dir = tmp_path / "data"
        manifest = json.loads(
            (data_dir / "mount_manifest.json").read_text(encoding="utf-8")
        )
        ctx = build_series_context(
            code_dir=tmp_path, data_dir=data_dir, manifest=manifest
        )
        assert ctx._bars_frame is None
        assert ctx._daily_frame is None
        first = ctx.bars
        assert ctx._bars_frame is first
        assert ctx.bars is first
        total = pq.read_table(data_dir / "bars.parquet").num_rows
        assert len(first) == total
        daily = ctx.daily_metrics
        assert daily is not None
        assert ctx.daily_metrics is daily

    async def test_frozen_context_rejects_mutation(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path / "data")
        data_dir = tmp_path / "data"
        manifest = json.loads(
            (data_dir / "mount_manifest.json").read_text(encoding="utf-8")
        )
        ctx = build_series_context(
            code_dir=tmp_path, data_dir=data_dir, manifest=manifest
        )
        with pytest.raises(AttributeError, match="只读上下文"):
            ctx.dates = ()
        with pytest.raises(AttributeError, match="只读上下文"):
            ctx.anything = 1


class TestSourceContract:
    """双来源二选一与 Arrow 回退过滤边界。"""

    def _bars_table(self, *, with_available_at: bool = True) -> pa.Table:
        days = list(_ALL_DAYS)
        rows = {
            "symbol": ["600000.SH"] * len(days),
            "date": pa.array(days, type=pa.date32()),
            "close": [10.0 + i for i in range(len(days))],
        }
        if with_available_at:
            available_at = pa.array(
                [
                    datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)
                    for d in days
                ],
                type=pa.timestamp("us", tz="UTC"),
            )
            rows["available_at"] = available_at
        return pa.table(rows)

    def test_mixed_sources_rejected(self) -> None:
        with pytest.raises(ValueError, match="二选一"):
            FactorSeriesContext(
                dates=_DATES,
                symbols=_SYMBOLS,
                bars=pd.DataFrame({"symbol": [], "date": []}),
                bars_table=self._bars_table(),
            )

    def test_arrow_fallback_filters_by_date32(self) -> None:
        """无 available_at 的 Arrow 数据面按 date32 业务列回退(D1 等值)。"""
        ctx = FactorSeriesContext(
            dates=_DATES,
            symbols=_SYMBOLS,
            bars_table=self._bars_table(with_available_at=False),
        )
        view = ctx.bars_view(date(2024, 6, 4))
        assert view.frame["close"].to_list() == [10.0, 11.0]

    def test_arrow_fallback_rejects_non_date_column(self) -> None:
        table = pa.table(
            {
                "symbol": ["600000.SH"],
                "date": pa.array(
                    [datetime(2024, 6, 3)], type=pa.timestamp("us", tz="UTC")
                ),
                "close": [10.0],
            }
        )
        ctx = FactorSeriesContext(
            dates=_DATES, symbols=_SYMBOLS, bars_table=table
        )
        with pytest.raises(ValueError, match="date 列"):
            ctx.bars_view(_DATES[0])

    def test_null_available_at_rows_invisible(self) -> None:
        """available_at 为 null 的行不可见(与 pandas NaT 口径一致)。"""
        table = pa.table(
            {
                "symbol": ["600000.SH", "600000.SH"],
                "date": pa.array(
                    [date(2024, 6, 3), date(2024, 6, 4)], type=pa.date32()
                ),
                "close": [10.0, 11.0],
                "available_at": pa.array(
                    [datetime(2024, 6, 3, 15, 30, tzinfo=UTC), None],
                    type=pa.timestamp("us", tz="UTC"),
                ),
            }
        )
        ctx = FactorSeriesContext(
            dates=_DATES, symbols=_SYMBOLS, bars_table=table
        )
        assert len(ctx.bars_view(_DATES[-1]).frame) == 1

    def test_missing_datasets_view_none(self) -> None:
        ctx = FactorSeriesContext(
            dates=_DATES, symbols=_SYMBOLS, bars_table=self._bars_table()
        )
        assert ctx.daily_metrics is None
        assert ctx.dataset_view("daily_metrics", _DATES[0]).frame is None
        assert ctx.financial_indicators is None
