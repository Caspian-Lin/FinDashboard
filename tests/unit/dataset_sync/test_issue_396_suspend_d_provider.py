"""issue #396:suspend_d provider 解析与停复牌质量门。

* ``TushareResearchDataProvider.fetch_suspensions``(按日全市场,suspend_d):
  suspend_type/timing → suspend_kind 词表、PIT=当日(09:30 上海)、交易日
  一致性、截断护栏、行级跳过口径;
* ``ResearchDataQualityValidator.validate_suspensions``:kind/type 矛盾、
  available_at 早于交易日、交易日不一致均 ERROR。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

import finboard_data.tushare_provider as tushare_provider_module
from finboard_data import TushareResearchDataProvider
from finboard_data.quality import (
    QualityStatus,
    ResearchDataQualityValidator,
)
from finboard_data.research import (
    ResearchDataContractError,
    SuspensionRecord,
)

pytestmark = pytest.mark.unit

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_OBSERVED_AT = datetime(2026, 9, 9, 2, 0, tzinfo=UTC)
_TRADE_DATE = date(2026, 7, 24)


class _WarningCapture:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})


class NoopBudget:
    async def acquire(self) -> None:
        return None


class _Client:
    """可配置 suspend_d 原始行的离线 client;其他接口被调用即失败。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def index_basic(self, **kwargs: str) -> list[dict[str, object]]:
        return []

    def suspend_d(self, **kwargs: str) -> list[dict[str, object]]:
        assert kwargs["trade_date"] == "20260724"
        assert kwargs["fields"] == "ts_code,trade_date,suspend_timing,suspend_type"
        return list(self._rows)

    # 协议要求的其余接口;本测试不应触达。
    def daily_basic(self, **kwargs: str) -> object:
        raise AssertionError("daily_basic 不应被调用")

    def stock_basic(self, **kwargs: str) -> object:
        raise AssertionError("stock_basic 不应被调用")

    def fina_indicator(self, **kwargs: str) -> object:
        raise AssertionError("fina_indicator 不应被调用")

    def index_member_all(self, **kwargs: str) -> object:
        raise AssertionError("index_member_all 不应被调用")

    def namechange(self, **kwargs: str) -> object:
        raise AssertionError("namechange 不应被调用")

    def cb_basic(self, **kwargs: str) -> object:
        raise AssertionError("cb_basic 不应被调用")

    def income(self, **kwargs: str) -> object:
        raise AssertionError("income 不应被调用")

    def balancesheet(self, **kwargs: str) -> object:
        raise AssertionError("balancesheet 不应被调用")

    def cashflow(self, **kwargs: str) -> object:
        raise AssertionError("cashflow 不应被调用")

    def dividend(self, **kwargs: str) -> object:
        raise AssertionError("dividend 不应被调用")

def _provider(client: _Client) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client,
        now=lambda: _OBSERVED_AT,
        budget=NoopBudget(),
    )


def _suspend_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "trade_date": "20260724",
        "suspend_timing": "",
        "suspend_type": "S",
    }
    row.update(overrides)
    return row


