"""data_mount —— PIT 物理隔离与挂载清单(issue #216)。

用 stub provider(结构与 FrozenReleaseProvider 一致)覆盖:多发布合并、
symbols 过滤、PIT fail-closed 防线、缺 bars 拒绝、清单与 checksum。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_sandbox.data_mount import (
    SandboxMountError,
    build_data_mount,
)

_DECISION_AT = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


@dataclass
class _Inst:
    code: str
    market: str = "SH"


@dataclass
class _Sym:
    code: str


@dataclass
class _Bar:
    symbol: _Sym
    timestamp: datetime
    open: float = 9.0
    high: float = 11.0
    low: float = 8.0
    close: float = 10.0
    volume: float = 100.0
    amount: float = 1000.0


@dataclass
class _PITBar:
    close: float
    day: date
    code: str
    # issue #359:窗口挂载(v3)读取逐行 available_at;缺省按 D1 语义
    # 派生为业务日 15:30 UTC(FrozenReleaseProvider 的确定性规则)。
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            self.available_at = datetime(
                self.day.year, self.day.month, self.day.day, 15, 30,
                tzinfo=UTC,
            )

    @property
    def bar(self) -> _Bar:
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=datetime(self.day.year, self.day.month, self.day.day),
            close=self.close,
        )


@dataclass
class _Release:
    release_id: str
    dataset_kind: Any
    instruments: list[_Inst] = field(default_factory=list)
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)


@dataclass
class _DailyRecord:
    """DailySecurityMetrics 的最小投影(dataclass 供 fields() 枚举)。"""

    symbol: str
    trade_date: date
    pb: Decimal | None = None
    # issue #359:窗口挂载(v3)读取逐行 available_at
    available_at: datetime | None = None


@dataclass
class _FinRecord:
    """FinancialIndicator 的最小投影。"""

    symbol: str
    announcement_date: date
    report_period: date
    eps: Decimal | None = None
    # issue #359:窗口挂载(v3)读取逐行 available_at
    available_at: datetime | None = None


@dataclass
class _StubProvider:
    """结构与 FrozenReleaseProvider 对齐的最小 stub。

    ``gate_slack_days`` 模拟 provider PIT 门控缺陷(返回未来行)—— data_mount
    的 fail-closed 防线应在这种情况下拒绝生成挂载。
    """

    release: _Release
    bars: list[_PITBar] = field(default_factory=list)
    daily: list[_DailyRecord] = field(default_factory=list)
    financial: list[_FinRecord] = field(default_factory=list)
    gate_slack_days: int = 0

    def _gate(self, decision_at: datetime) -> date:
        return decision_at.date() + timedelta(days=self.gate_slack_days)

    async def fetch_point_in_time_bars(
        self, symbol, period, start, end, *, decision_at, adjust="qfq"
    ):
        return [
            b for b in self.bars if b.code == symbol.code and b.day <= self._gate(decision_at)
        ]

    async def fetch_daily_metrics(self, symbol, *, start, end, decision_at):
        return [
            r for r in self.daily
            if r.symbol == symbol.code and r.trade_date <= self._gate(decision_at)
        ]

    async def fetch_financial_indicators(self, symbol, *, decision_at):
        return [
            r
            for r in self.financial
            if r.symbol == symbol.code
            and r.announcement_date <= self._gate(decision_at)
        ]


class _Kind:
    def __init__(self, value: str) -> None:
        self.value = value


def _bars_provider(
    code: str = "600000.SH", days: tuple[date, ...] = (date(2024, 6, 3),)
) -> _StubProvider:
    return _StubProvider(
        release=_Release("DR-bars", _Kind("bars"), [_Inst(code)]),
        bars=[_PITBar(code=code, day=d, close=10.0 + i) for i, d in enumerate(days)],
    )


class TestBuildDataMount:
    async def test_bars_mount_and_manifest(self, tmp_path: Path) -> None:
        provider = _bars_provider(
            days=(date(2024, 6, 1), date(2024, 6, 2), date(2024, 6, 3))
        )
        mount = await build_data_mount(
            providers=[provider],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
        )
        table = pq.read_table(tmp_path / "data" / "bars.parquet")
        assert table.num_rows == 3
        assert set(table.column_names) == {
            "symbol", "date", "open", "high", "low", "close", "volume", "amount"
        }
        assert table.column("close").to_pylist() == [10.0, 11.0, 12.0]
        assert mount.symbols == ("600000.SH",)
        assert mount.datasets[0].release_id == "DR-bars"
        assert mount.datasets[0].max_data_date == date(2024, 6, 3)
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        assert manifest["decision_at"] == _DECISION_AT.isoformat()
        assert manifest["symbols"] == ["600000.SH"]
        assert len(mount.manifest_checksum) == 64

    async def test_strategy_mount_manifest_v2(
        self, tmp_path: Path
    ) -> None:
        """issue #218:策略协议挂载清单携带权重回显与约束视图(checksum 覆盖)。"""
        provider = _bars_provider(
            days=(date(2024, 6, 1), date(2024, 6, 2), date(2024, 6, 3))
        )
        mount = await build_data_mount(
            providers=[provider],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
            current_weights={"600000.SH": 0.42},
            strategy_constraints={
                "max_weight_per_asset": 0.2,
                "long_only": True,
                "max_gross_exposure": 1.0,
                "min_cash_buffer": 0.05,
            },
        )
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == 2
        assert manifest["current_weights"] == {"600000.SH": 0.42}
        assert manifest["strategy_constraints"]["max_weight_per_asset"] == 0.2
        assert mount.current_weights == {"600000.SH": 0.42}
        assert len(mount.manifest_checksum) == 64

    async def test_factor_mount_omits_v2_fields(self, tmp_path: Path) -> None:
        provider = _bars_provider(days=(date(2024, 6, 2),))
        mount = await build_data_mount(
            providers=[provider],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
        )
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        assert "current_weights" not in manifest
        assert "strategy_constraints" not in manifest

    async def test_empty_daily_and_financial_files_removed(
        self, tmp_path: Path
    ) -> None:
        mount = await build_data_mount(
            providers=[_bars_provider()],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
        )
        assert not (tmp_path / "data" / "daily_metrics.parquet").exists()
        assert not (tmp_path / "data" / "financial_indicators.parquet").exists()
        assert mount.datasets[0].file == "bars.parquet"

    async def test_pit_future_rows_fail_closed(self, tmp_path: Path) -> None:
        # gate_slack_days=1 模拟 provider PIT 门控缺陷(漏进未来行),
        # data_mount 防线必须拒绝生成挂载。
        provider = _bars_provider(days=(date(2024, 6, 4),))
        provider.gate_slack_days = 1
        with pytest.raises(SandboxMountError, match="PIT 违规"):
            await build_data_mount(
                providers=[provider],
                decision_at=_DECISION_AT,
                out_root=tmp_path / "data",
            )

    async def test_no_bars_rows_rejected(self, tmp_path: Path) -> None:
        empty = _StubProvider(release=_Release("DR-empty", _Kind("bars")))
        with pytest.raises(SandboxMountError, match="不含任何行情行"):
            await build_data_mount(
                providers=[empty],
                decision_at=_DECISION_AT,
                out_root=tmp_path / "data",
            )

    async def test_naive_decision_at_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxMountError, match="带时区"):
            await build_data_mount(
                providers=[_bars_provider()],
                decision_at=datetime(2024, 6, 3, 7, 0),
                out_root=tmp_path / "data",
            )

    async def test_symbols_filter_missing_symbol_fails(
        self, tmp_path: Path
    ) -> None:
        provider = _bars_provider()
        with pytest.raises(SandboxMountError, match="缺少请求的标的"):
            await build_data_mount(
                providers=[provider],
                decision_at=_DECISION_AT,
                out_root=tmp_path / "data",
                symbols=["000001.SZ"],
            )

    async def test_joined_releases_merged(self, tmp_path: Path) -> None:
        bars = _bars_provider()
        fin = _StubProvider(
            release=_Release(
                "DR-fin", _Kind("financial_indicators"), [_Inst("600000.SH")]
            ),
            financial=[
                _FinRecord(
                    symbol="600000.SH",
                    announcement_date=date(2024, 5, 1),
                    report_period=date(2024, 3, 31),
                    eps=Decimal("1.2"),
                )
            ],
        )
        daily = _StubProvider(
            release=_Release(
                "DR-daily", _Kind("daily_metrics"), [_Inst("600000.SH")]
            ),
            daily=[
                _DailyRecord(
                    symbol="600000.SH",
                    trade_date=date(2024, 6, 3),
                    pb=Decimal("0.9"),
                )
            ],
        )
        mount = await build_data_mount(
            providers=[bars, fin, daily],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
        )
        fin_table = pq.read_table(tmp_path / "data" / "financial_indicators.parquet")
        assert fin_table.column("eps").to_pylist() == [1.2]
        daily_table = pq.read_table(tmp_path / "data" / "daily_metrics.parquet")
        assert daily_table.column("pb").to_pylist() == [0.9]
        assert {d.release_id for d in mount.datasets} == {
            "DR-bars", "DR-fin", "DR-daily"
        }
        manifest = json.loads(mount.manifest_path.read_text(encoding="utf-8"))
        # PIT 断言素材:每个数据集的 max_data_date 都不晚于 decision_at 当日
        for dataset in manifest["datasets"]:
            if dataset["max_data_date"]:
                assert dataset["max_data_date"] <= "2024-06-03"

    async def test_unsupported_kind_rejected(self, tmp_path: Path) -> None:
        bogus = _StubProvider(release=_Release("DR-x", _Kind("widgets")))
        with pytest.raises(SandboxMountError, match="不支持挂载"):
            await build_data_mount(
                providers=[bogus],
                decision_at=_DECISION_AT,
                out_root=tmp_path / "data",
            )
