"""``finboard.data_write.*`` / ``finboard.etf.*`` 工具 —— 数据写操作测试(#137)。

用 ``AsyncMock`` 模拟 ``AsyncSession`` + monkeypatch repository / 依赖,验证:

* 任务化工具(sync/bulk_download/quality_repair/dataset_publish):
  写禁用拒绝;成功返回 JobOut + created;conflict 映射;idempotency_key 上送信封;
  审计记录。
* data_fetch(同步):写禁用拒绝;provider 全失败 → unavailable;成功写入。
* config_get/update:读 JSON 文件;update 合并字段。
* etf_sync:dry_run 预览 / 实写双路径;ImportError → unavailable。
* etf_batch_confirm:写禁用拒绝;成功返回确认数。
* etf_update:instrument 不存在 → not_found;非 etf → conflict;成功返回更新行。
* etf_review_queue:只读,返回 list。

无需 DB。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import data_write as dw
from finboard_persistence import EtfMetadataRepository
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)

# --------------------------------------------------------------------------- #
# 辅助(与 test_mcp_tools_jobs 同口径)
# --------------------------------------------------------------------------- #


async def _async_return(value: object) -> object:
    return value


def _job_row(
    job_id: str = "BJ-1",
    *,
    kind: str = "data_sync",
    status: str = "queued",
    result_ref: str | None = None,
) -> Any:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    return SimpleNamespace(
        job_id=job_id,
        kind=kind,
        queue="data",
        status=status,
        priority=0,
        payload={"msg": "hi"},
        payload_checksum="abc123",
        idempotency_key="idem-key-1234",
        progress_total=0,
        progress_done=0,
        phase=None,
        result_ref=result_ref,
        error_code=None,
        error_summary=None,
        attempt=0,
        max_attempts=3,
        worker_id=None,
        heartbeat_at=None,
        lease_until=None,
        requested_by="mcp:data_sync",
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )


def _make_echo_create_or_get() -> Any:
    """构造一个 ``create_or_get`` mock,回显入参的 payload/idempotency_key/kind。

    用于校验任务化工具确实把正确的 kind/payload/幂等键送进了队列。
    """

    async def _echo(self: Any, **kw: Any) -> Any:
        row = _job_row(
            job_id=kw.get("job_id", "BJ-1"),
            kind=kw.get("kind", "data_sync"),
        )
        # 覆盖 payload / idempotency_key / requested_by 以反映入参
        row.payload = kw.get("payload", {})
        row.idempotency_key = kw.get("idempotency_key", "idem-key-1234")
        row.requested_by = kw.get("requested_by", "mcp:data_sync")
        row.payload_checksum = kw.get("payload_checksum", "abc123")
        return (row, True)

    return _echo


def _session_maker() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _get_session(app: McpAppContext) -> AsyncMock:
    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
    settings: Settings | None = None,
) -> McpAppContext:
    return McpAppContext(
        settings=settings or Settings(),
        session_maker=session_maker or _session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _etf_row(code: str = "159915", review_status: str = "needs_review") -> Any:
    return SimpleNamespace(
        code=code,
        fund_code=code,
        category="stock_etf",
        execution_profile="domestic_equity_etf",
        underlying_market="domestic",
        strategy_type="index",
        underlying_index="创业板指",
        underlying_asset_class="equity",
        management_fee_rate=None,
        custody_fee_rate=None,
        tracking_error=None,
        inception_date=None,
        listing_date=None,
        delisting_date=None,
        iopv_available=True,
        allows_t_plus_0=False,
        dividend_policy=None,
        source="manual",
        rule_version="v1",
        confidence=None,
        review_status=review_status,
        evidence=[],
        manual_override=True,
    )


# --------------------------------------------------------------------------- #
# 任务化工具:sync_universe / bulk_download / quality_repair /
#             dataset_publish
# --------------------------------------------------------------------------- #


class TestEnqueueBasedTools:
    async def test_sync_universe_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.data_sync_universe(app)
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_sync_universe_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.data_sync_universe(app)
        assert env.status == "ok"
        assert env.data["kind"] == "data_sync"
        assert env.data["queue"] == "data"
        assert env.data["created"] is True
        # 幂等键(payload 内,含日期)
        assert env.data["idempotency_key"].startswith("data_sync:")
        assert env.data["payload"]["as_of"]  # 当天 ISO 日期

    async def test_sync_universe_records_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((_job_row(), True)),
        )
        await dw.data_sync_universe(app)
        assert app.audit.records[0].tool_name == "finboard.data.sync_universe"
        assert app.audit.records[0].status == "ok"

    async def test_sync_universe_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        async def _raise(self: Any, **kw: Any) -> Any:
            raise BackgroundJobPersistenceConflictError("checksum mismatch")

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", _raise)
        env = await dw.data_sync_universe(app)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_bulk_download_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.data_bulk_download_start(
            app,
            market="a_share",
            start="2020-01-01",
            source="akshare",
        )
        assert env.status == "ok"
        assert env.data["kind"] == "bulk_download"
        assert env.data["payload"]["market"] == "a_share"
        assert env.data["payload"]["start"] == "2020-01-01"
        assert (
            env.data["idempotency_key"]
            == "bulk_download:a_share:akshare:2020-01-01:all"
        )

    async def test_bulk_download_symbols_subset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symbols 子集(#347)落 payload(去重保序)+ 幂等键带摘要。"""
        import hashlib

        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        symbols = ["000858.SZ", "000001.SZ", "000001.SZ"]
        digest = hashlib.sha256(
            ",".join(dict.fromkeys(symbols)).encode("utf-8")
        ).hexdigest()[:16]
        env = await dw.data_bulk_download_start(
            app,
            market="a_share",
            start="2020-01-01",
            source="akshare",
            symbols=symbols,
        )
        assert env.status == "ok"
        assert env.data["payload"]["symbols"] == ["000858.SZ", "000001.SZ"]
        assert (
            env.data["idempotency_key"]
            == f"bulk_download:a_share:akshare:2020-01-01:all:sub:{digest}"
        )

    async def test_bulk_download_unknown_source_invalid_argument(self) -> None:
        """>#347 入队期契约:未知 source 秒级 invalid_argument(不再等执行期)。"""
        app = _make_app()
        env = await dw.data_bulk_download_start(
            app, market="a_share", start="2020-01-01", source="wind"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "wind" in env.error.message
        assert "invalid_field_value" in env.error.message

    async def test_bulk_download_bad_date_invalid_argument(self) -> None:
        """>#347:坏日期入队即拒。"""
        app = _make_app()
        env = await dw.data_bulk_download_start(
            app, market="a_share", start="2020/01/01", source="akshare"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_bulk_download_tushare_etf_invalid_argument(self) -> None:
        """>#347:tushare x etf 字面量预检入队即拒(执行器 DB 行 scope 校验保留)。"""
        app = _make_app()
        env = await dw.data_bulk_download_start(
            app,
            market="a_share",
            start="2020-01-01",
            source="tushare",
            instrument_type="etf",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "tushare_scope_mismatch" in env.error.message

    async def test_quality_repair_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.data_quality_repair(
            app, symbols=["000001", "600000"], source="akshare", adjust="qfq"
        )
        assert env.status == "ok"
        assert env.data["kind"] == "quality_repair"
        assert env.data["payload"]["symbols"] == ["000001", "600000"]
        # idempotency_key 含 symbols_digest(payload 内)
        assert env.data["idempotency_key"].startswith("quality_repair:")

    async def test_quality_repair_empty_symbols(self) -> None:
        app = _make_app()
        env = await dw.data_quality_repair(app, symbols=[])
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_quality_repair_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.data_quality_repair(app, symbols=["000001"])
        assert env.status == "denied"

    async def test_dataset_publish_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-001",
            symbols=["000001", "600000"],
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
            release_kind="multi_asset_mixed",
        )
        assert env.status == "ok"
        assert env.data["kind"] == "dataset_publish"
        assert env.data["payload"]["release_id"] == "RL-2026-001"
        assert env.data["payload"]["release_kind"] == "multi_asset_mixed"
        assert env.data["idempotency_key"] == "publish:RL-2026-001"

    async def test_dataset_publish_invalid_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        # version pattern 校验失败 → invalid_argument(schema validator)
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-001",
            symbols=["000001"],
            version="",  # min_length=1
            start_date="2020-01-01",
            end_date="2026-01-01",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_dataset_publish_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-001",
            symbols=["000001"],
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
        )
        assert env.status == "denied"

    # --------------------------------------------------------------- #261

    async def test_dataset_publish_symbols_from_release_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:symbols_from_release 入队期解析成具体 symbols + 溯源。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        resolved = [f"SYM{i:04d}.SZ" for i in range(100)]

        async def _fake_resolve(session: Any, **kw: Any) -> list[str]:
            assert kw["symbols_from_release"] == "RL-BARS-MAIN"
            assert kw["symbols"] is None
            assert kw["full_market"] is False
            assert kw["release_kind"] == "daily_metrics"
            return resolved

        monkeypatch.setattr(dw, "resolve_release_symbols", _fake_resolve)
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-DM-1",
            symbols_from_release="RL-BARS-MAIN",
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
            release_kind="daily_metrics",
            source="tushare",
            adjustment="none",
        )
        assert env.status == "ok"
        assert env.data["kind"] == "dataset_publish"
        assert env.data["payload"]["symbols"] == resolved
        assert env.data["payload"]["symbols_source"] == {
            "mode": "from_release",
            "release_id": "RL-BARS-MAIN",
        }
        assert env.data["idempotency_key"] == "publish:RL-2026-DM-1"

    async def test_dataset_publish_full_market_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:full_market=true 入队期展开,溯源 mode=full_market。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )

        async def _fake_resolve(session: Any, **kw: Any) -> list[str]:
            assert kw["full_market"] is True
            assert kw["symbols"] is None
            assert kw["symbols_from_release"] is None
            return ["000300.SH", "510300.SH", "600519.SH"]

        monkeypatch.setattr(dw, "resolve_release_symbols", _fake_resolve)
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-FM-1",
            full_market=True,
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
            release_kind="multi_asset_mixed",
            source="mixed",
        )
        assert env.status == "ok"
        assert env.data["payload"]["symbols_source"] == {"mode": "full_market"}
        assert env.data["payload"]["symbols"] == [
            "000300.SH",
            "510300.SH",
            "600519.SH",
        ]

    async def test_dataset_publish_source_release_not_found_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:来源发布不存在 → 入队期 invalid_argument 具名拒绝。"""
        from finboard_persistence import ReleaseSymbolSourceError

        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )

        async def _fake_resolve(session: Any, **kw: Any) -> list[str]:
            raise ReleaseSymbolSourceError(
                "source_release_not_found", "标的集来源发布不存在: RL-MISSING"
            )

        monkeypatch.setattr(dw, "resolve_release_symbols", _fake_resolve)
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-BAD",
            symbols_from_release="RL-MISSING",
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "source_release_not_found" in env.error.message

    async def test_dataset_publish_symbol_source_missing_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:三种来源都未声明 → schema 层 invalid_argument。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-EMPTY",
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_dataset_publish_symbol_source_ambiguous_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:symbols 与 symbols_from_release 同时声明 → invalid_argument。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository, "create_or_get", _make_echo_create_or_get()
        )
        env = await dw.dataset_release_publish(
            app,
            release_id="RL-2026-DUP",
            symbols=["600519.SH"],
            symbols_from_release="RL-BARS-MAIN",
            version="v1",
            start_date="2020-01-01",
            end_date="2026-01-01",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "三选一" in env.error.message


# --------------------------------------------------------------------------- #
# data_fetch(同步)
# --------------------------------------------------------------------------- #


class TestDataFetch:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.data_fetch(
            app, symbol="000001", start="2020-01-01", end="2020-02-01"
        )
        assert env.status == "denied"

    async def test_invalid_source(self) -> None:
        app = _make_app()
        env = await dw.data_fetch(
            app,
            symbol="000001",
            start="2020-01-01",
            end="2020-02-01",
            source="bogus",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_all_sources_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()

        class _FailingProvider:
            async def fetch_bars(self, *a: Any, **kw: Any) -> list[Any]:
                raise RuntimeError("network down")

        monkeypatch.setattr(dw, "_get_provider", lambda source, **kw: _FailingProvider())
        monkeypatch.setattr(dw, "_resolve_provider_name", lambda s, **kw: "akshare")
        monkeypatch.setattr(dw, "_fallback_provider_name", lambda p, **kw: "yfinance")
        env = await dw.data_fetch(
            app, symbol="000001", start="2020-01-01", end="2020-02-01"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "unavailable"

    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        bar1 = SimpleNamespace(
            timestamp=datetime(2020, 1, 2, tzinfo=UTC), source="akshare"
        )
        bar2 = SimpleNamespace(
            timestamp=datetime(2020, 1, 3, tzinfo=UTC), source="akshare"
        )

        class _OkProvider:
            async def fetch_bars(self, *a: Any, **kw: Any) -> list[Any]:
                return [bar1, bar2]

        monkeypatch.setattr(dw, "_get_provider", lambda source, **kw: _OkProvider())
        monkeypatch.setattr(dw, "_resolve_provider_name", lambda s, **kw: "akshare")
        monkeypatch.setattr(dw, "_fallback_provider_name", lambda p, **kw: None)
        monkeypatch.setattr(
            dw, "_store_fetched_bars", lambda *a, **kw: _async_return(None)
        )
        env = await dw.data_fetch(
            app, symbol="000001", start="2020-01-01", end="2020-02-01"
        )
        assert env.status == "ok"
        assert env.data["symbol"] == "000001"
        assert env.data["bar_count"] == 2
        assert env.data["source"] == "akshare"
        assert env.data["fallback_used"] is False


# --------------------------------------------------------------------------- #
# config_get / config_update
# --------------------------------------------------------------------------- #


class TestConfig:
    async def test_config_get_no_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tmpdir = Path(tempfile.mkdtemp(prefix="finboard-mcp-test-"))
        try:
            app = _make_app(settings=Settings(data_provider="akshare"))
            monkeypatch.setattr(dw, "_CONFIG_FILE", str(tmpdir / "missing.json"))
            env = await dw.data_config_get(app)
            assert env.status == "ok"
            # 默认值
            assert env.data["sync_enabled"] is True
            assert env.data["sync_time"] == "15:35"
            assert env.data["data_provider"] == "akshare"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_config_get_existing_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmpdir = Path(tempfile.mkdtemp(prefix="finboard-mcp-test-"))
        try:
            cfg = tmpdir / "data_config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "sync_enabled": False,
                        "sync_time": "16:00",
                        "download_enabled": False,
                    }
                ),
                encoding="utf-8",
            )
            app = _make_app(settings=Settings(data_provider="tushare"))
            monkeypatch.setattr(dw, "_CONFIG_FILE", str(cfg))
            env = await dw.data_config_get(app)
            assert env.status == "ok"
            assert env.data["sync_enabled"] is False
            assert env.data["sync_time"] == "16:00"
            assert env.data["download_enabled"] is False
            assert env.data["data_provider"] == "tushare"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_config_update_merges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmpdir = Path(tempfile.mkdtemp(prefix="finboard-mcp-test-"))
        try:
            cfg = tmpdir / "data_config.json"
            cfg.write_text(
                json.dumps({"sync_enabled": True, "sync_time": "15:35"}),
                encoding="utf-8",
            )
            app = _make_app(settings=Settings(data_provider="akshare"))
            monkeypatch.setattr(dw, "_CONFIG_FILE", str(cfg))
            env = await dw.data_config_update(
                app, sync_time="17:30", download_lookback_days=10
            )
            assert env.status == "ok"
            # 更新字段
            assert env.data["sync_time"] == "17:30"
            assert env.data["download_lookback_days"] == 10
            # 未传字段保留
            assert env.data["sync_enabled"] is True
            # 持久化
            persisted = json.loads(cfg.read_text(encoding="utf-8"))
            assert persisted["sync_time"] == "17:30"
            assert persisted["download_lookback_days"] == 10
            assert persisted["sync_enabled"] is True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_config_update_write_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmpdir = Path(tempfile.mkdtemp(prefix="finboard-mcp-test-"))
        try:
            app = _make_app(write_enabled=False)
            monkeypatch.setattr(dw, "_CONFIG_FILE", str(tmpdir / "x.json"))
            env = await dw.data_config_update(app, sync_time="17:30")
            assert env.status == "denied"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# etf_sync
# --------------------------------------------------------------------------- #


class TestEtfSync:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.etf_sync(app)
        assert env.status == "denied"

    async def test_dry_run_preview(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        fake_sync = MagicMock()
        fake_sync.discover_and_classify = AsyncMock(
            return_value=[SimpleNamespace(review_status=SimpleNamespace(value="needs_review"))]
        )
        monkeypatch.setattr(
            "finboard_data.assets.EtfMetadataSync", lambda *a, **kw: fake_sync
        )
        preview = SimpleNamespace(
            total=10, to_insert=5, to_update=3, skipped_override=1,
            needs_review=8, auto_adopted=2,
        )
        monkeypatch.setattr(
            EtfMetadataRepository, "preview_upsert",
            lambda self, cls: _async_return(preview),
        )
        env = await dw.etf_sync(app, dry_run=True)
        assert env.status == "ok"
        assert env.data["dry_run"] is True
        assert env.data["total"] == 10
        assert env.data["to_insert"] == 5
        assert env.data["auto_adopted"] == 2

    async def test_write_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        classifications = [
            SimpleNamespace(review_status=SimpleNamespace(value="needs_review")),
            SimpleNamespace(review_status=SimpleNamespace(value="auto_adopted")),
        ]
        fake_sync = MagicMock()
        fake_sync.discover_and_classify = AsyncMock(return_value=classifications)
        monkeypatch.setattr(
            "finboard_data.assets.EtfMetadataSync", lambda *a, **kw: fake_sync
        )
        monkeypatch.setattr(
            EtfMetadataRepository, "upsert_batch",
            lambda self, cls, **kw: _async_return((2, 0, 0)),
        )
        env = await dw.etf_sync(app, dry_run=False)
        assert env.status == "ok"
        assert env.data["dry_run"] is False
        assert env.data["total"] == 2
        assert env.data["to_insert"] == 2
        assert env.data["needs_review"] == 1
        assert env.data["auto_adopted"] == 1

    async def test_import_error_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        def _raise(*a: Any, **kw: Any) -> Any:
            raise ImportError("akshare not installed")

        monkeypatch.setattr("finboard_data.assets.EtfMetadataSync", _raise)
        env = await dw.etf_sync(app)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "unavailable"


# --------------------------------------------------------------------------- #
# etf_batch_confirm
# --------------------------------------------------------------------------- #


class TestEtfBatchConfirm:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.etf_batch_confirm(app, codes=["159915"])
        assert env.status == "denied"

    async def test_empty_codes(self) -> None:
        app = _make_app()
        env = await dw.etf_batch_confirm(app, codes=[])
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            EtfMetadataRepository, "batch_confirm",
            lambda self, codes, **kw: _async_return(3),
        )
        env = await dw.etf_batch_confirm(
            app, codes=["159915", "510300", "588000"], reason="audit"
        )
        assert env.status == "ok"
        assert env.data["confirmed"] == 3


# --------------------------------------------------------------------------- #
# etf_update
# --------------------------------------------------------------------------- #


class TestEtfUpdate:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await dw.etf_update(app, code="159915", reason="fix")
        assert env.status == "denied"

    async def test_instrument_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        session = _get_session(app)
        # select(...).where(...).scalar_one_or_none() 链路 → 返回 None
        session.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        )
        env = await dw.etf_update(app, code="UNKNOWN", reason="fix")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_not_etf_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        session = _get_session(app)
        instrument = SimpleNamespace(code="600000", instrument_type="stock")
        session.execute = AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: instrument
            )
        )
        env = await dw.etf_update(app, code="600000", reason="fix")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        session = _get_session(app)
        instrument = SimpleNamespace(code="159915", instrument_type="etf")
        session.execute = AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: instrument
            )
        )
        row = _etf_row("159915", review_status="manually_overridden")
        monkeypatch.setattr(
            EtfMetadataRepository,
            "apply_manual_override",
            lambda self, code, **kw: _async_return(row),
        )
        env = await dw.etf_update(
            app,
            code="159915",
            reason="correct classification",
            execution_profile="domestic_equity_etf",
        )
        assert env.status == "ok"
        assert env.data["code"] == "159915"
        assert env.data["review_status"] == "manually_overridden"


# --------------------------------------------------------------------------- #
# etf_review_queue(只读)
# --------------------------------------------------------------------------- #


class TestEtfReviewQueue:
    async def test_returns_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            EtfMetadataRepository,
            "list_by_review_status",
            lambda self, status, **kw: _async_return(
                [_etf_row("159915"), _etf_row("510300")]
            ),
        )
        env = await dw.etf_review_queue(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert len(env.data) == 2
        assert env.data[0]["code"] == "159915"

    async def test_invalid_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        def _raise(self: Any, status: Any, **kw: Any) -> Any:
            raise ValueError("invalid status")

        monkeypatch.setattr(
            EtfMetadataRepository, "list_by_review_status", _raise
        )
        env = await dw.etf_review_queue(app, review_status="bogus")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_records_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            EtfMetadataRepository,
            "list_by_review_status",
            lambda self, status, **kw: _async_return([]),
        )
        await dw.etf_review_queue(app)
        assert app.audit.records[0].tool_name == "finboard.etf.review_queue"
        assert app.audit.records[0].status == "ok"
