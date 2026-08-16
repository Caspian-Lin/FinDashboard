"""``research_data_sync`` worker 集成测试(issue #171)。

fake research provider(实现 ``ResearchDataProvider`` 协议)验证:

* 四类数据集(profiles / daily_metrics / financial / industry)编排摄取,
  batch 达到 published;
* 部分失败不覆盖已发布数据,重跑补齐未发布切片(断点续跑 / 幂等);
* 幂等重跑:已发布切片跳过。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.executors.research_data_sync import (
    ResearchDataSyncExecutor,
)
from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
    ResearchDataUpstreamError,
)
from finboard_persistence import (
    ResearchDailyMetricModel,
    ResearchFinancialIndicatorModel,
    ResearchIndustryMembershipModel,
    ResearchInstrumentProfileModel,
    ResearchSyncBatchModel,
    SyncBatchStatus,
    create_async_engine,
    session_factory,
)

pytestmark = pytest.mark.asyncio

def _now() -> datetime:
    """运行时时间(质量门 max_observation_age 检查 observed_at 新鲜度)。"""
    return datetime.now(UTC)


NOW = _now()
TRADE_DATE = date(2026, 7, 24)
START_DATE = date(2026, 7, 24)
END_DATE = date(2026, 7, 27)
SYMBOLS = ("000001.SZ", "000002.SZ")


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from finboard_persistence import Base
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        for table in (
            "research_sync_batches",
            "research_instrument_profiles",
            "research_daily_metrics",
            "research_financial_indicators",
            "research_industry_classifications",
            "research_industry_memberships",
            "background_jobs",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


# ---- fake provider ----------------------------------------------------------


class FakeResearchProvider:
    """离线 ``ResearchDataProvider``:可注入失败标的,验证部分失败语义。"""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = SYMBOLS,
        fail_symbols: frozenset[str] = frozenset(),
        empty_days: set[date] | frozenset[date] = frozenset(),
    ) -> None:
        self.symbols = symbols
        self.fail_symbols = fail_symbols
        self.empty_days = empty_days
        self.calls: list[str] = []

    def _profile(self, symbol: str) -> InstrumentProfile:
        return InstrumentProfile(
            symbol=symbol,
            name=f"标的-{symbol}",
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

    def _daily(self, symbol: str, trade_date: date) -> DailySecurityMetrics:
        return DailySecurityMetrics(
            symbol=symbol,
            trade_date=trade_date,
            close=Decimal("12.34"),
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
            source="tushare",
            observed_at=NOW,
            available_at=NOW,
        )

    def _financial(self, symbol: str) -> FinancialIndicator:
        return FinancialIndicator(
            symbol=symbol,
            announcement_date=date(2026, 4, 30),
            report_period=date(2026, 3, 31),
            update_flag="1",
            eps=Decimal("0.8"),
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
                NOW.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            ),
        )

    def _industry(self, symbol: str) -> IndustryMembership:
        return IndustryMembership(
            symbol=symbol,
            security_name=f"标的-{symbol}",
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

    async def fetch_instrument_profiles(
        self, *, list_status: str = "L"
    ) -> list[InstrumentProfile]:
        self.calls.append("profiles")
        return [self._profile(symbol) for symbol in self.symbols]

    async def fetch_daily_metrics(
        self, trade_date: date
    ) -> list[DailySecurityMetrics]:
        self.calls.append(f"daily:{trade_date}")
        if trade_date in self.empty_days:
            return []
        return [self._daily(symbol, trade_date) for symbol in self.symbols]

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
    ) -> list[FinancialIndicator]:
        self.calls.append(f"financial:{symbol}")
        if symbol in self.fail_symbols:
            raise ResearchDataUpstreamError(f"上游失败: {symbol}")
        return [self._financial(symbol)]

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
    ) -> list[IndustryMembership]:
        self.calls.append(f"industry:{symbol}")
        if symbol in self.fail_symbols:
            raise ResearchDataUpstreamError(f"上游失败: {symbol}")
        return [self._industry(symbol)]


def _job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-RDS01",
        kind="research_data_sync",
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="integration-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "datasets": [
            "profiles",
            "daily_metrics",
            "financial_indicators",
            "industry_memberships",
        ],
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "symbols": list(SYMBOLS),
    }
    base.update(overrides)
    return base


def _make_executor(engine: AsyncEngine, provider: FakeResearchProvider) -> ResearchDataSyncExecutor:
    return ResearchDataSyncExecutor(
        session_maker=session_factory(engine),
        provider_factory=lambda: provider,
    )


async def _batch_rows(engine: AsyncEngine) -> list[ResearchSyncBatchModel]:
    from sqlalchemy import select

    async with session_factory(engine)() as session:
        rows = (
            await session.execute(
                select(ResearchSyncBatchModel).order_by(
                    ResearchSyncBatchModel.id
                )
            )
        ).scalars().all()
        return list(rows)


# ---- tests ------------------------------------------------------------------


class TestResearchDataSyncWorker:
    async def test_syncs_all_four_datasets(self, engine: AsyncEngine) -> None:
        provider = FakeResearchProvider(
            empty_days={date(2026, 7, 25), date(2026, 7, 26)}  # 周末无行情
        )
        executor = _make_executor(engine, provider)
        result = await executor.execute(_job(_payload()), _noop_progress)

        assert result.status == "succeeded"
        # 四个数据集均产生 published 批次。
        batches = await _batch_rows(engine)
        assert len(batches) >= 1
        assert all(batch.status == SyncBatchStatus.PUBLISHED.value for batch in batches)
        published_versions = {
            batch.dataset_version for batch in batches
        }
        assert any(item.startswith("profiles:") for item in published_versions)
        assert any(item.startswith("daily:") for item in published_versions)
        assert any(item.startswith("financial:") for item in published_versions)
        assert any(item.startswith("industry:") for item in published_versions)

        # 数据表有内容。
        from sqlalchemy import func, select

        async with session_factory(engine)() as session:
            daily_count = (
                await session.execute(
                    select(func.count()).select_from(ResearchDailyMetricModel)
                )
            ).scalar_one()
            financial_count = (
                await session.execute(
                    select(func.count()).select_from(ResearchFinancialIndicatorModel)
                )
            ).scalar_one()
            industry_count = (
                await session.execute(
                    select(func.count()).select_from(ResearchIndustryMembershipModel)
                )
            ).scalar_one()
            profile_count = (
                await session.execute(
                    select(func.count()).select_from(ResearchInstrumentProfileModel)
                )
            ).scalar_one()
        assert daily_count == 4  # 2 个交易日 x 2 个标的
        assert financial_count == 2
        assert industry_count == 2
        assert profile_count == 2

    async def test_partial_failure_rerun_completes(
        self, engine: AsyncEngine
    ) -> None:
        """部分标的失败:已发布切片保留;修复后重跑补齐,不覆盖已发布。"""
        provider = FakeResearchProvider(fail_symbols=frozenset({"000002.SZ"}))
        executor = _make_executor(engine, provider)

        from finboard_backtest.background_jobs.contracts import ExecutorError

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(_job(_payload()), _noop_progress)
        assert exc_info.value.code == "research_data_upstream"
        assert exc_info.value.retryable is True

        batches = await _batch_rows(engine)
        statuses = {batch.status for batch in batches}
        # 全部为 RUNNING/PARTIAL/FAILED —— 没有任何失败时被错误标记为 PUBLISHED 的切片。
        assert SyncBatchStatus.PUBLISHED.value in statuses  # 000001.SZ 切片已发布
        # 失败的 financial / industry 切片未发布。
        failed_versions = {
            batch.dataset_version
            for batch in batches
            if batch.status != SyncBatchStatus.PUBLISHED.value
        }
        assert any(item.startswith("financial:000002.SZ") for item in failed_versions)

        # 修复后重跑:整体 succeeded,已发布切片不重复发布。
        provider.fail_symbols = frozenset()
        result = await executor.execute(_job(_payload()), _noop_progress)
        assert result.status == "succeeded"
        batches = await _batch_rows(engine)
        published = [
            batch
            for batch in batches
            if batch.status == SyncBatchStatus.PUBLISHED.value
        ]
        # 每个切片只发布一次(同 version 不重复发布)。
        versions = [batch.dataset_version for batch in published]
        assert len(versions) == len(set(versions))

    async def test_rerun_idempotent(self, engine: AsyncEngine) -> None:
        """幂等重跑:同 payload 二次执行,已发布切片跳过、数据不重复。"""
        executor = _make_executor(engine, FakeResearchProvider())

        first = await executor.execute(_job(_payload()), _noop_progress)
        assert first.status == "succeeded"
        first_published = await _batch_rows(engine)

        second = await executor.execute(_job(_payload()), _noop_progress)
        assert second.status == "succeeded"
        second_published = await _batch_rows(engine)

        assert len(second_published) == len(first_published)
        assert all(
            item.status == SyncBatchStatus.PUBLISHED.value
            for item in second_published
        )

        from sqlalchemy import func, select

        async with session_factory(engine)() as session:
            daily_count = (
                await session.execute(
                    select(func.count()).select_from(ResearchDailyMetricModel)
                )
            ).scalar_one()
        assert daily_count == 4  # 未重复写入
