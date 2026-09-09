"""issue #392 golden:固定 mock tushare 响应 → 真实 PG 产物逐值对照。

方法(dataset_sync 框架迁移的等值验收基线):

1. 固定一份**原始 tushare 行**(dict,与 SDK 返回同形,含脏行)喂给
   ``TushareResearchDataProvider``(注入 fake client / 固定预算桩 / 真实时钟作
   ``observed_at``),经 dataset_sync 执行器全量同步 6 数据集落真实 PG;
2. 快照全部产物表(研究数据表 / instrument_names / convertible_metadata /
   instrument_lifecycle_events / research_sync_batches)为规范化 JSON,与
   ``tests/integration/goldens/dataset_sync_392.json`` 逐值对照;
3. 迁移到 dataset_sync 框架后,同一批 mock 喂新路径,快照必须与旧路径产物
   **逐字节一致**(占位符归一 <NOW>/<TODAY> 外不允许任何漂移);
   旧路径删除后本文件即新路径回归。

场景二(上游失败):financial_indicators 对其中一个标的失败 —— 固化
``record_upstream_failure`` 的批次记账形态(dataset_version 定位死点 /
error_summary / parameters)与「已发布切片保留」语义。

确定性:业务日期固定(2026-07-24 ~ 2026-07-27,跨一个周末);
``observed_at`` 取真实时钟(质量门 max_observation_age=1 天要求新鲜),
快照归一化时以 ``<NOW>`` / ``<TODAY>`` 占位。regenerate:
``FINBOARD_GOLDEN_REGENERATE=1 uv run pytest tests/integration/test_issue_392_golden_dataset_sync.py``。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_data.akshare_provider import ConvertibleRedemptionEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from finboard_data.research import ResearchDataProvider
    from finboard_shared.instruments import LifecycleEvent

from finboard_persistence import (
    ConvertibleMetadataModel,
    InstrumentLifecycleEventModel,
    InstrumentNameModel,
    ResearchDailyMetricModel,
    ResearchFinancialIndicatorModel,
    ResearchIndustryClassificationModel,
    ResearchIndustryMembershipModel,
    ResearchInstrumentProfileModel,
    ResearchSyncBatchModel,
    create_async_engine,
    session_factory,
)

pytestmark = pytest.mark.asyncio

GOLDEN_FILE = (
    Path(__file__).parent / "goldens" / "dataset_sync_392.json"
)

#: 运行时观察时间(质量门新鲜度检查要求 1 天内;快照归一为 <NOW>)。
NOW = datetime.now(UTC)

#: 集思录强赎事件的固定观察时间(与测试时钟解耦,完全确定)。
REDEEM_OBSERVED_AT = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)

TRADE_DATES = (date(2026, 7, 24), date(2026, 7, 27))
START_DATE = date(2026, 7, 24)
END_DATE = date(2026, 7, 27)
SYMBOLS = ("000001.SZ", "600000.SH")

#: 快照排除的列(自增主键 / 服务器默认时间戳 / 批次外键自增值 —— 均非
#: 产物语义;批次身份由 (dataset, source, dataset_version) 唯一约束承载)。
_EPHEMERAL_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "published_at",
        "ingested_at",
        "batch_id",
    }
)

_SNAPSHOT_TABLES: tuple[type[Any], ...] = (
    ResearchInstrumentProfileModel,
    ResearchDailyMetricModel,
    ResearchFinancialIndicatorModel,
    ResearchIndustryClassificationModel,
    ResearchIndustryMembershipModel,
    InstrumentNameModel,
    ConvertibleMetadataModel,
    InstrumentLifecycleEventModel,
    ResearchSyncBatchModel,
)


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
            "instrument_names",
            "convertible_metadata",
            "instrument_lifecycle_events",
            "instruments",
            "background_jobs",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


# ---- 固定 mock:原始 tushare 行(SDK 同形 dict)------------------------------


def _stock_basic_rows(list_status: str) -> list[dict[str, object]]:
    if list_status == "L":
        return [
            {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SZSE",
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": None,
            },
            # #389 脏行:历史前缀代码,行级跳过并具名告警。
            {
                "ts_code": "T600018.SH",
                "name": "老代码脏行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "19990101",
                "delist_date": None,
            },
            {
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "19991110",
                "delist_date": None,
            },
        ]
    return [
        {
            "ts_code": "000003.SZ",
            "name": "PT金田A",
            "industry": "综合",
            "market": "主板",
            "exchange": "SZSE",
            "list_status": "D",
            "list_date": "19910703",
            "delist_date": "20020521",
        }
    ]


def _daily_basic_rows(trade_date: date) -> list[dict[str, object]]:
    day = trade_date.strftime("%Y%m%d")
    return [
        {
            "ts_code": "000001.SZ",
            "trade_date": day,
            "close": "12.34" if trade_date == TRADE_DATES[0] else "12.56",
            "turnover_rate": "2.5",
            "turnover_rate_f": "3.1",
            "volume_ratio": "1.2",
            "pe": "8.5",
            "pe_ttm": "9.1",
            "pb": "0.92",
            "ps": "1.1",
            "ps_ttm": "1.2",
            "dv_ratio": "3.0",
            "dv_ttm": "3.2",
            "total_share": "1941000",
            "float_share": "1940000",
            "free_share": "1500000",
            "total_mv": "23950000",
            "circ_mv": "23900000",
            "limit_status": 0,
        },
        {
            "ts_code": "600000.SH",
            "trade_date": day,
            "close": "7.89" if trade_date == TRADE_DATES[0] else "8.02",
            "turnover_rate": "1.8",
            "turnover_rate_f": "2.2",
            "volume_ratio": "0.9",
            "pe": "5.6",
            "pe_ttm": "6.1",
            "pb": "0.45",
            "ps": "1.4",
            "ps_ttm": "1.5",
            "dv_ratio": "5.1",
            "dv_ttm": "5.3",
            "total_share": "2935200",
            "float_share": "2935000",
            "free_share": "2800000",
            "total_mv": "23160000",
            "circ_mv": "23150000",
            "limit_status": None,
        },
    ]


def _fina_indicator_rows(symbol: str) -> list[dict[str, object]]:
    base: dict[str, object] = {
        "ts_code": symbol,
        "ann_date": "20260430",
        "end_date": "20260331",
        "update_flag": "0",
        "eps": "2.10",
        "dt_eps": "2.05",
        "bps": "15.20",
        "ocfps": "3.10",
        "roe": "11.5",
        "roe_waa": "11.2",
        "grossprofit_margin": "45.2",
        "netprofit_margin": "30.1",
        "debt_to_assets": "46.5",
        "tr_yoy": "5.2",
        "netprofit_yoy": "8.3",
        "ocf_yoy": "12.4",
        # issue #401 批次 3 扩展字段(percent 类 / 倍率类各抽查)
        "or_yoy": "4.8",
        "basic_eps_yoy": "7.9",
        "dt_netprofit_yoy": "6.1",
        "op_yoy": "9.4",
        "q_gr_yoy": "3.3",
        "q_gr_qoq": "-2.1",
        "q_netprofit_yoy": "10.2",
        "q_netprofit_qoq": "-5.6",
        "roa": "7.4",
        "npta": "5.9",
        "roe_dt": "10.3",
        "roic": "8.8",
        "q_roe": "2.9",
        "q_npta": "1.5",
        "q_gsprofit_margin": "46.0",
        "q_netprofit_margin": "30.8",
        "expense_of_sales": "12.1",
        "inv_turn": "2.4",
        "ar_turn": "9.1",
        "ca_turn": "0.5",
        "fa_turn": "3.3",
        "assets_turn": "0.2",
        "current_ratio": "1.6",
        "quick_ratio": "1.4",
        "debt_to_eqt": "86.9",
        "ebit_to_interest": None,
        "assets_to_eqt": "2.4",
        "ocf_to_or": "0.27",
        "ocf_to_debt": "0.05",
    }
    revision = dict(base)
    revision.update(
        {
            "ann_date": "20260515",
            "update_flag": "1",
            "eps": "2.15",
            "dt_eps": "2.11",
        }
    )
    return [base, revision]


def _industry_rows(symbol: str) -> list[dict[str, object]]:
    if symbol == "000001.SZ":
        level3 = ("480000", "银行", "480100", "银行Ⅱ", "480101", "国有大型银行")
    else:
        level3 = ("480000", "银行", "480100", "银行Ⅱ", "480102", "股份制银行")
    return [
        {
            "l1_code": level3[0],
            "l1_name": level3[1],
            "l2_code": level3[2],
            "l2_name": level3[3],
            "l3_code": level3[4],
            "l3_name": level3[5],
            "ts_code": symbol,
            "name": "平安银行" if symbol == "000001.SZ" else "浦发银行",
            "in_date": "20211213",
            "out_date": None,
            "is_new": "Y",
        }
    ]


def _namechange_rows() -> list[dict[str, object]]:
    return [
        {
            "ts_code": "000001.SZ",
            "name": "深圳发展银行",
            "start_date": "19910403",
            "end_date": "20120409",
            "change_reason": "更名",
        },
        # #389 脏行:非 6 位数字代码,行级跳过并具名告警。
        {
            "ts_code": "X19363.SH",
            "name": "脏行标的",
            "start_date": "19930101",
            "end_date": None,
            "change_reason": None,
        },
        {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "start_date": "20120409",
            "end_date": None,
            "change_reason": None,
        },
    ]


def _cb_basic_rows(list_status: str) -> list[dict[str, object]]:
    if list_status == "D":
        return [
            {
                "ts_code": "127001.SZ",
                "bond_full_name": "金田转债摘牌",
                "bond_short_name": "金田转债",
                "stock_code": "000001.SZ",
                "stock_name": "平安银行",
                "list_date": "20200106",
                "delist_date": "20250110",
                "swap_price": "10.50",
                "value_date": "20191201",
                "mature_date": "20260101",
                "coupon_rate": "0.8",
            }
        ]
    return [
        {
            "ts_code": "127012.SZ",
            "bond_full_name": "浦发转债",
            "bond_short_name": "浦发转债",
            "stock_code": "600000.SH",
            "stock_name": "浦发银行",
            "list_date": "20210115",
            "delist_date": None,
            "swap_price": "12.80",
            "value_date": "20201201",
            "mature_date": "20261201",
            "coupon_rate": "1.2",
        }
    ]


class _FakeBudget:
    """预算桩:只计数,不限流、不落 usage 文件(共享预算口径的计数断言)。"""

    def __init__(self) -> None:
        self.acquires = 0

    async def acquire(self) -> None:
        self.acquires += 1


class FakeTushareClient:
    """SDK 同形 fake:方法名即 endpoint,返回原始 dict 行。"""

    def __init__(self, *, fail_financial_symbol: str | None = None) -> None:
        self.fail_financial_symbol = fail_financial_symbol

    def stock_basic(self, **kwargs: str) -> list[dict[str, object]]:
        return _stock_basic_rows(str(kwargs["list_status"]))

    def namechange(self, **kwargs: str) -> list[dict[str, object]]:
        return _namechange_rows()

    def daily_basic(self, **kwargs: str) -> list[dict[str, object]]:
        trade_date = str(kwargs["trade_date"])
        return _daily_basic_rows(date.fromisoformat(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"))

    def fina_indicator(self, **kwargs: str) -> list[dict[str, object]]:
        if kwargs["ts_code"] == self.fail_financial_symbol:
            raise RuntimeError("simulated upstream failure")
        return _fina_indicator_rows(kwargs["ts_code"])

    def index_member_all(self, **kwargs: str) -> list[dict[str, object]]:
        return _industry_rows(kwargs["ts_code"])

    def cb_basic(self, **kwargs: str) -> list[dict[str, object]]:
        return _cb_basic_rows(str(kwargs["list_status"]))


def _build_provider(
    client: FakeTushareClient, budget: _FakeBudget
) -> ResearchDataProvider:
    from finboard_data import TushareResearchDataProvider

    return TushareResearchDataProvider(
        client=client,
        budget=budget,
        now=lambda: NOW,
    )


def _fake_enrichment() -> (
    Callable[..., Awaitable[tuple[dict[str, str], list[LifecycleEvent]]]]
):
    """akshare 兜底增强的固定替身:评级映射 + 两条强赎事件。"""

    async def _inner(
        *, now: object = None
    ) -> tuple[dict[str, str], list[LifecycleEvent]]:
        from finboard_backtest.background_jobs.dataset_sync.specs import (
            _redemption_to_lifecycle_event,
        )

        ratings = {"127012.SZ": "AA+"}
        raw_events = [
            ConvertibleRedemptionEvent(
                code="127012.SZ",
                name="浦发转债",
                underlying_symbol="600000.SH",
                redemption_date=date(2026, 8, 15),
                stop_transfer_date=None,
                redemption_price=Decimal("100.50"),
                observed_at=REDEEM_OBSERVED_AT,
            ),
            ConvertibleRedemptionEvent(
                code="127001.SZ",
                name="金田转债",
                underlying_symbol="000001.SZ",
                redemption_date=date(2025, 1, 10),
                stop_transfer_date=None,
                redemption_price=Decimal("100.00"),
                observed_at=REDEEM_OBSERVED_AT,
            ),
        ]
        return ratings, [_redemption_to_lifecycle_event(item) for item in raw_events]

    return _inner


def _job(*, kind: str = "dataset_sync") -> JobRecord:
    return JobRecord(
        job_id="BJ-GOLDEN392",
        kind=kind,
        queue="data",
        payload={
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
        },
        attempt=1,
        max_attempts=3,
        requested_by="golden-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    return None


# ---- 快照与 fixture ----------------------------------------------------------


def _canonical(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        # PG 会话时区返回(本机 +08:00 / CI 可能 +00:00)—— 统一归一到 UTC,
        # 运行时钟(与 NOW 相差 1s 内)以 <NOW> 占位,保证跨环境可复现。
        utc = value.astimezone(UTC)
        if abs((utc - NOW).total_seconds()) < 1.0:
            return "<NOW>"
        return utc.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


async def _snapshot(
    engine: AsyncEngine, *, tables: tuple[type[Any], ...]
) -> dict[str, list[dict[str, object]]]:
    from sqlalchemy import select

    result: dict[str, list[dict[str, object]]] = {}
    async with session_factory(engine)() as session:
        for model in tables:
            rows: list[Any] = list((await session.execute(select(model))).scalars().all())
            serialized = [
                {
                    column.name: _canonical(getattr(row, column.name))
                    for column in model.__table__.columns
                    if column.name not in _EPHEMERAL_COLUMNS
                }
                for row in rows
            ]
            serialized.sort(key=lambda item: json.dumps(item, sort_keys=True))
            result[model.__tablename__] = serialized
    return result


def _normalize_placeholder(text: str) -> str:
    # dataset_version 的日期段来自执行机本地 date.today()(旧路径实现),
    # 用 <TODAY> 占位;datetime 的运行时钟已在 _canonical 归一为 <NOW>。
    return text.replace(date.today().isoformat(), "<TODAY>")


def _load_golden() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    return loaded


def _save_golden(payload: dict[str, object]) -> None:
    GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _compare_with_golden(section: str, snapshot: dict[str, list[dict[str, object]]]) -> None:
    actual_text = _normalize_placeholder(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    if os.getenv("FINBOARD_GOLDEN_REGENERATE"):
        golden = _load_golden() if GOLDEN_FILE.exists() else {}
        golden[section] = json.loads(actual_text)
        _save_golden(golden)
        return
    golden = _load_golden()
    assert section in golden, f"golden 缺少场景 {section}"
    assert json.loads(actual_text) == golden[section], (
        f"场景 {section} 产物与 golden 不一致;diff 详见断言输出"
    )


# ---- 场景执行 ----------------------------------------------------------------


async def _run_new_all_six(engine: AsyncEngine, client: FakeTushareClient, budget: _FakeBudget) -> None:
    """新路径:dataset_sync 框架(SyncSpec)全量同步 6 数据集。"""

    from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
    from finboard_backtest.background_jobs.dataset_sync import specs as specs_module

    executor = DatasetSyncExecutor(
        session_maker=session_factory(engine),
        provider_factory=lambda: _build_provider(client, budget),
    )
    original = specs_module._fetch_convertible_enrichment
    specs_module._fetch_convertible_enrichment = _fake_enrichment()  # type: ignore[assignment]
    try:
        result = await executor.execute(_job(kind="dataset_sync"), _noop_progress)
    finally:
        specs_module._fetch_convertible_enrichment = original
    assert result.status == "succeeded"


async def _run_new_upstream_failure(engine: AsyncEngine) -> _FakeBudget:
    """新路径:financial_indicators 对 600000.SH 上游失败(死点记账对照)。"""

    from finboard_backtest.background_jobs.contracts import ExecutorError
    from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
    from finboard_backtest.background_jobs.dataset_sync import specs as specs_module

    client = FakeTushareClient(fail_financial_symbol="600000.SH")
    budget = _FakeBudget()
    executor = DatasetSyncExecutor(
        session_maker=session_factory(engine),
        provider_factory=lambda: _build_provider(client, budget),
    )
    original = specs_module._fetch_convertible_enrichment
    specs_module._fetch_convertible_enrichment = _fake_enrichment()  # type: ignore[assignment]
    try:
        error: ExecutorError | None = None
        try:
            await executor.execute(_job(kind="dataset_sync"), _noop_progress)
        except ExecutorError as exc:
            error = exc
    finally:
        specs_module._fetch_convertible_enrichment = original
    assert error is not None
    assert error.code == "research_data_upstream"
    assert error.retryable is True
    return budget


# ---- tests -------------------------------------------------------------------


class TestGoldenDatasetSync:
    """dataset_sync 框架产物对 golden fixture 逐值对照。

    fixture 由迁移前的旧 ``research_data_sync`` 路径落定;框架迁移时以
    新旧双跑逐字节等值验收,旧路径删除后本类即新路径回归(语义锚点防
    fixture 自证)。
    """

    async def test_all_six_products_match_golden(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FINBOARD_CODE_VERSION", "golden392")
        client = FakeTushareClient()
        budget = _FakeBudget()
        await _run_new_all_six(engine, client, budget)
        # 预算共享计数与旧路径一致(provider 调用次数不变)。
        assert budget.acquires == 11

        snapshot = await _snapshot(engine, tables=_SNAPSHOT_TABLES)
        batch_status = {
            (row["dataset"], row["dataset_version"]): row["status"]
            for row in snapshot["research_sync_batches"]
        }
        assert set(batch_status.values()) == {"published"}
        assert len(snapshot["research_instrument_profiles"]) == 3  # 脏行已跳过
        assert all(
            row["instrument_code"] != "T600018.SH"
            for row in snapshot["instrument_names"]
        )
        daily_available = {
            row["available_at"] for row in snapshot["research_daily_metrics"]
        }
        # 交易日 17:00 上海时区 = 09:00 UTC(快照统一归一到 UTC naive)。
        assert daily_available == {
            "2026-07-24T09:00:00",
            "2026-07-27T09:00:00",
        }
        assert len(snapshot["instrument_lifecycle_events"]) == 2
        _compare_with_golden("all_six", snapshot)

    async def test_upstream_failure_accounting_matches_golden(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FINBOARD_CODE_VERSION", "golden392")
        await _run_new_upstream_failure(engine)

        snapshot = await _snapshot(engine, tables=(ResearchSyncBatchModel,))
        failed = [
            row
            for row in snapshot["research_sync_batches"]
            if row["status"] == "failed"
        ]
        assert len(failed) == 1
        assert failed[0]["dataset_version"] == (
            f"financial:600000.SH:{START_DATE.isoformat()}:{END_DATE.isoformat()}"
        )
        assert failed[0]["error_summary"] == (
            "ResearchDataUpstreamError: upstream fetch failed"
        )
        assert failed[0]["parameters"] == {
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
        }
        _compare_with_golden("upstream_failure", snapshot)
