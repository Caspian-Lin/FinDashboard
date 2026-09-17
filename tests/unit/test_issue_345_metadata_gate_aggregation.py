"""发布元数据门聚合报告(issue #345)单元测试。

``ReleaseInstrumentCatalogRepository.list_candidates`` 此前对 ETF 分类
元数据错误「首错即拒」:循环内第一只缺元数据 / 待复核的 ETF 当场 raise,
操作者只能「修一个换一个」(2026-09-06 现网三连拒事故)。本组测试锁定
聚合语义:

* 多只 ETF 元数据失败(缺元数据 / needs_review / 非法 execution_profile)
  一次性抛出,错误文本含全部标的与各自原因(含 1 只正常 ETF 的放行);
* ETF 失败与「完全缺 #35/#58 元数据」标的合并进同一条错误,一次给全
  完整缺口清单;
* 单只失败与全部通过路径行为不变,fail-visible 语义零放松。

元数据表读取全部 monkeypatch(跟随 ``test_release_symbol_source.py`` 的
替身风格),不触真实数据库。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_data.releases import ReleaseCapabilityError
from finboard_persistence.dataset_release_repo import (
    ReleaseInstrumentCatalogRepository,
)
from finboard_persistence.profile_metadata import ProfileMetadataLookup


def _instrument_row(code: str) -> SimpleNamespace:
    """构造 instruments 表行的最小替身(仅 _etf_candidate 消费的字段)。"""
    return SimpleNamespace(
        code=code,
        name=f"样本{code}",
        market="a_share",
        instrument_type="etf",
        exchange="SSE" if code.endswith(".SH") else "SZSE",
        list_date=date(2024, 1, 1),
        delist_date=None,
        industry=None,
        status="active",
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _etf_metadata_row(
    *,
    review_status: str,
    execution_profile: str | None,
    underlying_asset_class: str = "equity",
    category: str | None = None,
) -> SimpleNamespace:
    """构造 etf_metadata 表行的最小替身(仅 _resolve_etf_classification 消费的字段)。"""
    return SimpleNamespace(
        review_status=review_status,
        execution_profile=execution_profile,
        underlying_asset_class=underlying_asset_class,
        category=category,
        listing_date=date(2024, 1, 1),
    )


def _install_maps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instruments: dict[str, SimpleNamespace],
    etf_rows: dict[str, SimpleNamespace],
) -> None:
    """把 catalog 仓储的全部元数据读取替换为内存替身(session 传 None)。"""

    async def _instrument_map(self: Any, symbols: list[str]) -> dict[str, Any]:
        return instruments

    async def _etf_map(self: Any, symbols: list[str]) -> dict[str, Any]:
        return etf_rows

    async def _convertible_map(self: Any, symbols: list[str]) -> dict[str, Any]:
        return {}

    async def _futures_map(self: Any, symbols: list[str]) -> dict[str, Any]:
        return {}

    async def _name_history(self: Any, symbols: list[str]) -> dict[str, Any]:
        return {}

    async def _events(self: Any, symbols: list[str]) -> dict[str, Any]:
        return {}

    async def _profiles(self: Any, symbols: Any, **_kw: Any) -> dict[str, Any]:
        return {}

    repo = ReleaseInstrumentCatalogRepository
    monkeypatch.setattr(repo, "_instrument_map", _instrument_map)
    monkeypatch.setattr(repo, "_etf_map", _etf_map)
    monkeypatch.setattr(repo, "_convertible_map", _convertible_map)
    monkeypatch.setattr(repo, "_futures_map", _futures_map)
    monkeypatch.setattr(repo, "_name_history", _name_history)
    monkeypatch.setattr(repo, "_events", _events)
    monkeypatch.setattr(ProfileMetadataLookup, "profiles", _profiles)


@pytest.mark.asyncio
async def test_three_etfs_missing_metadata_reported_in_one_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 只 ETF 全部缺分类元数据:一次抛出,文本含全部标的与原因。"""
    codes = ["158000.SZ", "560650.SH", "588080.SH"]
    _install_maps(
        monkeypatch,
        instruments={code: _instrument_row(code) for code in codes},
        etf_rows={},
    )

    with pytest.raises(ReleaseCapabilityError) as exc_info:
        await ReleaseInstrumentCatalogRepository(None).list_candidates(codes)  # type: ignore[arg-type]

    text = str(exc_info.value)
    assert "以下 3 个标的发布元数据缺失/待复核,禁止猜测:" in text
    for code in codes:
        assert f"{code}: ETF 缺少分类元数据" in text


