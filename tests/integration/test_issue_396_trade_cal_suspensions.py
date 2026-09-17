"""issue #396 集成测试(真实 PG):suspend_d 研究数据集 + trade_cal 落库。

* suspensions 数据集(DAILY_MARKET)经 DatasetSyncExecutor 全链落库:
  逐工作日切片 published、非交易日空切片无批次行、重跑幂等跳过;
* ``list_suspensions_as_of`` PIT 读取(available_at=当日 09:30 门控);
* ``trade_cal`` 幂等 upsert(SSE/SZSE)+ ``PgTradingCalendarStore`` 往返;
* ``ensure_calendar_loaded`` DB 优先:回源 akshare 回写 DB 后,二次加载
  纯 DB 命中(akshare mock 计数为 0)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。纯离线研究数据域,不连
broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    ConvertibleProfile,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
    IndustryMembership,
    InstrumentNameChange,
    InstrumentProfile,
    SuspensionRecord,
)
from finboard_persistence import (
    ResearchDatasetRepository,
    ResearchSyncBatchModel,
    SyncBatchStatus,
    TradeCalRepository,
    create_async_engine,
    session_factory,
)
from finboard_persistence.trade_calendar_repo import PgTradingCalendarStore

pytestmark = pytest.mark.asyncio

_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 2026-07-24(周五,有停牌记录)/ 2026-07-27(周一,上游空响应 = 无停牌;
#: 注意 DAILY_MARKET 切片只枚举工作日,周末本就不产生切片)。
TRADE_DATE = date(2026, 7, 24)
EMPTY_DATE = date(2026, 7, 27)
WINDOW_START = date(2026, 7, 24)
WINDOW_END = date(2026, 7, 27)
SUSPENDED_SYMBOLS = ("000001.SZ", "600000.SH", "000002.SZ")


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
            "research_suspensions",
            "research_sync_batches",
            "trade_cal",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


def _now() -> datetime:
    return datetime.now(UTC)


class SuspensionsOnlyProvider:
    """只实现 suspensions 的离线 provider(其余协议方法空实现)。"""

    def __init__(self) -> None:
        self.suspend_calls: list[str] = []

    async def fetch_suspensions(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[SuspensionRecord]:
        assert dirty_row_policy == "skip"  # DAILY_MARKET → SKIP(框架推导)
        self.suspend_calls.append(trade_date.isoformat())
        if trade_date == EMPTY_DATE:
            return []
        available_at = datetime.combine(
            trade_date, datetime.min.time().replace(hour=9, minute=30), tzinfo=_SHANGHAI
        )
        kinds = ("suspension_day", "resumption", "suspension_day")
        return [
            SuspensionRecord(
                symbol=symbol,
                trade_date=trade_date,
                suspend_kind=kind,
                suspend_type="R" if kind == "resumption" else "S",
                suspend_timing=None,
                source="tushare",
                observed_at=_now(),
                available_at=available_at,
            )
            for symbol, kind in zip(SUSPENDED_SYMBOLS, kinds, strict=True)
        ]

    async def fetch_instrument_profiles(
        self,
        *,
        list_status: str = "L",
        dirty_row_policy: str | None = None,
    ) -> list[InstrumentProfile]:
        return []

    async def fetch_name_changes(
        self, *, dirty_row_policy: str | None = None
    ) -> list[InstrumentNameChange]:
        return []

    async def fetch_daily_metrics(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[DailySecurityMetrics]:
        return []

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
        dirty_row_policy: str | None = None,
    ) -> list[FinancialIndicator]:
        return []

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
        dirty_row_policy: str | None = None,
    ) -> list[IndustryMembership]:
        return []

    async def fetch_convertible_profiles(
        self, *, dirty_row_policy: str | None = None
    ) -> list[ConvertibleProfile]:
        return []


    async def fetch_income_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[IncomeStatement]:
        return []

    async def fetch_balance_sheets(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[BalanceSheet]:
        return []

    async def fetch_cashflow_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[CashflowStatement]:
        return []

    async def fetch_dividends(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[DividendRecord]:
        return []


def _job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-I39601",
        kind="dataset_sync",
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="integration-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass


class TestSuspensionsDatasetSync:
    async def test_sync_publishes_slices_and_pit_read(self, engine: AsyncEngine) -> None:
        provider = SuspensionsOnlyProvider()
        executor = DatasetSyncExecutor(
            session_maker=session_factory(engine),
            provider_factory=lambda: provider,
        )
        payload: dict[str, object] = {
            "datasets": ["suspensions"],
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
        }
        result = await executor.execute(_job(payload), _noop_progress)
        assert result.status == "succeeded"
        # 交易日切片 published;非交易日空切片不产生批次行。
        async with session_factory(engine)() as session:
            batches = (
                await session.execute(
                    select(ResearchSyncBatchModel).where(
                        ResearchSyncBatchModel.dataset == "suspensions"
                    )
                )
            ).scalars().all()
            assert [b.status for b in batches] == [SyncBatchStatus.PUBLISHED.value]
            assert batches[0].dataset_version == f"suspensions:{TRADE_DATE.isoformat()}"
            assert batches[0].parameters == {"trade_date": TRADE_DATE.isoformat()}

        async with session_factory(engine)() as session:
            repo = ResearchDatasetRepository(session)
            visible_at = datetime(2026, 7, 24, 15, 0, tzinfo=_SHANGHAI)
            records = await repo.list_suspensions_as_of(
                start_date=WINDOW_START,
                end_date=WINDOW_END,
                decision_at=visible_at,
                source="tushare",
            )
            by_symbol = {item.symbol: item for item in records}
            assert set(by_symbol) == set(SUSPENDED_SYMBOLS)
            assert by_symbol["000001.SZ"].suspend_kind == "suspension_day"
            assert by_symbol["600000.SH"].suspend_kind == "resumption"
            # PIT=当日:available_at = 交易日 09:30(上海)。
            assert (
                by_symbol["000001.SZ"].available_at
                == datetime(2026, 7, 24, 9, 30, tzinfo=_SHANGHAI)
            )

    async def test_rerun_published_slices_skipped(self, engine: AsyncEngine) -> None:
        provider = SuspensionsOnlyProvider()
        executor = DatasetSyncExecutor(
            session_maker=session_factory(engine),
            provider_factory=lambda: provider,
        )
        payload: dict[str, object] = {
            "datasets": ["suspensions"],
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
        }
        await executor.execute(_job(payload), _noop_progress)
        await executor.execute(_job(payload), _noop_progress)
        async with session_factory(engine)() as session:
            batches = (
                await session.execute(
                    select(ResearchSyncBatchModel).where(
                        ResearchSyncBatchModel.dataset == "suspensions"
                    )
                )
            ).scalars().all()
            # 已发布切片幂等命中:同 version 只有一行,且仍 published。
            assert len(batches) == 1
            assert batches[0].status == SyncBatchStatus.PUBLISHED.value
        assert provider.suspend_calls == [
            TRADE_DATE.isoformat(),
            EMPTY_DATE.isoformat(),
            TRADE_DATE.isoformat(),
            EMPTY_DATE.isoformat(),
        ]


class TestTradeCalRepository:
    async def test_upsert_idempotent_and_list(self, engine: AsyncEngine) -> None:
        days = {date(2026, 7, 20), date(2026, 7, 21)}
        async with session_factory(engine)() as session:
            repo = TradeCalRepository(session)
            written = await repo.upsert_trading_days(days, source="akshare")
            await session.commit()
            assert written == 2 * 2  # SSE + SZSE
            # 幂等:重复 upsert 不产生新行。
            written_again = await repo.upsert_trading_days(days, source="akshare")
            await session.commit()
            assert written_again == 0
            assert await repo.list_trading_days("SSE") == days
            assert await repo.list_trading_days("SZSE") == days

    async def test_store_round_trip(self, engine: AsyncEngine) -> None:
        store = PgTradingCalendarStore(session_factory(engine))
        assert await store.load() is None
        days = {date(2026, 7, 20), date(2026, 7, 21)}
        await store.save(days)
        assert await store.load() == days

    async def test_ensure_calendar_loaded_db_first(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_data import trading_calendar

        store = PgTradingCalendarStore(session_factory(engine))
        trading_calendar.reset_cache()
        trading_calendar.install_calendar_store(store)
        calls = {"akshare": 0}

        def _fake_fetch() -> set[date]:
            calls["akshare"] += 1
            # 相对今天的新鲜日历(max >= today 不触发过期回源分支)。
            today = date.today()
            return {today + timedelta(days=offset) for offset in range(3)}

        monkeypatch.setattr(
            trading_calendar, "_fetch_trade_dates", _fake_fetch
        )
        try:
            first = await trading_calendar.ensure_calendar_loaded()
            expected = {date.today() + timedelta(days=offset) for offset in range(3)}
            assert first == expected
            assert calls["akshare"] == 1
            trading_calendar.reset_cache()
            second = await trading_calendar.ensure_calendar_loaded()
            # DB 优先:二次加载纯 DB 命中,akshare 零调用。
            assert second == first
            assert calls["akshare"] == 1
        finally:
            trading_calendar.reset_cache()
            trading_calendar.install_calendar_store(None)
