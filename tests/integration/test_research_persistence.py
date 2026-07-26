"""时点化研究数据存储、发布与故障恢复集成测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_data import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
    ResearchDataQualityValidator,
)
from finboard_persistence import (
    ResearchDailyMetricModel,
    ResearchDataset,
    ResearchDatasetRepository,
    ResearchDataSyncService,
    ResearchSyncBatchModel,
    SyncBatchStatus,
    session_factory,
)

NOW = datetime(2026, 7, 27, 8, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 24)


def _daily(
    symbol: str = "000001.SZ",
    *,
    source: str = "tushare",
    close: str = "12.34",
) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=TRADE_DATE,
        close=Decimal(close),
        turnover_rate=Decimal("0.025"),
        turnover_rate_free=Decimal("0.031"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("8.5"),
        pe_ttm=Decimal("9.1"),
        pb=Decimal("0.92"),
        ps=Decimal("1.1"),
        ps_ttm=Decimal("1.2"),
        dividend_yield=Decimal("0.03"),
        dividend_yield_ttm=Decimal("0.032"),
        total_shares=Decimal("100000000"),
        float_shares=Decimal("80000000"),
        free_shares=Decimal("70000000"),
        total_market_cap=Decimal("1234000000"),
        circulating_market_cap=Decimal("987200000"),
        limit_status=0,
        source=source,
        observed_at=NOW,
        available_at=NOW,
    )


def _profile() -> InstrumentProfile:
    return InstrumentProfile(
        symbol="000001.SZ",
        name="平安银行",
        exchange="SZSE",
        market="主板",
        list_status="L",
        list_date=date(1991, 4, 3),
        delist_date=None,
        industry="银行",
        source="tushare",
        observed_at=NOW,
        available_at=NOW,
    )


def _financial(
    *,
    announcement_date: date,
    update_flag: str,
    eps: str,
) -> FinancialIndicator:
    return FinancialIndicator(
        symbol="000001.SZ",
        announcement_date=announcement_date,
        report_period=date(2026, 3, 31),
        update_flag=update_flag,
        eps=Decimal(eps),
        diluted_eps=None,
        book_value_per_share=Decimal("12"),
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal("0.08"),
        weighted_return_on_equity=None,
        gross_profit_margin=None,
        net_profit_margin=None,
        debt_to_assets=None,
        revenue_yoy=None,
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=NOW,
        available_at=datetime.combine(
            announcement_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=UTC,
        ),
    )


def _industry() -> IndustryMembership:
    return IndustryMembership(
        symbol="000001.SZ",
        security_name="平安银行",
        taxonomy="SW2021",
        level1_code="460000",
        level1_name="银行",
        level2_code="461100",
        level2_name="股份制银行Ⅱ",
        level3_code="461101",
        level3_name="股份制银行Ⅲ",
        effective_from=date(2021, 12, 13),
        effective_to=None,
        is_current=True,
        source="tushare",
        observed_at=NOW,
        available_at=NOW,
    )


def _service(engine: AsyncEngine) -> ResearchDataSyncService:
    return ResearchDataSyncService(
        session_factory(engine),
        validator_factory=lambda: ResearchDataQualityValidator(now=NOW),
    )


async def _sync_daily(
    service: ResearchDataSyncService,
    *,
    version: str,
    records: list[DailySecurityMetrics],
    expected_symbols: set[str] | None = None,
) -> ResearchSyncBatchModel:
    return await service.sync_daily_metrics(
        source="tushare",
        dataset_version=version,
        code_version="git-test",
        parameters={"trade_date": TRADE_DATE, "token": "must-not-persist"},
        raw_payload={"data": [{"ts_code": item.symbol} for item in records]},
        records=records,
        expected_trade_date=TRADE_DATE,
        expected_symbols=expected_symbols,
    )


@pytest.mark.asyncio
async def test_daily_sync_is_idempotent_archived_and_point_in_time(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
) -> None:
    del db_session
    service = _service(_engine)

    first = await _sync_daily(service, version="daily-v1", records=[_daily()])
    repeated = await _sync_daily(
        service,
        version="daily-v1",
        records=[replace(_daily(), close=Decimal("99"))],
    )

    assert first.status == SyncBatchStatus.PUBLISHED.value
    assert repeated.id == first.id
    assert repeated.parameters["token"] == "***"
    assert repeated.raw_payload == {"data": [{"ts_code": "000001.SZ"}]}

    maker = session_factory(_engine)
    async with maker() as session:
        repo = ResearchDatasetRepository(session)
        before = await repo.get_daily_metric_as_of(
            symbol="000001.SZ",
            trade_date=TRADE_DATE,
            decision_at=NOW - timedelta(seconds=1),
            source="tushare",
        )
        after = await repo.get_daily_metric_as_of(
            symbol="000001.SZ",
            trade_date=TRADE_DATE,
            decision_at=NOW,
            source="tushare",
            dataset_version="daily-v1",
        )
        wrong_source = await repo.get_daily_metric_as_of(
            symbol="000001.SZ",
            trade_date=TRADE_DATE,
            decision_at=NOW,
            source="akshare",
        )
        count = await session.scalar(select(func.count()).select_from(ResearchDailyMetricModel))

    assert before is None
    assert after is not None
    assert after.close == Decimal("12.34000000")
    assert wrong_source is None
    assert count == 1


@pytest.mark.asyncio
async def test_financial_revisions_and_industry_history_round_trip(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
) -> None:
    del db_session
    service = _service(_engine)
    revisions = [
        _financial(
            announcement_date=date(2026, 4, 20),
            update_flag="0",
            eps="0.45",
        ),
        _financial(
            announcement_date=date(2026, 5, 5),
            update_flag="1",
            eps="0.47",
        ),
    ]
    financial_batch = await service.sync_financial_indicators(
        source="tushare",
        dataset_version="finance-v1",
        code_version="git-test",
        parameters={"symbol": "000001.SZ"},
        raw_payload={"rows": 2},
        records=revisions,
    )
    industry_batch = await service.sync_industry_memberships(
        source="tushare",
        dataset_version="industry-v1",
        code_version="git-test",
        parameters={"taxonomy": "SW2021"},
        raw_payload={"rows": 1},
        records=[_industry()],
        expected_symbols={"000001.SZ"},
    )
    profile_batch = await service.sync_instrument_profiles(
        source="tushare",
        dataset_version="profiles-v1",
        code_version="git-test",
        parameters={"list_status": "L"},
        raw_payload={"rows": 1},
        records=[_profile()],
        expected_symbols={"000001.SZ"},
    )
    daily_batch = await _sync_daily(
        service,
        version="daily-factor-v1",
        records=[_daily()],
        expected_symbols={"000001.SZ"},
    )

    assert financial_batch.status == SyncBatchStatus.PUBLISHED.value
    assert industry_batch.status == SyncBatchStatus.PUBLISHED.value
    assert profile_batch.status == SyncBatchStatus.PUBLISHED.value
    assert daily_batch.status == SyncBatchStatus.PUBLISHED.value

    maker = session_factory(_engine)
    async with maker() as session:
        repo = ResearchDatasetRepository(session)
        before_revision = await repo.list_financial_indicators_as_of(
            symbol="000001.SZ",
            decision_at=datetime(2026, 4, 25, tzinfo=UTC),
            source="tushare",
        )
        after_revision = await repo.list_financial_indicators_as_of(
            symbol="000001.SZ",
            decision_at=datetime(2026, 5, 10, tzinfo=UTC),
            source="tushare",
        )
        memberships = await repo.list_industry_memberships_as_of(
            symbol="000001.SZ",
            business_date=date(2022, 1, 1),
            decision_at=NOW,
            source="tushare",
        )
        factor_inputs = await repo.load_factor_inputs(
            symbols=("000001.SZ",),
            business_date=TRADE_DATE,
            decision_at=NOW,
            source="tushare",
            required_datasets=frozenset(
                {
                    "instrument_profiles",
                    "daily_metrics",
                    "financial_indicators",
                    "industry_memberships",
                }
            ),
            dataset_versions={},
        )

    assert [item.eps for item in before_revision] == [Decimal("0.45000000")]
    assert [item.eps for item in after_revision] == [
        Decimal("0.45000000"),
        Decimal("0.47000000"),
    ]
    assert len(memberships) == 1
    assert memberships[0].level1_code == "460000"
    assert memberships[0].level2_name == "股份制银行Ⅱ"
    assert memberships[0].level3_code == "461101"
    assert factor_inputs.issues == ()
    assert factor_inputs.dataset_versions == {
        "daily_metrics": "daily-factor-v1",
        "financial_indicators": "finance-v1",
        "industry_memberships": "industry-v1",
        "instrument_profiles": "profiles-v1",
    }
    assert factor_inputs.records[0].financial is not None
    assert factor_inputs.records[0].financial.eps == Decimal("0.47000000")


@pytest.mark.asyncio
async def test_partial_duplicate_and_upstream_failure_do_not_replace_published(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
) -> None:
    del db_session
    service = _service(_engine)
    published = await _sync_daily(
        service,
        version="daily-good",
        records=[_daily()],
        expected_symbols={"000001.SZ"},
    )
    partial = await _sync_daily(
        service,
        version="daily-partial",
        records=[_daily(close="20")],
        expected_symbols={"000001.SZ", "600000.SH"},
    )
    duplicate = await _sync_daily(
        service,
        version="daily-duplicate",
        records=[_daily(), _daily()],
    )
    upstream = await service.record_upstream_failure(
        dataset=ResearchDataset.DAILY_METRICS,
        source="tushare",
        dataset_version="daily-rate-limited",
        code_version="git-test",
        parameters={"trade_date": TRADE_DATE, "token": "secret"},
        error_type="RateLimitError token=secret",
    )

    assert published.status == SyncBatchStatus.PUBLISHED.value
    assert partial.status == SyncBatchStatus.PARTIAL.value
    assert duplicate.status == SyncBatchStatus.FAILED.value
    assert upstream.status == SyncBatchStatus.FAILED.value
    assert "secret" not in (upstream.error_summary or "")

    maker = session_factory(_engine)
    async with maker() as session:
        latest = await ResearchDatasetRepository(session).get_daily_metric_as_of(
            symbol="000001.SZ",
            trade_date=TRADE_DATE,
            decision_at=NOW,
            source="tushare",
        )
        normalized_count = await session.scalar(
            select(func.count()).select_from(ResearchDailyMetricModel)
        )

    assert latest is not None
    assert latest.close == Decimal("12.34000000")
    assert normalized_count == 1


@pytest.mark.asyncio
async def test_database_interruption_marks_failed_and_same_version_can_retry(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del db_session
    service = _service(_engine)
    with monkeypatch.context() as patch:
        patch.setattr(
            ResearchDatasetRepository,
            "upsert_daily_metrics",
            AsyncMock(side_effect=RuntimeError("connection interrupted")),
        )
        with pytest.raises(RuntimeError, match="connection interrupted"):
            await _sync_daily(
                service,
                version="daily-retry",
                records=[_daily()],
            )

    maker = session_factory(_engine)
    async with maker() as session:
        failed = await session.scalar(
            select(ResearchSyncBatchModel).where(
                ResearchSyncBatchModel.dataset_version == "daily-retry"
            )
        )
        normalized_count = await session.scalar(
            select(func.count()).select_from(ResearchDailyMetricModel)
        )
    assert failed is not None
    assert failed.status == SyncBatchStatus.FAILED.value
    assert normalized_count == 0

    retried = await _sync_daily(
        service,
        version="daily-retry",
        records=[_daily()],
    )
    assert retried.id == failed.id
    assert retried.status == SyncBatchStatus.PUBLISHED.value
