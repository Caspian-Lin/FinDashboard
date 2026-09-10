"""scope 四元组共享解析(issue #392;#385 语义,bulk_download 同函数)。"""

from __future__ import annotations

import pytest

from finboard_backtest.background_jobs.dataset_sync.scope import (
    ScopeValueError,
    normalize_sync_scope,
)

pytestmark = pytest.mark.unit


class TestNormalizeSyncScope:
    def test_empty_scope_passes_through(self) -> None:
        scope = normalize_sync_scope()
        assert scope.exchange is None
        assert scope.listing_boards == ()
        assert scope.instrument_type is None
        assert scope.symbols == ()
        assert scope.has_universe_filters is False

    def test_exchange_normalized_upper(self) -> None:
        assert normalize_sync_scope(exchange=" sse ").exchange == "SSE"
        assert normalize_sync_scope(exchange="").exchange is None
        assert normalize_sync_scope(exchange=None).exchange is None

    def test_listing_boards_normalized_lower_dedup_sorted(self) -> None:
        scope = normalize_sync_scope(
            listing_boards=["STAR", "bse", "star", "", " chinext "]
        )
        assert scope.listing_boards == ("bse", "chinext", "star")
        assert scope.has_universe_filters is True

    def test_instrument_type_normalized_lower(self) -> None:
        scope = normalize_sync_scope(instrument_type=" Stock ")
        assert scope.instrument_type == "stock"
        assert scope.has_universe_filters is True

    def test_symbols_normalized_upper_dedup_keep_order(self) -> None:
        scope = normalize_sync_scope(
            symbols=["600000.SH", "", "000001.SZ", "600000.SH"]
        )
        assert scope.symbols == ("600000.SH", "000001.SZ")
        assert scope.has_universe_filters is False  # symbols 不算宇宙过滤

    def test_non_string_exchange_rejected(self) -> None:
        with pytest.raises(ScopeValueError, match="exchange"):
            normalize_sync_scope(exchange=["SSE"])

    def test_non_list_boards_rejected(self) -> None:
        with pytest.raises(ScopeValueError, match="listing_boards"):
            normalize_sync_scope(listing_boards="star")

    def test_non_string_symbol_item_rejected(self) -> None:
        with pytest.raises(ScopeValueError, match="symbols"):
            normalize_sync_scope(symbols=[123])

    def test_all_filters_together(self) -> None:
        scope = normalize_sync_scope(
            exchange="szse",
            listing_boards=["SSE_MAIN"],
            instrument_type="ETF",
            symbols=["000001.sz"],
        )
        assert scope.exchange == "SZSE"
        assert scope.listing_boards == ("sse_main",)
        assert scope.instrument_type == "etf"
        assert scope.symbols == ("000001.SZ",)
        assert scope.has_universe_filters is True
