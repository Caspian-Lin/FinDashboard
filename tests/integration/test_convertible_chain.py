"""可转债数据链路端到端集成测试(issue #265)。

打通「转债登记 → 行情入缓存 → 条款元数据回填 → 冻结发布 →
convertible_double_low 回测」全链路:

* ``discover_convertibles``(东财一览 bond_zh_cov,mock)→
  ``InstrumentRepository.sync_with_diff`` → instruments 表出现
  ``instrument_type=convertible`` 行(登记写入者,#256 指数同型);
* ``TushareBarProvider``(mock cb_daily / daily 客户端)把转债 + 正股日线
  同步进 parquet 缓存(转债走 2000 积分档 ``cb_daily`` 专属接口,#257 反例
  对照:tushare 拉得到);
* ``dataset_sync`` ``convertible_profiles`` 数据集:tushare cb_basic
  (mock)→ ``convertible_metadata`` upsert + instruments.list_date 回填 +
  akshare 评级 / 集思录强赎事件兜底(mock);
* ``multi_asset_mixed`` bars 发布(转债与正股同处一份发布)+
  ``convertible_metrics`` 发布(转股价值 / 转股溢价率从缓存 bars x 冻结
  转股价计算,带日期冻结观测);
* ``convertible_double_low`` 用**真实 FrozenReleaseProvider** 读发布构建
  逐日快照,2024-2025(2024-2026 区间)合成数据跑出非空成交。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。akshare / tushare 全部 mock,
不联网;不连 broker / 不下实盘单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
from finboard_backtest.convertible_double_low.backtest import run_backtest
from finboard_backtest.convertible_double_low.config import ConvertibleDoubleLowConfig
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_data import TushareBarProvider
from finboard_data.cache import make_symbol
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
)
from finboard_data.trading_calendar import trading_days
from finboard_persistence import (
    Base,
    InstrumentRepository,
    ResearchDatasetReleaseService,
    session_factory,
)
from finboard_persistence.models import (
    ConvertibleMetadataModel,
    InstrumentLifecycleEventModel,
    InstrumentModel,
    InstrumentNameModel,
    ResearchDatasetReleaseModel,
)
from finboard_shared.instruments import ConvertibleMetadata, Instrument
from finboard_shared.models import Symbol
from finboard_shared.types import (
    BarPeriod,
    InstrumentType,
    ListingStatus,
    Market,
)

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2025, 12, 31)
_MATURITY = date(2030, 12, 31)
_RELEASE_BARS = "conv-bars-r265"
_RELEASE_CONV = "conv-metrics-r265"

#: 转债代码必须满足 is_convertible_code(11xxxx.SH / 12xxxx.SZ)。
_BONDS = ("113050.SH", "113051.SH", "123101.SZ", "123102.SZ")
_UNDERLYINGS = ("600001.SH", "600002.SH", "000001.SZ", "000002.SZ")
_BOND_BASE = {"113050.SH": "105", "113051.SH": "108", "123101.SZ": "104", "123102.SZ": "111"}
_CONVERSION_PRICE = Decimal("10")
_STOCK_BASE = Decimal("10")


def _days() -> list[date]:
    """发布区间的真实 A 股交易日(exchange_calendars 本地日历,不联网)。"""
    return sorted(trading_days(_START, _END))


def _price(base: Decimal, index: int) -> Decimal:
    """温和正弦扰动:价格有波动但不越界(转债保持 100-130,正股 10 上下)。"""
    wave = Decimal(index % 20 - 10) / Decimal(400)  # -0.025 .. +0.02475
    return (base * (Decimal(1) + wave)).quantize(Decimal("0.0001"))


def _bond_close(code: str, index: int) -> Decimal:
    return _price(Decimal(_BOND_BASE[code]), index)


def _bond_volume(index: int) -> Decimal:
    # 20 万手 = 200 万张;amount = 收盘 x 数量(约 2 亿元)远超流动性下限。
    return Decimal("2000000") + Decimal(index % 7) * Decimal("10000")


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
            InstrumentLifecycleEventModel,
            InstrumentNameModel,
            ConvertibleMetadataModel,
            InstrumentModel,
            ResearchDatasetReleaseModel,
        ):
            await conn.execute(delete(table))


# ---- 步骤①:登记(mock bond_zh_cov → discover_convertibles → sync_with_diff)----


def _overview_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "债券代码": [code.split(".")[0] for code in _BONDS],
            "债券简称": ["链路转债一", "链路转债二", "链路转债三", "链路转债四"],
            "正股代码": [code.split(".")[0] for code in _UNDERLYINGS],
            "债券评级": ["AA+", "AA", "AA", None],
            "转股价": ["10", "10", "10", "10"],
        }
    )


async def _register_instruments(engine: AsyncEngine) -> list[dict[str, object]]:
    """转债走 discover_convertibles(东财一览 mock),正股按手工清单登记。"""
    from unittest.mock import patch

    from finboard_data.discovery import UniverseDiscovery

    with patch("akshare.bond_zh_cov", return_value=_overview_frame()):
        convertibles = await UniverseDiscovery().discover_convertibles()
    assert {ins.code for ins in convertibles} == set(_BONDS)

    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
            "listing_board": ins.listing_board.value,
        }
        for ins in convertibles
    ]
    dicts += [
        {
            "code": code,
            "name": f"正股{index}",
            "market": "a_share",
            "instrument_type": "stock",
            "exchange": "SSE" if code.endswith(".SH") else "SZSE",
            "listing_board": "sse_main" if code.endswith(".SH") else "szse_main",
        }
        for index, code in enumerate(_UNDERLYINGS)
    ]
    async with session_factory(engine)() as session:
        result = await InstrumentRepository(session).sync_with_diff(
            dicts, as_of=date.today()
        )
        await session.commit()
    assert result.new == len(_BONDS) + len(_UNDERLYINGS)
    return dicts


async def test_convertible_registration_via_sync_with_diff(engine: AsyncEngine) -> None:
    """discover_convertibles 经 sync_with_diff 写入 instruments(#265 断点①)。"""
    await _register_instruments(engine)

    async with session_factory(engine)() as session:
        rows = (
            (await session.execute(select(InstrumentModel))).scalars().all()
        )
        by_code = {row.code: row for row in rows}
        assert set(by_code) == set(_BONDS) | set(_UNDERLYINGS)
        bond_row = by_code["113050.SH"]
        assert bond_row.instrument_type == "convertible"
        assert bond_row.market == "a_share"
        assert bond_row.exchange == "SSE"
        # akshare 发现链路无上市日上游:保持 null,等 cb_basic 回填(断点③)。
        assert bond_row.list_date is None
        assert by_code["123101.SZ"].exchange == "SZSE"


# ---- 步骤②:转债 + 正股日线同步入缓存(mock tushare cb_daily / daily)---------


class _FakeChainTushareClient:
    """转债走 cb_daily、正股走 daily+adj_factor;记录调用供断言。"""

    def __init__(self, days: list[date]) -> None:
        self.days = days
        self.calls: list[tuple[str, str]] = []

    def _rows(
        self,
        ts_code: str,
        start: str,
        end: str,
        close_of,
        volume_of,
    ) -> list[dict[str, object]]:
        s = date(int(start[:4]), int(start[4:6]), int(start[6:]))
        e = date(int(end[:4]), int(end[4:6]), int(end[6:]))
        rows = []
        for index, day in enumerate(self.days):
            if s <= day <= e:
                close = close_of(index)
                rows.append(
                    {
                        "ts_code": ts_code,
                        "trade_date": day.strftime("%Y%m%d"),
                        "open": float(close),
                        "high": float(close) * 1.005,
                        "low": float(close) * 0.995,
                        "close": float(close),
                        "vol": float(volume_of(index)),
                        "amount": float(close) * float(volume_of(index)) / 1000,
                    }
                )
        return rows

    def cb_daily(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("cb_daily", kwargs["ts_code"]))
        return self._rows(
            kwargs["ts_code"],
            kwargs["start_date"],
            kwargs["end_date"],
            lambda i: float(_bond_close(kwargs["ts_code"], i)),
            lambda i: float(_bond_volume(i) / 10),  # tushare vol 单位为手(10 张)
        )

    def daily(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("daily", kwargs["ts_code"]))
        return self._rows(
            kwargs["ts_code"],
            kwargs["start_date"],
            kwargs["end_date"],
            lambda i: float(_STOCK_BASE),
            lambda i: 1000000.0,
        )

    def adj_factor(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("adj_factor", kwargs["ts_code"]))
        s = date(int(kwargs["start_date"][:4]), int(kwargs["start_date"][4:6]), int(kwargs["start_date"][6:]))
        e = date(int(kwargs["end_date"][:4]), int(kwargs["end_date"][4:6]), int(kwargs["end_date"][6:]))
        return [
            {"ts_code": kwargs["ts_code"], "trade_date": day.strftime("%Y%m%d"), "adj_factor": 1}
            for day in self.days
            if s <= day <= e
        ]

    def suspend_d(self, **kwargs: str) -> list[dict[str, object]]:
        return []


class _NoopBudget:
    async def acquire(self) -> None:
        return None


async def _sync_bars_into_cache(engine: AsyncEngine, cache_dir: Path) -> _FakeChainTushareClient:
    days = _days()
    client = _FakeChainTushareClient(days)
    provider = TushareBarProvider(
        client=client,
        budget=_NoopBudget(),
        cache_dir=cache_dir,
        max_retries=0,
    )
    for code in (*_BONDS, *_UNDERLYINGS):
        ok = await provider.update_cache(
            make_symbol(code),
            BarPeriod.D1,
            _START,
            _END,
            adjust="qfq",
        )
        assert ok is True
    # 转债只走 cb_daily,不碰股票 daily / adj_factor(#265 分流断言)。
    bond_calls = [name for name, code in client.calls if code in _BONDS]
    assert set(bond_calls) == {"cb_daily"}
    stock_calls = [name for name, code in client.calls if code in _UNDERLYINGS]
    assert set(stock_calls) == {"daily", "adj_factor"}
    return client


# ---- 步骤③:convertible_profiles(cb_basic → 元数据回填 + 评级/事件兜底)-------


def _cb_basic_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bond, underlying in zip(_BONDS, _UNDERLYINGS, strict=True):
        rows.append(
            {
                "ts_code": bond,
                "bond_full_name": f"{bond}可转债",
                "bond_short_name": f"链路{bond[:3]}",
                "stock_code": underlying,
                "stock_name": f"正股{underlying}",
                "list_date": "20210104",
                "delist_date": "",
                "swap_price": "10",
                "value_date": "20201228",
                "mature_date": _MATURITY.strftime("%Y%m%d"),
                "coupon_rate": "0.3",
            }
        )
    return rows


def _redeem_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "债券代码": [_BONDS[0].split(".")[0]],
            "债券简称": ["链路转债一"],
            "正股代码": [_UNDERLYINGS[0].split(".")[0]],
            "赎回日": ["2025-06-20"],
            "停止交易日": [None],
            "赎回价": ["100.5"],
        }
    )


