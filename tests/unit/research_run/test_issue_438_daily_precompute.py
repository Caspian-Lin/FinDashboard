"""decision_load 研究观测读取三层原生化(issue #438)。

生产取证:全市场 research_run(5534 标的 x 75 期)卡 decision_load 40+ 分钟,
worker 单核满载。根因两层:``_read_daily_metrics_columns`` 门控与
``_latest_daily_metrics_row`` 选行逐行 ``fromisoformat`` 持 GIL(每 run
~11.6 亿次 Python 级解析,to_thread 线程全被串行);每 (标的 x 决策期) 独立
``pq.read_table`` 整文件,同一标的文件被重复读 75 期(~41.5 万次/run)。

三层修复,全部语义逐值等值:(1) 门控 Arrow 原生化(available_at 整列一次
timestamp cast + 比较/区间过滤全 Arrow compute);(2) 选行 Arrow 原生化
(timestamp 列 max + 并列取原顺序末位);(3) run 级预计算矩阵(每标的一次
读取覆盖全部决策期,``SymbolDailyMetricsHistory`` 紧凑数组常驻,消费期按需
重建领域对象)。

本文件锁定:门控/选行与逐行 Python 参照实现逐值等值(string 与 timestamp
物理类型、null trade_date 具名拒绝、空表、start/end 边界)、预计算 ↔ 逐期
路径 FeatureValue 逐值等值(builder 真实发布)、每标的恰读 1 次 + 门控热路径
零 fromisoformat、矩阵 nbytes 规模。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    _build_candidates_and_lots,
    _build_daily_metrics_precompute,
    _daily_history_from_table,
    _latest_daily_metrics_row,
    _load_daily_metrics_features,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data import releases as releases_module
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
    ReleaseIntegrityError,
    ResearchDatasetRelease,
    _epoch_micros,
    _read_daily_metrics_columns,
    default_execution_metadata,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_RELEASE_ID = "daily-precompute-r1"
_CODES = ("600001.SH", "000002.SZ")
_RELEASE_START = date(2024, 1, 1)
_RELEASE_END = date(2024, 1, 31)


def _daily(
    symbol: str,
    trade_date: date,
    *,
    available_at: datetime,
    pb: str,
) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=trade_date,
        close=Decimal("12.0"),
        turnover_rate=Decimal("0.02"),
        turnover_rate_free=Decimal("0.015"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("10.0"),
        pe_ttm=Decimal("9.5"),
        pb=Decimal(pb),
        ps=Decimal("2.0"),
        ps_ttm=Decimal("1.9"),
        dividend_yield=Decimal("0.03"),
        dividend_yield_ttm=Decimal("0.031"),
        total_shares=Decimal("100000000"),
        float_shares=Decimal("80000000"),
        free_shares=Decimal("70000000"),
        total_market_cap=Decimal("1200000000"),
        circulating_market_cap=Decimal("960000000"),
        limit_status=0,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )


class _StubDailySource:
    """daily_metrics 注入源 stub(``ResearchDataReleaseSource`` 结构性满足)。"""

    def __init__(self, records: dict[str, list[DailySecurityMetrics]]) -> None:
        self._records = records

    async def daily_metrics(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DailySecurityMetrics]]:
        return {
            symbol: [
                item
                for item in self._records.get(symbol, [])
                if start_date <= item.trade_date <= end_date
            ]
            for symbol in symbols
        }

    async def financial_indicators(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[FinancialIndicator]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def income_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[IncomeStatement]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def balance_sheets(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[BalanceSheet]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def cashflow_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[CashflowStatement]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def dividends(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DividendRecord]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}


def _instrument(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2020, 1, 1),
    )


async def _publish_release(
    tmp_path: Path,
    records: dict[str, list[DailySecurityMetrics]],
) -> FrozenReleaseProvider:
    """用真实发布构建器冻结一份 daily_metrics 发布(#371 夹具同款)。"""
    release_root = tmp_path / "releases"
    await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=release_root,
        research_source=_StubDailySource(records),
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="daily_metrics_precompute",
            source="tushare",
            version="v1",
            start_date=_RELEASE_START,
            end_date=_RELEASE_END,
            code_version="deadbeef",
            fields=DAILY_METRICS_FIELDS,
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
            required_capabilities=("stock",),
            adjustment="none",
        ),
        [_instrument(code) for code in records],
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _candidate(code: str) -> UniverseCandidate:
    return UniverseCandidate(
        symbol=code,
        included=True,
        reasons=("unit-test",),
        asset_class="equity",
        market=Market.A_SHARE.value,
    )


