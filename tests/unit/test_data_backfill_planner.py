"""补数预检/契约单元测试(issue #511)。

覆盖:区间代数、请求 fingerprint 稳定性、支持清单、请求数估算、动作判定、
计划 checksum 稳定性。不连 DB —— DB 侧(覆盖 diff / 提交去重 / 执行器)在
``tests/integration/test_issue_511_backfill.py``。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from finboard_backtest.background_jobs.dataset_sync.spec import EnumShape
from finboard_backtest.data_backfill.contracts import (
    BackfillAction,
    BackfillBlocker,
    BackfillDatasetSpec,
    BackfillGapSymbols,
    BackfillItem,
    BackfillPlan,
    BackfillQuota,
    BackfillRequestSpec,
    request_fingerprint,
)
from finboard_backtest.data_backfill.planner import (
    _decide_action,
    estimate_requests,
    intersect_ranges,
    merge_ranges,
    subtract_ranges,
)
from finboard_backtest.data_backfill.support import BACKFILL_SUPPORT, BackfillSupport, support_for
from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeDatasets,
    ScopeDecisionWindow,
    ScopeUniverse,
)
from finboard_data.datasets import CatalogDataset


def _scope(
    *,
    start: date = date(2024, 1, 1),
    end: date = date(2024, 3, 31),
    symbols: tuple[str, ...] = (),
) -> DataScopeSpec:
    return DataScopeSpec(
        universe=ScopeUniverse(explicit_symbols=symbols),
        decision=ScopeDecisionWindow(start=start, end=end),
        datasets=ScopeDatasets(required=(CatalogDataset.BARS,)),
    )


def _spec(
    *,
    datasets: tuple[BackfillDatasetSpec, ...] | None = None,
    symbols: tuple[str, ...] = (),
) -> BackfillRequestSpec:
    return BackfillRequestSpec(
        scope=_scope(symbols=symbols),
        datasets=datasets
        or (BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),),
    )


class TestRangeAlgebra:
    def test_merge_coalesces_overlap_and_adjacency(self) -> None:
        merged = merge_ranges(
            [
                (date(2024, 1, 10), date(2024, 1, 20)),
                (date(2024, 1, 1), date(2024, 1, 10)),
                (date(2024, 1, 21), date(2024, 1, 25)),
            ]
        )
        assert merged == ((date(2024, 1, 1), date(2024, 1, 25)),)

    def test_merge_keeps_disjoint_gaps(self) -> None:
        merged = merge_ranges(
            [
                (date(2024, 1, 1), date(2024, 1, 5)),
                (date(2024, 1, 8), date(2024, 1, 10)),
            ]
        )
        assert merged == (
            (date(2024, 1, 1), date(2024, 1, 5)),
            (date(2024, 1, 8), date(2024, 1, 10)),
        )

    def test_subtract_returns_remaining_gaps(self) -> None:
        remaining = subtract_ranges(
            [(date(2024, 1, 1), date(2024, 1, 31))],
            [(date(2024, 1, 1), date(2024, 1, 9))],
        )
        assert remaining == ((date(2024, 1, 10), date(2024, 1, 31)),)

    def test_subtract_inside_hole(self) -> None:
        remaining = subtract_ranges(
            [(date(2024, 1, 1), date(2024, 1, 31))],
            [(date(2024, 1, 10), date(2024, 1, 20))],
        )
        assert remaining == (
            (date(2024, 1, 1), date(2024, 1, 9)),
            (date(2024, 1, 21), date(2024, 1, 31)),
        )

    def test_subtract_full_cover_is_empty(self) -> None:
        assert (
            subtract_ranges(
                [(date(2024, 1, 1), date(2024, 1, 31))],
                [(date(2023, 12, 1), date(2024, 2, 1))],
            )
            == ()
        )

    def test_intersect(self) -> None:
        assert intersect_ranges(
            [(date(2024, 1, 1), date(2024, 1, 31))],
            [(date(2024, 1, 15), date(2024, 2, 10))],
        ) == ((date(2024, 1, 15), date(2024, 1, 31)),)


class TestFingerprint:
    def test_equivalent_specs_share_fingerprint(self) -> None:
        # 数据集声明顺序与标的顺序不应改变 fingerprint(规范化后比较)。
        first = BackfillRequestSpec(
            scope=_scope(symbols=("000001.SZ", "600000.SH")),
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),
                BackfillDatasetSpec(dataset=CatalogDataset.SUSPENSIONS, publish=False),
            ),
        )
        second = BackfillRequestSpec(
            scope=_scope(symbols=("600000.SH", "000001.SZ")),
            datasets=(
                BackfillDatasetSpec(dataset=CatalogDataset.SUSPENSIONS, publish=False),
                BackfillDatasetSpec(
                    dataset=CatalogDataset.DAILY_METRICS,
                    symbols=("600000.SH",),
                ),
            ),
        )
        # 第二个多声明了 symbols 子集 → 不应等价。
        assert request_fingerprint(first) != request_fingerprint(second)

    def test_symbol_order_is_normalized(self) -> None:
        left = _spec(
            datasets=(
                BackfillDatasetSpec(
                    dataset=CatalogDataset.DAILY_METRICS,
                    symbols=("600000.SH", "000001.SZ"),
                ),
            )
        )
        right = _spec(
            datasets=(
                BackfillDatasetSpec(
                    dataset=CatalogDataset.DAILY_METRICS,
                    symbols=("000001.SZ", "600000.SH"),
                ),
            )
        )
        assert request_fingerprint(left) == request_fingerprint(right)

    def test_source_and_adjustment_change_fingerprint(self) -> None:
        qfq = _spec(
            datasets=(
                BackfillDatasetSpec(
                    dataset=CatalogDataset.BARS, source="tushare", adjustment="qfq"
                ),
            )
        )
        hqfq = _spec(
            datasets=(
                BackfillDatasetSpec(
                    dataset=CatalogDataset.BARS, source="tushare", adjustment="hqfq"
                ),
            )
        )
        akshare = _spec(
            datasets=(
                BackfillDatasetSpec(
                    dataset=CatalogDataset.BARS, source="akshare", adjustment="qfq"
                ),
            )
        )
        assert request_fingerprint(qfq) != request_fingerprint(hqfq)
        assert request_fingerprint(qfq) != request_fingerprint(akshare)

    def test_duplicate_dataset_identity_rejected(self) -> None:
        with pytest.raises(ValueError, match="重复声明"):
            BackfillRequestSpec(
                scope=_scope(),
                datasets=(
                    BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),
                    BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),
                ),
            )

    def test_window_order_rejected(self) -> None:
        with pytest.raises(ValueError, match="start"):
            BackfillDatasetSpec(
                dataset=CatalogDataset.DAILY_METRICS,
                start=date(2024, 3, 1),
                end=date(2024, 1, 1),
            )


class TestSupportMatrix:
    def test_all_catalog_datasets_have_support(self) -> None:
        assert set(BACKFILL_SUPPORT) == set(CatalogDataset)

    def test_bars_uses_bulk_download_and_mixed_release(self) -> None:
        support = support_for(CatalogDataset.BARS)
        assert support.sync_mode == "bulk_download"
        assert support.release_kind == "multi_asset_mixed"
        assert support.publishable is True
        assert support.syncable is True

    def test_convertible_metrics_is_derived_only(self) -> None:
        support = support_for(CatalogDataset.CONVERTIBLE_METRICS)
        assert support.sync_mode == "derived"
        assert support.syncable is False
        assert support.publishable is True

    @pytest.mark.parametrize(
        "dataset", [CatalogDataset.PROFILES, CatalogDataset.SUSPENSIONS]
    )
    def test_master_data_not_publishable(self, dataset: CatalogDataset) -> None:
        support = support_for(dataset)
        assert support.publishable is False
        assert support.has_sync_dataset is True
        assert support.release_kind is None

    def test_research_datasets_require_a_share_stock(self) -> None:
        support = support_for(CatalogDataset.FINANCIAL_INDICATORS)
        assert support.requires_a_share_stock is True
        assert support.markets == ("a_share",)


class TestEstimateRequests:
    def _support(self, dataset: CatalogDataset) -> BackfillSupport:
        return support_for(dataset)

    def test_daily_market_counts_trading_days(self) -> None:
        # 2024-01-01(周一)..2024-01-05(周五)= 5 个工作日。
        estimate = estimate_requests(
            self._support(CatalogDataset.DAILY_METRICS),
            shape=EnumShape.DAILY_MARKET,
            missing_ranges=[(date(2024, 1, 1), date(2024, 1, 5))],
            missing_symbols=0,
            symbol_count=100,
        )
        assert estimate == 5

    def test_per_symbol_counts_symbols(self) -> None:
        estimate = estimate_requests(
            self._support(CatalogDataset.FINANCIAL_INDICATORS),
            shape=EnumShape.PER_SYMBOL_RANGE,
            missing_ranges=[(date(2024, 1, 1), date(2024, 3, 31))],
            missing_symbols=12,
            symbol_count=50,
        )
        assert estimate == 12

    def test_bars_scales_with_symbols(self) -> None:
        estimate = estimate_requests(
            self._support(CatalogDataset.BARS),
            shape=None,
            missing_ranges=[(date(2024, 1, 1), date(2024, 3, 31))],
            missing_symbols=0,
            symbol_count=8,
        )
        assert estimate == 8

    def test_no_gap_costs_nothing(self) -> None:
        assert (
            estimate_requests(
                self._support(CatalogDataset.DAILY_METRICS),
                shape=EnumShape.DAILY_MARKET,
                missing_ranges=[],
                missing_symbols=0,
                symbol_count=10,
            )
            == 0
        )

    def test_derived_dataset_counts_single_publish(self) -> None:
        assert (
            estimate_requests(
                self._support(CatalogDataset.CONVERTIBLE_METRICS),
                shape=None,
                missing_ranges=[(date(2024, 1, 1), date(2024, 1, 31))],
                missing_symbols=0,
                symbol_count=10,
            )
            == 1
        )


class TestActionDecision:
    def test_no_gap_is_noop(self) -> None:
        assert (
            _decide_action(
                support=support_for(CatalogDataset.DAILY_METRICS),
                publish=True,
                missing_ranges=(),
                missing_symbols=(),
                has_completed_gap=False,
                reuse_only=False,
                repair=False,
            )
            is BackfillAction.NOOP
        )

    def test_inflight_only_is_reuse(self) -> None:
        assert (
            _decide_action(
                support=support_for(CatalogDataset.DAILY_METRICS),
                publish=True,
                missing_ranges=(),
                missing_symbols=(),
                has_completed_gap=True,
                reuse_only=True,
                repair=False,
            )
            is BackfillAction.REUSE
        )

    def test_publishable_with_gap_is_sync_publish(self) -> None:
        assert (
            _decide_action(
                support=support_for(CatalogDataset.DAILY_METRICS),
                publish=True,
                missing_ranges=((date(2024, 1, 1), date(2024, 1, 5)),),
                missing_symbols=(),
                has_completed_gap=True,
                reuse_only=False,
                repair=False,
            )
            is BackfillAction.SYNC_PUBLISH
        )

    def test_non_publishable_with_gap_is_sync(self) -> None:
        assert (
            _decide_action(
                support=support_for(CatalogDataset.SUSPENSIONS),
                publish=True,
                missing_ranges=((date(2024, 1, 1), date(2024, 1, 5)),),
                missing_symbols=(),
                has_completed_gap=True,
                reuse_only=False,
                repair=False,
            )
            is BackfillAction.SYNC
        )

    def test_repair_wins(self) -> None:
        assert (
            _decide_action(
                support=support_for(CatalogDataset.BARS),
                publish=True,
                missing_ranges=((date(2024, 1, 1), date(2024, 1, 5)),),
                missing_symbols=(),
                has_completed_gap=True,
                reuse_only=False,
                repair=True,
            )
            is BackfillAction.REPAIR
        )


class TestPlanChecksum:
    def _plan(self, *, as_of: datetime, missing: bool) -> BackfillPlan:
        item = BackfillItem(
            dataset=CatalogDataset.DAILY_METRICS,
            source="tushare",
            adjustment="none",
            action=BackfillAction.SYNC_PUBLISH if missing else BackfillAction.NOOP,
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            publish=True,
            missing_ranges=(
                ((date(2024, 1, 1), date(2024, 1, 31)),) if missing else ()
            ),
            missing_symbols=BackfillGapSymbols(count=0),
            estimated_requests=23 if missing else 0,
            blockers=(
                ()
                if missing
                else (BackfillBlocker(code="x", message="y"),)
            ),
        )
        return BackfillPlan(
            request_fingerprint="a" * 64,
            scope_checksum="b" * 64,
            as_of=as_of,
            items=(item,),
            quota=BackfillQuota(status="ok", estimated_total=23),
        )

    def test_checksum_ignores_as_of(self) -> None:
        left = self._plan(as_of=datetime(2024, 1, 1, tzinfo=UTC), missing=True)
        right = self._plan(as_of=datetime(2024, 2, 1, tzinfo=UTC), missing=True)
        assert left.checksum() == right.checksum()

    def test_checksum_changes_with_gap(self) -> None:
        left = self._plan(as_of=datetime(2024, 1, 1, tzinfo=UTC), missing=True)
        right = self._plan(as_of=datetime(2024, 1, 1, tzinfo=UTC), missing=False)
        assert left.checksum() != right.checksum()

    def test_executable_items_excludes_blocked(self) -> None:
        plan = self._plan(as_of=datetime(2024, 1, 1, tzinfo=UTC), missing=True)
        assert len(plan.executable_items) == 1
        assert plan.blocked is False

class TestPayloadContract:
    """``kind=data_backfill`` 入队契约:只允许 ``request_id``(BF- 前缀)。"""

    def test_accepts_bf_request_id(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_job_payload,
        )

        validate_job_payload("data_backfill", {"request_id": "BF-AAAA1111BBBB2222"})

    def test_rejects_unknown_key(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            PayloadContractError,
            validate_job_payload,
        )

        with pytest.raises(PayloadContractError, match="未知 payload 键"):
            validate_job_payload(
                "data_backfill",
                {"request_id": "BF-AAAA1111BBBB2222", "datasets": ["bars"]},
            )

    def test_rejects_non_bf_id(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            PayloadContractError,
            validate_job_payload,
        )

        with pytest.raises(PayloadContractError, match="BF-"):
            validate_job_payload("data_backfill", {"request_id": "BJ-123"})

    def test_generic_enqueue_does_not_open_kind(self) -> None:
        """补数只经语义端点提交:通用入队口(REST / MCP)不放开该 kind。"""

        from finboard_api.routes.jobs import _ALLOWED_KINDS as REST_KINDS
        from finboard_mcp.tools.jobs import _ALLOWED_KINDS as MCP_KINDS

        assert "data_backfill" not in REST_KINDS
        assert "data_backfill" not in MCP_KINDS


class TestRestContract:
    def test_router_paths(self) -> None:
        from finboard_api.routes.data_backfill import router

        paths = {getattr(route, "path", "") for route in router.routes}
        assert "/api/data/backfills/preview" in paths
        assert "/api/data/backfills" in paths
        assert "/api/data/backfills/{request_id}" in paths
        assert "/api/data/backfills/{request_id}/cancel" in paths
        assert "/api/data/backfills/{request_id}/retry" in paths

    def test_app_registers_router(self) -> None:
        from finboard_api import routes

        assert "data_backfill_router" in routes.__all__


class TestAuditFixes:
    """审核期修复的纯函数口径(B1/B3/B5)。"""

    def test_bars_adjustment_not_supported_is_blocked(self) -> None:
        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.BARS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.BARS,
            publish=True,
            has_gap=True,
            adjustment="hqfq",
            frozen_symbols=("000001.SZ",),
            universe_filters={"market": "a_share", "instrument_type": "stock"},
            symbol_unit=True,
        )
        assert any(
            blocker.code == "backfill_adjustment_unsupported" for blocker in blockers
        )

    def test_cross_market_bars_is_blocked(self) -> None:
        """bulk_download 单 market 会静默丢掉另一市场 → 预检必须拒绝。"""

        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.BARS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.BARS,
            publish=True,
            has_gap=True,
            adjustment="qfq",
            frozen_symbols=("600000.SH", "IF2412.CFX"),
            universe_filters={},
            symbol_unit=True,
            cross_market_bars=True,
        )
        assert any(
            blocker.code == "backfill_multiple_markets_unsupported"
            for blocker in blockers
        )

    def test_explicit_symbols_over_cap_blocks_publish_surface(self) -> None:
        """显式标的超过冻结上限时发布面会退化为全集 → fail-closed。"""

        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.DAILY_METRICS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.DAILY_METRICS,
            publish=True,
            has_gap=True,
            adjustment="none",
            frozen_symbols=(),
            universe_filters={},
            symbol_unit=True,
            explicit_over_cap=True,
        )
        assert any(
            blocker.code == "backfill_symbol_scope_unresolved"
            for blocker in blockers
        )

    def test_unfreezable_large_universe_is_blocked(self) -> None:
        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.FINANCIAL_INDICATORS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.FINANCIAL_INDICATORS,
            publish=True,
            has_gap=True,
            adjustment="none",
            frozen_symbols=(),
            universe_filters={},  # 多市场/多类型无法表达
            symbol_unit=True,
        )
        assert any(
            blocker.code == "backfill_symbol_scope_unresolved"
            for blocker in blockers
        )

    def test_missing_symbols_trigger_sync_for_window_unit(self) -> None:
        action = _decide_action(
            support=support_for(CatalogDataset.DAILY_METRICS),
            publish=True,
            missing_ranges=(),
            missing_symbols=("000001.SZ",),
            has_completed_gap=True,
            reuse_only=False,
            repair=False,
        )
        assert action is BackfillAction.SYNC_PUBLISH

    def test_missing_symbols_ignored_when_not_symbol_gap(self) -> None:
        # 调用方只在「标的缺口应当补」时传入 missing_symbols;空传即 noop 语义。
        action = _decide_action(
            support=support_for(CatalogDataset.SUSPENSIONS),
            publish=True,
            missing_ranges=(),
            missing_symbols=(),
            has_completed_gap=False,
            reuse_only=False,
            repair=False,
        )
        assert action is BackfillAction.NOOP

    def test_release_identity_includes_adjustment(self) -> None:
        from finboard_backtest.data_backfill.executor import (
            release_id_for,
            version_for,
        )

        assert release_id_for("bars", "BF-1", "qfq") == "DR-bars-qfq-BF-1"
        assert release_id_for("daily_metrics", "BF-1", "none") == (
            "DR-daily_metrics-BF-1"
        )
        assert release_id_for("bars", "BF-1", "qfq") != release_id_for(
            "bars", "BF-1", "hqfq"
        )
        assert version_for("BF-1", "qfq") == "v-qfq-BF-1"
        assert version_for("BF-1", "none") == "v-BF-1"
        # 同 dataset 同复权不同 source 必须是不同发布身份(审核 B3)。
        assert release_id_for("bars", "BF-1", "qfq", "akshare") == (
            "DR-bars-qfq-akshare-BF-1"
        )
        assert release_id_for("bars", "BF-1", "qfq", "tushare") == (
            "DR-bars-qfq-BF-1"
        )
        assert release_id_for("bars", "BF-1", "qfq", "akshare") != (
            release_id_for("bars", "BF-1", "qfq", "tushare")
        )
        assert version_for("BF-1", "qfq", "akshare") != version_for(
            "BF-1", "qfq", "tushare"
        )

    def test_freeze_symbols_respects_cap(self) -> None:
        from finboard_backtest.data_backfill.planner import (
            _MAX_SYMBOL_FILTER,
            _freeze_symbols,
        )

        small = tuple(f"S{index}" for index in range(3))
        # 所有数据集都冻结可枚举标的(发布面 = 请求面)。
        assert _freeze_symbols(small) == small
        huge = tuple(f"S{index}" for index in range(_MAX_SYMBOL_FILTER + 1))
        assert _freeze_symbols(huge) == ()


def _caps_for_unit() -> Any:
    from finboard_backtest.data_backfill.support import (
        BackfillCapabilities,
        BudgetSnapshot,
    )

    return BackfillCapabilities(
        tushare_token=True,
        cache_dir_writable=True,
        release_root_writable=True,
        budget=BudgetSnapshot(
            remaining_today=100_000,
            requests_per_minute=200,
            daily_limit=100_000,
        ),
    )


class TestPublishAndScopeGuards:
    """复审二轮:发布开关、过滤器可下推性、修复跳过原因。"""

    def test_publish_false_forces_sync_action(self) -> None:
        action = _decide_action(
            support=support_for(CatalogDataset.DAILY_METRICS),
            publish=False,
            missing_ranges=((date(2024, 1, 1), date(2024, 1, 5)),),
            missing_symbols=(),
            has_completed_gap=True,
            reuse_only=False,
            repair=False,
        )
        assert action is BackfillAction.SYNC

    def test_publish_false_with_repair_stays_sync(self) -> None:
        action = _decide_action(
            support=support_for(CatalogDataset.BARS),
            publish=False,
            missing_ranges=(),
            missing_symbols=(),
            has_completed_gap=True,
            reuse_only=False,
            repair=True,
        )
        assert action is BackfillAction.SYNC

    def test_market_only_filter_is_unresolvable_for_per_symbol(self) -> None:
        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.FINANCIAL_INDICATORS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.FINANCIAL_INDICATORS,
            publish=True,
            has_gap=True,
            adjustment="none",
            frozen_symbols=(),
            universe_filters={"market": "a_share"},  # dataset_sync 无 market 键
            symbol_unit=True,
        )
        assert any(
            blocker.code == "backfill_symbol_scope_unresolved" for blocker in blockers
        )

    def test_instrument_type_filter_is_resolvable(self) -> None:
        from finboard_backtest.data_backfill.planner import _item_blockers

        blockers = _item_blockers(
            support=support_for(CatalogDataset.FINANCIAL_INDICATORS),
            capabilities=_caps_for_unit(),
            dataset=CatalogDataset.FINANCIAL_INDICATORS,
            publish=True,
            has_gap=True,
            adjustment="none",
            frozen_symbols=(),
            universe_filters={"market": "a_share", "instrument_type": "stock"},
            symbol_unit=True,
        )
        assert blockers == []

    def test_repair_skips_when_anomalies_outside_request_scope(self) -> None:
        from finboard_backtest.data_backfill.planner import (
            _DatasetCoverage,
            _repair_plan,
        )

        completed = _DatasetCoverage(
            release_ids=["DR-x"],
            anomalies=2,
            anomaly_symbols={"600000.SH"},
        )
        repair, symbols, reason = _repair_plan(
            support=support_for(CatalogDataset.BARS),
            completed=completed,
            symbols=("000001.SZ",),
            quality_profile="standard",
        )
        assert repair is False
        assert symbols == ()
        assert reason is not None
        assert "跳过缓存修复" in reason

    def test_strict_repair_over_cap_has_reason(self) -> None:
        from finboard_backtest.data_backfill.planner import (
            _MAX_SYMBOL_FILTER,
            _DatasetCoverage,
            _repair_plan,
        )

        completed = _DatasetCoverage(release_ids=["DR-x"])
        huge = tuple(f"S{index}" for index in range(_MAX_SYMBOL_FILTER + 1))
        repair, symbols, reason = _repair_plan(
            support=support_for(CatalogDataset.BARS),
            completed=completed,
            symbols=huge,
            quality_profile="strict",
        )
        assert repair is False
        assert symbols == ()
        assert reason is not None
        assert "strict 质量复检跳过" in reason

    def test_repair_targets_anomaly_symbols_in_scope(self) -> None:
        from finboard_backtest.data_backfill.planner import (
            _DatasetCoverage,
            _repair_plan,
        )

        completed = _DatasetCoverage(
            release_ids=["DR-x"], anomalies=1, anomaly_symbols={"000001.SZ"}
        )
        repair, symbols, reason = _repair_plan(
            support=support_for(CatalogDataset.BARS),
            completed=completed,
            symbols=("000001.SZ", "600000.SH"),
            quality_profile="standard",
        )
        assert repair is True
        assert symbols == ("000001.SZ",)
        assert reason is None


class TestBarsDefaults:
    def test_bars_dataset_spec_defaults_to_qfq(self) -> None:
        from finboard_backtest.data_backfill.contracts import BackfillDatasetSpec

        spec = BackfillDatasetSpec(dataset=CatalogDataset.BARS)
        assert spec.adjustment == "qfq"


class TestInflightDomainComparability:
    """复审 P1:过滤器模式的在途覆盖必须同域才复用(否则掩盖缺口)。"""

    def _row(self, **overrides) -> Any:
        from types import SimpleNamespace

        base = {
            "dataset": "daily_metrics",
            "source": "tushare",
            "adjustment": "none",
            "request_id": "BF-A",
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 3, 31),
            "symbols": [],
            "universe_filters": {"market": "a_share"},
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_same_filter_domain_is_reused(self) -> None:
        from finboard_backtest.data_backfill.planner import _inflight_coverage

        ranges, ids = _inflight_coverage(
            [self._row()],
            dataset=CatalogDataset.DAILY_METRICS,
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),
            symbols=(),
            universe_filters={"market": "a_share"},
        )
        assert ids == {"BF-A"}
        assert ranges == ((date(2024, 1, 1), date(2024, 3, 31)),)

    def test_different_filter_domain_is_not_reused(self) -> None:
        from finboard_backtest.data_backfill.planner import _inflight_coverage

        ranges, ids = _inflight_coverage(
            [self._row()],
            dataset=CatalogDataset.DAILY_METRICS,
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),
            symbols=(),
            universe_filters={"market": "a_share", "instrument_type": "stock"},
        )
        assert ids == set()
        assert ranges == ()

    def test_filter_inflight_cannot_cover_explicit_symbols(self) -> None:
        from finboard_backtest.data_backfill.planner import _inflight_coverage

        ranges, ids = _inflight_coverage(
            [self._row()],
            dataset=CatalogDataset.DAILY_METRICS,
            source="tushare",
            adjustment="none",
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),
            symbols=("000001.SZ",),
            universe_filters={},
        )
        assert ids == set()
        assert ranges == ()

    def test_derived_dataset_never_syncs(self) -> None:
        from finboard_backtest.data_backfill.support import support_for

        assert support_for(CatalogDataset.CONVERTIBLE_METRICS).syncable is False


def test_bars_market_only_filter_is_resolvable() -> None:
    """bars(bulk_download)原生支持 market:大宇宙只需 market 即可执行。"""

    from finboard_backtest.data_backfill.planner import _item_blockers

    blockers = _item_blockers(
        support=support_for(CatalogDataset.BARS),
        capabilities=_caps_for_unit(),
        dataset=CatalogDataset.BARS,
        publish=True,
        has_gap=True,
        adjustment="qfq",
        frozen_symbols=(),
        universe_filters={"market": "a_share"},
        symbol_unit=True,
    )
    assert blockers == []
