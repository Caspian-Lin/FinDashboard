"""issue #371:挂载物化流式化 + 过滤挂载等值 + bars 主发布接线。

三个守卫面:

* **流式等值** —— v2 / v3 挂载改逐标的流式写后,落盘 parquet 的行集、行
  序、schema 与清单计数器和旧实现语义一致;row group 缓冲只改文件布局,
  不改表内容;
* **过滤挂载等值** —— ``filter_window_data_mount``(基线 Arrow 过滤)与
  「在 cut 现场物化」逐行等值,窗口防线照常 fail-closed;
* **bars 主发布接线** —— ``_series_providers`` 把 release_id 并入挂载
  provider(BJ-7DA144 事故根因回归),执行器对非 bars 主发布秒拒。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_sandbox.data_mount import (
    WindowDataMount,
    _bars_schema,
    build_window_data_mount,
    filter_window_data_mount,
)
from finboard_backtest.research_sandbox.errors import SandboxError
from finboard_backtest.research_sandbox.runner import (
    _series_provider_ids,
    _validate_mount_override,
)
from tests.unit.research_sandbox.test_data_mount import (
    _DailyRecord,
    _Inst,
    _Kind,
    _PITBar,
    _Release,
    _StubProvider,
)

_DECISION_AT = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)

_DAYS = (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5))


def _bars_provider(code: str, kind: str = "bars") -> _StubProvider:
    return _StubProvider(
        release=_Release(
            release_id=code,
            dataset_kind=_Kind(kind),
            instruments=[_Inst(code)],
            start_date=_DAYS[0],
            end_date=_DAYS[-1],
        ),
        bars=[
            _PITBar(close=10.0 + i, day=day, code=code)
            for i, day in enumerate(_DAYS)
        ],
    )


def _daily_provider(code: str, *, with_pb: bool = True) -> _StubProvider:
    return _StubProvider(
        release=_Release(
            release_id=code,
            dataset_kind=_Kind("daily_metrics"),
            instruments=[_Inst(code)],
            start_date=_DAYS[0],
            end_date=_DAYS[-1],
        ),
        daily=[
            _DailyRecord(
                symbol=code,
                trade_date=day,
                pb=Decimal("1.5") if with_pb else None,
                available_at=datetime(
                    day.year, day.month, day.day, 15, 30, tzinfo=UTC
                ),
            )
            for day in _DAYS
        ],
    )


# ----------------------------------------------------------------- 流式等值


class TestStreamingMountEquivalence:
    async def test_v3_rows_schema_and_manifest(self, tmp_path: Path) -> None:
        """流式写出的行集/行序/schema/清单计数与旧实现语义一致。"""
        mount = await build_window_data_mount(
            providers=[_bars_provider("600000.SH"), _daily_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "data",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=["600000.SH"],
        )
        bars = pq.read_table(tmp_path / "data" / "bars.parquet")
        # 行序 = instrument 序 x 日期序(与旧实现一致)
        assert bars.column("symbol").to_pylist() == ["600000.SH"] * 3
        assert [d.day for d in bars.column("date").to_pylist()] == [3, 4, 5]
        assert bars.column("close").to_pylist() == [10.0, 11.0, 12.0]
        # 显式 schema:date32 + float64 + available_at timestamp
        assert bars.schema == _bars_schema(include_available_at=True)
        daily = pq.read_table(tmp_path / "data" / "daily_metrics.parquet")
        assert daily.column("pb").type == pa.float64()
        # 清单计数:bars 与 daily 各自贡献 3 行,universe 单标的
        by_kind = {d.dataset_kind: d for d in mount.datasets}
        assert by_kind["bars"].row_count == 3
        assert by_kind["daily_metrics"].row_count == 3
        assert by_kind["bars"].max_data_date == _DAYS[-1]
        assert mount.symbols == ("600000.SH",)

    async def test_row_group_buffering_keeps_table_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """把 row group 目标行数调小 → 文件拆多 row group,表内容不变。"""
        import finboard_backtest.research_sandbox.data_mount as dm

        baseline = await build_window_data_mount(
            providers=[_bars_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "default",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=[],
        )
        monkeypatch.setattr(dm, "_ROW_GROUP_TARGET_ROWS", 2)
        chunked = await build_window_data_mount(
            providers=[_bars_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "chunked",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=[],
        )
        meta = pq.ParquetFile(tmp_path / "chunked" / "bars.parquet").metadata
        assert meta.num_row_groups >= 2  # 3 行 x 目标 2 → 至少 2 个 row group
        left = pq.read_table(tmp_path / "default" / "bars.parquet")
        right = pq.read_table(tmp_path / "chunked" / "bars.parquet")
        assert left.equals(right)
        assert baseline.manifest_checksum != chunked.manifest_checksum

    async def test_all_null_metric_column_typed_float64(
        self, tmp_path: Path
    ) -> None:
        """全 None 数值列按显式 schema 落 float64(旧行为推断 pa.null)。"""
        mount = await build_window_data_mount(
            providers=[
                _bars_provider("600000.SH"),
                _daily_provider("600000.SH", with_pb=False),
            ],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "data",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=["600000.SH"],
        )
        assert any(d.dataset_kind == "daily_metrics" for d in mount.datasets)
        daily = pq.read_table(tmp_path / "data" / "daily_metrics.parquet")
        assert daily.column("pb").type == pa.float64()
        assert daily.column("pb").to_pylist() == [None, None, None]

# ------------------------------------------------------------- 过滤挂载等值


class TestFilterWindowDataMount:
    async def _baseline_and_fresh(
        self, tmp_path: Path
    ) -> tuple[WindowDataMount, WindowDataMount, list[Any]]:
        cut = _DAYS[1]
        fresh_dates = [d for d in _DAYS if d <= cut]
        fresh = await build_window_data_mount(
            providers=[_bars_provider("600000.SH"), _daily_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=cut,
            dates=fresh_dates,
            out_root=tmp_path / "fresh",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=["600000.SH"],
        )
        baseline = await build_window_data_mount(
            providers=[_bars_provider("600000.SH"), _daily_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "baseline",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=["600000.SH"],
        )
        return baseline, fresh, fresh_dates

    async def test_filtered_mount_equals_fresh_materialization(
        self, tmp_path: Path
    ) -> None:
        """基线过滤 == cut 现场物化(表逐行 + 清单窗口/标的/计数)。"""
        baseline, fresh, fresh_dates = await self._baseline_and_fresh(tmp_path)
        filtered = await filter_window_data_mount(
            baseline,
            new_window_end=_DAYS[1],
            dates=fresh_dates,
            out_root=tmp_path / "filtered",
        )
        for dataset in fresh.datasets:
            left = pq.read_table(fresh.root / dataset.file)
            right = pq.read_table(filtered.root / dataset.file)
            assert left.equals(right), dataset.file
        assert filtered.window_end == fresh.window_end
        assert filtered.dates == fresh.dates
        assert filtered.symbols == fresh.symbols
        by_kind_fresh = {d.dataset_kind: d for d in fresh.datasets}
        by_kind_filtered = {d.dataset_kind: d for d in filtered.datasets}
        for kind, item in by_kind_fresh.items():
            assert by_kind_filtered[kind].row_count == item.row_count
            assert by_kind_filtered[kind].max_data_date == item.max_data_date

    async def test_filter_guard_rejects_late_dated_rows(self, tmp_path: Path) -> None:
        """available_at <= cut 但数据日期 > cut 的行(前视)被防线具名拒绝。"""
        from finboard_backtest.research_sandbox.data_mount import SandboxMountError

        baseline = await build_window_data_mount(
            providers=[_bars_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "baseline",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=[],
        )
        # 直接篡改基线 parquet:末行的 available_at 拉到窗口首日(制造
        # 「日期晚于 cut 但可用早于 cut」的前视行),过滤挂载必须拒绝。
        bars_path = baseline.root / "bars.parquet"
        table = pq.read_table(bars_path)
        col = table.column("available_at").to_pylist()
        col[-1] = datetime(2024, 6, 3, 15, 30, tzinfo=UTC)
        table = table.set_column(
            table.schema.get_field_index("available_at"),
            "available_at",
            pa.array(col, type=table.schema.field("available_at").type),
        )
        pq.write_table(table, bars_path, compression="zstd")
        with pytest.raises(SandboxMountError, match="窗口外数据"):
            await filter_window_data_mount(
                baseline,
                new_window_end=_DAYS[1],
                dates=[d for d in _DAYS if d <= _DAYS[1]],
                out_root=tmp_path / "filtered",
            )

    async def test_filter_rejects_window_end_outside_baseline(
        self, tmp_path: Path
    ) -> None:
        from finboard_backtest.research_sandbox.data_mount import SandboxMountError

        baseline = await build_window_data_mount(
            providers=[_bars_provider("600000.SH")],
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            out_root=tmp_path / "baseline",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="600000.SH",
            dataset_release_ids=[],
        )
        with pytest.raises(SandboxMountError, match="须落在基线窗口"):
            await filter_window_data_mount(
                baseline,
                new_window_end=date(2025, 1, 1),
                dates=list(_DAYS),
                out_root=tmp_path / "filtered",
            )


# --------------------------------------------------------- 列式快路径等值


class TestColumnarDailyMetrics:
    """列式快路径(fetch_daily_metrics_columns)与对象路径逐值等值(#371)。"""

    @staticmethod
    def _real_daily(code: str, day: date, *, available: bool = True) -> Any:
        from finboard_data.research import DailySecurityMetrics

        ts = datetime(day.year, day.month, day.day, 15, 30, tzinfo=UTC)
        return DailySecurityMetrics(
            symbol=code,
            trade_date=day,
            close=Decimal("10.5"),
            turnover_rate=None,
            turnover_rate_free=None,
            volume_ratio=None,
            pe=None,
            pe_ttm=None,
            pb=Decimal("1.25"),
            ps=None,
            ps_ttm=None,
            dividend_yield=None,
            dividend_yield_ttm=None,
            total_shares=None,
            float_shares=None,
            free_shares=None,
            total_market_cap=None,
            circulating_market_cap=None,
            limit_status=None,
            source="tushare",
            observed_at=ts,
            available_at=ts,
        )

    def _columnar_provider(self, code: str) -> Any:
        """带列式读取的 stub:原始表形态对齐发布 parquet(ISO 字符串日期)。"""
        import pyarrow as pa

        provider = _StubProvider(
            release=_Release(
                release_id=code,
                dataset_kind=_Kind("daily_metrics"),
                instruments=[_Inst(code)],
                start_date=_DAYS[0],
                end_date=_DAYS[-1],
            ),
            daily=[
                self._real_daily(code, day) for day in _DAYS
            ],
        )

        async def fetch_daily_metrics_columns(symbol, *, start, end, decision_at):
            rows = [
                r
                for r in provider.daily
                if r.symbol == symbol.code
                and r.available_at is not None
                and r.available_at <= decision_at
                and start <= r.trade_date <= end
            ]
            metric_cols: dict[str, list[Any]] = {}
            for name in (
                "close", "pb", "turnover_rate", "turnover_rate_free",
                "volume_ratio", "pe", "pe_ttm", "ps", "ps_ttm",
                "dividend_yield", "dividend_yield_ttm", "total_shares",
                "float_shares", "free_shares", "total_market_cap",
                "circulating_market_cap", "limit_status",
            ):
                metric_cols[name] = [
                    float(getattr(r, name))
                    if getattr(r, name) is not None
                    else None
                    for r in rows
                ]
            return pa.table(
                {
                    "trade_date": [r.trade_date.isoformat() for r in rows],
                    "available_at": [
                        r.available_at.isoformat() if r.available_at else None
                        for r in rows
                    ],
                    "observed_at": [
                        r.observed_at.isoformat() if r.observed_at else None
                        for r in rows
                    ],
                    "source": [r.source for r in rows],
                    **metric_cols,
                }
            )

        provider.fetch_daily_metrics_columns = fetch_daily_metrics_columns  # type: ignore[attr-defined]
        return provider

    async def test_columnar_mount_matches_object_path(self, tmp_path: Path) -> None:
        code = "600000.SH"
        columnar_provider = self._columnar_provider(code)
        object_provider = _StubProvider(
            release=_Release(
                release_id=code,
                dataset_kind=_Kind("daily_metrics"),
                instruments=[_Inst(code)],
                start_date=_DAYS[0],
                end_date=_DAYS[-1],
            ),
            daily=[self._real_daily(code, day) for day in _DAYS],
        )
        bars = _bars_provider(code)
        via_columnar = await build_window_data_mount(
            providers=[bars, columnar_provider],
            out_root=tmp_path / "columnar",
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id=code,
            dataset_release_ids=[code],
        )
        via_object = await build_window_data_mount(
            providers=[_bars_provider(code), object_provider],
            out_root=tmp_path / "object",
            window_start=_DAYS[0],
            window_end=_DAYS[-1],
            dates=list(_DAYS),
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id=code,
            dataset_release_ids=[code],
        )
        left = pq.read_table(tmp_path / "columnar" / "daily_metrics.parquet")
        right = pq.read_table(tmp_path / "object" / "daily_metrics.parquet")
        assert left.schema == right.schema
        assert left.equals(right)
        assert via_columnar.symbols == via_object.symbols
        by_kind = {d.dataset_kind: d for d in via_columnar.datasets}
        assert by_kind["daily_metrics"].row_count == 3
        assert by_kind["daily_metrics"].max_data_date == _DAYS[-1]

    async def test_columnar_pit_gate_excludes_late_rows(
        self, tmp_path: Path
    ) -> None:
        """列式路径 PIT 门控:available_at 晚于上界的行不进挂载。"""
        code = "600000.SH"
        provider = self._columnar_provider(code)
        from dataclasses import replace as _dc_replace

        late = _dc_replace(
            self._real_daily(code, _DAYS[-1]),
            available_at=datetime(2024, 12, 31, 15, 30, tzinfo=UTC),
        )
        provider.daily.append(late)
        mount = await build_window_data_mount(
            providers=[_bars_provider(code), provider],
            window_start=_DAYS[0],
            window_end=_DAYS[1],
            dates=[_DAYS[0], _DAYS[1]],
            out_root=tmp_path / "data",
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id=code,
            dataset_release_ids=[code],
        )
        table = pq.read_table(tmp_path / "data" / "daily_metrics.parquet")
        by_kind = {d.dataset_kind: d for d in mount.datasets}
        assert by_kind["daily_metrics"].row_count == 2
        assert table.column("trade_date").to_pylist() == [_DAYS[0], _DAYS[1]]


# ------------------------------------------------------------ mount 覆盖校验


class TestValidateMountOverride:
    def _mount(self, **overrides: Any) -> WindowDataMount:
        kwargs: dict[str, Any] = {
            "root": Path("unused"),
            "window_start": date(2024, 1, 1),
            "window_end": date(2024, 1, 3),
            "dates": (date(2024, 1, 2), date(2024, 1, 3)),
            "symbols": ("600000.SH",),
            "datasets": (),
            "manifest_checksum": "x",
            "code_artifact": "mom20",
            "code_commit": "c" * 40,
            "release_id": "R1",
            "dataset_release_ids": ("R2",),
        }
        kwargs.update(overrides)
        return WindowDataMount(**kwargs)

    def _spec(self, **overrides: Any) -> Any:
        from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

        kwargs: dict[str, Any] = {
            "code_artifact": "mom20",
            "code_commit": "c" * 40,
            "release_id": "R1",
            "dataset_release_ids": ("R2",),
            "params": {},
            "window_start": date(2024, 1, 1),
            "window_end": date(2024, 1, 3),
            "dates": (date(2024, 1, 2), date(2024, 1, 3)),
        }
        kwargs.update(overrides)
        return FactorSeriesRunSpec(**kwargs)

    def test_mismatch_raises_named_error(self, tmp_path: Path) -> None:
        spec = self._spec()
        # 窗口末端不一致(变体挂载未随 spec 截断)→ fail-closed 拒绝
        mount = self._mount(root=tmp_path, window_end=date(2024, 6, 30))
        mount.manifest_path.write_text("{}", encoding="utf-8")
        with pytest.raises(SandboxError, match="mount_spec_mismatch"):
            _validate_mount_override(spec, mount)

    def test_consistent_override_passes(self, tmp_path: Path) -> None:
        spec = self._spec()
        mount = self._mount(root=tmp_path)
        mount.manifest_path.write_text("{}", encoding="utf-8")
        _validate_mount_override(spec, mount)


# --------------------------------------------------------- bars 主发布接线


class TestSeriesProviderWiring:
    def test_provider_ids_lead_with_bars_main_and_dedup(self) -> None:
        from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

        spec = FactorSeriesRunSpec(
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="R-BARS",
            dataset_release_ids=("R-DAILY", "R-BARS"),
            params={},
            window_start=date(2024, 1, 1),
            window_end=date(2024, 1, 3),
            dates=(date(2024, 1, 2),),
        )
        assert _series_provider_ids(spec) == ["R-BARS", "R-DAILY"]

    async def test_series_providers_include_bars_main(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归:bars 主发布必须出现在挂载 provider 列表(BJ-7DA144 根因)。"""
        from finboard_backtest.research_sandbox import runner as runner_mod
        from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

        requested: list[str] = []

        def factory(rid: str) -> Any:
            requested.append(rid)
            return object()

        spec = FactorSeriesRunSpec(
            code_artifact="mom20",
            code_commit="c" * 40,
            release_id="R-BARS",
            dataset_release_ids=("R-DAILY",),
            params={},
            window_start=date(2024, 1, 1),
            window_end=date(2024, 1, 3),
            dates=(date(2024, 1, 2),),
        )
        providers, checksums = await runner_mod._series_providers(
            spec, factory, settings=None
        )
        assert requested == ["R-BARS", "R-DAILY"]
        assert len(providers) == 2
        assert checksums == {}

    async def test_require_releases_rejects_non_bars_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """release_id 锚定非 bars 发布 → 秒拒(不再走完全量物化才失败)。"""
        from finboard_backtest.background_jobs.contracts import ExecutorError
        from finboard_backtest.background_jobs.executors.factor_series_build import (
            FactorSeriesBuildExecutor,
            FactorSeriesBuildPayload,
        )
        from finboard_data.releases import ReleaseDatasetKind

        class _ReleaseRow:
            def __init__(self, kind: ReleaseDatasetKind) -> None:
                self.dataset_kind = kind

        class _Repo:
            def __init__(self, session: Any) -> None:
                pass

            async def get(self, release_id: str) -> _ReleaseRow | None:
                if release_id == "R-DAILY":
                    return _ReleaseRow(ReleaseDatasetKind.DAILY_METRICS)
                if release_id == "R-BARS":
                    return _ReleaseRow(ReleaseDatasetKind.BARS)
                return None

        import finboard_persistence

        monkeypatch.setattr(
            finboard_persistence,
            "ResearchDatasetReleaseRepository",
            _Repo,
        )

        class _Session:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        executor = FactorSeriesBuildExecutor(
            session_maker=lambda: _Session(),  # type: ignore[arg-type]
            settings_factory=lambda: None,
        )
        payload = FactorSeriesBuildPayload(
            kind="factor",
            name="rev5",
            release_id="R-DAILY",
            dataset_release_ids=(),
            window_start=date(2024, 1, 1),
            window_end=date(2024, 1, 3),
        )
        with pytest.raises(ExecutorError) as excinfo:
            await executor._require_releases(payload)
        # ExecutorError 是 dataclass,str() 为空,断言 summary 属性
        assert "bars 主发布" in excinfo.value.summary
        assert excinfo.value.code == "invalid_payload"
        # bars 主发布 + 研究发布联合集:通过
        ok_payload = FactorSeriesBuildPayload(
            kind="factor",
            name="rev5",
            release_id="R-BARS",
            dataset_release_ids=("R-DAILY",),
            window_start=date(2024, 1, 1),
            window_end=date(2024, 1, 3),
        )
        await executor._require_releases(ok_payload)
