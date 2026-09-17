"""SyncSpec 注册表、三种枚举形态与行级口径推导(issue #392 单测)。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.background_jobs.dataset_sync import specs  # noqa: F401  # 注册副作用
from finboard_backtest.background_jobs.dataset_sync.spec import (
    ROW_POLICY_BY_SHAPE,
    SYNC_SPECS,
    EnumShape,
    RowPolicy,
    UnknownDatasetError,
    slices_for_spec,
    workdays,
)

pytestmark = pytest.mark.unit


class TestRegistry:
    def test_builtin_seven_datasets_registered(self) -> None:
        # #396:第七集 suspensions(停复牌,DAILY_MARKET)注册;
        # #397:第八至十一集 = 三表 income/balance/cashflow + dividends。
        assert SYNC_SPECS.names == frozenset(
            {
                "profiles",
                "name_changes",
                "convertible_profiles",
                "daily_metrics",
                "suspensions",
                "financial_indicators",
                "industry_memberships",
                "income_statements",
                "balance_sheets",
                "cashflow_statements",
                "dividends",
            }
        )

    def test_declaration_order_is_default_execution_order(self) -> None:
        # 编排顺序:旧六集保持原相对序(profiles → name_changes →
        # convertible → daily → financial → industry,golden 依赖此序),
        # #396 suspensions 插在 daily_metrics 之后,#397 四集追加在尾部
        # (不影响既有数据集的默认执行序)。
        assert SYNC_SPECS.default_names() == (
            "profiles",
            "name_changes",
            "convertible_profiles",
            "daily_metrics",
            "suspensions",
            "financial_indicators",
            "industry_memberships",
            "income_statements",
            "balance_sheets",
            "cashflow_statements",
            "dividends",
        )

    def test_three_statements_dividends_shapes_and_versions(self) -> None:
        # #397:四集均为 PER_SYMBOL_RANGE(REJECT 行级口径按形态推导),
        # dataset_version 形状 = <前缀>:<symbol>:<start>:<end>。
        from finboard_backtest.background_jobs.dataset_sync.spec import SliceQuery

        query = SliceQuery(
            dataset="income_statements",
            row_policy="reject",
            symbol="600519.SH",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 6, 30),
        )
        for name, prefix in (
            ("income_statements", "income"),
            ("balance_sheets", "balance"),
            ("cashflow_statements", "cashflow"),
            ("dividends", "dividend"),
        ):
            spec = SYNC_SPECS.get(name)
            assert spec.shape is EnumShape.PER_SYMBOL_RANGE
            assert spec.row_policy is RowPolicy.REJECT
            assert spec.is_per_symbol
            assert spec.slice_version(query) == (
                f"{prefix}:600519.SH:2026-01-01:2026-06-30"
            )
            assert spec.slice_parameters(query) == {
                "symbol": "600519.SH",
                "start_date": "2026-01-01",
                "end_date": "2026-06-30",
            }

    def test_duplicate_registration_rejected(self) -> None:
        from finboard_backtest.background_jobs.dataset_sync.spec import SyncSpec

        spec = SyncSpec(
            name="profiles",
            shape=EnumShape.FULL_PAGED,
            title="重复注册",
            fetch=None,  # type: ignore[arg-type]
            persist=None,  # type: ignore[arg-type]
            slice_version=lambda _q: "x",
        )
        with pytest.raises(ValueError, match="重复注册"):
            SYNC_SPECS.register(spec)

    def test_unknown_dataset_error(self) -> None:
        with pytest.raises(UnknownDatasetError):
            SYNC_SPECS.get("nope")

    def test_empty_name_rejected(self) -> None:
        from finboard_backtest.background_jobs.dataset_sync.spec import SyncSpec

        spec = SyncSpec(
            name="",
            shape=EnumShape.FULL_PAGED,
            title="空名",
            fetch=None,  # type: ignore[arg-type]
            persist=None,  # type: ignore[arg-type]
            slice_version=lambda _q: "x",
        )
        with pytest.raises(ValueError, match="不能为空"):
            SYNC_SPECS.register(spec)


class TestRowPolicyDerivation:
    def test_row_policy_derived_from_shape_not_hand_written(self) -> None:
        # #389 固化:全市场枚举=行级跳过;按 symbol 精确查询=整批拒。
        assert ROW_POLICY_BY_SHAPE[EnumShape.FULL_PAGED] is RowPolicy.SKIP
        assert ROW_POLICY_BY_SHAPE[EnumShape.DAILY_MARKET] is RowPolicy.SKIP
        assert ROW_POLICY_BY_SHAPE[EnumShape.PER_SYMBOL_RANGE] is RowPolicy.REJECT
        for spec in SYNC_SPECS.specs:
            assert spec.row_policy is ROW_POLICY_BY_SHAPE[spec.shape]

    def test_builtin_shapes(self) -> None:
        by_name = {spec.name: spec for spec in SYNC_SPECS.specs}
        assert by_name["profiles"].shape is EnumShape.FULL_PAGED
        assert by_name["name_changes"].shape is EnumShape.FULL_PAGED
        assert by_name["convertible_profiles"].shape is EnumShape.FULL_PAGED
        assert by_name["daily_metrics"].shape is EnumShape.DAILY_MARKET
        assert by_name["financial_indicators"].shape is EnumShape.PER_SYMBOL_RANGE
        assert by_name["industry_memberships"].shape is EnumShape.PER_SYMBOL_RANGE


class TestSlicesForSpec:
    START = date(2026, 7, 24)  # 周五
    END = date(2026, 7, 27)  # 周一
    POOL = ("000001.SZ", "600000.SH")

    def _spec(self, name: str):
        return SYNC_SPECS.get(name)

    def test_full_paged_single_slice(self) -> None:
        slices = slices_for_spec(
            self._spec("profiles"),
            start_date=self.START,
            end_date=self.END,
            symbol_pool=self.POOL,
        )
        assert len(slices) == 1
        assert slices[0].dataset == "profiles"
        assert slices[0].row_policy == "skip"
        assert slices[0].trade_date is None
        assert slices[0].symbol is None

    def test_daily_market_one_slice_per_workday(self) -> None:
        slices = slices_for_spec(
            self._spec("daily_metrics"),
            start_date=self.START,
            end_date=self.END,
            symbol_pool=(),
        )
        # 周末(7-25/26)被排除;切片带交易日与行级口径。
        assert [s.trade_date for s in slices] == [
            date(2026, 7, 24),
            date(2026, 7, 27),
        ]
        assert all(s.row_policy == "skip" for s in slices)
        assert all(s.symbol is None for s in slices)

    def test_per_symbol_range_one_slice_per_symbol(self) -> None:
        slices = slices_for_spec(
            self._spec("financial_indicators"),
            start_date=self.START,
            end_date=self.END,
            symbol_pool=self.POOL,
        )
        assert [s.symbol for s in slices] == list(self.POOL)
        assert all(s.row_policy == "reject" for s in slices)
        assert all(s.start_date == self.START for s in slices)
        assert all(s.end_date == self.END for s in slices)


class TestWorkdays:
    def test_weekend_excluded(self) -> None:
        days = workdays(date(2026, 7, 24), date(2026, 7, 27))
        assert days == (date(2026, 7, 24), date(2026, 7, 27))

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能晚于"):
            workdays(date(2026, 7, 27), date(2026, 7, 24))