@pytest.mark.asyncio
async def test_mixed_etf_metadata_failures_and_pass_reported_in_one_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺元数据 / 待复核 / 非法 execution_profile 三类失败 + 1 只正常放行。"""
    missing_code = "158000.SZ"
    needs_review_code = "560650.SH"
    invalid_profile_code = "588080.SH"
    good_code = "159010.SZ"
    codes = [missing_code, needs_review_code, invalid_profile_code, good_code]
    _install_maps(
        monkeypatch,
        instruments={code: _instrument_row(code) for code in codes},
        etf_rows={
            needs_review_code: _etf_metadata_row(
                review_status="needs_review",
                execution_profile="domestic_equity_etf",
            ),
            invalid_profile_code: _etf_metadata_row(
                review_status="auto_adopted",
                execution_profile="bogus_profile",
            ),
            good_code: _etf_metadata_row(
                review_status="auto_adopted",
                execution_profile="cross_border_etf",
            ),
        },
    )

    with pytest.raises(ReleaseCapabilityError) as exc_info:
        await ReleaseInstrumentCatalogRepository(None).list_candidates(codes)  # type: ignore[arg-type]

    text = str(exc_info.value)
    assert f"{missing_code}: ETF 缺少分类元数据" in text
    assert f"{needs_review_code}: ETF 分类待复核" in text
    assert f"{invalid_profile_code}: ETF execution_profile 无效" in text
    # 计数只含失败标的:正常 ETF 的存在不影响聚合报告的口径。
    assert "以下 3 个标的发布元数据缺失/待复核" in text


@pytest.mark.asyncio
async def test_missing_symbols_and_etf_failures_merged_in_one_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 只 ETF 缺元数据 + 1 只完全缺 #35/#58 元数据:全部 4 个信息一条抛出。"""
    etf_codes = ["158000.SZ", "560650.SH"]
    unknown_code = "TST999.SH"
    _install_maps(
        monkeypatch,
        instruments={code: _instrument_row(code) for code in etf_codes},
        etf_rows={},
    )

    with pytest.raises(ReleaseCapabilityError) as exc_info:
        await ReleaseInstrumentCatalogRepository(None).list_candidates(  # type: ignore[arg-type]
            [*etf_codes, unknown_code]
        )

    text = str(exc_info.value)
    # 两段缺口合并进同一条错误,而不是「修一个换一个」。
    assert f"以下标的缺少 #35/#58 元数据,禁止猜测: {unknown_code}" in text
    assert "以下 2 个标的发布元数据缺失/待复核,禁止猜测:" in text
    for code in etf_codes:
        assert f"{code}: ETF 缺少分类元数据" in text


@pytest.mark.asyncio
async def test_single_etf_failure_message_still_names_code_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单只失败:文本同样含标的与原因(聚合格式,信息量不减)。"""
    code = "158000.SZ"
    _install_maps(
        monkeypatch,
        instruments={code: _instrument_row(code)},
        etf_rows={},
    )

    with pytest.raises(ReleaseCapabilityError) as exc_info:
        await ReleaseInstrumentCatalogRepository(None).list_candidates([code])  # type: ignore[arg-type]

    assert str(exc_info.value) == (
        "以下 1 个标的发布元数据缺失/待复核,禁止猜测: "
        f"{code}: ETF 缺少分类元数据"
    )


@pytest.mark.asyncio
async def test_all_pass_returns_candidates_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部通过路径行为不变:返回候选,fail-visible 语义零放松。"""
    good_code = "159010.SZ"
    _install_maps(
        monkeypatch,
        instruments={good_code: _instrument_row(good_code)},
        etf_rows={
            good_code: _etf_metadata_row(
                review_status="auto_adopted",
                execution_profile="cross_border_etf",
            ),
        },
    )

    result = await ReleaseInstrumentCatalogRepository(None).list_candidates([good_code])  # type: ignore[arg-type]

    assert [item.code for item in result] == [good_code]