def _pb_values(values: Sequence[FeatureValue]) -> set[tuple[str, float | None]]:
    """提取 pb 特征的 (symbol, value) 集合。"""
    return {(item.symbol, item.value) for item in values if item.feature_id == "pb"}


# ---------------------------------------------------------------------------
# 门控/选行参照实现(旧逐行 Python 路径原样复刻,做等值对照)
# ---------------------------------------------------------------------------


def _reference_gate(
    table: pa.Table,
    *,
    start: date,
    end: date,
    decision_at: datetime,
) -> pa.Table:
    """旧版 ``_read_daily_metrics_columns`` 逐行实现(等值对照参照)。"""
    if table.num_rows == 0:
        return table
    keep: list[bool] = []
    for available_raw, trade_raw in zip(
        table.column("available_at").to_pylist(),
        table.column("trade_date").to_pylist(),
        strict=True,
    ):
        available = (
            available_raw
            if isinstance(available_raw, datetime)
            else datetime.fromisoformat(str(available_raw))
        )
        if available > decision_at:
            keep.append(False)
            continue
        if trade_raw is None:
            raise ReleaseIntegrityError("参照实现: 可见行缺 trade_date")
        record_date = (
            trade_raw.date() if isinstance(trade_raw, datetime) else trade_raw
        )
        keep.append(start <= record_date <= end)
    return table.filter(pa.array(keep))


def _reference_latest_row(table: pa.Table) -> dict[str, object] | None:
    """旧版 ``_latest_daily_metrics_row`` 逐行实现(等值对照参照)。"""
    if table.num_rows == 0:
        return None
    available_values = [
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        for value in table.column("available_at").to_pylist()
    ]
    max_available = max(available_values)
    index = len(available_values) - 1
    while available_values[index] != max_available:
        index -= 1
    row: dict[str, object] = table.slice(index, 1).to_pylist()[0]
    return row


def _write_daily_parquet(
    path: Path,
    *,
    trade_dates: list[Any],
    available_at: list[Any],
    pb: list[float | None],
    limit_status: list[int | None] | None = None,
) -> None:
    """手工构造 daily_metrics 形制的 parquet(真实发布的列物理类型)。"""
    columns: dict[str, object] = {
        "trade_date": pa.array(trade_dates, type=pa.date32()),
        "available_at": pa.array(available_at, type=pa.string()),
        "observed_at": pa.array(
            ["2026-08-29T00:00:00+00:00"] * len(available_at), type=pa.string()
        ),
        "source": pa.array(["tushare"] * len(available_at), type=pa.string()),
        "pb": pa.array(pb, type=pa.float64()),
    }
    if limit_status is not None:
        columns["limit_status"] = pa.array(limit_status, type=pa.int64())
    pq.write_table(pa.table(columns), path)


