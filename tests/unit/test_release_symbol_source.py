"""``resolve_release_symbols`` 发布标的集来源解析的单元测试(issue #261)。

覆盖三选一语义:内联 symbols 放行、``symbols_from_release`` 复制既有可用
发布的冻结标的集(来源缺失 / 不可用 / 空清单具名拒绝)、``full_market``
按发布 kind 语义展开 instruments 表活跃标的;互斥声明兜底校验。
执行器零改动由「解析结果就是具体 symbols 列表」这一契约隐含锁定。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_persistence import (
    ReleaseSymbolSourceError,
    resolve_release_symbols,
)
from finboard_persistence.dataset_release_repo import (
    ResearchDatasetReleaseRepository,
)
from finboard_persistence.repo import InstrumentRepository


def _async_return(value: Any) -> Any:
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _release(
    *,
    instruments: tuple[dict[str, str], ...] = (),
    usable: bool = True,
    quality: str = "passed",
) -> SimpleNamespace:
    """构造最小发布领域对象替身(is_usable / quality_status / instruments)。"""
    return SimpleNamespace(
        instruments=instruments,
        is_usable=usable,
        quality_status=SimpleNamespace(value=quality),
    )


@pytest.mark.asyncio
async def test_inline_symbols_normalized_passthrough() -> None:
    # 归一化(strip+upper)但保持入参原序(不做排序,内联清单由调用方决定)。
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="a_share_tushare",
        symbols=[" 600519.sh ", "000001.SZ"],
    )
    assert result == ["600519.SH", "000001.SZ"]


@pytest.mark.asyncio
async def test_inline_symbols_empty_rejected() -> None:
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols=["  ", ""],
        )
    assert exc_info.value.code == "symbol_source_missing"


@pytest.mark.asyncio
async def test_no_source_declared_rejected() -> None:
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(None, release_kind="a_share_tushare")  # type: ignore[arg-type]
    assert exc_info.value.code == "symbol_source_missing"


@pytest.mark.asyncio
async def test_multiple_sources_rejected() -> None:
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols=["600519.SH"],
            full_market=True,
        )
    assert exc_info.value.code == "symbol_source_ambiguous"


@pytest.mark.asyncio
async def test_from_release_copies_frozen_symbol_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复制语义:标的集与来源发布冻结清单一致(排序稳定)。"""

    async def _fake_get(self: Any, release_id: str) -> Any:
        assert release_id == "RL-SRC-1"
        return _release(
            instruments=(
                {"code": "600519.SH"},
                {"code": "000001.SZ"},
                {"code": "300750.SZ"},
            )
        )

    monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="daily_metrics",
        symbols_from_release="RL-SRC-1",
    )
    assert result == ["000001.SZ", "300750.SZ", "600519.SH"]


@pytest.mark.asyncio
async def test_from_release_not_found_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(self: Any, release_id: str) -> Any:
        return None

    monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols_from_release="RL-MISSING",
        )
    assert exc_info.value.code == "source_release_not_found"


@pytest.mark.asyncio
async def test_from_release_not_usable_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(self: Any, release_id: str) -> Any:
        return _release(
            instruments=({"code": "600519.SH"},),
            usable=False,
            quality="failed",
        )

    monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols_from_release="RL-BAD",
        )
    assert exc_info.value.code == "source_release_not_usable"
    assert "failed" in exc_info.value.summary


@pytest.mark.asyncio
async def test_from_release_empty_members_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(self: Any, release_id: str) -> Any:
        return _release()

    monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            symbols_from_release="RL-EMPTY",
        )
    assert exc_info.value.code == "source_release_empty"


@pytest.mark.asyncio
async def test_full_market_stock_kind_expands_a_share_stocks_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        **_kw: Any,
    ) -> list[str]:
        calls.append((market, instrument_type))
        return {"stock": ["000001.SZ", "600519.SH"]}.get(instrument_type or "", [])

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind="a_share_tushare",
        full_market=True,
    )
    assert result == ["000001.SZ", "600519.SH"]
    assert calls == [("a_share", "stock")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_kind",
    ["multi_asset_mixed"],
)
async def test_full_market_mixed_expands_stock_etf_index_convertible_futures(
    monkeypatch: pytest.MonkeyPatch,
    release_kind: str,
) -> None:
    """#265 mixed 全市场展开并入 convertible;#267 再并入期货主连。"""
    requested_types: list[str | None] = []

    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        **_kw: Any,
    ) -> list[str]:
        requested_types.append(instrument_type)
        return {
            "stock": ["600519.SH"],
            "etf": ["510300.SH"],
            "index": ["000300.SH"],
            "convertible": ["113050.SH"],
            "futures": ["IF0.CFFEX"],
        }.get(instrument_type or "", [])

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    result = await resolve_release_symbols(
        None,  # type: ignore[arg-type]
        release_kind=release_kind,
        full_market=True,
    )
    assert result == [
        "000300.SH",
        "113050.SH",
        "510300.SH",
        "600519.SH",
        "IF0.CFFEX",
    ]
    assert requested_types == ["stock", "etf", "index", "convertible", "futures"]


@pytest.mark.asyncio
async def test_full_market_empty_pool_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list_codes(
        self: Any,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        **_kw: Any,
    ) -> list[str]:
        return []

    monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
    with pytest.raises(ReleaseSymbolSourceError) as exc_info:
        await resolve_release_symbols(
            None,  # type: ignore[arg-type]
            release_kind="a_share_tushare",
            full_market=True,
        )
    assert exc_info.value.code == "full_market_empty"
