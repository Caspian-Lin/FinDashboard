"""股票 bars 主源切 tushare(issue #393)。

锁定的契约:

* 默认主源 ``data_provider = "tushare"``、回退默认 ``data_fallback_provider
  = "akshare"``(回落链语义保持 #257),各入口(REST / MCP / CLI / worker
  executor)的缺省回落链一致;
* ``bulk_download`` 在 tushare 源下对股票标的同步 ``suspend_d`` 停复牌事件:
  与 REST / MCP fetch 共用同一落库函数(单一事实源),失败可见不阻断;
* 双源切换的缓存重建语义:异源缓存部分覆盖时丢弃重建纯 tushare、不混源
  (#257),重建后的缓存经发布冻结再读回逐值一致。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_app.config import Settings
from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.executors import bulk_download as bulk_download_module
from finboard_backtest.background_jobs.executors._providers import resolve_provider_name
from finboard_backtest.background_jobs.executors.bulk_download import BulkDownloadExecutor
from finboard_backtest.providers import build_backtest_bar_provider
from finboard_data import (
    AkShareProvider,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    TushareBarProvider,
    default_execution_metadata,
)
from finboard_data.cache import ParquetCache, make_symbol
from finboard_data.tushare_bar_provider import TushareLifecycleEvent
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    InstrumentType,
    Market,
)

# --------------------------------------------------------------------------- #
# 公共 fake / helper
# --------------------------------------------------------------------------- #


class NoopBudget:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


class FakeTushareBarClient:
    """daily / adj_factor / suspend_d 三接口的离线 fake。"""

    def __init__(
        self,
        *,
        suspend_rows: list[dict[str, object]] | None = None,
        suspend_error_codes: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.suspend_calls: list[dict[str, str]] = []
        self.daily_rows: list[dict[str, object]] = [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20240103",
                "open": 20,
                "high": 22,
                "low": 19,
                "close": 21,
                "vol": 12,
                "amount": 34.5,
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20240102",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 10,
                "amount": 20,
            },
        ]
        self.factor_rows: list[dict[str, object]] = [
            {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2},
            {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1},
        ]
        self.index_rows: list[dict[str, object]] = [
            {
                "ts_code": "000852.SH",
                "trade_date": "20240103",
                "open": 1000,
                "high": 1000,
                "low": 1000,
                "close": 1000,
                "vol": 0,
                "amount": 0,
            }
        ]
        self.suspend_rows: list[dict[str, object]] = suspend_rows if suspend_rows is not None else [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20240103",
                "suspend_timing": "",
                "suspend_type": "S",
            }
        ]
        self.suspend_error_codes = suspend_error_codes or set()

    def daily(self, **kwargs: str) -> object:
        self.calls.append(("daily", kwargs))
        return self.daily_rows

    def cb_daily(self, **kwargs: str) -> object:
        self.calls.append(("cb_daily", kwargs))
        return []

    def index_daily(self, **kwargs: str) -> object:
        self.calls.append(("index_daily", kwargs))
        return self.index_rows

    def adj_factor(self, **kwargs: str) -> object:
        self.calls.append(("adj_factor", kwargs))
        return self.factor_rows

    def suspend_d(self, **kwargs: str) -> object:
        self.suspend_calls.append(kwargs)
        if kwargs.get("ts_code") in self.suspend_error_codes:
            raise RuntimeError(f"suspend_d upstream failure for {kwargs.get('ts_code')}")
        return self.suspend_rows


def _tushare_provider(
    client: FakeTushareBarClient,
    *,
    cache_dir: str | Path | None = None,
    use_cache: bool = False,
) -> TushareBarProvider:
    return TushareBarProvider(
        client=client,
        budget=NoopBudget(),
        cache_dir=cache_dir,
        use_cache=use_cache,
        max_retries=0,
    )


def _make_job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-TEST393",
        kind="bulk_download",
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    return None


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _fake_session_maker_with_log(log: list[_FakeSession]) -> Any:
    def _maker() -> _FakeSession:
        session = _FakeSession()
        log.append(session)
        return session

    return _maker


class _FakeInstrumentRepository:
    def __init__(self, session: object, instruments: list[SimpleNamespace]) -> None:
        self._instruments = instruments

    async def list_active(self, **kwargs: object) -> tuple[list[SimpleNamespace], int]:
        return self._instruments, len(self._instruments)


def _instruments() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(code="000001.SZ", market="a_share", instrument_type="stock"),
        SimpleNamespace(code="000852.SH", market="a_share", instrument_type="index"),
    ]


def _akshare_bar(day: date, close: str, *, source: str = "akshare") -> Bar:
    symbol = Symbol(code="000001.SZ", market=Market.A_SHARE)
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("100"),
        amount=Decimal("10000"),
        source=source,
    )


# --------------------------------------------------------------------------- #
# 1. 默认主源 / 回退默认值与三入口回落链
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_settings_default_primary_tushare_fallback_akshare() -> None:
    """#393 拍板:settings 默认主源 tushare,回退默认 akshare。"""
    settings = Settings()
    assert settings.data_provider == "tushare"
    assert settings.data_fallback_provider == "akshare"


