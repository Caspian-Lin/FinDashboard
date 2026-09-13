"""issue #394/#391 执行器层单元测试:指数/期货主源偏好 + data_sync list_date 回填。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import structlog
from structlog.testing import capture_logs

import finboard_backtest.background_jobs.executors.bulk_download as bulk_download_module
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


# ---- bulk_download 指数/期货主源偏好(#394/#391)--------------------------------


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试环境可能带 FINBOARD_DATA_PROVIDER(.env):回落链断言需隔离。"""
    monkeypatch.delenv("FINBOARD_DATA_PROVIDER", raising=False)


@pytest.fixture(autouse=True)
def _unfiltered_structlog():
    """隔离 ``setup_logging`` 对 structlog 的全局污染,保日志断言确定性。

    任何先行的测试调用 ``setup_logging``(INFO 级 ``make_filtering_bound_logger``
    + ``cache_logger_on_first_use=True``)后,偏好日志在 wrapper 层即被丢弃或
    走旧 wrapper,``capture_logs`` 抓空/抓旧。本文件要断言 index/futures 两个
    具名事件(test_issue_394_data_executors 日志断言),故测试期重置为无级别
    过滤 + 不缓存并重建模块 logger,结束恢复原配置(test_cache_read_cache 先例)。
    """
    saved_config = structlog.get_config()
    saved_logger = bulk_download_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    bulk_download_module.logger = structlog.get_logger(
        "finboard_backtest.background_jobs.executors.bulk_download"
    )
    yield
    bulk_download_module.logger = saved_logger
    structlog.configure(**saved_config)


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
def test_index_preference_logs_index_event_with_domain() -> None:
    """指数域偏好日志保持具名 ``index_source_preference`` 事件 + domain 字段。"""
    instruments = [_ins("000300.SH", "index"), _ins("000905.SH", "index")]
    with capture_logs() as logs:
        name = _resolve_bulk_provider_name(
            None, instruments, _settings_factory("akshare"), job_id="J1"
        )
    assert name == "tushare"
    events = [e for e in logs if "source_preference" in str(e.get("event"))]
    assert len(events) == 1
    assert events[0]["event"] == "bulk_download.index_source_preference"
    assert events[0]["domain"] == "index"
    assert events[0]["resolved_provider"] == "tushare"


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
    # 默认源无偏置(#404 起回落链末端即 tushare,股票 qfq 链路归 settings 管)
    assert name == resolve_provider_name(None, _settings_factory(None))


@pytest.mark.unit
def test_stock_scope_keeps_default_provider() -> None:
    instruments = [_ins("600000.SH", "stock")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory(None), job_id="J1"
    )
    # 默认源无偏置(#404 起默认即 tushare,股票 qfq 链路归 settings 管)
    assert name == resolve_provider_name(None, _settings_factory(None))


@pytest.mark.unit
def test_empty_scope_keeps_default_provider() -> None:
    """空域(理论不可达,入队期已拒)不触发偏好。"""
    name = _resolve_bulk_provider_name(
        None, [], _settings_factory(None), job_id="J1"
    )
    assert name == resolve_provider_name(None, _settings_factory(None))


@pytest.mark.unit
def test_futures_scope_uses_futures_preference_not_index() -> None:
    """指数偏好不作用于期货域;期货域走自己的期货偏好(#391 用户拍板)。

    #395 前本测试断言「偏好函数只认指数域,期货域不因偏好改源」;#391 起
    期货域有自己的具名偏好,语义更新为:期货域触发的必须是
    ``futures_source_preference`` 事件(而非指数偏好)—— 解析结果同为
    tushare,但来源是期货偏好本身,日志可区分。
    """
    instruments = [_ins("IF0.CFFEX", "futures"), _ins("IM0.CFFEX", "futures")]
    with capture_logs() as logs:
        name = _resolve_bulk_provider_name(
            None, instruments, _settings_factory("akshare"), job_id="J1"
        )
    assert name == "tushare"
    events = [e for e in logs if "source_preference" in str(e.get("event"))]
    assert len(events) == 1
    # 指数偏好不作用于期货域:事件名与 domain 都必须是 futures 专属。
    assert events[0]["event"] == "bulk_download.futures_source_preference"
    assert events[0]["event"] != "bulk_download.index_source_preference"
    assert events[0]["domain"] == "futures"
    assert events[0]["default_provider"] == "akshare"
    assert events[0]["resolved_provider"] == "tushare"


@pytest.mark.unit
def test_futures_scope_default_resolves_to_tushare() -> None:
    """未显式声明 source 且筛选域全期货 → 覆盖为 tushare(fut_daily 主源,#391)。"""
    instruments = [
        _ins("IF0.CFFEX", "futures"),
        _ins("IH0.CFFEX", "futures"),
        _ins("IC0.CFFEX", "futures"),
        _ins("IM0.CFFEX", "futures"),
    ]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory("akshare"), job_id="J1"
    )
    assert name == "tushare"


@pytest.mark.unit
def test_futures_explicit_source_always_wins() -> None:
    """显式 source=akshare 恒优先,期货域也不覆盖(新浪主连副源可显式选回)。"""
    instruments = [_ins("IF0.CFFEX", "futures")]
    name = _resolve_bulk_provider_name(
        "akshare", instruments, _settings_factory("akshare"), job_id="J1"
    )
    assert name == "akshare"


@pytest.mark.unit
def test_mixed_futures_stock_scope_keeps_default_provider() -> None:
    """混合域(期货 + 股票)不覆盖:偏好只对类型专属任务生效。"""
    instruments = [_ins("IF0.CFFEX", "futures"), _ins("600000.SH", "stock")]
    name = _resolve_bulk_provider_name(
        None, instruments, _settings_factory("akshare"), job_id="J1"
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


@pytest.mark.unit
def test_settings_tushare_short_circuits_futures_domain() -> None:
    """全局默认已是 tushare:期货域偏好同样短路(幂等,无偏好日志)。"""
    instruments = [_ins("IF0.CFFEX", "futures")]
    with capture_logs() as logs:
        name = _resolve_bulk_provider_name(
            None, instruments, _settings_factory("tushare"), job_id="J1"
        )
    assert name == "tushare"
    assert not [e for e in logs if "source_preference" in str(e.get("event"))]


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
