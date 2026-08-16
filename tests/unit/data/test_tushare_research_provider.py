"""Tushare 时点化研究数据契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from finboard_data import (
    ResearchDataConfigurationError,
    ResearchDataContractError,
    ResearchDataDependencyError,
    ResearchDataProvider,
    ResearchDataUpstreamError,
    TushareResearchDataProvider,
)

OBSERVED_AT = datetime(2026, 7, 27, 2, 30, tzinfo=UTC)


class NoopBudget:
    async def acquire(self) -> None:
        return None


def _daily_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "trade_date": "20260724",
        "close": 12.34,
        "turnover_rate": 2.5,
        "turnover_rate_f": 3.75,
        "volume_ratio": 1.2,
        "pe": None,
        "pe_ttm": float("nan"),
        "pb": 1.1,
        "ps": 2.2,
        "ps_ttm": 2.3,
        "dv_ratio": 1.5,
        "dv_ttm": 1.8,
        "total_share": 100,
        "float_share": 80,
        "free_share": 60,
        "total_mv": 12345.67,
        "circ_mv": 9876.54,
        "limit_status": 1,
    }
    row.update(overrides)
    return row


def _financial_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20260425",
        "end_date": "20260331",
        "update_flag": "1",
        "eps": 0.4,
        "dt_eps": 0.4,
        "bps": 21.5,
        "ocfps": 0.8,
        "roe": 3.2,
        "roe_waa": 3.1,
        "grossprofit_margin": 42.5,
        "netprofit_margin": 18.4,
        "debt_to_assets": 91.2,
        "tr_yoy": 6.5,
        "netprofit_yoy": -2.5,
        "ocf_yoy": 8.1,
    }
    row.update(overrides)
    return row


def _industry_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "l1_code": "801780.SI",
        "l1_name": "银行",
        "l2_code": "801192.SI",
        "l2_name": "股份制银行Ⅱ",
        "l3_code": "851911.SI",
        "l3_name": "股份制银行Ⅲ",
        "ts_code": "000001.SZ",
        "name": "平安银行",
        "in_date": "20211213",
        "out_date": "",
        "is_new": "Y",
    }
    row.update(overrides)
    return row


class FakeTushareClient:
    """记录调用参数并返回离线记录。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.stock_rows: list[dict[str, object]] = [
            {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SZSE",
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": "",
            }
        ]
        self.daily_rows = [_daily_row()]
        self.financial_rows = [_financial_row()]
        self.industry_rows = [_industry_row()]

    def stock_basic(self, **kwargs: str) -> object:
        self.calls.append(("stock_basic", kwargs))
        return self.stock_rows

    def daily_basic(self, **kwargs: str) -> object:
        self.calls.append(("daily_basic", kwargs))
        return self.daily_rows

    def fina_indicator(self, **kwargs: str) -> object:
        self.calls.append(("fina_indicator", kwargs))
        return self.financial_rows

    def index_member_all(self, **kwargs: str) -> object:
        self.calls.append(("index_member_all", kwargs))
        return self.industry_rows


def _provider(
    client: FakeTushareClient | None = None,
) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client or FakeTushareClient(),
        now=lambda: OBSERVED_AT,
        budget=NoopBudget(),
    )


@pytest.mark.unit
def test_provider_satisfies_protocol() -> None:
    assert isinstance(_provider(), ResearchDataProvider)


@pytest.mark.unit
async def test_fetches_instrument_profile_as_observed_snapshot() -> None:
    client = FakeTushareClient()
    profile = (await _provider(client).fetch_instrument_profiles())[0]

    assert profile.symbol == "000001.SZ"
    assert profile.list_date == date(1991, 4, 3)
    assert profile.delist_date is None
    assert profile.industry == "银行"
    assert profile.available_at == OBSERVED_AT
    assert client.calls[0][1]["list_status"] == "L"


@pytest.mark.unit
async def test_daily_metrics_normalize_units_missing_values_and_availability() -> None:
    metric = (await _provider().fetch_daily_metrics(date(2026, 7, 24)))[0]

    assert metric.turnover_rate == Decimal("0.025")
    assert metric.turnover_rate_free == Decimal("0.0375")
    assert metric.pe is None
    assert metric.pe_ttm is None
    assert metric.total_shares == Decimal("1000000")
    assert metric.total_market_cap == Decimal("123456700")
    assert metric.available_at.isoformat() == "2026-07-24T17:00:00+08:00"
    with pytest.raises(FrozenInstanceError):
        metric.__setattr__("pb", Decimal("2"))