class TestGateArrowNativeEquivalence:
    """层 1:门控 Arrow 原生化 ↔ 逐行 Python 参照逐值等值。"""

    def test_string_column_multi_case(self, tmp_path: Path) -> None:
        """真实发布物理形态(string available_at + date32 trade_date)。"""
        path = tmp_path / "000001.SZ.parquet"
        _write_daily_parquet(
            path,
            trade_dates=[
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 31),
                date(2024, 2, 1),
            ],
            available_at=[
                "2024-01-02T15:30:00+00:00",
                "2024-01-03T15:30:00+00:00",
                "2024-01-04T15:30:00+00:00",
                "2024-01-31T15:30:00+00:00",
                "2024-02-01T15:30:00+00:00",
            ],
            pb=[1.1, 1.3, None, 1.7, 1.9],
        )
        table = pq.read_table(path)
        for decision_at, start, end in (
            (datetime(2024, 1, 4, 15, 30, tzinfo=UTC), _RELEASE_START, date(2024, 1, 4)),
            (datetime(2024, 1, 4, 15, 29, 59, 999999, tzinfo=UTC), _RELEASE_START, date(2024, 1, 4)),
            (datetime(2024, 2, 2, tzinfo=UTC), date(2024, 1, 3), date(2024, 1, 31)),
            (datetime(2024, 2, 2, tzinfo=UTC), _RELEASE_START, date(2024, 2, 1)),
            (datetime(2023, 12, 31, tzinfo=UTC), _RELEASE_START, _RELEASE_END),
        ):
            got = _read_daily_metrics_columns(
                path, release_id="r1", start=start, end=end, decision_at=decision_at
            )
            expected = _reference_gate(
                table, start=start, end=end, decision_at=decision_at
            )
            assert got.to_pylist() == expected.to_pylist()
        # 全部不可见 → 空表(列结构保留)。
        empty = _read_daily_metrics_columns(
            path,
            release_id="r1",
            start=_RELEASE_START,
            end=_RELEASE_END,
            decision_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert empty.num_rows == 0
        assert empty.column_names == table.column_names

    def test_offset_and_fractional_seconds_parse_to_same_instant(
        self, tmp_path: Path
    ) -> None:
        """非 UTC 偏移与带小数秒的 ISO 串与 fromisoformat 同瞬时可见性。"""
        path = tmp_path / "000001.SZ.parquet"
        _write_daily_parquet(
            path,
            trade_dates=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            available_at=[
                "2024-01-02T15:30:00+08:00",
                "2024-01-03T15:30:00.123456+08:00",
                "2024-01-04T23:59:59Z",
            ],
            pb=[1.1, 1.3, 1.5],
        )
        decision_at = datetime(2024, 1, 3, 7, 30, 0, 123456, tzinfo=UTC)
        got = _read_daily_metrics_columns(
            path,
            release_id="r1",
            start=_RELEASE_START,
            end=_RELEASE_END,
            decision_at=decision_at,
        )
        expected = _reference_gate(
            pq.read_table(path),
            start=_RELEASE_START,
            end=_RELEASE_END,
            decision_at=decision_at,
        )
        assert got.to_pylist() == expected.to_pylist()
        assert got.num_rows == 2

    def test_timestamp_physical_column(self, tmp_path: Path) -> None:
        """物理类型为 timestamp 的 available_at/trade_date 与对象路径等值。"""
        path = tmp_path / "timestamp.parquet"
        pq.write_table(
            pa.table(
                {
                    "trade_date": pa.array(
                        [date(2024, 1, 2), date(2024, 1, 3)], type=pa.date32()
                    ),
                    "available_at": pa.array(
                        [datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                         datetime(2024, 1, 3, 15, 30, tzinfo=UTC)],
                    ),
                    "pb": pa.array([1.1, 1.3]),
                }
            ),
            path,
        )
        for decision_at in (
            datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
            datetime(2024, 1, 3, 15, 29, 59, tzinfo=UTC),
        ):
            got = _read_daily_metrics_columns(
                path,
                release_id="r1",
                start=_RELEASE_START,
                end=_RELEASE_END,
                decision_at=decision_at,
            )
            expected = _reference_gate(
                pq.read_table(path),
                start=_RELEASE_START,
                end=_RELEASE_END,
                decision_at=decision_at,
            )
            assert got.to_pylist() == expected.to_pylist()

    def test_empty_table_passthrough(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.parquet"
        _write_daily_parquet(path, trade_dates=[], available_at=[], pb=[])
        got = _read_daily_metrics_columns(
            path,
            release_id="r1",
            start=_RELEASE_START,
            end=_RELEASE_END,
            decision_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        assert got.num_rows == 0

    def test_visible_row_missing_trade_date_raises(self, tmp_path: Path) -> None:
        """PIT 可见行缺 trade_date → 具名 ReleaseIntegrityError。"""
        path = tmp_path / "null_trade.parquet"
        table = pa.table(
            {
                "trade_date": pa.array([date(2024, 1, 2), None], type=pa.date32()),
                "available_at": pa.array(
                    ["2024-01-02T15:30:00+00:00", "2024-01-03T15:30:00+00:00"],
                    type=pa.string(),
                ),
                "pb": pa.array([1.1, 1.3]),
            }
        )
        pq.write_table(table, path)
        with pytest.raises(ReleaseIntegrityError, match="研究记录缺少日期字段"):
            _read_daily_metrics_columns(
                path,
                release_id="r1",
                start=_RELEASE_START,
                end=_RELEASE_END,
                decision_at=datetime(2024, 1, 4, tzinfo=UTC),
            )
        # 同一行对更早决策期不可见 → 不拒绝(可见子集 null 检查语义)。
        early = _read_daily_metrics_columns(
            path,
            release_id="r1",
            start=_RELEASE_START,
            end=_RELEASE_END,
            decision_at=datetime(2024, 1, 2, 16, 0, tzinfo=UTC),
        )
        assert early.num_rows == 1
        # null available_at:对象路径 fromisoformat(None) 同为 ValueError。
        bad_available = tmp_path / "null_available.parquet"
        pq.write_table(
            pa.table(
                {
                    "trade_date": pa.array([date(2024, 1, 2)], type=pa.date32()),
                    "available_at": pa.array([None], type=pa.string()),
                    "pb": pa.array([1.1]),
                }
            ),
            bad_available,
        )
        with pytest.raises(ValueError, match="available_at"):
            _read_daily_metrics_columns(
                bad_available,
                release_id="r1",
                start=_RELEASE_START,
                end=_RELEASE_END,
                decision_at=datetime(2024, 1, 4, tzinfo=UTC),
            )

    def test_out_of_range_rows_never_trigger_null_check(self, tmp_path: Path) -> None:
        """null trade_date 行「可见即拒」(与是否落在日期区间无关,参照同语义);
        不可见行(available_at 晚于决策时点)不触发拒绝。"""
        path = tmp_path / "range_null.parquet"
        table = pa.table(
            {
                "trade_date": pa.array([None, date(2024, 1, 3)], type=pa.date32()),
                "available_at": pa.array(
                    ["2024-01-05T15:30:00+00:00", "2024-01-03T15:30:00+00:00"],
                    type=pa.string(),
                ),
                "pb": pa.array([1.1, 1.3]),
            }
        )
        pq.write_table(table, path)
        with pytest.raises(ReleaseIntegrityError):
            _read_daily_metrics_columns(
                path,
                release_id="r1",
                start=date(2024, 1, 3),
                end=date(2024, 1, 3),
                decision_at=datetime(2024, 1, 6, tzinfo=UTC),
            )
        # null 行对更早决策期不可见 → 不拒,仅保留区间内的可见行。
        got = _read_daily_metrics_columns(
            path,
            release_id="r1",
            start=date(2024, 1, 3),
            end=date(2024, 1, 3),
            decision_at=datetime(2024, 1, 4, tzinfo=UTC),
        )
        expected = _reference_gate(
            table,
            start=date(2024, 1, 3),
            end=date(2024, 1, 3),
            decision_at=datetime(2024, 1, 4, tzinfo=UTC),
        )
        assert got.to_pylist() == expected.to_pylist()
        assert got.num_rows == 1


class TestLatestRowArrowNativeEquivalence:
    """层 2:选行 Arrow 原生化 ↔ 逐行 Python 参照逐值等值。"""

    def test_ties_and_boundaries_match_reference(self, tmp_path: Path) -> None:
        path = tmp_path / "ties.parquet"
        _write_daily_parquet(
            path,
            trade_dates=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            available_at=[
                "2024-01-04T15:30:00+00:00",
                "2024-01-04T15:30:00+00:00",
                "2024-01-04T15:30:00+00:00",
            ],
            pb=[1.1, 2.2, 3.3],
        )
        table = pq.read_table(path)
        # 全列并列 → 取原顺序最后一条;单行/空表边界。
        latest = _latest_daily_metrics_row(table)
        assert latest == _reference_latest_row(table)
        assert latest is not None
        assert latest["pb"] == 3.3
        assert _latest_daily_metrics_row(table.slice(0, 1)) == _reference_latest_row(
            table.slice(0, 1)
        )
        two = _latest_daily_metrics_row(table.slice(0, 2))
        assert two is not None
        assert two["pb"] == 2.2
        assert _latest_daily_metrics_row(table.slice(0, 0)) is None

    def test_offset_equivalence_same_instant_is_tie(self, tmp_path: Path) -> None:
        """同瞬时不同偏移串(与对象路径 aware 比较)按并列处理。"""
        path = tmp_path / "offset_tie.parquet"
        _write_daily_parquet(
            path,
            trade_dates=[date(2024, 1, 2), date(2024, 1, 3)],
            available_at=[
                "2024-01-03T15:30:00+00:00",
                "2024-01-03T23:30:00+08:00",
            ],
            pb=[1.1, 2.2],
        )
        table = pq.read_table(path)
        row = _latest_daily_metrics_row(table)
        reference = _reference_latest_row(table)
        assert row is not None
        assert reference is not None
        # 同一瞬时(aware datetime 相等)→ 并列取原顺序最后一条。
        assert row["pb"] == reference["pb"]
        assert row["pb"] == 2.2


# ---------------------------------------------------------------------------
# 层 3:run 级预计算矩阵(等值 / 读次数 / 内存)
# ---------------------------------------------------------------------------


def _matrix_args(provider: FrozenReleaseProvider, decisions: list[datetime]) -> dict[str, Any]:
    return {
        "decision_at_micros": pa.array(
            [_epoch_micros(at) for at in decisions], type=pa.int64()
        ).to_numpy(zero_copy_only=False),
        "decision_day_ends": pa.array(
            [at.date().toordinal() - date(1970, 1, 1).toordinal() for at in decisions],
            type=pa.int32(),
        ).to_numpy(zero_copy_only=False),
        "range_start_day": provider.release.start_date.toordinal()
        - date(1970, 1, 1).toordinal(),
    }


class TestPrecomputeEquivalence:
    """预计算路径 ↔ 逐期路径 FeatureValue / missing 逐值等值。"""

    async def test_multi_symbol_multi_decision_equivalent(self, tmp_path: Path) -> None:
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="1.3",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 4),
                    available_at=datetime(2024, 1, 4, 15, 30, tzinfo=UTC),
                    pb="1.5",
                ),
            ],
            "000002.SZ": [
                _daily(
                    "000002.SZ",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="2.0",
                ),
                _daily(
                    "000002.SZ",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="2.2",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate(code) for code in _CODES]
        decisions = [
            datetime(2024, 1, 1, 16, 0, tzinfo=UTC),
            datetime(2024, 1, 3, 15, 30, tzinfo=UTC),  # PIT 边界:恰好等于 available_at
            datetime(2024, 1, 3, 15, 29, 59, 999999, tzinfo=UTC),  # -1µs
            datetime(2024, 1, 5, 16, 0, tzinfo=UTC),
        ]
        precompute = await _build_daily_metrics_precompute(
            provider, candidates, decisions, _RELEASE_ID
        )
        assert set(precompute.period_index) == set(decisions)
        for decision_at in decisions:
            via_precompute = await _load_daily_metrics_features(
                provider,
                candidates,
                decision_at,
                _RELEASE_ID,
                precompute=precompute,
            )
            per_period = await _load_daily_metrics_features(
                provider, candidates, decision_at, _RELEASE_ID
            )
            assert via_precompute == per_period
        # 全量可见时各标的取最新可见行;发布前决策日两路径同为空。
        values, missing = await _load_daily_metrics_features(
            provider, candidates, decisions[-1], _RELEASE_ID, precompute=precompute
        )
        assert missing == ()
        assert _pb_values(values) == {("600001.SH", 1.5), ("000002.SZ", 2.2)}
        empty, empty_missing = await _load_daily_metrics_features(
            provider, candidates, decisions[0], _RELEASE_ID, precompute=precompute
        )
        assert empty == []
        assert empty_missing == ()
        # PIT 边界:== decision_at 可见 / -1µs 取前一根。
        at_boundary, _ = await _load_daily_metrics_features(
            provider, candidates, decisions[1], _RELEASE_ID, precompute=precompute
        )
        assert _pb_values(at_boundary) == {("600001.SH", 1.3), ("000002.SZ", 2.2)}
        before, _ = await _load_daily_metrics_features(
            provider, candidates, decisions[2], _RELEASE_ID, precompute=precompute
        )
        assert _pb_values(before) == {("600001.SH", 1.1), ("000002.SZ", 2.0)}

    async def test_tie_break_matches_per_period(self, tmp_path: Path) -> None:
        """available_at 并列:预计算与逐期路径同取原顺序最后一条。"""
        same_available = datetime(2024, 1, 3, 15, 30, tzinfo=UTC)
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=same_available,
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=same_available,
                    pb="2.2",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate("600001.SH")]
        decisions = [datetime(2024, 1, 6, 16, 0, tzinfo=UTC)]
        precompute = await _build_daily_metrics_precompute(
            provider, candidates, decisions, _RELEASE_ID
        )
        values, missing = await _load_daily_metrics_features(
            provider, candidates, decisions[0], _RELEASE_ID, precompute=precompute
        )
        per_period, per_missing = await _load_daily_metrics_features(
            provider, candidates, decisions[0], _RELEASE_ID
        )
        assert values == per_period
        assert missing == per_missing == ()
        assert _pb_values(values) == {("600001.SH", 2.2)}

    async def test_missing_symbol_tolerated(self, tmp_path: Path) -> None:
        """标的不在研究发布:预计算与逐期路径同计 #252 missing。"""
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.3",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate("600001.SH"), _candidate("000002.SZ")]
        decisions = [datetime(2024, 1, 4, 16, 0, tzinfo=UTC)]
        precompute = await _build_daily_metrics_precompute(
            provider, candidates, decisions, _RELEASE_ID
        )
        values, missing = await _load_daily_metrics_features(
            provider, candidates, decisions[0], _RELEASE_ID, precompute=precompute
        )
        per_period, per_missing = await _load_daily_metrics_features(
            provider, candidates, decisions[0], _RELEASE_ID
        )
        assert values == per_period
        assert missing == per_missing
        assert missing == ("000002.SZ",)
        # 未预建决策期(契约外调用)回退逐期路径,结果不漂移。
        fallback, fallback_missing = await _load_daily_metrics_features(
            provider,
            candidates,
            datetime(2024, 1, 5, 16, 0, tzinfo=UTC),
            _RELEASE_ID,
            precompute=precompute,
        )
        per_fallback, _ = await _load_daily_metrics_features(
            provider, candidates, datetime(2024, 1, 5, 16, 0, tzinfo=UTC), _RELEASE_ID
        )
        assert fallback == per_fallback
        assert fallback_missing == ("000002.SZ",)

    async def test_visible_row_missing_trade_date_named_rejection(
        self, tmp_path: Path
    ) -> None:
        """预建期对「任一冻结决策期可见」的 null trade_date 行具名拒绝。"""
        provider = await _publish_release(
            tmp_path,
            {
                "600001.SH": [
                    _daily(
                        "600001.SH",
                        date(2024, 1, 2),
                        available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                        pb="1.1",
                    ),
                ],
            },
        )

        class _NullTradeProvider(FrozenReleaseProvider):
            """列式读取返回带 null trade_date 行的表(手工构造病态文件)。"""

            async def fetch_daily_metrics_columns(
                self,
                symbol: Symbol,
                *,
                start: date,
                end: date,
                decision_at: datetime,
            ) -> pa.Table:
                del start, end, decision_at
                return pa.table(
                    {
                        "trade_date": pa.array([date(2024, 1, 2), None], type=pa.date32()),
                        "available_at": pa.array(
                            [
                                "2024-01-02T15:30:00+00:00",
                                "2024-01-03T15:30:00+00:00",
                            ],
                            type=pa.string(),
                        ),
                        "pb": pa.array([1.1, 1.3]),
                    }
                )

        wrapped = _NullTradeProvider(
            release_root=provider.release_dir.parent, release_id=_RELEASE_ID
        )
        decisions = [datetime(2024, 1, 4, 16, 0, tzinfo=UTC)]
        with pytest.raises(ReleaseIntegrityError, match="研究记录缺少日期字段"):
            await _build_daily_metrics_precompute(
                wrapped, [_candidate("600001.SH")], decisions, _RELEASE_ID
            )


