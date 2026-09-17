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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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
from finboard_data.research import (
    FuturesContractProfile,
    FuturesTradeCalendarDay,
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
from finboard_shared.models import Bar
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
    return list(pd.bdate_range(_START, _END))


def _main_frame(start: str, end: str) -> pd.DataFrame:
    """形制对齐 akshare ``futures_main_sina``:中文列 + 持仓量,无成交额。

    价格温和扰动(3500 上下),保证 OHLC 关系成立;主连价格是换月
    拼接产物,合成数据只要形状/数值合法即可(数据面不验证价格语义)。
    """
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    window = [day for day in _days() if s <= day <= e]
    dates: list[datetime] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for index, day in enumerate(window):
        base = Decimal("3500") + Decimal(index % 30) / Decimal(10)
        dates.append(day.to_pydatetime())
        opens.append(float(base - Decimal("2")))
        highs.append(float(base + Decimal("6")))
        lows.append(float(base - Decimal("6")))
        closes.append(float(base + Decimal("2")))
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘价": opens,
            "最高价": highs,
            "最低价": lows,
            "收盘价": closes,
            "成交量": [100000 + index for index in range(len(dates))],
            "持仓量": [200000 + index for index in range(len(dates))],
        }
    )


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
    """具体合约代码在 akshare 缓存层 fail-visible 拒绝 —— 主连/合约语义不混淆。"""
    from unittest.mock import patch

    provider = AkShareProvider(
        cache_dir=tmp_path / "cache2",
        use_cache=True,
        request_interval=0,
        max_retries=1,
    )
    contract = make_symbol("IF2406.CFFEX")
    with patch("akshare.futures_main_sina", side_effect=AssertionError("不应发起网络请求")):
        with pytest.raises(ValueError, match="主连"):
            await provider.fetch_bars(contract, BarPeriod.D1, _START, _END)


# ---------------------------------------------------------------------------
# #395:tushare 期货链路(fut_basic 合约登记 + fut_daily 日线 + fut_trade_cal)
# ---------------------------------------------------------------------------


def _fut_daily_frame(days: list[pd.Timestamp], *, ts_code: str) -> list[dict[str, object]]:
    """形制对齐 tushare ``fut_daily``:vol 手、amount 万元(2026-09-09 实测)。"""
    rows: list[dict[str, object]] = []
    for index, day in enumerate(days):
        base = Decimal("3500") + Decimal(index % 30) / Decimal(10)
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": day.strftime("%Y%m%d"),
                "open": float(base - Decimal("2")),
                "high": float(base + Decimal("6")),
                "low": float(base - Decimal("6")),
                "close": float(base + Decimal("2")),
                "vol": 64495.0 + index,
                "amount": 7459193.514 + index,
            }
        )
    return rows


