"""``finboard.instrument.*`` / ``finboard.dataset.*`` / ``finboard.data.*`` /
``finboard.tushare.*`` 工具 —— 数据查询只读工具测试(mock session,无需 DB)。

覆盖:

* DB-backed(instrument / dataset release / manifest)—— mock ``AsyncSession`` 的
  ``execute().scalars().all()`` / ``scalar_one_or_none()``;
* file-backed(cache status / quality / tushare quota)—— monkeypatch
  ``finboard_data`` 内的 ``ParquetCache`` / ``BarQualityChecker`` /
  ``shared_tushare_budget``,避免依赖真实文件系统。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import data as data_tools
from finboard_persistence.models import (
    DatasetManifestModel,
    InstrumentModel,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    """辅助:把 monkeypatch 的同步 lambda 包装成 async 返回值。"""
    return value


def _instrument_model(code: str = "000001") -> InstrumentModel:
    return InstrumentModel(
        code=code,
        name="平安银行",
        market="a_share",
        instrument_type="stock",
        exchange="SZSE",
        listing_board="main",
        list_date=date(2026, 1, 1),
        delist_date=None,
        status="active",
        sector="金融",
        industry="银行",
    )


def _manifest_model() -> DatasetManifestModel:
    return DatasetManifestModel(
        id=1,
        dataset_name="a_share_daily",
        source="tushare",
        version="2026.01",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        row_count=1000,
        symbol_count=10,
        coverage_pct=Decimal("99.5"),
        gaps=[],
        checksum="abc123",
        quality_status="passed",
        quality_report={},
        published_at=datetime(2026, 1, 31, tzinfo=UTC),
        code_version="v1",
    )


def _release_domain() -> SimpleNamespace:
    """构造一个类 ResearchDatasetRelease 领域对象(duck-typed)。"""
    cap = SimpleNamespace(
        key="daily_bars",
        status=SimpleNamespace(value="ready"),
        symbol_count=10,
        ready_count=10,
        missing_requirements=[],
    )
    period = SimpleNamespace(value="D1")
    quality = SimpleNamespace(value="passed")
    return SimpleNamespace(
        release_id="REL-1",
        dataset_name="a_share_daily",
        source="tushare",
        version="2026.01",
        schema_version="1",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        period=period,
        adjustment="qfq",
        code_version="v1",
        published_at=datetime(2026, 1, 31, tzinfo=UTC),
        symbol_count=10,
        row_count=1000,
        coverage_pct=Decimal("99.5"),
        capabilities=[cap],
        quality_status=quality,
        known_limitations=[],
        metadata_version="1",
        release_checksum="abc123",
        as_dict=lambda: {
            "release_id": "REL-1",
            "dataset_name": "a_share_daily",
            "fields": ["open", "close"],
        },
    )


def _session_maker_from_execute(
    execute_results: list,
) -> async_sessionmaker[AsyncSession]:
    """构造 mock session_maker,execute 按调用顺序返回 execute_results。

    每个 element 可以是 ``MagicMock``(预设了 scalars().all() 等)或一个 list
    (会被包装成 ``scalars().all()`` 返回该 list)。
    """
    session = AsyncMock()
    mocks: list[MagicMock] = []
    for res in execute_results:
        m = MagicMock()
        if isinstance(res, list):
            m.scalars.return_value.all.return_value = res
        else:
            m.scalar_one_or_none.return_value = res
        mocks.append(m)
    session.execute = AsyncMock(side_effect=mocks)

    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app(session_maker: async_sessionmaker[AsyncSession] | None = None) -> McpAppContext:
    provider = FakeLLMProvider()
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or _session_maker_from_execute([]),
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        provider=provider,
    )


# ---------------------------------------------------------------------------
# finboard.instrument.*
# ---------------------------------------------------------------------------


class TestInstrumentList:
    async def test_returns_items_with_total(self) -> None:
        # list_page 调 execute 两次:count(用 scalars().all() 取 len) + rows
        sm = _session_maker_from_execute(
            [[_instrument_model()], [_instrument_model()]]
        )
        app = _make_app(sm)
        env = await data_tools.instrument_list(app, limit=10)
        assert env.status == "ok"
        assert env.data["total"] == 1
        assert env.data["items"][0]["code"] == "000001"
        assert env.data["items"][0]["market"] == "a_share"
        assert env.data["limit"] == 10

    async def test_records_audit(self) -> None:
        sm = _session_maker_from_execute([[], []])
        app = _make_app(sm)
        await data_tools.instrument_list(app)
        assert len(app.audit.records) == 1
        assert app.audit.records[0].tool_name == "finboard.instrument.list"


class TestInstrumentGet:
    async def test_returns_detail(self) -> None:
        sm = _session_maker_from_execute([_instrument_model()])
        app = _make_app(sm)
        env = await data_tools.instrument_get(app, "000001")
        assert env.status == "ok"
        assert env.data["code"] == "000001"
        assert env.data["name"] == "平安银行"

    async def test_not_found(self) -> None:
        sm = _session_maker_from_execute([None])
        app = _make_app(sm)
        env = await data_tools.instrument_get(app, "MISSING")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestInstrumentSearch:
    async def test_returns_list(self) -> None:
        sm = _session_maker_from_execute([[_instrument_model(), _instrument_model("000002")]])
        app = _make_app(sm)
        env = await data_tools.instrument_search(app, "平安", limit=5)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert len(env.data) == 2
        assert env.data[0]["code"] == "000001"


# ---------------------------------------------------------------------------
# finboard.dataset.*
# ---------------------------------------------------------------------------


class TestDatasetReleaseList:
    async def test_returns_summaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release = _release_domain()
        # ResearchDatasetReleaseRepository.list 内部走 _release_from_row(ORM→domain),
        # 这里 monkeypatch 直接返回领域对象,避免构造合法 manifest dict。
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )

        monkeypatch.setattr(
            Repo, "list", lambda self, **kw: _async_return([release])
        )
        app = _make_app()
        env = await data_tools.dataset_release_list(app, limit=10)
        assert env.status == "ok"
        assert env.data[0]["release_id"] == "REL-1"
        assert env.data[0]["period"] == "D1"
        assert env.data[0]["quality_status"] == "passed"
        assert env.data[0]["capabilities"][0]["key"] == "daily_bars"

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )

        monkeypatch.setattr(Repo, "list", lambda self, **kw: _async_return([]))
        app = _make_app()
        await data_tools.dataset_release_list(app)
        assert app.audit.records[0].tool_name == "finboard.dataset_release.list"


class TestDatasetReleaseGet:
    async def test_returns_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release = _release_domain()
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )

        monkeypatch.setattr(
            Repo, "get", lambda self, release_id: _async_return(release)
        )
        app = _make_app()
        env = await data_tools.dataset_release_get(app, "REL-1")
        assert env.status == "ok"
        assert env.data["release_id"] == "REL-1"
        assert env.data["symbol_count"] == 10
        assert env.data["coverage_pct"] == "99.5"  # Decimal → str
        assert "fields" in env.data  # from as_dict()

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )

        monkeypatch.setattr(
            Repo, "get", lambda self, release_id: _async_return(None)
        )
        app = _make_app()
        env = await data_tools.dataset_release_get(app, "REL-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestDatasetManifestList:
    async def test_returns_manifests(self) -> None:
        sm = _session_maker_from_execute([[_manifest_model()]])
        app = _make_app(sm)
        env = await data_tools.dataset_manifest_list(app, limit=10)
        assert env.status == "ok"
        assert env.data[0]["dataset_name"] == "a_share_daily"
        assert env.data[0]["quality_status"] == "passed"
        assert env.data[0]["coverage_pct"] == "99.5"


# ---------------------------------------------------------------------------
# finboard.data.* / finboard.tushare.*(file-backed,monkeypatch)
# ---------------------------------------------------------------------------


class TestDataCacheStatus:
    async def test_returns_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """monkeypatch ParquetCache + infer_a_share_listing_board + Path.glob。"""
        metadata = SimpleNamespace(
            bar_count=100,
            first_date=date(2026, 1, 1),
            last_date=date(2026, 1, 31),
            source="tushare",
        )
        fake_cache = MagicMock()
        fake_cache.metadata = AsyncMock(return_value=metadata)

        board = SimpleNamespace(value="main")
        fake_board_fn = MagicMock(return_value=board)

        # 模拟 glob 返回一个 parquet 文件
        from pathlib import Path as RealPath

        fake_path = MagicMock()
        fake_path.stem = "000001_D1_qfq"
        monkeypatch.setattr(
            RealPath, "glob", lambda self, pattern: iter([fake_path])
        )

        import finboard_data.cache as cache_mod
        import finboard_data.discovery as disc_mod

        monkeypatch.setattr(cache_mod, "ParquetCache", lambda *a, **kw: fake_cache)
        monkeypatch.setattr(disc_mod, "infer_a_share_listing_board", fake_board_fn)

        app = _make_app()
        env = await data_tools.data_cache_status(app, limit=10)
        assert env.status == "ok"
        assert env.data["total"] == 1
        assert env.data["items"][0]["symbol"] == "000001"
        assert env.data["items"][0]["bar_count"] == 100

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import Path as RealPath

        monkeypatch.setattr(RealPath, "glob", lambda self, pattern: iter([]))
        app = _make_app()
        await data_tools.data_cache_status(app)
        assert app.audit.records[0].tool_name == "finboard.data.cache_status"


class TestDataQualityCheck:
    async def test_returns_quality_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qr = SimpleNamespace(
            total_bars=100,
            anomaly_count=0,
            duplicate_count=0,
            sources=["tushare"],
            anomalies=[],
            passed=True,
        )
        fake_cache = MagicMock()
        fake_cache.read = AsyncMock(return_value=[1, 2, 3])  # truthy bars
        fake_checker = MagicMock()
        fake_checker.check = MagicMock(return_value=qr)

        import finboard_data.cache as cache_mod
        import finboard_data.quality as quality_mod

        monkeypatch.setattr(cache_mod, "ParquetCache", lambda *a, **kw: fake_cache)
        monkeypatch.setattr(cache_mod, "make_symbol", lambda code: code)
        monkeypatch.setattr(quality_mod, "BarQualityChecker", lambda: fake_checker)

        app = _make_app()
        env = await data_tools.data_quality_check(app, symbols="000001")
        assert env.status == "ok"
        assert env.data[0]["symbol"] == "000001"
        assert env.data[0]["total_bars"] == 100
        assert env.data[0]["passed"] is True
        assert env.data[0]["primary_source"] == "tushare"

    async def test_per_symbol_error_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cache = MagicMock()
        fake_cache.read = AsyncMock(side_effect=FileNotFoundError("no data"))
        fake_checker = MagicMock()

        import finboard_data.cache as cache_mod
        import finboard_data.quality as quality_mod

        monkeypatch.setattr(cache_mod, "ParquetCache", lambda *a, **kw: fake_cache)
        monkeypatch.setattr(cache_mod, "make_symbol", lambda code: code)
        monkeypatch.setattr(quality_mod, "BarQualityChecker", lambda: fake_checker)

        app = _make_app()
        env = await data_tools.data_quality_check(app, symbols="000001")
        assert env.status == "ok"
        assert env.data[0]["passed"] is False
        assert "no data" in env.data[0]["error"]


class TestTushareQuota:
    async def test_returns_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot = SimpleNamespace(
            date="2026-08-10",
            requests_per_minute=200,
            daily_limit=100_000,
            used=50,
            remaining=99_950,
        )
        fake_budget = MagicMock()
        fake_budget.snapshot = AsyncMock(return_value=snapshot)
        fake_factory = MagicMock(return_value=fake_budget)

        import finboard_data.tushare_budget as budget_mod

        monkeypatch.setattr(budget_mod, "shared_tushare_budget", fake_factory)

        app = _make_app()
        env = await data_tools.tushare_quota(app)
        assert env.status == "ok"
        assert env.data["date"] == "2026-08-10"
        assert env.data["remaining"] == 99_950

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot = SimpleNamespace(
            date="2026-08-10",
            requests_per_minute=200,
            daily_limit=100_000,
            used=0,
            remaining=100_000,
        )
        fake_budget = MagicMock()
        fake_budget.snapshot = AsyncMock(return_value=snapshot)
        import finboard_data.tushare_budget as budget_mod

        monkeypatch.setattr(
            budget_mod, "shared_tushare_budget", lambda **kw: fake_budget
        )
        app = _make_app()
        await data_tools.tushare_quota(app)
        assert app.audit.records[0].tool_name == "finboard.tushare.quota"