class TestPrecomputeReadCountAndHotPath:
    """每标的恰读 1 次;门控热路径零逐行解析(fromisoformat 打桩 = 0)。"""

    async def test_one_read_per_symbol_and_zero_fromisoformat_in_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, day),
                    available_at=datetime(2024, 1, day, 15, 30, tzinfo=UTC),
                    pb=f"{day}.1",
                )
                for day in range(2, 6)
            ],
            "000002.SZ": [
                _daily(
                    "000002.SZ",
                    date(2024, 1, day),
                    available_at=datetime(2024, 1, day, 15, 30, tzinfo=UTC),
                    pb=f"{day}.2",
                )
                for day in range(2, 6)
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate(code) for code in _CODES]
        decisions = [
            datetime(2024, 1, day, 16, 0, tzinfo=UTC) for day in range(3, 7)
        ]

        reads = {"n": 0}
        real_read_table = pq.read_table

        def counting_read_table(*args: object, **kwargs: object) -> pa.Table:
            reads["n"] += 1
            return real_read_table(*args, **kwargs)

        monkeypatch.setattr(pq, "read_table", counting_read_table)

        # 门控热路径零逐行解析:给 releases 模块打 fromisoformat 桩,
        # 预建(门控 + 选行 + 列抽取)全程计数必须为 0;逐期路径的选行
        # 同样为 0,只有消费期领域对象重建(每选中行一次)允许出现。
        from datetime import datetime as real_datetime

        fromisoformats = {"n": 0}

        class _CountingDatetime(real_datetime):
            @classmethod
            def fromisoformat(cls, value: object) -> Any:
                fromisoformats["n"] += 1
                return real_datetime.fromisoformat(str(value))

        monkeypatch.setattr(releases_module, "datetime", _CountingDatetime)
        precompute = await _build_daily_metrics_precompute(
            provider, candidates, decisions, _RELEASE_ID
        )
        assert fromisoformats["n"] == 0
        # 每标的恰读 1 次(预建);消费期查矩阵零读取。
        assert reads["n"] == len(_CODES)
        for decision_at in decisions:
            values, missing = await _load_daily_metrics_features(
                provider, candidates, decision_at, _RELEASE_ID, precompute=precompute
            )
            assert missing == ()
            assert values
        assert reads["n"] == len(_CODES)
        # 消费期领域对象重建只对选中行做 fromisoformat(每期 x 有观测标的),
        # 与全历史逐行解析(4 标的日 x 2 x 期数)严格区分。
        monkeypatch.setattr(
            releases_module,
            "datetime",
            _CountingDatetime,
        )
        fromisoformats["n"] = 0
        for decision_at in decisions:
            await _load_daily_metrics_features(
                provider, candidates, decision_at, _RELEASE_ID, precompute=precompute
            )
        assert fromisoformats["n"] == len(decisions) * len(_CODES)


