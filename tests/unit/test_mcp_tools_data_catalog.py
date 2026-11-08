"""``finboard.data_catalog.*`` / ``finboard.dataset.release.*`` 只读工具测试
(serializer + register 契约 + mock service,无需 DB)。

覆盖:

* dataclass → dict 序列化(release / batch / entry / instrument / presence /
  reference),``Decimal`` 一律 ``float``,日期/时间 ISO 字符串;
* ``register(mcp)`` 精确暴露 5 个只读工具(其余工具不在本模块注册);
* 入参防御性收敛(limit 1..500 / offset>=0 / q 截断)与未知 dataset/status
  的 ``invalid_argument``;
* ``CatalogError`` 映射(``release_not_found`` → ``not_found``,其余 →
  ``conflict``)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.data_catalog.contracts import (
    CatalogEntry,
    CatalogError,
    CoverageReport,
    DatasetDetail,
    InstrumentPage,
    ReferencePage,
)
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_data.datasets import (
    CatalogDataset,
    CatalogStorage,
    catalog_spec,
)
from finboard_data.releases import ReleaseDatasetKind
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import data_catalog as catalog_tools
from finboard_persistence.catalog_repo import (
    BatchHeader,
    PresenceRow,
    ReleaseHeader,
    ReleaseInstrumentRow,
    ReleaseReferenceRow,
)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _release() -> ReleaseHeader:
    return ReleaseHeader(
        release_id="REL-509",
        dataset_name="daily_metrics",
        source="tushare",
        version="v1",
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        schema_version="1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        adjustment="none",
        symbol_count=1,
        row_count=4,
        coverage_pct=Decimal("0.75"),
        quality_status="passed",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        release_checksum="d" * 64,
        storage_uri="data_releases/REL-509",
        capabilities=({"key": "daily_metrics", "status": "ready"},),
    )


def _batch() -> BatchHeader:
    return BatchHeader(
        id=7,
        dataset="daily_metrics",
        source="tushare",
        dataset_version="2024.01",
        status="completed",
        quality_status="passed",
        expected_rows=4,
        received_rows=4,
        accepted_rows=3,
        rejected_rows=1,
        parameters={"start_date": "2024-01-02", "end_date": "2024-01-05"},
        error_summary=None,
        started_at=datetime(2024, 1, 6, 1, tzinfo=UTC),
        completed_at=datetime(2024, 1, 6, 2, tzinfo=UTC),
        published_at=None,
    )


def _entry() -> CatalogEntry:
    return CatalogEntry(
        key=CatalogDataset.DAILY_METRICS,
        title="每日指标",
        title_en="Daily metrics",
        storage=CatalogStorage.RESEARCH_TABLE,
        publishable=True,
        source="tushare",
        status="usable",
        release=_release(),
        batch=_batch(),
        coverage_pct=Decimal("99.5"),
        symbol_count=1,
        row_count=4,
        denominator="发布 expected_sessions x 发布标的数",
        local_start=date(2024, 1, 2),
        local_end=date(2024, 1, 5),
        updated_at=datetime(2024, 1, 6, tzinfo=UTC),
        cache_files=0,
        cache_bytes=0,
        warnings=("最近更新超过 30 天",),
    )


def _instrument() -> ReleaseInstrumentRow:
    return ReleaseInstrumentRow(
        release_id="REL-509",
        code="APIT509.SH",
        name="接口目录样本",
        market="a_share",
        instrument_type="stock",
        asset_class="equity",
        exchange="SSE",
        listing_board="main",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        row_count=4,
        expected_sessions=4,
        missing_sessions=1,
        suspended_sessions=0,
        anomaly_count=0,
        duplicate_count=0,
        coverage_pct=Decimal("0.5"),
        ready=False,
        issues=("missing_sessions",),
        list_date=date(2020, 1, 1),
        delist_date=None,
        status="active",
    )


def _presence() -> PresenceRow:
    return PresenceRow(
        release_id="REL-509",
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        version="v1",
        quality_status="passed",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        coverage_pct=Decimal("0.75"),
        ready=True,
    )


def _reference() -> ReleaseReferenceRow:
    return ReleaseReferenceRow(
        kind="research_run",
        ref_id="RR-123",
        status="completed",
        created_at=datetime(2024, 2, 1, 8, tzinfo=UTC),
    )


def _make_app() -> McpAppContext:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _fake_service(**results: Any) -> MagicMock:
    """构造只含指定 async 方法的服务替身(其余属性 MagicMock)。"""
    service = MagicMock()
    for name, value in results.items():
        setattr(service, name, AsyncMock(return_value=value))
    return service


# ---------------------------------------------------------------------------
# serializers
# ---------------------------------------------------------------------------


class TestSerializers:
    def test_release_dict(self) -> None:
        payload = catalog_tools._release_to_dict(_release())
        assert payload["release_id"] == "REL-509"
        assert payload["dataset_kind"] == "daily_metrics"
        assert payload["coverage_pct"] == 0.75
        assert isinstance(payload["coverage_pct"], float)
        assert payload["start_date"] == "2024-01-02"
        assert payload["published_at"] == "2024-01-06T00:00:00+00:00"
        assert payload["capabilities"] == [
            {"key": "daily_metrics", "status": "ready"}
        ]

    def test_batch_dict(self) -> None:
        payload = catalog_tools._batch_to_dict(_batch())
        assert payload["id"] == 7
        assert payload["status"] == "completed"
        assert payload["accepted_rows"] == 3
        assert payload["started_at"] == "2024-01-06T01:00:00+00:00"
        assert payload["completed_at"] == "2024-01-06T02:00:00+00:00"
        assert payload["published_at"] is None

    def test_entry_dict_nests_release_and_batch(self) -> None:
        payload = catalog_tools._entry_to_dict(_entry())
        assert payload["key"] == "daily_metrics"
        assert payload["storage"] == "research_table"
        assert payload["coverage_pct"] == 99.5
        assert isinstance(payload["coverage_pct"], float)
        assert payload["local_start"] == "2024-01-02"
        assert payload["local_end"] == "2024-01-05"
        assert payload["updated_at"] == "2024-01-06T00:00:00+00:00"
        assert payload["warnings"] == ["最近更新超过 30 天"]
        assert payload["release"]["release_id"] == "REL-509"
        assert payload["batch"]["id"] == 7

    def test_entry_dict_without_release_or_batch(self) -> None:
        entry = CatalogEntry(
            key=CatalogDataset.BARS,
            title="行情 bars",
            title_en="Market bars",
            storage=CatalogStorage.BARS_CACHE,
            publishable=True,
            source=None,
            status="not_ingested",
            denominator="expected_sessions",
            warnings=(),
        )
        payload = catalog_tools._entry_to_dict(entry)
        assert payload["release"] is None
        assert payload["batch"] is None
        assert payload["coverage_pct"] is None
        assert payload["key"] == "bars"

    def test_instrument_dict(self) -> None:
        payload = catalog_tools._instrument_to_dict(_instrument())
        assert payload["code"] == "APIT509.SH"
        assert payload["coverage_pct"] == 0.5
        assert isinstance(payload["coverage_pct"], float)
        assert payload["start_date"] == "2024-01-02"
        assert payload["list_date"] == "2020-01-01"
        assert payload["delist_date"] is None
        assert payload["issues"] == ["missing_sessions"]
        assert payload["ready"] is False

    def test_presence_dict(self) -> None:
        payload = catalog_tools._presence_to_dict(_presence())
        assert payload["dataset_kind"] == "daily_metrics"
        assert payload["coverage_pct"] == 0.75
        assert isinstance(payload["coverage_pct"], float)
        assert payload["published_at"] == "2024-01-06T00:00:00+00:00"

    def test_reference_dict(self) -> None:
        payload = catalog_tools._reference_to_dict(
            _reference(), "/research/runs?run=RR-123"
        )
        assert payload == {
            "kind": "research_run",
            "ref_id": "RR-123",
            "status": "completed",
            "created_at": "2024-02-01T08:00:00+00:00",
            "deep_link": "/research/runs?run=RR-123",
        }

    def test_reference_dict_without_deep_link(self) -> None:
        payload = catalog_tools._reference_to_dict(_reference())
        assert payload["deep_link"] is None


# ---------------------------------------------------------------------------
# register 契约
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS = {
    "finboard_data_catalog_list",
    "finboard_data_catalog_get",
    "finboard_data_coverage_get",
    "finboard_dataset_release_instruments",
    "finboard_dataset_release_references",
}


class _FakeMCPServer:
    """只记录 ``@mcp.tool(...)`` 注册结果的 MCPServer 替身。"""

    def __init__(self) -> None:
        self.tools: dict[str, str] = {}

    def tool(self, *, name: str, description: str = "") -> Callable[[F], F]:
        def decorator(fn: F) -> F:
            self.tools[name] = description
            return fn

        return decorator


class TestRegister:
    def test_registers_exactly_five_readonly_tools(self) -> None:
        fake = _FakeMCPServer()
        catalog_tools.register(cast("MCPServer", fake))
        assert set(fake.tools) == _EXPECTED_TOOLS
        assert len(fake.tools) == 5
        for name, description in fake.tools.items():
            assert "只读" in description, name
            assert "1..500" in description or "无分页" in description, name
            assert "不触发 dataset_sync" in description, name


# ---------------------------------------------------------------------------
# 工具行为(clamp / 错误映射 / payload)
# ---------------------------------------------------------------------------


class TestCatalogList:
    async def test_clamps_pagination_and_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _fake_service(list_entries=((_entry(),), 1))
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        app = _make_app()
        env = await catalog_tools.data_catalog_list(
            app,
            dataset="bars",
            status="usable",
            q="  " + "x" * 300,
            limit=9999,
            offset=-3,
        )
        assert env.status == "ok", env.error
        query = service.list_entries.await_args.args[0]
        assert query.dataset is CatalogDataset.BARS
        assert query.status == "usable"
        assert query.q == "x" * 128
        assert query.limit == 500
        assert query.offset == 0
        assert env.data["total"] == 1
        assert env.data["limit"] == 500
        assert env.data["offset"] == 0
        assert env.data["items"][0]["coverage_pct"] == 99.5
        assert env.data["generated_at"].endswith("+00:00")
        assert app.audit.records[0].tool_name == "finboard.data_catalog.list"

    async def test_rejects_unknown_dataset(self) -> None:
        env = await catalog_tools.data_catalog_list(_make_app(), dataset="nope")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "nope" in env.error.message

    async def test_rejects_unknown_status(self) -> None:
        env = await catalog_tools.data_catalog_list(_make_app(), status="bogus")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestCatalogGet:
    async def test_returns_detail_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        detail = DatasetDetail(
            entry=_entry(),
            spec=catalog_spec(CatalogDataset.DAILY_METRICS),
            releases=(_release(),),
            batches=(_batch(),),
            failures=(),
            adjustment="none",
        )
        service = _fake_service(get_dataset=detail)
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.data_catalog_get(
            _make_app(), "daily_metrics"
        )
        assert env.status == "ok", env.error
        assert service.get_dataset.await_args.args[0] is CatalogDataset.DAILY_METRICS
        assert env.data["entry"]["key"] == "daily_metrics"
        assert env.data["fields"]
        assert env.data["unit_notes"]
        assert env.data["pit_rule"].startswith("trade_date")
        assert env.data["calendar_market"] == "SSE"
        assert env.data["adjustment"] == "none"
        assert env.data["publishable"] is True
        assert env.data["releases"][0]["release_id"] == "REL-509"
        assert env.data["batches"][0]["id"] == 7
        assert env.data["failures"] == []


class TestCoverage:
    async def test_returns_bounded_preview(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = CoverageReport(
            dataset=CatalogDataset.DAILY_METRICS,
            denominator="发布 expected_sessions x 发布标的数",
            release=_release(),
            batch=None,
            coverage_pct=Decimal("0.75"),
            expected_sessions=4,
            missing_sessions=1,
            symbols_with_gaps=1,
            missing_preview=(_instrument(),),
            warnings=("逐标的索引已自愈",),
        )
        service = _fake_service(get_coverage=report)
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.data_coverage_get(
            _make_app(), dataset="daily_metrics", limit=9999
        )
        assert env.status == "ok", env.error
        assert service.get_coverage.await_args.kwargs["limit"] == 500
        assert env.data["dataset"] == "daily_metrics"
        assert env.data["denominator"]
        assert env.data["coverage_pct"] == 0.75
        assert env.data["symbols_with_gaps"] == 1
        assert env.data["missing_preview"][0]["code"] == "APIT509.SH"
        assert env.data["warnings"] == ["逐标的索引已自愈"]

    async def test_release_not_found_maps_to_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = MagicMock()
        service.get_coverage = AsyncMock(
            side_effect=CatalogError(
                "release_not_found", "未找到研究数据发布: REL-X"
            )
        )
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.data_coverage_get(
            _make_app(), dataset="daily_metrics", release_id="REL-X"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
        assert "REL-X" in env.error.message


class TestReleaseInstruments:
    async def test_returns_page_and_clamps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = InstrumentPage(
            release_id="REL-509", items=(_instrument(),), total=1
        )
        service = _fake_service(list_release_instruments=page)
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.dataset_release_instruments(
            _make_app(),
            release_id="REL-509",
            missing_only=True,
            limit=9999,
            offset=-1,
        )
        assert env.status == "ok", env.error
        kwargs = service.list_release_instruments.await_args.kwargs
        assert kwargs["missing_only"] is True
        assert kwargs["limit"] == 500
        assert kwargs["offset"] == 0
        assert env.data["release_id"] == "REL-509"
        assert env.data["total"] == 1
        assert env.data["limit"] == 500
        assert env.data["items"][0]["missing_sessions"] == 1
        assert env.data["items"][0]["coverage_pct"] == 0.5

    async def test_index_unavailable_maps_to_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = MagicMock()
        service.list_release_instruments = AsyncMock(
            side_effect=CatalogError(
                "release_index_unavailable", "逐标的索引不可重建"
            )
        )
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.dataset_release_instruments(
            _make_app(), release_id="REL-509"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_blank_release_id_rejected(self) -> None:
        env = await catalog_tools.dataset_release_instruments(
            _make_app(), release_id="   "
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestReleaseReferences:
    async def test_returns_deep_links(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = ReferencePage(
            release_id="REL-509",
            items=(_reference(),),
            deep_links={
                "research_run:RR-123": "/research/runs?run=RR-123"
            },
        )
        service = _fake_service(list_release_references=page)
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.dataset_release_references(
            _make_app(), release_id="REL-509", limit=9999
        )
        assert env.status == "ok", env.error
        assert service.list_release_references.await_args.kwargs["limit"] == 500
        assert env.data["release_id"] == "REL-509"
        assert env.data["items"] == [
            {
                "kind": "research_run",
                "ref_id": "RR-123",
                "status": "completed",
                "created_at": "2024-02-01T08:00:00+00:00",
                "deep_link": "/research/runs?run=RR-123",
            }
        ]

    async def test_release_not_found_maps_to_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = MagicMock()
        service.list_release_references = AsyncMock(
            side_effect=CatalogError(
                "release_not_found", "未找到研究数据发布: REL-X"
            )
        )
        monkeypatch.setattr(catalog_tools, "_service", lambda session: service)
        env = await catalog_tools.dataset_release_references(
            _make_app(), release_id="REL-X"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
