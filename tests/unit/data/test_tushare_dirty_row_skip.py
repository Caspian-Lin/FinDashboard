"""tushare 全市场档案脏行行级跳过契约(2026-09-09 T600018.SH 事故)。

tushare 退市档案含历史前缀代码(T600018.SH 上港集箱退)、namechange 含
X 前缀代码(X19363.SH),旧契约整批拒绝会让 research_data_sync 在 profiles
第一步即崩且每次重试必然复现。现契约:单行违规跳过并具名告警
``tushare.dirty_row_skipped``;批级护栏(全部行被跳过、截断防护、
list_status 一致性)仍 fail-closed。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import finboard_data.tushare_provider as tushare_provider_module
from finboard_data import (
    ResearchDataContractError,
    TushareResearchDataProvider,
)

OBSERVED_AT = datetime(2026, 9, 9, 2, 0, tzinfo=UTC)


class _WarningCapture:
    """替换模块 logger;structlog LogCapture 无 warning 便利方法,桩更直接。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})


class NoopBudget:
    async def acquire(self) -> None:
        return None


class _ArchiveClient:
    """仅实现档案 / 名称变更两个接口的离线 client。"""

    def __init__(self) -> None:
        self.stock_rows: list[dict[str, object]] = []
        self.namechange_rows: list[dict[str, object]] = []

    def stock_basic(self, **kwargs: str) -> object:
        return self.stock_rows

    def daily_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def fina_indicator(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def index_member_all(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def namechange(self, **kwargs: str) -> object:
        return self.namechange_rows

    def index_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def cb_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def suspend_d(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")


    def income(self, **kwargs: str) -> object:
        """调用 ``income``(#397;本测试不触达,返回空)。"""
        return []

    def balancesheet(self, **kwargs: str) -> object:
        """调用 ``balancesheet``(#397;本测试不触达,返回空)。"""
        return []

    def cashflow(self, **kwargs: str) -> object:
        """调用 ``cashflow``(#397;本测试不触达,返回空)。"""
        return []

    def dividend(self, **kwargs: str) -> object:
        """调用 ``dividend``(#397;本测试不触达,返回空)。"""
        return []


def _provider(client: _ArchiveClient) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client,
        now=lambda: OBSERVED_AT,
        budget=NoopBudget(),
    )


def _stock_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "name": "平安银行",
        "industry": "银行",
        "market": "主板",
        "exchange": "SZSE",
        "list_status": "L",
        "list_date": "19910403",
        "delist_date": "",
    }
    row.update(overrides)
    return row


def _namechange_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "name": "平安银行",
        "start_date": "19910403",
        "end_date": "",
        "change_reason": "其他",
    }
    row.update(overrides)
    return row


def _dirty_delisted_row() -> dict[str, object]:
    """事故复现行:tushare 退市档案原文(上港集箱退,2006 退市)。"""
    return _stock_row(
        ts_code="T600018.SH",
        name="上港集箱(退)",
        market="主板",
        list_date="20000719",
        delist_date="20061020",
    )


@pytest.mark.unit
async def test_profiles_skip_dirty_delisted_symbol_with_named_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ArchiveClient()
    client.stock_rows = [_stock_row(), _dirty_delisted_row()]
    capture = _WarningCapture()
    monkeypatch.setattr(tushare_provider_module, "logger", capture)

    profiles = await _provider(client).fetch_instrument_profiles()

    assert [item.symbol for item in profiles] == ["000001.SZ"]
    skipped = [entry for entry in capture.entries if entry["event"] == "tushare.dirty_row_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["endpoint"] == "stock_basic"
    assert skipped[0]["ts_code"] == "T600018.SH"
    assert skipped[0]["name"] == "上港集箱(退)"
    reason = skipped[0]["reason"]
    assert isinstance(reason, str)
    assert "6 位数字" in reason


@pytest.mark.unit
async def test_profiles_skip_row_without_symbol_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ArchiveClient()
    client.stock_rows = [_stock_row(), _stock_row(ts_code="", name="无码行")]
    capture = _WarningCapture()
    monkeypatch.setattr(tushare_provider_module, "logger", capture)

    profiles = await _provider(client).fetch_instrument_profiles()

    assert [item.symbol for item in profiles] == ["000001.SZ"]
    skipped = [entry for entry in capture.entries if entry["event"] == "tushare.dirty_row_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["ts_code"] == ""


@pytest.mark.unit
async def test_profiles_all_dirty_rows_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ArchiveClient()
    client.stock_rows = [_dirty_delisted_row()]
    monkeypatch.setattr(tushare_provider_module, "logger", _WarningCapture())

    with pytest.raises(ResearchDataContractError, match="全部 1 行均违反契约"):
        await _provider(client).fetch_instrument_profiles()


@pytest.mark.unit
async def test_profiles_list_status_mismatch_still_rejected() -> None:
    client = _ArchiveClient()
    client.stock_rows = [_stock_row(list_status="D")]

    with pytest.raises(ResearchDataContractError, match="请求状态之外"):
        await _provider(client).fetch_instrument_profiles()


@pytest.mark.unit
async def test_namechange_skip_dirty_prefix_code_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实脏行 X19363.SH 在 namechange 分页流里被跳过,分页护栏不受影响。"""
    client = _ArchiveClient()
    client.namechange_rows = [_namechange_row(), _namechange_row(ts_code="X19363.SH")]
    capture = _WarningCapture()
    monkeypatch.setattr(tushare_provider_module, "logger", capture)

    changes = await _provider(client).fetch_name_changes()

    assert [(item.symbol, item.name) for item in changes] == [("000001.SZ", "平安银行")]
    skipped = [entry for entry in capture.entries if entry["event"] == "tushare.dirty_row_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["endpoint"] == "namechange"
    assert skipped[0]["ts_code"] == "X19363.SH"


@pytest.mark.unit
async def test_namechange_all_dirty_page_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ArchiveClient()
    client.namechange_rows = [_namechange_row(ts_code="X19363.SH")]
    monkeypatch.setattr(tushare_provider_module, "logger", _WarningCapture())

    with pytest.raises(ResearchDataContractError, match="均违反契约"):
        await _provider(client).fetch_name_changes()