class TestPrecomputeStorage:
    """预计算矩阵紧凑存储与历史抽取逐值形态。"""

    async def test_history_arrays_and_nbytes_budget(self, tmp_path: Path) -> None:
        provider = await _publish_release(
            tmp_path,
            {
                "600001.SH": [
                    _daily(
                        "600001.SH",
                        date(2024, 1, 2),
                        available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                        pb="1.1",
                    ),
                    _daily(
                        "600001.SH",
                        date(2024, 1, 3),
                        available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                        pb="2.2",
                    ),
                ],
            },
        )
        decisions = [
            datetime(2024, 1, 2, 16, 0, tzinfo=UTC),
            datetime(2024, 1, 3, 16, 0, tzinfo=UTC),
            datetime(2024, 1, 1, 16, 0, tzinfo=UTC),  # 无可见行
        ]
        table = await provider.fetch_daily_metrics_columns(
            Symbol(code="600001.SH", market=Market.A_SHARE),
            start=provider.release.start_date,
            end=provider.release.end_date,
            decision_at=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        history = _daily_history_from_table(
            table, release_id=_RELEASE_ID, **_matrix_args(provider, decisions)
        )
        assert history.n_periods == 3
        assert history.available_at[2] is None
        assert history.available_at[0] == "2024-01-02T15:30:00+00:00"
        assert history.valid[:2].all()
        assert not history.valid[2].any()
        # 领域对象重建与对象路径逐值一致(Decimal 语义)。
        record = history.record(1, symbol="600001.SH")
        assert record is not None
        assert record.pb == Decimal("2.2")
        assert record.available_at == datetime(2024, 1, 3, 15, 30, tzinfo=UTC)
        assert history.record(2, symbol="600001.SH") is None
        # 紧凑数组:nbytes 与形状精确(非 Python 对象嵌套形态)。
        n_cols = len(history.columns)
        expected = 3 * n_cols * 8 + 3 * n_cols + 3 * 8 + 3 + 3 * 4
        assert history.values.shape == (3, n_cols)
        assert history.nbytes == expected
        # 全市场规模预算:5534 标的 x 75 期 x 16 列 < 200MB。
        per_symbol_75 = 75 * 16 * (8 + 1) + 75 * (8 + 1 + 4)
        assert per_symbol_75 * 5534 < 200 * 1024 * 1024


@dataclass
class _ObjectPathOnlyProvider:
    """只暴露对象路径的 provider 包装(getattr 探测回归保障)。"""

    inner: FrozenReleaseProvider

    @property
    def release(self) -> ResearchDatasetRelease:
        return self.inner.release

    async def fetch_daily_metrics(
        self,
        symbol: Symbol,
        *,
        start: date,
        end: date,
        decision_at: datetime,
    ) -> list[DailySecurityMetrics]:
        return await self.inner.fetch_daily_metrics(
            symbol, start=start, end=end, decision_at=decision_at
        )


# ---------------------------------------------------------------------------
# loader 级接线:ensure_daily_metrics_histories → load_context 消费
# ---------------------------------------------------------------------------

_BARS_RELEASE_ID = "bars-precompute-r1"
_SESSIONS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))


def _session_bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=Decimal("10.1"),
            high=Decimal("10.2"),
            low=Decimal("9.9"),
            close=Decimal("10.0"),
            volume=Decimal(10000),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in _SESSIONS
    ]


async def _publish_bars_and_daily(
    tmp_path: Path,
    daily_records: dict[str, list[DailySecurityMetrics]],
) -> dict[str, FrozenReleaseProvider]:
    """冻结一份 bars 主发布 + 一份 daily_metrics 研究发布(loader 接线用)。"""
    release_root = tmp_path / "releases"
    cache_dir = tmp_path / "cache"
    instruments = [_instrument(code) for code in _CODES]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            _session_bars(instrument.code),
        )
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_BARS_RELEASE_ID,
            dataset_name="daily_metrics_precompute_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_SESSIONS[0],
            end_date=_SESSIONS[-1],
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "daily-cache",
        release_root=release_root,
        research_source=_StubDailySource(daily_records),
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="daily_metrics_precompute",
            source="tushare",
            version="v1",
            start_date=_SESSIONS[0],
            end_date=_SESSIONS[-1],
            code_version="deadbeef",
            fields=DAILY_METRICS_FIELDS,
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
            required_capabilities=("stock",),
            adjustment="none",
        ),
        instruments,
    )
    return {
        _BARS_RELEASE_ID: FrozenReleaseProvider(
            release_root=release_root, release_id=_BARS_RELEASE_ID
        ),
        _RELEASE_ID: FrozenReleaseProvider(
            release_root=release_root, release_id=_RELEASE_ID
        ),
    }