@pytest.mark.unit
async def test_financial_indicator_preserves_revision_and_uses_next_day() -> None:
    client = FakeTushareClient()
    client.financial_rows = [
        _financial_row(update_flag="0", eps=0.3),
        _financial_row(ann_date="20260426", update_flag="1", eps=0.4),
    ]

    indicators = await _provider(client).fetch_financial_indicators(
        "000001.sz",
        start_period=date(2026, 1, 1),
        end_period=date(2026, 3, 31),
    )

    assert [item.update_flag for item in indicators] == ["0", "1"]
    assert indicators[0].return_on_equity == Decimal("0.032")
    assert indicators[0].revenue_yoy == Decimal("0.065")
    assert indicators[0].available_at.isoformat() == "2026-04-26T00:00:00+08:00"
    assert client.calls[0][1]["start_date"] == "20260101"
    assert client.calls[0][1]["end_date"] == "20260331"


@pytest.mark.unit
async def test_industry_membership_keeps_effective_interval_but_is_observed_now() -> None:
    client = FakeTushareClient()
    membership = (await _provider(client).fetch_industry_memberships(symbol="000001.SZ"))[0]

    assert membership.taxonomy == "SW2021"
    assert membership.level1_name == "银行"
    assert membership.effective_from == date(2021, 12, 13)
    assert membership.effective_to is None
    assert membership.is_current is True
    assert membership.available_at == OBSERVED_AT
    assert client.calls[0][1]["is_new"] == "Y"


@pytest.mark.unit
async def test_dataframe_shape_is_converted_without_leaking_dataframe() -> None:
    client = FakeTushareClient()
    frame = SimpleNamespace(to_dict=lambda **kwargs: [_daily_row()])
    client.daily_basic = lambda **kwargs: frame  # type: ignore[method-assign]

    result = await _provider(client).fetch_daily_metrics(date(2026, 7, 24))

    assert result[0].symbol == "000001.SZ"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("trade_date", "20260230"),
        ("ts_code", "AAPL"),
        ("pb", float("inf")),
    ],
)
async def test_bad_row_rejects_entire_batch(field: str, bad_value: object) -> None:
    client = FakeTushareClient()
    client.daily_rows = [_daily_row(), _daily_row(**{field: bad_value})]

    with pytest.raises(ResearchDataContractError):
        await _provider(client).fetch_daily_metrics(date(2026, 7, 24))


@pytest.mark.unit
async def test_missing_upstream_field_is_actionable() -> None:
    client = FakeTushareClient()
    row = _daily_row()
    del row["total_mv"]
    client.daily_rows = [row]

    with pytest.raises(ResearchDataContractError, match="缺少字段: total_mv"):
        await _provider(client).fetch_daily_metrics(date(2026, 7, 24))


@pytest.mark.unit
async def test_possible_upstream_truncation_is_rejected() -> None:
    client = FakeTushareClient()
    client.financial_rows = [_financial_row(update_flag=str(index)) for index in range(100)]

    with pytest.raises(ResearchDataContractError, match="结果可能被截断"):
        await _provider(client).fetch_financial_indicators(
            "000001.SZ",
            start_period=date(2026, 1, 1),
            end_period=date(2026, 12, 31),
        )


@pytest.mark.unit
async def test_response_outside_requested_date_is_rejected() -> None:
    client = FakeTushareClient()
    client.daily_rows = [_daily_row(trade_date="20260723")]

    with pytest.raises(ResearchDataContractError, match="请求交易日之外"):
        await _provider(client).fetch_daily_metrics(date(2026, 7, 24))


@pytest.mark.unit
def test_missing_token_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINBOARD_TUSHARE_TOKEN", raising=False)

    with pytest.raises(
        ResearchDataConfigurationError,
        match="FINBOARD_TUSHARE_TOKEN",
    ):
        TushareResearchDataProvider()


@pytest.mark.unit
def test_missing_sdk_is_lazy_and_actionable() -> None:
    with (
        patch(
            "finboard_data.tushare_provider.importlib.import_module",
            side_effect=ModuleNotFoundError,
        ),
        pytest.raises(ResearchDataDependencyError, match=r"finboard-data\[tushare\]"),
    ):
        TushareResearchDataProvider(token="private-token")


@pytest.mark.unit
async def test_token_and_upstream_error_are_redacted() -> None:
    secret = "private-token-value"

    class FailingClient(FakeTushareClient):
        def daily_basic(self, **kwargs: str) -> object:
            raise RuntimeError(f"request failed with {secret}")

    provider = TushareResearchDataProvider(
        token=secret,
        client=FailingClient(),
        now=lambda: OBSERVED_AT,
    )

    with pytest.raises(ResearchDataUpstreamError) as exc_info:
        await provider.fetch_daily_metrics(date(2026, 7, 24))

    assert secret not in repr(provider)
    assert secret not in str(exc_info.value)


@pytest.mark.unit
def test_naive_clock_is_rejected() -> None:
    provider = TushareResearchDataProvider(
        client=FakeTushareClient(),
        now=lambda: datetime(2026, 7, 27),
    )

    with pytest.raises(ResearchDataConfigurationError, match="带时区"):
        provider._observed_at()