class TestFetchSuspensions:
    async def test_full_day_intraday_and_resumption_kinds(self) -> None:
        client = _Client(
            [
                _suspend_row(ts_code="000001.SZ", suspend_type="S"),
                _suspend_row(ts_code="000002.SZ", suspend_type="S", suspend_timing="盘中停牌"),
                _suspend_row(ts_code="600000.SH", suspend_type="R"),
            ]
        )
        records = await _provider(client).fetch_suspensions(_TRADE_DATE)
        by_symbol = {item.symbol: item for item in records}
        assert by_symbol["000001.SZ"].suspend_kind == "suspension_day"
        assert by_symbol["000002.SZ"].suspend_kind == "intraday_suspension"
        assert by_symbol["600000.SH"].suspend_kind == "resumption"
        # 词表与缓存侧 TushareLifecycleEvent.event_type 对齐(对账口径)。
        assert by_symbol["000001.SZ"].suspend_type == "S"
        assert by_symbol["600000.SH"].suspend_timing is None

    async def test_available_at_is_same_day_0930_shanghai(self) -> None:
        records = await _provider(_Client([_suspend_row()])).fetch_suspensions(
            _TRADE_DATE
        )
        assert records[0].available_at == datetime(2026, 7, 24, 9, 30, tzinfo=_SHANGHAI)
        assert records[0].observed_at == _OBSERVED_AT
        assert records[0].source == "tushare"

    async def test_wrong_trade_date_rejected(self) -> None:
        client = _Client([_suspend_row(trade_date="20260723")])
        with pytest.raises(ResearchDataContractError, match="交易日之外"):
            await _provider(client).fetch_suspensions(_TRADE_DATE)

    async def test_invalid_suspend_type_rejected_by_default(self) -> None:
        client = _Client([_suspend_row(suspend_type="X")])
        with pytest.raises(ResearchDataContractError, match="suspend_type"):
            await _provider(client).fetch_suspensions(_TRADE_DATE)

    async def test_skip_policy_skips_dirty_row_with_named_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client(
            [
                _suspend_row(ts_code="bad code"),
                _suspend_row(ts_code="600000.SH"),
            ]
        )
        capture = _WarningCapture()
        monkeypatch.setattr(tushare_provider_module, "logger", capture)
        records = await _provider(client).fetch_suspensions(
            _TRADE_DATE, dirty_row_policy="skip"
        )
        assert [item.symbol for item in records] == ["600000.SH"]
        skipped = [
            e for e in capture.entries if e["event"] == "tushare.dirty_row_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["endpoint"] == "suspend_d"

    async def test_skip_policy_all_dirty_rows_still_rejected(self) -> None:
        client = _Client([_suspend_row(ts_code="bad code")])
        with pytest.raises(ResearchDataContractError, match="均违反契约"):
            await _provider(client).fetch_suspensions(
                _TRADE_DATE, dirty_row_policy="skip"
            )

    async def test_empty_response_is_empty_list(self) -> None:
        # 非交易日上游空响应 → 空列表(框架按空切片跳过,不产生批次行)。
        assert await _provider(_Client([])).fetch_suspensions(_TRADE_DATE) == []


class TestSuspensionsQualityGate:
    def _validator(self) -> ResearchDataQualityValidator:
        return ResearchDataQualityValidator(now=_OBSERVED_AT)

    def _record(self, **overrides: object) -> SuspensionRecord:
        fields: dict[str, object] = {
            "symbol": "000001.SZ",
            "trade_date": _TRADE_DATE,
            "suspend_kind": "suspension_day",
            "suspend_type": "S",
            "suspend_timing": None,
            "source": "tushare",
            "observed_at": _OBSERVED_AT,
            "available_at": datetime(2026, 7, 24, 9, 30, tzinfo=_SHANGHAI),
        }
        fields.update(overrides)
        return SuspensionRecord(
            symbol=cast(str, fields["symbol"]),
            trade_date=cast(date, fields["trade_date"]),
            suspend_kind=cast(str, fields["suspend_kind"]),
            suspend_type=cast(str, fields["suspend_type"]),
            suspend_timing=cast("str | None", fields["suspend_timing"]),
            source=cast(str, fields["source"]),
            observed_at=cast(datetime, fields["observed_at"]),
            available_at=cast(datetime, fields["available_at"]),
        )

    def test_clean_batch_passes(self) -> None:
        report = self._validator().validate_suspensions(
            [self._record()],
            expected_trade_date=_TRADE_DATE,
            expected_source="tushare",
        )
        assert report.status is QualityStatus.PASSED
        assert report.publishable

    def test_kind_type_contradiction_rejected(self) -> None:
        report = self._validator().validate_suspensions(
            [self._record(suspend_type="R", suspend_kind="suspension_day")],
            expected_trade_date=_TRADE_DATE,
            expected_source="tushare",
        )
        assert report.status is QualityStatus.FAILED
        assert any(issue.code == "invalid_suspend_kind" for issue in report.issues)

    def test_available_at_before_trade_date_rejected(self) -> None:
        report = self._validator().validate_suspensions(
            [
                self._record(
                    available_at=datetime(2026, 7, 23, 9, 30, tzinfo=_SHANGHAI)
                )
            ],
            expected_trade_date=_TRADE_DATE,
            expected_source="tushare",
        )
        assert report.status is QualityStatus.FAILED
        assert any(issue.code == "invalid_available_at" for issue in report.issues)

    def test_trade_date_mismatch_rejected(self) -> None:
        report = self._validator().validate_suspensions(
            [self._record(trade_date=date(2026, 7, 23))],
            expected_trade_date=_TRADE_DATE,
            expected_source="tushare",
        )
        assert report.status is QualityStatus.FAILED
        assert any(
            issue.code == "business_date_mismatch" for issue in report.issues
        )
