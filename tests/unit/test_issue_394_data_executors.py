"""issue #394 执行器层单元测试:指数主源偏好 + data_sync list_date 回填。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from finboard_data.discovery import InstrumentInfo

if TYPE_CHECKING:
    from finboard_app.config import Settings

from finboard_backtest.background_jobs.executors._providers import (
    resolve_provider_name,
)
from finboard_backtest.background_jobs.executors.bulk_download import (
    _resolve_bulk_provider_name,
)
from finboard_backtest.background_jobs.executors.data_sync import DataSyncExecutor
from finboard_data.research import ResearchDataConfigurationError


def _ins(code: str, instrument_type: str) -> SimpleNamespace:
    return SimpleNamespace(code=code, instrument_type=instrument_type)


def _settings(provider: str | None) -> object:
    if provider is None:
        return None
    return SimpleNamespace(data_provider=provider)


# ---- bulk_download 指数主源偏好(#394)------------------------------------------


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试环境可能带 FINBOARD_DATA_PROVIDER(.env):回落链断言需隔离。"""
    monkeypatch.delenv("FINBOARD_DATA_PROVIDER", raising=False)


def _settings_factory(provider: str | None) -> Callable[[], Settings | None]:
    def _factory() -> Settings | None:
        return _settings(provider)  # type: ignore[return-value]

    return _factory


@pytest.mark.unit
def test_index_scope_default_resolves_to_tushare() -> None:
    """未显式声明 source 且筛选域全指数 → 覆盖为 tushare(index_daily 主源)。"""
    instruments = [_ins("000300.SH", "index"), _ins("000905.SH", "index")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory(None), job_id="J1"
    )
    assert name == "tushare"


@pytest.mark.unit
def test_explicit_source_always_wins() -> None:
    """显式 source=akshare 恒优先,指数域也不覆盖。"""
    instruments = [_ins("000300.SH", "index")]
    name = _resolve_bulk_provider_name(
        "akshare", instruments, _settings_factory(None), job_id="J1"
    )
    assert name == "akshare"


@pytest.mark.unit
def test_mixed_scope_keeps_default_provider() -> None:
    """混合域(含股票)不覆盖 —— 全局默认源切换归 settings(#404)管。"""
    instruments = [_ins("000300.SH", "index"), _ins("600000.SH", "stock")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory(None), job_id="J1"
    )
    assert name == resolve_provider_name(None, _settings_factory(None))  # akshare


@pytest.mark.unit
def test_stock_scope_keeps_default_provider() -> None:
    instruments = [_ins("600000.SH", "stock")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory(None), job_id="J1"
    )
    assert name == "akshare"


@pytest.mark.unit
def test_empty_scope_keeps_default_provider() -> None:
    """空域(理论不可达,入队期已拒)不触发偏好。"""
    name = _resolve_bulk_provider_name(
        None, [], _settings_factory(None), job_id="J1"
    )
    assert name == "akshare"


@pytest.mark.unit
def test_futures_scope_not_preferred_to_tushare() -> None:
    """期货主连不在 tushare scope(#267),偏好不生效(否则 scope 拒绝)。"""
    instruments = [_ins("IF0.CFFEX", "futures")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory(None), job_id="J1"
    )
    assert name == "akshare"


@pytest.mark.unit
def test_settings_tushare_short_circuits() -> None:
    """配置默认已是 tushare:偏好不触发(幂等,无额外日志)。"""
    instruments = [_ins("000300.SH", "index")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory("tushare"), job_id="J1"
    )
    assert name == "tushare"


# ---- data_sync 执行器:指数 list_date 回填 + 配置错误映射 -----------------------


class _FakeRepo:
    def __init__(self, session: object) -> None:
        self.synced: list[dict[str, object]] | None = None
        self.profiles_symbols: list[str] | None = None
        self.listing_records: dict[str, tuple[date | None, date | None]] | None = None

    async def sync_with_diff(self, dicts: list[dict[str, object]], *, as_of: date) -> object:
        self.synced = dicts
        return SimpleNamespace(new=0, updated=0, renamed=[], pending_delist=[], delisted=[])

    async def backfill_metadata_from_profiles(self, *, symbols: list[str] | None = None) -> object:
        self.profiles_symbols = symbols
        return SimpleNamespace(
            as_dict=lambda: {
                "profile_batch_available": False,
                "scoped": 0,
                "backfilled_list_date": 0,
                "backfilled_industry": 0,
                "backfilled_delist_date": 0,
                "missing_list_date": 0,
                "missing_industry": 0,
                "missing_delist_date": 0,
            }
        )

    async def backfill_listing_dates(
        self, records: dict[str, tuple[date | None, date | None]]
    ) -> dict[str, int]:
        self.listing_records = records
        return {
            "scoped": len(records),
            "backfilled_list_date": len(records),
            "backfilled_delist_date": 0,
            "missing_list_date": 0,
            "missing_delist_date": 0,
        }


