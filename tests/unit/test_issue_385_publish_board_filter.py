"""dataset_release_publish full_market 展开 exchange / listing_boards 过滤(issue #385)。

事故:`a-share-full-20260908-v4` full_market 展开无法剔除北交所,publish 端
没有任何板块/交易所过滤参数,而 bulk_download 有——本组测试锁定对齐后的
语义:仅 full_market 模式生效、归一化(exchange 大写 / boards 小写去重)、
与其他标的来源混用具名拒绝、过滤透传 `InstrumentRepository.list_codes`。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from finboard_persistence import (
    ReleaseSymbolSourceError,
    resolve_release_symbols,
)
from finboard_persistence.repo import InstrumentRepository


def _create_base() -> dict[str, Any]:
    return {
        "release_id": "api-r385-v1",
        "version": "v1",
        "start_date": date(2024, 1, 2),
        "end_date": date(2024, 1, 5),
    }


# --------------------------------------------------------------------------- #
# schema 层:归一化 + 与其他标的来源互斥
# --------------------------------------------------------------------------- #


def test_schema_normalizes_exchange_and_boards() -> None:
    from finboard_api.schemas import ResearchDatasetReleaseCreate

    body = ResearchDatasetReleaseCreate(
        **_create_base(),
        full_market=True,
        exchange=" sse ",
        listing_boards=[" BSE ", "sse_main", "SSE_MAIN", "", "  "],
    )
    assert body.exchange == "SSE"
    # 小写归一 + 去重 + 排序(确定性 payload)。
    assert body.listing_boards == ["bse", "sse_main"]


def test_schema_filter_with_inline_symbols_rejected() -> None:
    from finboard_api.schemas import ResearchDatasetReleaseCreate

    with pytest.raises(ValidationError, match="仅支持 full_market"):
        ResearchDatasetReleaseCreate(
            **_create_base(),
            symbols=["600519.SH"],
            listing_boards=["sse_main"],
        )


def test_schema_filter_with_from_release_rejected() -> None:
    from finboard_api.schemas import ResearchDatasetReleaseCreate

    with pytest.raises(ValidationError, match="仅支持 full_market"):
        ResearchDatasetReleaseCreate(
            **_create_base(),
            symbols_from_release="RL-SRC",
            exchange="SSE",
        )


def test_schema_filter_with_full_market_allowed() -> None:
    from finboard_api.schemas import ResearchDatasetReleaseCreate

    body = ResearchDatasetReleaseCreate(
        **_create_base(),
        full_market=True,
        exchange="SZSE",
        listing_boards=["szse_main", "chinext"],
    )
    assert body.full_market is True
    assert body.listing_boards == ["chinext", "szse_main"]


# --------------------------------------------------------------------------- #
# resolve_release_symbols:互斥兜底 + 过滤透传
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_filter_with_inline_symbols_rejected() -> None:
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols=["600519.SH"],
            listing_boards=["bse"],
        )
    assert exc_info.value.code == "symbol_filter_requires_full_market"


@pytest.mark.asyncio
async def test_resolve_filter_with_from_release_rejected() -> None:
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols_from_release="RL-SRC",
            exchange="SSE",
        )
    assert exc_info.value.code == "symbol_filter_requires_full_market"


@pytest.mark.asyncio
async def test_resolve_forwards_normalized_filters_to_list_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        **kwargs: Any,
    ) -> list[str]:
        captured.append({"market": market, "instrument_type": instrument_type, **kwargs})
        return {"stock": ["600519.SH"], "etf": ["510300.SH"]}.get(instrument_type or "", [])

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="multi_asset_mixed",
        full_market=True,
        exchange=" sse ",
        listing_boards=[" SSE_MAIN ", "UNKNOWN", ""],
    )
    assert result == ["510300.SH", "600519.SH"]
    # mixed 五类逐类型查询都带上归一化后的过滤(exchange 大写 / boards 小写)。
    assert captured
    for call in captured:
        assert call["exchange"] == "SSE"
        assert call["listing_boards"] == ["sse_main", "unknown"]


@pytest.mark.asyncio
async def test_resolve_board_filter_excludes_bse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全市场股票发布剔除北交所的验收场景:listing_boards 不含 bse。"""

    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
    ) -> list[str]:
        assert instrument_type == "stock"
        codes_by_board = {
            "sse_main": ["600519.SH"],
            "szse_main": ["000001.SZ"],
            "bse": ["920023.BJ", "834799.BJ"],
        }
        if listing_boards is None:
            return sorted(code for codes in codes_by_board.values() for code in codes)
        return sorted(
            code
            for board, codes in codes_by_board.items()
            if board in listing_boards
            for code in codes
        )

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="a_share_tushare",
        full_market=True,
        listing_boards=["sse_main", "szse_main", "star", "chinext", "cdr"],
    )
    assert result == ["000001.SZ", "600519.SH"]
    assert not any(code.endswith(".BJ") for code in result)


@pytest.mark.asyncio
async def test_resolve_blank_filters_are_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全空白过滤参数归一化后等价未声明,不产生过滤语义。"""
    captured: list[dict[str, Any]] = []

    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        **kwargs: Any,
    ) -> list[str]:
        captured.append(kwargs)
        return ["600519.SH"]

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="a_share_tushare",
        full_market=True,
        exchange="   ",
        listing_boards=["", "   "],
    )
    assert result == ["600519.SH"]
    assert captured == [{"exchange": None, "listing_boards": None}]
