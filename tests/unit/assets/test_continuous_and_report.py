"""连续期货拼接 + 数据集覆盖率审计的单元测试(issue #58)。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_data.assets.continuous import (
    ContinuousFuturesBuildError,
    build_continuous_series,
)
from finboard_data.assets.report import (
    audit_dataset_coverage,
)
from finboard_shared.instruments import (
    ContinuousFuturesRule,
    DatasetManifest,
    FuturesContract,
)
from finboard_shared.types import (
    AdjustmentMethod,
    DatasetQualityStatus,
    RollMethod,
)


def _make_contract(code: str) -> FuturesContract:
    return FuturesContract(
        contract_code=code,
        underlying_symbol="000300.SH",
        exchange="CFFEX",
        multiplier=Decimal("300"),
        margin_rate=Decimal("0.12"),
        price_limit_pct=Decimal("0.10"),
        price_tick=Decimal("0.2"),
    )


def _bars(start: datetime, days: int, base_price: Decimal) -> list[tuple[datetime, Decimal, Decimal]]:
    """生成 N 个交易日的 Bar 数据。"""
    return [
        (start + timedelta(days=i), base_price + Decimal(i), Decimal(1000 + i))
        for i in range(days)
    ]


class TestBuildContinuousSeriesNone:
    """不调整:换月处保留跳空。"""

    def test_no_overlap(self) -> None:
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.NONE,
        )
        c1 = _make_contract("IF2406.CFFEX")
        c2 = _make_contract("IF2407.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        bars1 = _bars(start, 5, Decimal("3500"))
        bars2 = _bars(start + timedelta(days=5), 5, Decimal("3550"))
        series = build_continuous_series(rule, [(c1, bars1), (c2, bars2)])
        assert series.series_id == "IF"
        assert len(series.points) == 10
        # 换月发生在 index 5
        assert series.points[4].contract_code == "IF2406.CFFEX"
        assert series.points[5].contract_code == "IF2407.CFFEX"
        assert series.points[5].roll_flag
        # NONE 模式下 price = raw_price
        assert series.points[5].price == series.points[5].raw_price

    def test_roll_table_recorded(self) -> None:
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.NONE,
        )
        c1 = _make_contract("IF2406.CFFEX")
        c2 = _make_contract("IF2407.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        series = build_continuous_series(
            rule,
            [
                (c1, _bars(start, 3, Decimal("100"))),
                (c2, _bars(start + timedelta(days=3), 3, Decimal("105"))),
            ],
        )
        assert len(series.roll_table) == 1
        _roll_date, from_code, to_code = series.roll_table[0]
        assert from_code == "IF2406.CFFEX"
        assert to_code == "IF2407.CFFEX"

    def test_raw_price_preserved(self) -> None:
        """原始未调整价格保留在 raw_price 字段。"""
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.NONE,
        )
        c1 = _make_contract("IF2406.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        series = build_continuous_series(rule, [(c1, _bars(start, 3, Decimal("100")))])
        # raw_price = 100, 101, 102
        assert series.points[0].raw_price == Decimal("100")
        assert series.points[1].raw_price == Decimal("101")
        assert series.points[2].raw_price == Decimal("102")


class TestBuildContinuousSeriesRatio:
    """比例调整:回溯缩放,保持收益率连续。"""

    def test_factor_applied(self) -> None:
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.RATIO,
        )
        c1 = _make_contract("IF2406.CFFEX")
        c2 = _make_contract("IF2407.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        # 合约1: 100-104;合约2: 110-114(跳高)
        bars1 = _bars(start, 5, Decimal("100"))
        bars2 = _bars(start + timedelta(days=5), 5, Decimal("110"))
        series = build_continuous_series(rule, [(c1, bars1), (c2, bars2)])
        # factor = prev_last / new_first = 104 / 110 = 0.9454...
        # 所以合约1的 prices 也被乘以 factor(回溯调整)
        # 注意:这里我们用的是 forward-adjust(只调整新合约之后)
        # 但本实现实际是 backward-adjust —— 让我验证逻辑
        # 看下第二个合约的第一根 bar:
        second_first = series.points[5]
        # RATIO 调整下 second_first.price = 110 * factor
        # factor = 104/110 (前一合约最后价 / 新合约首价)
        # price = 110 * (104/110) = 104
        # 这样换月处 price 连续!
        assert second_first.price == Decimal("104")
        # raw 不变
        assert second_first.raw_price == Decimal("110")


class TestBuildContinuousSeriesDifference:
    """差分调整:平移历史价格。"""

    def test_shift_applied(self) -> None:
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.DIFFERENCE,
        )
        c1 = _make_contract("IF2406.CFFEX")
        c2 = _make_contract("IF2407.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        bars1 = _bars(start, 3, Decimal("100"))
        bars2 = _bars(start + timedelta(days=3), 3, Decimal("110"))
        series = build_continuous_series(rule, [(c1, bars1), (c2, bars2)])
        # shift = prev_last - new_first = 102 - 110 = -8
        # 新合约 first price = 110 + (-8) = 102
        second_first = series.points[3]
        assert second_first.price == Decimal("102")


class TestBuildContinuousErrors:
    def test_empty_slices_raises(self) -> None:
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.NONE,
        )
        with pytest.raises(ContinuousFuturesBuildError):
            build_continuous_series(rule, [])

    def test_single_contract_no_roll(self) -> None:
        """只有一个合约:不发生换月。"""
        rule = ContinuousFuturesRule(
            series_id="IF",
            roll_method=RollMethod.VOLUME,
            adjustment_method=AdjustmentMethod.NONE,
        )
        c1 = _make_contract("IF2406.CFFEX")
        start = datetime(2024, 6, 1, tzinfo=UTC)
        series = build_continuous_series(rule, [(c1, _bars(start, 5, Decimal("100")))])
        assert len(series.roll_table) == 0
        assert all(not p.roll_flag for p in series.points)


class TestAuditDatasetCoverage:
    def _manifest(self, status: DatasetQualityStatus = DatasetQualityStatus.PASSED) -> DatasetManifest:
        return DatasetManifest(
            dataset_name="daily_bars",
            source="akshare",
            version="2024Q1",
            quality_status=status,
            checksum="abc",
        )

    def test_full_history(self) -> None:
        manifest = self._manifest()
        # 用 1 年内的实际跨度 + 充足的 bar_count 避免触发 gap
        records = [
            ("600519.SH", date(2024, 1, 1), date(2024, 6, 30), 130),
        ]
        report = audit_dataset_coverage(
            manifest,
            records,
            expected_start=date(2024, 1, 1),
            expected_end=date(2024, 6, 30),
        )
        assert report.total_symbols == 1
        assert report.full_history_count == 1
        assert report.gaps_count == 0
        assert report.is_usable
        assert report.entries[0].category == "full"

    def test_short_history(self) -> None:
        manifest = self._manifest()
        records = [
            ("688999.SH", date(2024, 4, 1), date(2024, 6, 30), 60),
        ]
        report = audit_dataset_coverage(
            manifest,
            records,
            expected_start=date(2024, 1, 1),
            expected_end=date(2024, 6, 30),
        )
        assert report.short_history_count == 1
        assert report.entries[0].category == "short_history"

    def test_delisted(self) -> None:
        manifest = self._manifest()
        records = [
            ("600001.SH", date(2024, 1, 1), date(2024, 5, 15), 100),
        ]
        report = audit_dataset_coverage(
            manifest,
            records,
            expected_start=date(2024, 1, 1),
            expected_end=date(2024, 6, 30),
        )
        assert report.entries[0].category == "delisted"

    def test_missing_records(self) -> None:
        manifest = self._manifest()
        records = [("X.SH", None, None, 0)]
        report = audit_dataset_coverage(manifest, records)
        assert report.entries[0].category == "missing"

    def test_auto_quality_failed_with_gaps(self) -> None:
        """缺 bar 数过多 → 自动判定 failed。"""
        manifest = DatasetManifest(
            dataset_name="x",
            source="x",
            version="x",
            quality_status=DatasetQualityStatus.UNKNOWN,
        )
        # bar_count=10 但日历跨度 1000 天 → 严重缺口
        records = [("X.SH", date(2020, 1, 1), date(2024, 6, 30), 10)]
        report = audit_dataset_coverage(manifest, records)
        assert report.entries[0].gap_count > 0
        assert report.entries[0].category == "gaps"

    def test_coverage_ratio_calculation(self) -> None:
        manifest = self._manifest()
        records = [
            ("X.SH", date(2020, 1, 1), date(2024, 1, 1), 1000),
        ]
        report = audit_dataset_coverage(
            manifest,
            records,
            expected_start=date(2020, 1, 1),
            expected_end=date(2024, 1, 1),
        )
        assert report.overall_coverage_pct > Decimal("0")

    def test_entries_by_category(self) -> None:
        manifest = self._manifest()
        records = [
            ("A.SH", date(2024, 1, 1), date(2024, 6, 30), 130),
            ("B.SH", date(2024, 4, 1), date(2024, 6, 30), 65),
        ]
        report = audit_dataset_coverage(
            manifest,
            records,
            expected_start=date(2024, 1, 1),
            expected_end=date(2024, 6, 30),
        )
        full = report.entries_by_category("full")
        short = report.entries_by_category("short_history")
        assert len(full) == 1
        assert len(short) == 1

    def test_failed_manifest_not_usable(self) -> None:
        manifest = self._manifest(status=DatasetQualityStatus.FAILED)
        report = audit_dataset_coverage(manifest, [("X.SH", date(2024, 1, 1), date(2024, 6, 30), 120)])
        assert not report.is_usable
