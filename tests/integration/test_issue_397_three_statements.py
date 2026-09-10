"""issue #397 集成:三表 + dividend 同步 → 冻结发布 → PIT 门控读取全链路。

真实 PostgreSQL + 真实 provider(fake tushare client)+ 真实发布器:

1. ``DatasetSyncExecutor`` 同步四数据集落 ``research_*`` 表(批次发布语义);
2. ``ResearchDatasetReleaseService`` 冻结 ``income_statements`` / ``dividends``
   发布(经 ``ResearchTableReleaseSource`` 读表,#187 机制确认可引用);
3. ``FrozenReleaseProvider.fetch_income_statements`` / ``fetch_dividends`` 按
   ``available_at <= decision_at`` PIT 门控:公告前不可见、修订公告后可见;
4. 幂等:同 dataset_version 重跑不重复落行(修订行不互相覆盖)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。纯离线研究数据域,
不连 broker / 不下单;release_root 用 ``tmp_path``,不触真实发布目录。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
from finboard_data import TushareResearchDataProvider
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
)
from finboard_data.tushare_provider import (
    _BALANCE_FIELDS,
    _CASHFLOW_FIELDS,
    _DIVIDEND_FIELDS,
    _INCOME_FIELDS,
)
from finboard_persistence import (
    InstrumentModel,
    ResearchDatasetReleaseService,
    ResearchIncomeStatementModel,
    create_async_engine,
    session_factory,
)
from finboard_shared.models import Symbol
from finboard_shared.types import Market

pytestmark = pytest.mark.asyncio

_CODE = "600398.SH"
_SYMBOL = Symbol(_CODE, Market.A_SHARE)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime.now(UTC)


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
async def _clean(engine: AsyncEngine) -> AsyncIterator[None]:
    # setup 与 teardown 双清理:teardown 防止本模块的 instruments 行泄漏到
    # 同库的其他测试(全表 DELETE 与 golden 测试同口径,测试库专用)。
    async def _delete_all() -> None:
        async with engine.begin() as conn:
            for table in (
                "research_income_statements",
                "research_balance_sheets",
                "research_cashflow_statements",
                "research_dividends",
                "research_sync_batches",
                "research_dataset_releases",
                "instruments",
            ):
                await conn.execute(text(f"DELETE FROM {table}"))

    await _delete_all()
    yield
    await _delete_all()


class _FakeBudget:
    async def acquire(self) -> None:
        return None


def _full_row(fields: str, values: dict[str, object]) -> dict[str, object]:
    """白名单全键行(缺省 None),再覆盖具体值 —— 契约要求全字段在场。"""
    row: dict[str, object] = dict.fromkeys(fields.split(","))
    row.update(values)
    return row


class _FakeClient:
    """固定原始行(SDK 同形 dict),方法名即 endpoint。"""

    def income(self, **kwargs: str) -> list[dict[str, object]]:
        base = _full_row(
            _INCOME_FIELDS,
            {
                "ts_code": _CODE,
                "ann_date": "20260315",
                "f_ann_date": "20260315",
                "end_date": "20251231",
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "0",
                "total_revenue": 1200.5,
                "n_income_attr_p": 300.25,
                "ebitda": 420.75,
            },
        )
        revision = {
            **base,
            "ann_date": "20260420",
            "f_ann_date": "20260420",
            "update_flag": "1",
            "total_revenue": 1210.0,
            "basic_eps": 2.5,
        }
        return [base, revision]

    def balancesheet(self, **kwargs: str) -> list[dict[str, object]]:
        return [
            _full_row(
                _BALANCE_FIELDS,
                {
                    "ts_code": _CODE,
                    "ann_date": "20260315",
                    "f_ann_date": "20260315",
                    "end_date": "20251231",
                    "report_type": "1",
                    "comp_type": "1",
                    "update_flag": "0",
                    "money_cap": 500.0,
                    "inventories": 120.0,
                    "total_cur_assets": 900.0,
                    "total_cur_liab": 300.0,
                    "total_assets": 2000.0,
                },
            )
        ]

    def cashflow(self, **kwargs: str) -> list[dict[str, object]]:
        return [
            _full_row(
                _CASHFLOW_FIELDS,
                {
                    "ts_code": _CODE,
                    "ann_date": "20260315",
                    "f_ann_date": "20260315",
                    "end_date": "20251231",
                    "report_type": "1",
                    "comp_type": "1",
                    "update_flag": "1",
                    "n_cashflow_act": 360.0,
                    "c_pay_acq_const_fiolta": 60.0,
                    "free_cashflow": 300.0,
                },
            )
        ]

    def dividend(self, **kwargs: str) -> list[dict[str, object]]:
        plan = _full_row(
            _DIVIDEND_FIELDS,
            {
                "ts_code": _CODE,
                "end_date": "20251231",
                "ann_date": "20260315",
                "div_proc": "预案",
                "cash_div": 3.0,
                "cash_div_tax": 2.85,
            },
        )
        impl = {
            **plan,
            "ann_date": "20260520",
            "div_proc": "实施",
            "record_date": "20260625",
            "ex_date": "20260626",
            "imp_ann_date": "20260612",
        }
        return [plan, impl]


def _provider() -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=_FakeClient(),  # type: ignore[arg-type]
        now=lambda: _NOW,
    )


def _job() -> JobRecord:
    return JobRecord(
        job_id="BJ-ISSUE397",
        kind="dataset_sync",
        queue="data",
        payload={
            "datasets": [
                "income_statements",
                "balance_sheets",
                "cashflow_statements",
                "dividends",
            ],
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "symbols": [_CODE],
        },
        attempt=1,
        max_attempts=3,
        requested_by="integration-397",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    return None


async def _seed_instrument(engine: AsyncEngine) -> None:
    async with session_factory(engine)() as session:
        session.add(
            InstrumentModel(
                code=_CODE,
                name="issue397 三表样本",
                market="a_share",
                instrument_type="stock",
                exchange="SSE",
                list_date=date(2020, 1, 1),
                status="active",
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()


async def _sync_all(engine: AsyncEngine) -> None:
    executor = DatasetSyncExecutor(
        session_maker=session_factory(engine),
        provider_factory=_provider,
    )
    result = await executor.execute(_job(), _noop_progress)
    assert result.status == "succeeded"


async def _publish(
    engine: AsyncEngine,
    tmp_path: Path,
    *,
    kind: ReleaseDatasetKind,
    release_id: str,
    fields: tuple[str, ...],
):
    service = ResearchDatasetReleaseService(
        None,
        session_factory=session_factory(engine),
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    return await service.publish(
        DatasetReleaseSpec(
            release_id=release_id,
            dataset_name=kind.value,
            source="tushare",
            version="v1",
            # 发布窗对齐单个报告期(季度覆盖审计按 quarter-end 计数)。
            start_date=date(2025, 12, 31),
            end_date=date(2025, 12, 31),
            code_version="integration-397",
            adjustment="none",
            fields=fields,
            dataset_kind=kind,
            # 研究数据发布与 dataset_publish 执行器同口径:只要求 stock 能力。
            required_capabilities=("stock",),
            # 单标的测试样本按报告期季度覆盖审计;放宽发布级阈值避免环境耦合。
            minimum_release_coverage=Decimal("0.5"),
        ),
        [_CODE],
    )


class TestThreeStatementsChain:
    async def test_sync_freeze_pit_gate(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        await _seed_instrument(engine)
        await _sync_all(engine)

        income_release = await _publish(
            engine,
            tmp_path,
            kind=ReleaseDatasetKind.INCOME_STATEMENTS,
            release_id="integration-r397-income",
            # announcement_date 是读取端身份列(默认白名单恒含,与
            # financial_indicators 先例一致)。
            fields=(
                "announcement_date",
                "total_revenue",
                "n_income_attr_p",
                "ebitda",
                "update_flag",
            ),
        )
        assert income_release.instrument(_CODE).row_count == 2

        provider = FrozenReleaseProvider(
            release_root=tmp_path / "releases",
            release_id=income_release.release_id,
        )
        # 公告前(ann_date 当日早于 ann_date+1 零点):什么都不可见。
        early = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
        assert (
            await provider.fetch_income_statements(_SYMBOL, decision_at=early)
            == []
        )
        # 决策时点在两次公告(3-15 / 4-20)之间:只有首版可见。
        mid = datetime(2026, 4, 1, tzinfo=UTC)
        visible = await provider.fetch_income_statements(
            _SYMBOL, decision_at=mid
        )
        assert len(visible) == 1
        assert visible[0].update_flag == "0"
        assert visible[0].total_revenue == Decimal("1200.5")
        assert visible[0].available_at == datetime(
            2026, 3, 16, 0, 0, tzinfo=_SHANGHAI
        )
        # 修订公告后:两版都可见(修订可见,不互相覆盖)。
        late = datetime(2026, 5, 1, tzinfo=UTC)
        all_rows = await provider.fetch_income_statements(
            _SYMBOL, decision_at=late
        )
        assert [(r.update_flag, r.total_revenue) for r in all_rows] == [
            ("0", Decimal("1200.5")),
            ("1", Decimal("1210.0")),
        ]

        # dividends 发布 + div_proc 进展行全保留,ex_date 随实施公告可见。
        dividend_release = await _publish(
            engine,
            tmp_path,
            kind=ReleaseDatasetKind.DIVIDENDS,
            release_id="integration-r397-dividends",
            fields=(
                "announcement_date",
                "div_proc",
                "cash_div",
                "ex_date",
            ),
        )
        dividend_provider = FrozenReleaseProvider(
            release_root=tmp_path / "releases",
            release_id=dividend_release.release_id,
        )
        dividends_mid = await dividend_provider.fetch_dividends(
            _SYMBOL, decision_at=mid
        )
        # 实施公告(5-20 公告,5-21 可见)之前只有预案行。
        assert [r.div_proc for r in dividends_mid] == ["预案"]
        # 实施公告(5-20)次日可见:再晚的决策时点两条进展行都在。
        after_impl = datetime(2026, 6, 1, tzinfo=UTC)
        dividends = await dividend_provider.fetch_dividends(
            _SYMBOL, decision_at=after_impl
        )
        assert [(r.div_proc, r.cash_div, r.ex_date) for r in dividends] == [
            ("预案", Decimal("3.0"), None),
            ("实施", Decimal("3.0"), date(2026, 6, 26)),
        ]

    async def test_idempotent_rerun_same_version(
        self, engine: AsyncEngine
    ) -> None:
        await _seed_instrument(engine)
        await _sync_all(engine)
        await _sync_all(engine)

        async with session_factory(engine)() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(ResearchIncomeStatementModel)
                )
            ).scalar_one()
            assert total == 2

            from finboard_persistence import ResearchDividendModel

            dividends = (
                await session.execute(
                    select(func.count()).select_from(ResearchDividendModel)
                )
            ).scalar_one()
            assert dividends == 2
