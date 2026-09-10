"""``dataset_sync`` worker 集成测试(issue #171;#392 起自 research_data_sync
改名迁移到数据集驱动框架)。

fake research provider(实现 ``ResearchDataProvider`` 协议)验证:

* 六类数据集(profiles / name_changes / daily_metrics / financial /
  industry / convertible)按 SyncSpec 编排摄取,batch 达到 published;
* 部分失败不覆盖已发布数据,重跑补齐未发布切片(断点续跑 / 幂等);
* 幂等重跑:已发布切片跳过;
* scope 四元组宇宙过滤(#392):exchange/instrument_type 从 instruments 表
  解析逐标的同步池。

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
from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
from finboard_data.research import (
    ConvertibleProfile,
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentNameChange,
    InstrumentProfile,
    ResearchDataUpstreamError,
    SuspensionRecord,
)
from finboard_persistence import (
    InstrumentNameModel,
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
            "instrument_names",
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

    async def fetch_convertible_profiles(
        self, *, dirty_row_policy: str | None = None
    ) -> list[ConvertibleProfile]:
        # #265:本测试不覆盖转债段(datasets 显式声明),协议要求空实现。
        return []

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
        self, *, list_status: str = "L", dirty_row_policy: str | None = None
    ) -> list[InstrumentProfile]:
        self.calls.append("profiles")
        if list_status != "L":
            return []  # fake 数据集全部为在市标的;#251 退市档案请求返回空
        return [self._profile(symbol) for symbol in self.symbols]

    async def fetch_name_changes(
        self, *, dirty_row_policy: str | None = None
    ) -> list[InstrumentNameChange]:
        """#251:历史名称变更(单标的两段区间,含去重排序由仓储处理)。"""
        self.calls.append("name_changes")
        return [
            InstrumentNameChange(
                symbol=self.symbols[0],
                name="曾用名",
                start_date=date(1991, 4, 3),
                end_date=date(1992, 3, 9),
                change_reason="更名",
                source="tushare",
                observed_at=NOW,
                available_at=NOW,
            ),
            InstrumentNameChange(
                symbol=self.symbols[0],
                name="现用名",
                start_date=date(1992, 3, 9),
                end_date=None,
                change_reason=None,
                source="tushare",
                observed_at=NOW,
                available_at=NOW,
            ),
        ]

    async def fetch_daily_metrics(
        self, trade_date: date, *, dirty_row_policy: str | None = None
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
        dirty_row_policy: str | None = None,
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
        dirty_row_policy: str | None = None,
    ) -> list[IndustryMembership]:
        self.calls.append(f"industry:{symbol}")
        if symbol in self.fail_symbols:
            raise ResearchDataUpstreamError(f"上游失败: {symbol}")
        return [self._industry(symbol)]

    async def fetch_suspensions(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[SuspensionRecord]:
        self.calls.append(f"suspensions:{trade_date.isoformat()}")
        return []


def _job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-RDS01",
        kind="dataset_sync",
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
            "name_changes",
            "convertible_profiles",
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


def _make_executor(engine: AsyncEngine, provider: FakeResearchProvider) -> DatasetSyncExecutor:
    return DatasetSyncExecutor(
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


class TestDatasetSyncWorker:
    async def test_syncs_all_datasets(self, engine: AsyncEngine) -> None:
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
            # #251:名称历史直接重建主数据表 instrument_names(不走批次)。
            name_history_count = (
                await session.execute(
                    select(func.count()).select_from(InstrumentNameModel)
                )
            ).scalar_one()
        assert daily_count == 4  # 2 个交易日 x 2 个标的
        assert financial_count == 2
        assert industry_count == 2
        assert profile_count == 2
        assert name_history_count == 2

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


class TestScopeUniverseFilter:
    """scope 四元组宇宙过滤(#392):逐标的池从 instruments 表按
    exchange/listing_boards/instrument_type 解析(#385 语义,与 bulk_download
    共享 normalize_sync_scope)。"""

    @staticmethod
    def _instrument(code: str, instrument_type: str, exchange: str) -> dict[str, object]:
        return {
            "code": code,
            "name": f"标的-{code}",
            "market": "a_share",
            "instrument_type": instrument_type,
            "exchange": exchange,
            "listing_board": "unknown",
            "status": "active",
        }

    async def _register(self, engine: AsyncEngine) -> None:
        from finboard_persistence import InstrumentRepository

        instruments: list[dict[str, object]] = [
            self._instrument("000001.SZ", "stock", "SZSE"),
            self._instrument("600000.SH", "stock", "SSE"),
            self._instrument("510300.SH", "etf", "SSE"),
        ]
        async with session_factory(engine)() as session:
            await InstrumentRepository(session).upsert_many(instruments)
            await session.commit()

    async def test_universe_filter_resolves_pool_from_instruments(
        self, engine: AsyncEngine
    ) -> None:
        await self._register(engine)
        provider = FakeResearchProvider(symbols=())
        executor = _make_executor(engine, provider)
        payload: dict[str, object] = {
            "datasets": ["industry_memberships"],
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "instrument_type": "stock",
        }
        result = await executor.execute(_job(payload), _noop_progress)

        assert result.status == "succeeded"
        # 逐标的池 = instruments 表 stock 过滤结果(ETF 510300.SH 被剔除)。
        industry_calls = [c for c in provider.calls if c.startswith("industry:")]
        assert sorted(industry_calls) == [
            "industry:000001.SZ",
            "industry:600000.SH",
        ]
        assert "profiles" not in provider.calls  # 未选中 profiles,不发档案请求

    async def test_symbols_intersected_with_universe_filter(
        self, engine: AsyncEngine
    ) -> None:
        await self._register(engine)
        provider = FakeResearchProvider(symbols=())
        executor = _make_executor(engine, provider)
        payload: dict[str, object] = {
            "datasets": ["financial_indicators"],
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "symbols": ["510300.SH", "000001.SZ"],  # 510300 不在 stock 过滤内
            "instrument_type": "stock",
        }
        result = await executor.execute(_job(payload), _noop_progress)

        assert result.status == "succeeded"
        financial_calls = [c for c in provider.calls if c.startswith("financial:")]
        assert sorted(financial_calls) == ["financial:000001.SZ"]

    async def test_empty_universe_filter_rejected_named(
        self, engine: AsyncEngine
    ) -> None:
        """宇宙过滤在 instruments 表解析为空 → 具名 no_instruments 拒绝。"""
        await self._register(engine)
        provider = FakeResearchProvider(symbols=())
        executor = _make_executor(engine, provider)
        payload: dict[str, object] = {
            "datasets": ["industry_memberships"],
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "exchange": "CFFEX",  # 登记的标的均非 CFFEX
        }

        from finboard_backtest.background_jobs.contracts import ExecutorError

        with pytest.raises(ExecutorError) as exc_info:
            await executor.execute(_job(payload), _noop_progress)
        assert exc_info.value.code == "no_instruments"
