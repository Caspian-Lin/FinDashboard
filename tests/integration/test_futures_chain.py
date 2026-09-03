"""期货 EOD 数据链路端到端集成测试(issue #267)。

打通「期货主连登记 → 主连日线入缓存 → 冻结发布 → 作为研究数据可读」
全链路(数据面;期货撮合与对冲组合工程另行立项):

* ``discover_futures_main``(受控登记表 ``FUTURES_MAIN_SERIES_REGISTRY``,
  无网络调用)→ ``InstrumentRepository.sync_with_diff`` → instruments 表
  出现 ``market=future`` / ``instrument_type=futures`` 行(登记写入者,
  #256 指数同型);
* ``AkShareProvider``(mock ``futures_main_sina``)把 IF 主连日线同步进
  parquet 缓存(期货无复权,缓存键 no-op adjust,#256 同策略);具体合约
  代码在缓存层 fail-visible 拒绝 —— 主连 / 具体合约语义不混淆;
* ``multi_asset_mixed`` 之前的 BARS 发布(source=akshare,期货主连带
  ``futures`` capability)从缓存冻结;
* **真实 FrozenReleaseProvider** 读发布取回 IF 主连 bars —— 期货作为
  研究数据 / 基准可读;``is_benchmark_only_instrument`` 把期货排除出
  研究运行候选池(不可撮合,对齐 #184 指数先例)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。akshare 全部 mock,不联网;
不连 broker / 不下实盘单,不触实盘单市场硬约束议题。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.strategy_spec.universe_precheck import (
    is_benchmark_only_instrument,
)
from finboard_data import AkShareProvider
from finboard_data.cache import make_symbol
from finboard_data.discovery import UniverseDiscovery
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
)
from finboard_persistence import (
    Base,
    InstrumentRepository,
    ResearchDatasetReleaseService,
    session_factory,
)
from finboard_persistence.models import (
    InstrumentModel,
    ResearchDatasetReleaseModel,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2025, 12, 31)
_RELEASE_BARS = "fut-bars-r267"

#: IF 主连(路线 C 空头腿,登记表首个品种);基准排除断言用指数行对照。
_MAIN = "IF0.CFFEX"
_INDEX = "000300.SH"


def _days() -> list[pd.Timestamp]:
    """发布区间的营业日(合成 CFFEX 日线用,与 A 股日历近似即可)。"""
    return pd.bdate_range(_START, _END)


def _main_frame(start: str, end: str) -> pd.DataFrame:
    """形制对齐 akshare ``futures_main_sina``:中文列 + 持仓量,无成交额。

    价格温和正弦扰动(3500 上下),保证 OHLC 关系成立;主连价格是换月
    拼接产物,合成数据只要形状/数值合法即可(数据面不验证价格语义)。
    """
    days = _days()
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    window = days[(days >= s) & (days <= e)]
    rows: dict[str, object] = {"日期": [], "开盘价": [], "最高价": [], "最低价": [], "收盘价": []}
    for index, day in enumerate(window):
        base = Decimal("3500") + Decimal(index % 30) / Decimal(10)
        o = base - Decimal("2")
        c = base + Decimal("2")
        h = base + Decimal("6")
        low = base - Decimal("6")
        rows["日期"].append(day.to_pydatetime())
        rows["开盘价"].append(float(o))
        rows["最高价"].append(float(h))
        rows["最低价"].append(float(low))
        rows["收盘价"].append(float(c))
    rows["成交量"] = [100000 + index for index in range(len(window))]
    rows["持仓量"] = [200000 + index for index in range(len(window))]
    return pd.DataFrame(rows)


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


def create_engine(url: str) -> AsyncEngine:
    from finboard_persistence import create_async_engine

    return create_async_engine(url)


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in (
            InstrumentModel,
            ResearchDatasetReleaseModel,
        ):
            await conn.execute(delete(table))


async def _register_instruments(engine: AsyncEngine) -> None:
    """期货主连走 discover_futures_main(受控登记表),指数行供排除对照。"""
    futures = await UniverseDiscovery().discover_futures_main()
    assert any(ins.code == _MAIN for ins in futures)

    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
            "listing_board": ins.listing_board.value,
        }
        for ins in futures
    ]
    dicts.append(
        {
            "code": _INDEX,
            "name": "沪深300",
            "market": "a_share",
            "instrument_type": "index",
            "exchange": "SSE",
            "listing_board": "unknown",
        }
    )
    async with session_factory(engine)() as session:
        result = await InstrumentRepository(session).sync_with_diff(
            dicts, as_of=date.today()
        )
        await session.commit()
    assert result.new == len(dicts)


async def _sync_main_bars_into_cache(cache_dir: Path) -> list[Bar]:
    """mock futures_main_sina → AkShareProvider 缓存链路,返回拉取的 bars。"""
    from unittest.mock import patch

    provider = AkShareProvider(
        cache_dir=cache_dir,
        use_cache=True,
        request_interval=0,
        max_retries=1,
    )
    with patch(
        "akshare.futures_main_sina",
        side_effect=lambda *, symbol, start_date, end_date: _main_frame(start_date, end_date),
    ):
        bars = await provider.fetch_bars(
            make_symbol(_MAIN), BarPeriod.D1, _START, _END
        )
    assert bars, "主连日线同步结果为空"
    assert all(_START <= bar.timestamp.date() <= _END for bar in bars)
    assert all(bar.close > 0 for bar in bars)
    # 缓存 read-through:第二次拉取应命中缓存(不再触发 mock 网络也能取到)。
    with patch("akshare.futures_main_sina", side_effect=AssertionError("应命中缓存")):
        cached = await provider.fetch_bars(
            make_symbol(_MAIN), BarPeriod.D1, _START, _END
        )
    assert [bar.timestamp for bar in cached] == [bar.timestamp for bar in bars]
    return bars


async def _publish_bars_release(
    engine: AsyncEngine, cache_dir: Path, release_root: Path
) -> None:
    async with session_factory(engine)() as session:
        service = ResearchDatasetReleaseService(
            session,
            cache_dir=cache_dir,
            release_root=release_root,
        )
        await service.publish(
            DatasetReleaseSpec(
                release_id=_RELEASE_BARS,
                dataset_name="fut_chain_bars",
                source="akshare",
                version="r267",
                start_date=_START,
                end_date=_END,
                code_version="integration-test",
                dataset_kind=ReleaseDatasetKind.BARS,
                required_capabilities=("futures",),
            ),
            [_MAIN],
        )
        await session.commit()


async def test_registration_via_sync_with_diff(engine: AsyncEngine) -> None:
    """discover_futures_main 经 sync_with_diff 写入 instruments(#267 断点②)。"""
    await _register_instruments(engine)

    async with session_factory(engine)() as session:
        row = (
            await session.execute(
                select(InstrumentModel).where(InstrumentModel.code == _MAIN)
            )
        ).scalar_one()
        assert row.instrument_type == "futures"
        assert row.market == "future"
        assert row.exchange == "CFFEX"
        # 主连是连续序列,无 list_date 结构化上游:保持 null 可见缺失(#256 同风格)。
        assert row.list_date is None


async def test_sync_publish_and_read_back(engine: AsyncEngine, tmp_path: Path) -> None:
    """主连日线同步 → 发布 → 真实 FrozenReleaseProvider 可读(#267 E2E)。"""
    await _register_instruments(engine)
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    cache_dir.mkdir()
    release_root.mkdir()

    bars = await _sync_main_bars_into_cache(cache_dir)
    await _publish_bars_release(engine, cache_dir, release_root)

    provider = FrozenReleaseProvider(
        release_root=release_root,
        release_id=_RELEASE_BARS,
    )
    assert provider.release.is_usable
    item = provider.release.instrument(_MAIN)
    assert item.instrument_type.value == "futures"
    assert item.market.value == "future"

    read_back = await provider.fetch_bars(
        make_symbol(_MAIN), BarPeriod.D1, _START, _END
    )
    assert [bar.timestamp for bar in read_back] == [bar.timestamp for bar in bars]
    assert [bar.close for bar in read_back] == [bar.close for bar in bars]

    # 期货主连与指数同属基准数据资产:不进研究运行候选池(#184 先例)。
    # 谓词入参是带 instrument_type 属性的对象(ORM 行 / 规格对象)。
    def _like(code: str, instrument_type: str) -> SimpleNamespace:
        return SimpleNamespace(code=code, instrument_type=instrument_type)

    assert is_benchmark_only_instrument(_like(_MAIN, "futures")) is True
    assert is_benchmark_only_instrument(_like(_INDEX, "index")) is True
    assert is_benchmark_only_instrument(_like("600519.SH", "stock")) is False


async def test_main_continuous_semantics_guard(tmp_path: Path) -> None:
    """具体合约代码在缓存层 fail-visible 拒绝 —— 主连/合约语义不混淆。"""
    from unittest.mock import patch

    provider = AkShareProvider(
        cache_dir=tmp_path / "cache2",
        use_cache=True,
        request_interval=0,
        max_retries=1,
    )
    contract = cast(Symbol, make_symbol("IF2406.CFFEX"))
    with patch("akshare.futures_main_sina", side_effect=AssertionError("不应发起网络请求")):
        with pytest.raises(ValueError, match="主连"):
            await provider.fetch_bars(contract, BarPeriod.D1, _START, _END)