class _FakeSession:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _job_record() -> object:
    from finboard_backtest.background_jobs.contracts import JobRecord
    from finboard_shared.background_jobs import generate_background_job_id

    return JobRecord(
        job_id=generate_background_job_id(),
        kind="data_sync",
        queue="data",
        payload={},
        attempt=1,
        max_attempts=3,
        requested_by="test:394",
    )


def _run_executor(
    monkeypatch: pytest.MonkeyPatch,
    discovered: list[InstrumentInfo],
    *,
    session_maker: object,
) -> dict[str, object]:
    """打桩 UniverseDiscovery + InstrumentRepository,同步驱动 executor。"""
    captured: dict[str, object] = {}

    class _FakeDiscovery:
        def __init__(self) -> None:
            pass

        async def discover_all(self) -> list[InstrumentInfo]:
            return discovered

    monkeypatch.setattr(
        "finboard_data.discovery.UniverseDiscovery", _FakeDiscovery
    )

    def _repo_factory(session: object) -> _FakeRepo:
        repo = _FakeRepo(session)
        captured["repo"] = repo
        return repo

    monkeypatch.setattr(
        "finboard_persistence.InstrumentRepository", _repo_factory
    )
    executor = DataSyncExecutor(
        session_maker=session_maker,  # type: ignore[arg-type]
    )
    result = asyncio.run(
        executor.execute(_job_record(), AsyncMock())  # type: ignore[arg-type]
    )
    captured["result"] = result
    return captured


def _session_maker_of(session: _FakeSession) -> Callable[[], _FakeSession]:
    return lambda: session


@pytest.mark.unit
def test_data_sync_backfills_index_list_date_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指数携带 base_date → backfill_listing_dates;股票不在回填域。"""
    from finboard_shared.types import InstrumentType, ListingBoard, Market

    discovered = [
        InstrumentInfo(
            code="000300.SH",
            name="沪深300",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.INDEX,
            exchange="SSE",
            list_date=date(2005, 4, 8),
        ),
        InstrumentInfo(
            code="600000.SH",
            name="浦发银行",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            exchange="SSE",
            listing_board=ListingBoard.SSE_MAIN,
        ),
        # 基日缺失的指数:不进回填映射(records 只收非 None)。
        InstrumentInfo(
            code="000010.SH",
            name="上证180",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.INDEX,
            exchange="SSE",
        ),
    ]
    session = _FakeSession(_FakeRepo(None))
    captured = _run_executor(
        monkeypatch,
        discovered,
        session_maker=_session_maker_of(session),
    )
    repo = captured["repo"]
    assert isinstance(repo, _FakeRepo)
    assert repo.listing_records == {"000300.SH": (date(2005, 4, 8), None)}


@pytest.mark.unit
def test_data_sync_config_error_is_named_and_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tushare token 缺失 → data_source_unavailable 且不重试(#394 配置错误)。"""
    from finboard_backtest.background_jobs.contracts import ExecutorError

    class _BrokenDiscovery:
        def __init__(self) -> None:
            pass

        async def discover_all(self) -> list[SimpleNamespace]:
            raise ResearchDataConfigurationError(
                "未配置 Tushare token;请设置 FINBOARD_TUSHARE_TOKEN"
            )

    monkeypatch.setattr(
        "finboard_data.discovery.UniverseDiscovery", _BrokenDiscovery
    )
    executor = DataSyncExecutor(
        session_maker=_session_maker_of(_FakeSession(_FakeRepo(None))),  # type: ignore[arg-type]
    )

    async def _drive() -> ExecutorError:
        try:
            await executor.execute(_job_record(), AsyncMock())  # type: ignore[arg-type]
        except ExecutorError as exc:
            return exc
        raise AssertionError("应抛出 ExecutorError")

    exc = asyncio.run(_drive())
    assert exc.code == "data_source_unavailable"
    assert exc.retryable is False
    assert "FINBOARD_TUSHARE_TOKEN" in exc.summary
