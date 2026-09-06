"""窗口挂载 v3 —— build_window_data_mount(issue #359)。

复用 test_data_mount 的 stub provider 形态。覆盖:

* 挂载语义:窗口全量数据入库(available_at <= window_end 日终),
  bars 长表携带逐行 available_at 列;
* fail-closed:任何数据日期晚于 window_end 当日的行具名拒绝
  (「窗口外数据」),bars / daily_metrics / financial 三条链路同防线;
* 清单 v3:version=3、window 决策日序列、代码/发布溯源字段、checksum;
* 入参校验:空 dates、乱序 dates、窗口外决策日、window_start > window_end;
* v2 单日挂载(build_data_mount)零变化:既有行为回归 + 清单 version 仍为 2。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_sandbox.data_mount import (
    WINDOW_MANIFEST_VERSION,
    SandboxMountError,
    build_data_mount,
    build_window_data_mount,
)
from tests.unit.research_sandbox.test_data_mount import (
    _bars_provider,
    _Inst,
    _Kind,
    _Release,
    _StubProvider,
)

_DECISION_AT = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)
_WINDOW_START = date(2024, 6, 1)
_WINDOW_END = date(2024, 6, 3)
_DATES = (date(2024, 6, 2), date(2024, 6, 3))


def _daily_record_stub(day: date) -> _StubProvider:
    from tests.unit.research_sandbox.test_data_mount import _DailyRecord

    return _StubProvider(
        release=_Release("DR-daily", _Kind("daily_metrics"), [_Inst("600000.SH")]),
        daily=[_DailyRecord(symbol="600000.SH", trade_date=day, pb=Decimal("0.9"))],
    )


class TestWindowMount:
    async def test_window_mount_full_window_and_available_at_column(
        self, tmp_path: Path
    ) -> None:
        """窗口全量数据入库(不止首个决策日),bars 带逐行 available_at。"""
        provider = _bars_provider(
            days=(date(2024, 6, 1), date(2024, 6, 2), date(2024, 6, 3))
        )
        mount = await build_window_data_mount(
            providers=[provider],
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            dates=_DATES,
            out_root=tmp_path / "data",
            code_artifact="mom_series",
            code_commit="a" * 40,
            release_id="DR-bars",
            dataset_release_ids=("DR-bars",),
        )
        table = pq.read_table(tmp_path / "data" / "bars.parquet")
        assert table.num_rows == 3  # 整个窗口的数据(单日挂载只会有门控前缀)
        assert "available_at" in table.column_names
        assert set(mount.symbols) == {"600000.SH"}
        assert mount.window_start == _WINDOW_START
        assert mount.window_end == _WINDOW_END
        assert mount.dates == _DATES

    async def test_manifest_v3_shape_and_checksum(self, tmp_path: Path) -> None:
        provider = _bars_provider(days=(date(2024, 6, 2), date(2024, 6, 3)))
        mount = await build_window_data_mount(
            providers=[provider],
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            dates=_DATES,
            out_root=tmp_path / "data",
            code_artifact="mom_series",
            code_commit="a" * 40,
            release_id="DR-bars",
            dataset_release_ids=("DR-bars",),
        )
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == WINDOW_MANIFEST_VERSION == 3
        assert manifest["window"]["window_start"] == "2024-06-01"
        assert manifest["window"]["window_end"] == "2024-06-03"
        assert manifest["window"]["dates"] == [d.isoformat() for d in _DATES]
        assert manifest["code_artifact"] == "mom_series"
        assert manifest["code_commit"] == "a" * 40
        assert manifest["release_id"] == "DR-bars"
        assert manifest["dataset_release_ids"] == ["DR-bars"]
        assert len(mount.manifest_checksum) == 64

    async def test_bars_after_window_end_fail_closed(self, tmp_path: Path) -> None:
        """provider 门控缺陷漏进窗口之后的行 → 具名拒绝(窗口外数据)。"""
        provider = _bars_provider(days=(date(2024, 6, 2), date(2024, 6, 4)))
        provider.gate_slack_days = 2  # provider 门控放到 window_end+1
        with pytest.raises(SandboxMountError, match="窗口外数据"):
            await build_window_data_mount(
                providers=[provider],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=_DATES,
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_daily_metrics_after_window_end_fail_closed(
        self, tmp_path: Path
    ) -> None:
        provider = _daily_record_stub(date(2024, 6, 4))
        provider.gate_slack_days = 2
        bars = _bars_provider(days=(date(2024, 6, 2),))
        with pytest.raises(SandboxMountError, match="窗口外数据"):
            await build_window_data_mount(
                providers=[bars, provider],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=_DATES,
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_window_day_inclusive_boundary(self, tmp_path: Path) -> None:
        """window_end 当日的数据可见(日终上界含当日)。"""
        provider = _bars_provider(days=(date(2024, 6, 3),))
        mount = await build_window_data_mount(
            providers=[provider],
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            dates=_DATES,
            out_root=tmp_path / "data",
            code_artifact="mom_series",
            code_commit="a" * 40,
            release_id="DR-bars",
            dataset_release_ids=("DR-bars",),
        )
        table = pq.read_table(tmp_path / "data" / "bars.parquet")
        assert table.num_rows == 1
        assert mount.datasets[0].max_data_date == date(2024, 6, 3)

    async def test_empty_dates_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxMountError, match="非空 dates"):
            await build_window_data_mount(
                providers=[_bars_provider()],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=(),
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_unsorted_dates_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxMountError, match="升序"):
            await build_window_data_mount(
                providers=[_bars_provider()],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=(date(2024, 6, 3), date(2024, 6, 2)),
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_dates_outside_window_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxMountError, match="窗口外决策日"):
            await build_window_data_mount(
                providers=[_bars_provider()],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=(date(2024, 6, 2), date(2024, 6, 30)),
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_window_start_after_end_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxMountError, match="晚于"):
            await build_window_data_mount(
                providers=[_bars_provider()],
                window_start=date(2024, 6, 3),
                window_end=date(2024, 6, 1),
                dates=_DATES,
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
            )

    async def test_no_bars_rows_rejected(self, tmp_path: Path) -> None:
        empty = _StubProvider(release=_Release("DR-empty", _Kind("bars")))
        with pytest.raises(SandboxMountError, match="不含任何行情行"):
            await build_window_data_mount(
                providers=[empty],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=_DATES,
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-empty",
                dataset_release_ids=("DR-empty",),
            )

    async def test_symbols_filter_still_applies(self, tmp_path: Path) -> None:
        provider = _bars_provider()
        with pytest.raises(SandboxMountError, match="缺少请求的标的"):
            await build_window_data_mount(
                providers=[provider],
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                dates=_DATES,
                out_root=tmp_path / "data",
                code_artifact="mom_series",
                code_commit="a" * 40,
                release_id="DR-bars",
                dataset_release_ids=("DR-bars",),
                symbols=["000001.SZ"],
            )


class TestV2Unchanged:
    async def test_v2_mount_version_stays_2(self, tmp_path: Path) -> None:
        """v2 单日挂载零变化:清单 version=2、无 window/available_at 列。"""
        provider = _bars_provider(
            days=(date(2024, 6, 1), date(2024, 6, 2), date(2024, 6, 3))
        )
        mount = await build_data_mount(
            providers=[provider],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
        )
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == 2
        assert "window" not in manifest
        assert "code_artifact" not in manifest
        table = pq.read_table(tmp_path / "data" / "bars.parquet")
        assert "available_at" not in table.column_names
        # v2 PIT 上界仍是 decision_at 当日(而非窗口)
        assert mount.datasets[0].max_data_date == date(2024, 6, 3)