class _FakeTushareFuturesClient:
    """离线 tushare client:只打 fut_daily(记录 ts_code 翻译)。"""

    def __init__(self, days: list[pd.Timestamp]) -> None:
        self._days = days
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fut_daily(self, **kwargs: str) -> object:
        self.calls.append(("fut_daily", kwargs))
        # 单窗口测试窗口小,直接全量返回(按请求窗口裁剪与真实上游一致地空)。
        start = pd.Timestamp(kwargs["start_date"])
        end = pd.Timestamp(kwargs["end_date"])
        window = [day for day in self._days if start <= day <= end]
        ts_code = kwargs["ts_code"]
        if ts_code.startswith("IF2612"):
            return _fut_daily_frame(window, ts_code=ts_code)
        return []

    def daily(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def cb_daily(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def adj_factor(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def suspend_d(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")


class _NoopBudget:
    async def acquire(self) -> None:
        return None


async def _register_tushare_chain_instruments(engine: AsyncEngine) -> None:
    """合约级登记(discover_futures_contracts 离线注入)+ 主连受控登记。"""
    profiles = [
        FuturesContractProfile(
            symbol="IF2612.CFFEX",
            name="IF2612",
            product="IF",
            exchange="CFFEX",
            multiplier=Decimal("300"),
            price_tick=Decimal("0.2"),
            quote_unit_desc="0.2指数点",
            list_date=_START,
            delist_date=None,
            source="tushare",
            observed_at=datetime.now(UTC),
            available_at=datetime.now(UTC),
        )
    ]

    class _StubProvider:
        async def fetch_futures_contract_profiles(
            self,
        ) -> list[FuturesContractProfile]:
            return profiles

    contracts = await UniverseDiscovery().discover_futures_contracts(_StubProvider())  # type: ignore[arg-type]
    assert [ins.code for ins in contracts] == ["IF2612.CFFEX"]
    mains = await UniverseDiscovery().discover_futures_main()
    assert any(ins.code == _MAIN for ins in mains)

    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
            "listing_board": ins.listing_board.value,
        }
        for ins in [*contracts, *mains]
    ]
    async with session_factory(engine)() as session:
        result = await InstrumentRepository(session).sync_with_diff(
            dicts, as_of=date.today()
        )
        await session.commit()
    assert result.new == len(dicts)


async def test_tushare_contract_registration_and_daily_chain(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """tushare 链路(#395):合约登记 → fut_daily 缓存(万元口径)→ 发布 → 读回。"""
    from finboard_data.tushare_bar_provider import TushareBarProvider

    await _register_tushare_chain_instruments(engine)
    contract = "IF2612.CFFEX"

    cache_dir = tmp_path / "tushare-cache"
    cache_dir.mkdir()
    client = _FakeTushareFuturesClient(_days())
    provider = TushareBarProvider(
        client=client,
        budget=_NoopBudget(),
        cache_dir=cache_dir,
        use_cache=True,
        max_retries=0,
    )
    ok = await provider.update_cache(make_symbol(contract), BarPeriod.D1, _START, _END)
    assert ok
    # ts_code 翻译:具体合约 IF2612.CFFEX → IF2612.CFX(后缀映射)。
    assert client.calls, "fut_daily 应被调用"
    assert all(kwargs["ts_code"] == "IF2612.CFX" for _, kwargs in client.calls)

    # 缓存 read-through:同窗口第二次拉取应命中缓存(不再触发 mock 网络)。
    class _ExplodeClient(_FakeTushareFuturesClient):
        def fut_daily(self, **kwargs: str) -> object:
            raise AssertionError("应命中缓存")

    reader = TushareBarProvider(
        client=_ExplodeClient(_days()),
        budget=_NoopBudget(),
        cache_dir=cache_dir,
        use_cache=True,
        max_retries=0,
    )
    bars = await reader.fetch_bars(
        make_symbol(contract), BarPeriod.D1, _START, _END
    )
    assert bars, "tushare 期货日线缓存为空"
    assert all(bar.source == "tushare" for bar in bars)
    # amount 万元 → 元(x10000):首日 raw amount=7459193.514 万元。
    assert bars[0].amount == Decimal("7459193.514") * Decimal("10000")
    assert bars[0].volume == Decimal("64495")

    # 发布(source=tushare)→ FrozenReleaseProvider 读回。
    release_root = tmp_path / "tushare-releases"
    release_root.mkdir()
    async with session_factory(engine)() as session:
        service = ResearchDatasetReleaseService(
            session,
            cache_dir=cache_dir,
            release_root=release_root,
        )
        await service.publish(
            DatasetReleaseSpec(
                release_id="fut-bars-r395",
                dataset_name="fut_chain_bars_tushare",
                source="tushare",
                version="r395",
                start_date=_START,
                end_date=_END,
                code_version="integration-test",
                dataset_kind=ReleaseDatasetKind.BARS,
                required_capabilities=("futures",),
            ),
            [contract],
        )
        await session.commit()
    frozen = FrozenReleaseProvider(
        release_root=release_root,
        release_id="fut-bars-r395",
    )
    assert frozen.release.is_usable
    item = frozen.release.instrument(contract)
    assert item.instrument_type.value == "futures"
    assert item.market.value == "future"
    read_back = await frozen.fetch_bars(
        make_symbol(contract), BarPeriod.D1, _START, _END
    )
    assert [bar.timestamp for bar in read_back] == [bar.timestamp for bar in bars]
    assert [bar.close for bar in read_back] == [bar.close for bar in bars]


async def test_tushare_trade_cal_upsert_and_read(engine: AsyncEngine) -> None:
    """fut_trade_cal 行集幂等落库 trade_cal(CFFEX 行集,#396 同表同构)。"""
    from finboard_persistence import TradeCalRepository

    observed = datetime.now(UTC)
    days = [
        FuturesTradeCalendarDay(
            exchange="CFFEX",
            cal_date=date(2026, 9, 9),
            is_open=True,
            pretrade_date=date(2026, 9, 8),
            source="tushare",
            observed_at=observed,
            available_at=observed,
        ),
        FuturesTradeCalendarDay(
            exchange="CFFEX",
            cal_date=date(2026, 10, 1),
            is_open=False,
            pretrade_date=date(2026, 9, 30),
            source="tushare",
            observed_at=observed,
            available_at=observed,
        ),
    ]
    async with session_factory(engine)() as session:
        repo = TradeCalRepository(session)
        first = await repo.upsert_calendar_days(days, source="tushare")
        assert first == {"received": 2, "written": 2, "trading_days": 1}
        # 幂等:重跑同一上游快照,读取口径不变。
        await repo.upsert_calendar_days(days, source="tushare")
        trading = await repo.list_trading_days("CFFEX")
        await session.commit()
    assert trading == {date(2026, 9, 9)}
    # SSE 行集不受 CFFEX 行集影响(exchange 区分)。
    async with session_factory(engine)() as session:
        assert await TradeCalRepository(session).list_trading_days("SSE") == set()