@pytest.mark.unit
def test_executor_provider_chain_defaults_to_tushare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker executor 侧回落链:source 缺省 → settings → env → tushare。"""
    monkeypatch.delenv("FINBOARD_DATA_PROVIDER", raising=False)
    assert resolve_provider_name(None, lambda: None) == "tushare"
    # 显式 source 与 env 覆盖语义不变(#341 跟进口径)。
    assert resolve_provider_name("AKShare", lambda: None) == "akshare"
    monkeypatch.setenv("FINBOARD_DATA_PROVIDER", "akshare")
    assert resolve_provider_name(None, lambda: None) == "akshare"


@pytest.mark.unit
def test_api_data_route_chain_defaults_to_tushare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST fetch 路由的源解析与回退选择(#393 后的主副源方向)。"""
    from finboard_api.routes.data import _fallback_provider_name, _resolve_provider_name

    monkeypatch.delenv("FINBOARD_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("FINBOARD_DATA_FALLBACK_PROVIDER", raising=False)
    assert _resolve_provider_name(None, settings=None) == "tushare"
    # 主源 tushare → 回退 akshare;主源 akshare → 回退 yfinance(既有方向)。
    assert _fallback_provider_name("tushare", settings=None) == "akshare"
    assert _fallback_provider_name("akshare", settings=None) == "yfinance"


@pytest.mark.unit
def test_backtest_provider_wraps_akshare_fallback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回测链:默认回退配置下 tushare 主源包裹 akshare 回退(#257 语义)。"""
    from finboard_data import FallbackBarProvider

    monkeypatch.setattr(
        TushareBarProvider,
        "_create_client",
        staticmethod(lambda token: object())
    )
    provider = build_backtest_bar_provider("tushare", Settings(tushare_token="unit-test"))
    assert isinstance(provider, FallbackBarProvider)
    assert provider._primary_name == "tushare"
    assert provider._fallback_name == "akshare"


@pytest.mark.unit
def test_cli_bar_provider_routes_tushare(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 数据命令:env 缺省 tushare 且路由到 TushareBarProvider。

    #393 前的三处 ``else → yfinance`` 会把 tushare 静默落到 yfinance,
    本测试锁定补齐后的路由。
    """
    import finboard_backtest.background_jobs.executors._providers as providers_mod
    from finboard_app.cli import _cli_bar_provider

    monkeypatch.setattr(
        providers_mod,
        "default_settings_factory",
        lambda: Settings(tushare_token="unit-test"),
    )
    monkeypatch.setattr(
        TushareBarProvider,
        "_create_client",
        staticmethod(lambda token: object())
    )
    tushare_provider = _cli_bar_provider("tushare")
    assert isinstance(tushare_provider, TushareBarProvider)
    akshare_provider = _cli_bar_provider("akshare")
    assert isinstance(akshare_provider, AkShareProvider)


# --------------------------------------------------------------------------- #
# 2. bulk_download 的 suspend_d 停复牌同步
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bulk_download_tushare_syncs_suspension_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tushare 源:股票标的逐标的拉 suspend_d 并幂等落库,指数跳过。"""
    client = FakeTushareBarClient()
    provider = _tushare_provider(client)
    sessions: list[_FakeSession] = []
    persisted: list[list[TushareLifecycleEvent]] = []

    async def _fake_persist(session: Any, events: list[TushareLifecycleEvent]) -> int:
        persisted.append(list(events))
        return len(events)

    monkeypatch.setattr(
        "finboard_persistence.InstrumentRepository",
        lambda session: _FakeInstrumentRepository(session, _instruments()),
    )
    monkeypatch.setattr(
        bulk_download_module,
        "build_bar_provider",
        lambda name, factory, **kwargs: provider,
    )
    monkeypatch.setattr(
        "finboard_persistence.persist_tushare_lifecycle_events",
        _fake_persist,
    )

    executor = BulkDownloadExecutor(
        session_maker=_fake_session_maker_with_log(sessions),
        settings_factory=lambda: None,
    )
    phases: list[str | None] = []

    async def _progress(_done: int, _total: int | None, phase: str | None) -> None:
        phases.append(phase)

    result = await executor.execute(
        _make_job({"market": "a_share", "source": "tushare", "start": "2024-01-01"}),
        _progress,
    )

    assert result.status == "succeeded"
    assert result.error_summary is None
    # 只对股票标的调 suspend_d(指数跳过),事件落在共享落库函数里。
    assert len(client.suspend_calls) == 1
    assert client.suspend_calls[0]["ts_code"] == "000001.SZ"
    assert len(persisted) == 1
    assert [event.event_type for event in persisted[0]] == ["suspension_day"]
    assert sessions
    assert sessions[0].commits == 1
    assert any(
        phase and phase == "bulk_download:suspension 0/1" for phase in phases
    )
    assert phases[-1] == "bulk_download:done"


@pytest.mark.asyncio
async def test_bulk_download_suspension_failure_visible_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停复牌同步失败:任务仍 succeeded(bars 成功),缺口进 error_summary。"""
    client = FakeTushareBarClient(suspend_error_codes={"000001.SZ"})
    provider = _tushare_provider(client)

    async def _fake_persist(session: Any, events: list[TushareLifecycleEvent]) -> int:
        return len(events)

    monkeypatch.setattr(
        "finboard_persistence.InstrumentRepository",
        lambda session: _FakeInstrumentRepository(session, _instruments()),
    )
    monkeypatch.setattr(
        bulk_download_module,
        "build_bar_provider",
        lambda name, factory, **kwargs: provider,
    )
    monkeypatch.setattr(
        "finboard_persistence.persist_tushare_lifecycle_events",
        _fake_persist,
    )

    executor = BulkDownloadExecutor(
        session_maker=_fake_session_maker_with_log([]),
        settings_factory=lambda: None,
    )
    result = await executor.execute(
        _make_job({"market": "a_share", "source": "tushare", "start": "2024-01-01"}),
        _noop_progress,
    )

    assert result.status == "succeeded"
    assert result.error_summary is not None
    assert "停复牌事件同步失败" in result.error_summary
    assert "1/1" in result.error_summary
    assert "000001.SZ" in result.error_summary
    assert "RuntimeError" in result.error_summary


@pytest.mark.asyncio
async def test_bulk_download_akshare_source_skips_suspension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 tushare 源:完全不触发停复牌段(与 #393 前行为一致)。"""
    stub_calls: list[str] = []

    class _StubProvider:
        async def update_cache_batch(self, *args: Any, **kwargs: Any) -> dict[str, bool]:
            stub_calls.append("update_cache_batch")
            symbols = args[0]
            return {sym.code: True for sym in symbols}

    monkeypatch.setattr(
        "finboard_persistence.InstrumentRepository",
        lambda session: _FakeInstrumentRepository(session, _instruments()),
    )
    monkeypatch.setattr(
        bulk_download_module,
        "build_bar_provider",
        lambda name, factory, **kwargs: _StubProvider(),
    )
    persist_calls: list[list[TushareLifecycleEvent]] = []

    async def _fake_persist(session: Any, events: list[TushareLifecycleEvent]) -> int:
        persist_calls.append(list(events))
        return len(events)

    monkeypatch.setattr(
        "finboard_persistence.persist_tushare_lifecycle_events",
        _fake_persist,
    )

    executor = BulkDownloadExecutor(
        session_maker=_fake_session_maker_with_log([]),
        settings_factory=lambda: None,
    )
    result = await executor.execute(
        _make_job({"market": "a_share", "source": "akshare", "start": "2024-01-01"}),
        _noop_progress,
    )

    assert result.status == "succeeded"
    assert result.error_summary is None
    assert stub_calls == ["update_cache_batch"]
    assert persist_calls == []


# --------------------------------------------------------------------------- #
# 3. 双源切换:缓存重建(不混源)→ 发布冻结读回逐值一致
# --------------------------------------------------------------------------- #


def _release_spec(release_id: str = "issue393-r1") -> DatasetReleaseSpec:
    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="multi_asset_daily_bars",
        source="tushare",
        version="r1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        code_version="deadbeef",
        required_capabilities=(InstrumentType.STOCK.value,),
    )


def _stock_spec() -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code="000001.SZ",
        name="平安银行",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2015, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        exchange="SZSE",
        list_date=None,
        delist_date=None,
    )


@pytest.mark.asyncio
async def test_source_switch_rebuilds_pure_tushare_cache_and_freeze_roundtrips(
    tmp_path: Any,
) -> None:
    """主源切换验收(合成数据,tmp 缓存):

    akshare 部分覆盖的旧缓存 → tushare update_cache 走「丢弃重建纯
    tushare」(#257),缓存单一来源且逐值等于 tushare 自算 qfq;
    冻结发布后再经 FrozenReleaseProvider 读回,与缓存逐值一致。
    """
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    symbol = make_symbol("000001.SZ")
    day1 = date(2024, 1, 2)
    day2 = date(2024, 1, 3)
    # 旧主源(akshare)只留 day1 一根:对 tushare 请求区间是部分覆盖。
    await ParquetCache(cache_dir).write(
        symbol,
        BarPeriod.D1,
        "qfq",
        [_akshare_bar(day1, "999.99")],
    )

    client = FakeTushareBarClient()
    provider = _tushare_provider(client, cache_dir=cache_dir, use_cache=True)
    bars = await provider.fetch_bars(
        symbol,
        BarPeriod.D1,
        day1,
        day2,
        adjust="qfq",
    )

    # 重建后缓存单一来源 tushare,qfq 自算口径 = raw x factor / 窗口末端 factor:
    # day2 因子 2(基准),day1 因子 1 → day1 close 10.5 x (1/2) = 5.25。
    assert {bar.source for bar in bars} == {"tushare"}
    assert [bar.timestamp.date() for bar in bars] == [day1, day2]
    assert bars[0].close == Decimal("5.25")
    assert bars[1].close == Decimal("21")

    cached = await ParquetCache(cache_dir).read(symbol, BarPeriod.D1, "qfq")
    assert {bar.source for bar in cached} == {"tushare"}
    assert cached == bars

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(_release_spec(), [_stock_spec()])

    frozen_provider = FrozenReleaseProvider(
        release_root=release_root,
        release_id=release.release_id,
    )
    frozen_bars = await frozen_provider.fetch_bars(
        Symbol(code="000001.SZ", market=Market.A_SHARE),
        BarPeriod.D1,
        day1,
        day2,
    )

    def _row(bar: Bar) -> tuple[object, ...]:
        return (
            bar.timestamp,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.amount,
        )

    assert [_row(bar) for bar in frozen_bars] == [_row(bar) for bar in cached]


@pytest.mark.unit
def test_suspension_event_shape_unchanged() -> None:
    """TushareLifecycleEvent 的字段形状保持(消费方零改动)。"""
    event = TushareLifecycleEvent(
        symbol="000001.SZ",
        event_type="suspension_day",
        effective_date=date(2024, 1, 3),
        suspend_timing=None,
    )
    assert event.symbol == "000001.SZ"
    assert event.effective_date == date(2024, 1, 3)
    assert event.suspend_timing is None