class _CbBasicProvider:
    """离线 tushare 研究数据 provider:只提供 cb_basic 快照。"""

    async def fetch_convertible_profiles(
        self, *, dirty_row_policy: str | None = None
    ) -> list[object]:
        from finboard_data.research import ConvertibleProfile

        observed = datetime(2026, 8, 1, tzinfo=UTC)
        return [
            ConvertibleProfile(
                symbol=str(row["ts_code"]),
                name=str(row["bond_short_name"]),
                underlying_symbol=str(row["stock_code"]),
                underlying_name=str(row["stock_name"]),
                list_date=date(2021, 1, 4),
                delist_date=None,
                conversion_price=Decimal("10"),
                issue_date=date(2020, 12, 28),
                maturity_date=_MATURITY,
                coupon_rate=Decimal("0.003"),
                source="tushare",
                observed_at=observed,
                available_at=observed,
            )
            for row in _cb_basic_rows()
        ]


async def _run_convertible_profiles_sync(engine: AsyncEngine) -> None:
    def _job() -> JobRecord:
        return JobRecord(
            job_id="BJ-CONVE2E",
            kind="dataset_sync",
            queue="data",
            payload={
                "datasets": ["convertible_profiles"],
                "start_date": _START.isoformat(),
                "end_date": _END.isoformat(),
            },
            attempt=1,
            max_attempts=1,
            requested_by="convertible-chain-test",
        )

    async def _noop(_done: int, _total: int | None, _phase: str | None) -> None:
        return None

    executor = DatasetSyncExecutor(
        session_maker=session_factory(engine),
        provider_factory=lambda: _CbBasicProvider(),  # type: ignore[arg-type,return-value]
    )
    from unittest.mock import patch

    with (
        patch("akshare.bond_zh_cov", return_value=_overview_frame()),
        patch("akshare.bond_cb_redeem_jsl", return_value=_redeem_frame()),
    ):
        result = await executor.execute(_job(), _noop)
    assert result.status == "succeeded"


# ---- 步骤④:发布(bars 混发 + convertible_metrics)----------------------------


async def _publish_releases(
    engine: AsyncEngine, cache_dir: Path, release_root: Path
) -> tuple[object, object]:
    from finboard_data.releases import CONVERTIBLE_METRICS_FIELDS

    symbols = [*_BONDS, *_UNDERLYINGS]
    async with session_factory(engine)() as session:
        service = ResearchDatasetReleaseService(
            session,
            cache_dir=cache_dir,
            release_root=release_root,
        )
        bars_release = await service.publish(
            DatasetReleaseSpec(
                release_id=_RELEASE_BARS,
                dataset_name="conv_chain_bars",
                source="tushare",
                version="r265",
                start_date=_START,
                end_date=_END,
                code_version="integration-test",
                dataset_kind=ReleaseDatasetKind.BARS,
                required_capabilities=("stock", "convertible"),
            ),
            symbols,
        )
        conv_release = await service.publish(
            DatasetReleaseSpec(
                release_id=_RELEASE_CONV,
                dataset_name="conv_chain_metrics",
                source="tushare",
                version="r265",
                start_date=_START,
                end_date=_END,
                code_version="integration-test",
                dataset_kind=ReleaseDatasetKind.CONVERTIBLE_METRICS,
                fields=tuple(CONVERTIBLE_METRICS_FIELDS),
                required_capabilities=("convertible",),
            ),
            list(_BONDS),
        )
        await session.commit()
    return bars_release, conv_release


# ---- 步骤⑤:双低策略消费冻结发布(真实 FrozenReleaseProvider)-----------------


async def _build_snapshots_and_inputs(
    release_root: Path,
) -> tuple[dict[date, list[ConvertibleSnapshot]], dict[date, dict[str, Decimal]], dict[date, dict[str, Decimal]]]:
    bars_provider = FrozenReleaseProvider(
        release_root=release_root, release_id=_RELEASE_BARS
    )
    metrics_provider = FrozenReleaseProvider(
        release_root=release_root, release_id=_RELEASE_CONV
    )
    days = _days()
    decision_at = datetime(2027, 1, 1, tzinfo=UTC)

    # 转债条款随 manifest 冻结:转股价 / 正股代码 / 评级直接读发布清单。
    instruments_meta: dict[str, tuple[Instrument, ConvertibleMetadata]] = {}
    for bond in _BONDS:
        item = bars_provider.release.instrument(bond)
        assert item is not None
        assert item.convertible is not None
        conv = item.convertible
        instruments_meta[bond] = (
            Instrument(
                code=bond,
                name=item.name,
                market=Market.A_SHARE,
                instrument_type=InstrumentType.CONVERTIBLE,
                list_date=date(2021, 1, 4),
                status=ListingStatus.ACTIVE,
            ),
            ConvertibleMetadata(
                underlying_stock_code=conv.underlying_stock_code,
                conversion_price=conv.conversion_price,
                maturity_date=conv.maturity_date,
            ),
        )
        assert conv.conversion_price == _CONVERSION_PRICE
        assert conv.maturity_date == _MATURITY

    # 冻结派生指标(转股价值 / 转股溢价率,available_at PIT 门控)。
    premiums: dict[str, dict[date, Decimal]] = {}
    for bond in _BONDS:
        metrics = await metrics_provider.fetch_convertible_metrics(
            Symbol(bond, Market.A_SHARE),
            start=_START,
            end=_END,
            decision_at=decision_at,
        )
        assert metrics, f"{bond} 的 convertible_metrics 观测为空"
        premiums[bond] = {
            metric.trade_date: metric.conversion_premium
            for metric in metrics
            if metric.conversion_premium is not None
        }

    # 冻结 bars(转债收盘 / 成交额与正股开盘,全部来自发布通道)。
    closes: dict[str, dict[date, Decimal]] = {}
    amounts: dict[str, dict[date, Decimal]] = {}
    opens: dict[str, dict[date, Decimal]] = {}
    for code in (*_BONDS, *_UNDERLYINGS):
        bars = await bars_provider.fetch_bars(
            Symbol(code, Market.A_SHARE), BarPeriod.D1, _START, _END, adjust="qfq"
        )
        assert len(bars) == len(days)
        closes[code] = {bar.timestamp.date(): bar.close for bar in bars}
        amounts[code] = {bar.timestamp.date(): bar.amount for bar in bars}
        opens[code] = {bar.timestamp.date(): bar.open for bar in bars}

    snapshots: dict[date, list[ConvertibleSnapshot]] = {day: [] for day in days}
    next_opens: dict[date, dict[str, Decimal]] = {}
    next_volumes: dict[date, dict[str, Decimal]] = {}
    amount_series: dict[str, list[Decimal]] = {
        bond: [amounts[bond][day] for day in days] for bond in _BONDS
    }
    for index, day in enumerate(days):
        for bond in _BONDS:
            premium = premiums[bond].get(day)
            if premium is None:
                continue
            instr, bond_meta = instruments_meta[bond]
            recent = amount_series[bond][max(0, index - 19) : index + 1]
            snapshots[day].append(
                ConvertibleSnapshot(
                    instrument=instr,
                    metadata=bond_meta,
                    as_of=day,
                    close=closes[bond][day],
                    volume=_bond_volume(index),
                    amount=amounts[bond][day],
                    conversion_premium=premium,
                    conversion_price=bond_meta.conversion_price,
                    conversion_value=closes[bond][day] / (Decimal(1) + premium),
                    ytm=None,
                    days_to_maturity=(_MATURITY - day).days,
                    remaining_size=Decimal("100000000"),
                    avg_amount_20d=sum(recent) / Decimal(len(recent)),
                )
            )
        if index + 1 < len(days):
            nxt = days[index + 1]
            next_opens[day] = {bond: opens[bond][nxt] for bond in _BONDS}
            next_volumes[day] = {bond: _bond_volume(index + 1) for bond in _BONDS}
    return snapshots, next_opens, next_volumes


class TestConvertibleChain:
    async def test_sync_publish_and_double_low_backtest(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """登记 → 缓存 → 元数据 → 双发布 → 双低回测出非空成交(#265)。"""
        cache_dir = tmp_path / "cache"
        release_root = tmp_path / "releases"
        await _register_instruments(engine)
        await _sync_bars_into_cache(engine, cache_dir)
        await _run_convertible_profiles_sync(engine)

        # 断点③:条款元数据落库 + 上市日期回填 + 评级 / 强赎事件兜底可见。
        async with session_factory(engine)() as session:
            meta_rows = (
                (await session.execute(select(ConvertibleMetadataModel)))
                .scalars()
                .all()
            )
            assert {row.code for row in meta_rows} == set(_BONDS)
            assert all(row.conversion_price == Decimal("10") for row in meta_rows)
            assert all(row.maturity_date == _MATURITY for row in meta_rows)
            # 评级来自 akshare bond_zh_cov(第 4 只在源数据中缺评级 → null 可见)。
            by_code = {row.code: row for row in meta_rows}
            assert by_code["113050.SH"].rating == "AA+"
            assert by_code["123102.SZ"].rating is None
            # cb_basic 回填 instruments.list_date(只补 null)。
            instr_rows = (
                (await session.execute(select(InstrumentModel))).scalars().all()
            )
            assert all(
                row.list_date == date(2021, 1, 4)
                for row in instr_rows
                if row.code in _BONDS
            )
            # 集思录强赎事件进入生命周期表(available_at >= 生效日开盘)。
            events = (
                (await session.execute(select(InstrumentLifecycleEventModel)))
                .scalars()
                .all()
            )
            assert [row.event_type for row in events] == ["forced_redemption"]
            assert events[0].symbol == _BONDS[0]
            assert events[0].effective_date == date(2025, 6, 20)

        bars_release, conv_release = await _publish_releases(
            engine, cache_dir, release_root
        )

        # bars 混发:转债与正股同处一份发布,manifest 携带转债条款快照。
        bond_item = bars_release.instrument(_BONDS[0])  # type: ignore[attr-defined]
        assert bond_item is not None
        assert bond_item.ready
        assert bond_item.convertible is not None
        assert bond_item.convertible.rating == "AA+"
        # 质量报告:转债标的数与关键字段完整度计数可见(报告级具名块)。
        report = bars_release.quality_report  # type: ignore[attr-defined]
        conv_block = cast("dict[str, object]", report["convertible_instruments"])
        assert conv_block["total"] == len(_BONDS)
        assert conv_block["with_metadata"] == len(_BONDS)
        assert conv_block["missing_rating"] == 1  # 第 4 只源数据无评级

        # convertible_metrics 发布:转股溢价率观测非空且带日期。
        conv_item = conv_release.instrument(_BONDS[0])  # type: ignore[attr-defined]
        assert conv_item is not None
        assert conv_item.ready
        assert conv_item.row_count == len(_days())
        assert "premium_missing_days" not in conv_item.issues

        # 步骤⑤:双低策略消费冻结发布跑回测(2024-2026 区间)。
        snapshots, next_opens, next_volumes = await _build_snapshots_and_inputs(
            release_root
        )
        result = run_backtest(
            snapshots,
            next_opens,
            next_volumes,
            events={},  # 事件在 2024-2025 窗口尚不可知(available_at 门控)
            config=ConvertibleDoubleLowConfig(capital=Decimal("100000")),
        )
        assert result.trades, "双低策略在转债数据链路上必须产生成交"
        assert result.rebalance_dates
        assert result.final_equity > 0
        # 权益曲线逐日 mark-to-market:首点在次日,末点为区间最后一日。
        assert result.equity_curve[0][0] == _days()[1]
        assert result.equity_curve[-1][0] == _days()[-1]