def _precompute_manifest() -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_research",
        dataset_release_ids=(_BARS_RELEASE_ID, _RELEASE_ID),
    )
    return ResearchRunManifest(
        run_id="RR-precompute438test1",
        idempotency_key="precompute-438-test-001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_BARS_RELEASE_ID,
                version="2024.01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="b" * 64,
                capabilities=("stock",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


class TestLoaderEnsureWiring:
    """ensure_daily_metrics_histories → _load_research_features / load_context。"""

    async def test_ensure_wires_precompute_and_load_context_matches_fresh(
        self, tmp_path: Path
    ) -> None:
        daily_records = {
            code: [
                _daily(
                    code,
                    day,
                    available_at=datetime.combine(day, time(15, 30), tzinfo=UTC),
                    pb=f"{10 + index}.{index}",
                )
                for index, day in enumerate(_SESSIONS)
            ]
            for code in _CODES
        }
        providers = await _publish_bars_and_daily(tmp_path, daily_records)

        def factory(release_id: str) -> FrozenReleaseProvider:
            return providers[release_id]

        async def snapshot_provider(snapshot_id: str) -> None:
            del snapshot_id
            return None

        manifest = _precompute_manifest()
        bars_provider = providers[_BARS_RELEASE_ID]
        candidates, _ = _build_candidates_and_lots(
            list(bars_provider.release.instruments)
        )
        decisions = [
            datetime(2024, 1, 3, 16, 0, tzinfo=UTC),
            datetime(2024, 1, 4, 16, 0, tzinfo=UTC),
        ]
        loader = FrozenInputLoader(
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        await loader.ensure_daily_metrics_histories(manifest, decisions)
        assert _RELEASE_ID in loader._daily_precompute
        for decision_at in decisions:
            via, missing = await loader._load_research_features(
                manifest, candidates, decision_at
            )
            # 未预建的新 loader(逐期路径)同值。
            fresh = FrozenInputLoader(
                release_provider_factory=factory,
                snapshot_provider=snapshot_provider,
            )
            reference, ref_missing = await fresh._load_research_features(
                manifest, candidates, decision_at
            )
            assert via == reference
            assert missing == ref_missing == {}
        # load_context 端到端:研究观测经预计算矩阵进入特征截面。
        context = await loader.load_context(
            manifest,
            decision_at=decisions[0],
            execution_at=datetime(2024, 1, 4, 9, 30, tzinfo=UTC),
        )
        feature_ids = {item.feature_id for item in context.features}
        assert "pb" in feature_ids
        assert context.research_release_missing_symbols == {}
        # 幂等:二次 ensure 不重建(内部缓存键不重复)。
        await loader.ensure_daily_metrics_histories(manifest, decisions)
        assert _RELEASE_ID in loader._daily_precompute
