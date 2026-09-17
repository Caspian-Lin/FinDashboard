"""mixed 发布任意子集放行回归测试。

历史行为:#144 时代语义是「multi_asset_mixed 必须同时包含股票和 ETF」;
#184/#265/#267 逐类型放行时注释与文案改成「至少含其一」,但执行器的集合差
仍按「五者缺一不可」求值 —— 任意非全五子集(如 股票 + 期货主连,哪怕期货
已登记且缓存有数据)被 ``mixed_scope_violation`` 误拒。

修复后语义:五种放行类型(stock/etf/index/convertible/futures)的任意非空
子集均可发布;只拒绝白名单之外的 instrument_type(fail-closed,防未来新增
类型静默混入 bars 发布)。

* 股票 + 期货主连子集经真实 ``DatasetPublishExecutor`` 发布成功,发布冻结
  两只标的(期货腿经 mock akshare ``futures_main_sina`` 写缓存,键与
  test_futures_chain 同源);
* 白名单外类型具名拒绝,不写任何发布数据。

需 PostgreSQL(``FINBOARD_TEST_DB_URL``)。akshare 全部 mock,不联网;
不连 broker / 不下实盘单,纯离线数据域。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.background_jobs.contracts import ExecutorError, JobRecord
from finboard_backtest.background_jobs.executors.dataset_publish import (
    DatasetPublishExecutor,
)
from finboard_data import AkShareProvider
from finboard_data.cache import ParquetCache, make_symbol
from finboard_data.discovery import UniverseDiscovery
from finboard_persistence import (
    InstrumentRepository,
    ResearchDatasetReleaseModel,
    ResearchDatasetReleaseRepository,
    session_factory,
)
from finboard_persistence.models import InstrumentModel
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_STOCK = "TST080.SH"
_MAIN = "IF0.CFFEX"
_RELEASE_ID = "mixed-subset-v1"


@pytest_asyncio.fixture
async def registered_stock_and_futures(db_session: AsyncSession) -> AsyncIterator[None]:
    """股票行手工登记;期货主连走受控登记表 discover_futures_main(#267 同型)。"""
    await db_session.execute(
        delete(InstrumentModel).where(
            InstrumentModel.code.in_((_STOCK, _MAIN))
        )
    )
    db_session.add(
        InstrumentModel(
            code=_STOCK,
            name="mixed 子集回归股票样本",
            market="a_share",
            instrument_type="stock",
            exchange="SSE",
            list_date=date(2020, 1, 1),
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.flush()
    futures = await UniverseDiscovery().discover_futures_main()
    assert any(ins.code == _MAIN for ins in futures)
    await InstrumentRepository(db_session).sync_with_diff(
        [
            {
                "code": ins.code,
                "name": ins.name,
                "market": ins.market.value,
                "instrument_type": ins.instrument_type.value,
                "exchange": ins.exchange,
                "listing_board": ins.listing_board.value,
            }
            for ins in futures
        ],
        as_of=date.today(),
    )
    await db_session.commit()
    yield
    await db_session.rollback()


async def _seed_stock_bars(cache_dir: Path) -> None:
    cache = ParquetCache(cache_dir)
    symbol = Symbol(_STOCK, Market.A_SHARE)
    bars = [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(
                _START + timedelta(days=offset),
                datetime.min.time(),
                tzinfo=UTC,
            ),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=Decimal("1000"),
            amount=Decimal("10500"),
            source="tushare",
        )
        for offset in range(4)
    ]
    await cache.write(symbol, BarPeriod.D1, "qfq", bars)


async def _seed_futures_bars(cache_dir: Path) -> None:
    """mock akshare ``futures_main_sina`` → AkShareProvider 写期货主连缓存。

    与 test_futures_chain 同一写入口,保证缓存键/复权语义与发布读取端
    一致(不在此处手工猜键)。
    """
    from unittest.mock import patch

    frame = pd.DataFrame(
        {
            "日期": list(pd.bdate_range(_START, _END).to_pydatetime()),
            "开盘价": [3498.0 + index for index in range(4)],
            "最高价": [3506.0 + index for index in range(4)],
            "最低价": [3494.0 + index for index in range(4)],
            "收盘价": [3502.0 + index for index in range(4)],
            "成交量": [100000 + index for index in range(4)],
            "持仓量": [200000 + index for index in range(4)],
        }
    )
    provider = AkShareProvider(
        cache_dir=cache_dir,
        use_cache=True,
        request_interval=0,
        max_retries=1,
    )
    with patch("akshare.futures_main_sina", side_effect=lambda **_: frame):
        bars = await provider.fetch_bars(make_symbol(_MAIN), BarPeriod.D1, _START, _END)
    assert len(bars) == 4


def _job(symbols: list[str]) -> JobRecord:
    return JobRecord(
        job_id="BJ-MIXSUB01",
        kind="dataset_publish",
        queue="data",
        payload={
            "release_id": _RELEASE_ID,
            "dataset_name": "multi_asset_daily_bars",
            "release_kind": "multi_asset_mixed",
            "version": "subset-v1",
            "start_date": _START.isoformat(),
            "end_date": _END.isoformat(),
            "adjustment": "qfq",
            "symbols": symbols,
        },
        attempt=1,
        max_attempts=3,
        requested_by="integration-test",
    )


async def _cleanup_release(db_session: AsyncSession) -> None:
    await db_session.execute(
        delete(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.release_id == _RELEASE_ID
        )
    )
    await db_session.commit()


@pytest.mark.usefixtures("registered_stock_and_futures")
async def test_stock_futures_subset_publishes(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """股票 + 期货主连子集(修复前被误拒)应发布成功并冻结两只标的。"""
    await _seed_stock_bars(tmp_path / "cache")
    await _seed_futures_bars(tmp_path / "cache")
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.cache_dir",
        lambda: str(tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.release_root",
        lambda: str(tmp_path / "releases"),
    )
    executor = DatasetPublishExecutor(session_maker=session_factory(_engine))
    result = await executor.execute(_job([_STOCK, _MAIN]), _noop_progress)
    assert result.status == "succeeded"
    assert result.result_ref == _RELEASE_ID

    release = await ResearchDatasetReleaseRepository(db_session).get(_RELEASE_ID)
    assert release is not None
    assert sorted(item.code for item in release.instruments) == [_MAIN, _STOCK]
    await _cleanup_release(db_session)


@pytest.mark.usefixtures("registered_stock_and_futures")
async def test_disallowed_type_rejected(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
) -> None:
    """白名单外的 instrument_type 具名拒绝(fail-closed 防御分支)。"""
    db_session.add(
        InstrumentModel(
            code="BND001.SH",
            name="未放行类型样本",
            market="a_share",
            instrument_type="bond",
            exchange="SSE",
            list_date=date(2020, 1, 1),
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.commit()
    executor = DatasetPublishExecutor(session_maker=session_factory(_engine))
    with pytest.raises(ExecutorError) as exc_info:
        await executor.execute(_job(["BND001.SH"]), _noop_progress)
    assert exc_info.value.code == "mixed_scope_violation"
    assert exc_info.value.summary is not None
    assert "bond" in exc_info.value.summary
    assert "包含未放行类型" in exc_info.value.summary
    assert await ResearchDatasetReleaseRepository(db_session).get(_RELEASE_ID) is None


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass
