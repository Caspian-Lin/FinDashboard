"""instruments API 路由的单元测试(issue #58)。"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_shared.types import BarPeriod, DatasetQualityStatus


def _async_return(value: Any) -> asyncio.Future[Any]:
    """构造已完成的 awaitable(repo.get 等 async 方法的 monkeypatch 桩)。"""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_result(value)
    return fut


def _dataset_release() -> ResearchDatasetRelease:
    return ResearchDatasetRelease(
        release_id="api-r77-v1",
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
        version="api-v1",
        schema_version="v1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=("timestamp", "close"),
        availability_rules=(("daily_bars", "T 日收盘后可知"),),
        code_version="deadbeef",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        instruments=(),
        capabilities=(
            AssetCapability(
                key="futures",
                status=CapabilityStatus.INCOMPLETE,
                symbol_count=0,
                ready_count=0,
                missing_requirements=("no_instruments",),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1"},
        storage_uri="api-r77-v1",
        release_checksum="a" * 64,
    )


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    return session


class TestInstrumentsList:
    @pytest.mark.asyncio
    async def test_list_instruments(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_instruments

        # 构造 mock result
        row = MagicMock()
        row.code = "600519.SH"
        row.name = "贵州茅台"
        row.market = "a_share"
        row.instrument_type = "stock"
        row.exchange = "SSE"
        row.list_date = date(2001, 8, 27)
        row.delist_date = None
        row.status = "active"
        row.sector = "白酒"
        row.industry = "食品饮料"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_instruments(
            market="a_share",
            instrument_type="stock",
            limit=100,
            session=mock_session,
        )
        assert len(items) == 1
        assert items[0].code == "600519.SH"
        assert items[0].instrument_type == "stock"

    @pytest.mark.asyncio
    async def test_get_instrument_not_found(self, mock_session: MagicMock) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import get_instrument

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await get_instrument("NOTEXIST.SH", session=mock_session)
        assert exc_info.value.status_code == 404


class TestEtfClassification:
    @pytest.mark.asyncio
    async def test_create_manual_etf_classification(
        self,
        mock_session: MagicMock,
    ) -> None:
        from finboard_api.routes.instruments import update_etf_classification
        from finboard_api.schemas import EtfClassificationUpdate

        instrument = MagicMock()
        instrument.instrument_type = "etf"
        instrument_result = MagicMock()
        instrument_result.scalar_one_or_none.return_value = instrument
        metadata_result = MagicMock()
        metadata_result.scalars.return_value.first.return_value = None
        bare_metadata_result = MagicMock()
        bare_metadata_result.scalars.return_value.first.return_value = None
        mock_session.execute = AsyncMock(
            side_effect=[instrument_result, metadata_result, bare_metadata_result]
        )
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        result = await update_etf_classification(
            "159001.sz",
            EtfClassificationUpdate(execution_profile="money_market_etf"),
            session=mock_session,
        )

        assert result.execution_profile == "money_market_etf"
        assert result.category == "money_market"
        assert result.underlying_asset_class == "cash"
        assert result.allows_t_plus_0 is True
        assert result.manual_override is True
        assert result.review_status == "manually_overridden"
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_classification_for_non_etf(
        self,
        mock_session: MagicMock,
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import update_etf_classification
        from finboard_api.schemas import EtfClassificationUpdate

        instrument = MagicMock()
        instrument.instrument_type = "stock"
        result = MagicMock()
        result.scalar_one_or_none.return_value = instrument
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await update_etf_classification(
                "600519.SH",
                EtfClassificationUpdate(execution_profile="domestic_equity_etf"),
                session=mock_session,
            )

        assert exc_info.value.status_code == 409
        mock_session.add.assert_not_called()


class TestDatasetManifestList:
    @pytest.mark.asyncio
    async def test_list_manifests(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_dataset_manifests

        row = MagicMock()
        row.id = 1
        row.dataset_name = "daily_bars"
        row.source = "akshare"
        row.version = "2024Q1"
        row.start_date = date(2024, 1, 1)
        row.end_date = date(2024, 6, 30)
        row.row_count = 10000
        row.symbol_count = 100
        row.coverage_pct = Decimal("0.98")
        row.gaps = []
        row.checksum = "abc123"
        row.quality_status = "passed"
        row.quality_report = {}
        row.published_at = date(2024, 7, 1)
        row.code_version = "v1"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_dataset_manifests(
            dataset_name="daily_bars",
            quality_status="passed",
            limit=50,
            session=mock_session,
        )
        assert len(items) == 1
        assert items[0].dataset_name == "daily_bars"
        assert items[0].quality_status == "passed"


class TestResearchDatasetReleases:
    @pytest.mark.asyncio
    async def test_list_releases_exposes_capability_report(
        self,
        mock_session: MagicMock,
    ) -> None:
        from finboard_api.routes.instruments import list_dataset_releases

        row = MagicMock()
        row.manifest = _dataset_release().as_dict()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_dataset_releases(
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            quality_status="passed",
            limit=50,
            session=mock_session,
        )
        assert items[0].release_id == "api-r77-v1"
        assert items[0].capabilities[0].status == "incomplete"
        assert items[0].capabilities[0].missing_requirements == ["no_instruments"]

    @pytest.mark.asyncio
    async def test_get_release_not_found(self, mock_session: MagicMock) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import get_dataset_release

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await get_dataset_release("missing", session=mock_session)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_release_coverage_pct_serialized_as_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #349:API 层 coverage_pct 序列化为数值,manifest str(Decimal) 不动。

        manifest as_dict 的 str(Decimal) 参与 checksum 语义,必须保持原样;
        只有 API 响应模型把 coverage_pct 归一为 float 输出数值。
        """
        import json

        from finboard_data.releases import ReleasedInstrument, default_execution_metadata
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )
        from finboard_shared.types import AssetClass, InstrumentType, Market

        instrument = ReleasedInstrument(
            code="600519.SH",
            name="贵州茅台",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            asset_class=AssetClass.EQUITY,
            available_at=datetime(2024, 1, 6, tzinfo=UTC),
            execution=default_execution_metadata(
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            ),
            artifact_path="bars/600519.SH_D1_qfq.parquet",
            artifact_checksum="b" * 64,
            artifact_size=1024,
            row_count=40,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            expected_sessions=40,
            missing_sessions=3,
            suspended_sessions=2,
            anomaly_count=1,
            coverage_pct=Decimal("0.9833"),
            category="stock",
            ready=True,
        )
        import dataclasses

        base = _dataset_release()
        release = dataclasses.replace(
            base,
            instruments=(instrument,),
            quality_report={
                "release_coverage": "0.9833",
                "coverage": {
                    "missing_sessions": 3,
                    "suspended_sessions": 2,
                    "anomaly_count": 1,
                },
            },
        )
        monkeypatch.setattr(
            Repo, "get", lambda self, release_id: _async_return(release)
        )

        from finboard_api.routes.instruments import get_dataset_release

        out = await get_dataset_release("api-r77-v1", session=MagicMock())

        # 响应模型字段与 JSON 序列化均为数值(非 Decimal 字符串)。
        assert isinstance(out.coverage_pct, float)
        assert out.coverage_pct == pytest.approx(0.9833)
        assert isinstance(out.instruments[0].coverage_pct, float)
        assert out.instruments[0].coverage_pct == pytest.approx(0.9833)
        payload = json.loads(out.model_dump_json())
        assert isinstance(payload["coverage_pct"], float)
        assert isinstance(payload["instruments"][0]["coverage_pct"], float)

        # manifest 冻结语义保持:as_dict 仍输出 str(Decimal),checksum 口径不变。
        manifest = release.as_dict()
        manifest_instruments = cast(
            list[dict[str, object]], manifest["instruments"]
        )
        assert manifest_instruments[0]["coverage_pct"] == "0.9833"

    @pytest.mark.asyncio
    async def test_release_summary_coverage_pct_serialized_as_number(
        self,
    ) -> None:
        """issue #349:发布列表 API 的 coverage_pct 同样输出数值。"""
        import json

        from finboard_api.routes.instruments import list_dataset_releases
        from finboard_api.schemas import ResearchDatasetReleaseSummaryOut

        # model_validate 传入 Decimal,验证 lax 模式归一为 float(数值序列化)。
        summary = ResearchDatasetReleaseSummaryOut.model_validate(
            {
                "release_id": "api-r77-v1",
                "dataset_name": "multi_asset_daily_bars",
                "source": "fixed_sample",
                "version": "api-v1",
                "schema_version": "v1",
                "start_date": date(2024, 1, 2),
                "end_date": date(2024, 1, 5),
                "period": "D1",
                "adjustment": "qfq",
                "dataset_kind": "bars",
                "code_version": "deadbeef",
                "published_at": datetime(2024, 1, 6, tzinfo=UTC),
                "symbol_count": 1,
                "row_count": 40,
                "coverage_pct": Decimal("0.9833"),
                "capabilities": [],
                "quality_status": "passed",
                "known_limitations": [],
                "metadata_version": "v1",
                "release_checksum": "a" * 64,
            }
        )
        assert isinstance(summary.coverage_pct, float)
        payload = json.loads(summary.model_dump_json())
        assert isinstance(payload["coverage_pct"], float)

        # 列表路由透传领域对象 Decimal("1") 时同样归一为数值。
        row = MagicMock()
        row.manifest = _dataset_release().as_dict()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=result)
        items = await list_dataset_releases(
            dataset_name=None,
            source=None,
            quality_status=None,
            limit=50,
            session=mock_session,
        )
        assert isinstance(items[0].coverage_pct, float)
        assert items[0].coverage_pct == 0.0  # 空清单发布 coverage_pct = Decimal("0")

    @pytest.mark.asyncio
    async def test_check_release_symbols_membership(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #238:轻量成员核对,免全量 detail。"""
        from types import SimpleNamespace

        from finboard_api.routes.instruments import check_dataset_release_symbols
        from finboard_persistence.dataset_release_repo import (
            ResearchDatasetReleaseRepository as Repo,
        )

        release = SimpleNamespace(
            release_id="REL-1",
            instruments=[SimpleNamespace(code="600000.SH")],
        )
        monkeypatch.setattr(Repo, "get", lambda self, release_id: _async_return(release))

        out = await check_dataset_release_symbols(
            "REL-1", codes="600000.SH,000001.SZ", session=MagicMock()
        )
        assert out.release_id == "REL-1"
        assert out.requested == 2
        assert out.matched == ["600000.SH"]
        assert out.missing == ["000001.SZ"]

    @pytest.mark.asyncio
    async def test_check_release_symbols_not_found(
        self, mock_session: MagicMock
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import check_dataset_release_symbols

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await check_dataset_release_symbols(
                "missing", codes="600000.SH", session=mock_session
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_check_release_symbols_over_limit_rejected(
        self, mock_session: MagicMock
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import check_dataset_release_symbols

        with pytest.raises(HTTPException) as exc_info:
            await check_dataset_release_symbols(
                "REL-1",
                codes=",".join(f"S{i}" for i in range(501)),
                session=mock_session,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_create_release_enqueues_dataset_publish_job(
        self,
        mock_session: MagicMock,
    ) -> None:
        """create_dataset_release 迁移到统一队列:返回 202 + job_id(#144)。

        实际发布编排(标的校验 + DatasetReleaseSpec 构造 + service.publish)迁移到
        ``DatasetPublishExecutor``,本测试只验证路由 enqueue 行为。
        """
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import Response

        from finboard_api.routes.instruments import create_dataset_release
        from finboard_api.schemas import ResearchDatasetReleaseCreate

        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        request = ResearchDatasetReleaseCreate(
            release_id="api-r77-v1",
            dataset_name="a_share_daily_bars",
            release_kind="a_share_tushare",
            source="tushare",
            version="2026-07-31-v1",
            symbols=["600519.sh"],
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            adjustment="qfq",
            required_capabilities=[],
        )
        fake_row = SimpleNamespace(
            job_id="BJ-TESTPUB1",
            kind="dataset_publish",
            queue="data",
            status="queued",
            priority=0,
            payload={"release_id": "api-r77-v1"},
            payload_checksum="x" * 64,
            idempotency_key="publish:api-r77-v1",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:dataset_publish",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )

        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=AsyncMock(return_value=(fake_row, True)),
        ):
            result = await create_dataset_release(
                request, Response(status_code=202), session=mock_session
            )

        assert result.kind == "dataset_publish"
        assert result.job_id == "BJ-TESTPUB1"
        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()

    # --------------------------------------------------------------- #261

    def _release_create_base(self) -> dict[str, Any]:
        return {
            "release_id": "api-r78-v1",
            "version": "v1",
            "start_date": date(2024, 1, 2),
            "end_date": date(2024, 1, 5),
        }

    def test_create_release_symbol_source_exclusive(self) -> None:
        """#261:标的集来源三选一——全缺/多声明在 schema 层即拒绝。"""
        from pydantic import ValidationError

        from finboard_api.schemas import ResearchDatasetReleaseCreate

        base = self._release_create_base()
        with pytest.raises(ValidationError, match=r"三选一|之一"):
            ResearchDatasetReleaseCreate(**base)
        with pytest.raises(ValidationError, match="三选一"):
            ResearchDatasetReleaseCreate(
                **base, symbols=["600519.SH"], symbols_from_release="RL-SRC"
            )
        with pytest.raises(ValidationError, match="三选一"):
            ResearchDatasetReleaseCreate(**base, symbols=["600519.SH"], full_market=True)
        with pytest.raises(ValidationError, match="三选一"):
            ResearchDatasetReleaseCreate(
                **base, symbols_from_release="RL-SRC", full_market=True
            )
        body = ResearchDatasetReleaseCreate(**base, symbols_from_release="RL-SRC")
        assert body.symbols is None
        assert body.full_market is False

    @pytest.mark.asyncio
    async def test_create_release_symbols_from_release_resolves(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:symbols_from_release 入队期复制来源发布的冻结标的集。"""
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import Response

        from finboard_api.routes.instruments import create_dataset_release
        from finboard_api.schemas import ResearchDatasetReleaseCreate
        from finboard_persistence import ResearchDatasetReleaseRepository

        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        fake_release = SimpleNamespace(
            instruments=({"code": "600519.SH"}, {"code": "000001.SZ"}),
            is_usable=True,
            quality_status=SimpleNamespace(value="passed"),
        )

        async def _fake_get(self: Any, release_id: str) -> Any:
            assert release_id == "RL-SRC"
            return fake_release

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
        request = ResearchDatasetReleaseCreate(
            release_kind="a_share_tushare",
            source="tushare",
            adjustment="qfq",
            symbols_from_release="RL-SRC",
            **self._release_create_base(),
        )
        fake_row = SimpleNamespace(
            job_id="BJ-TESTPUB2",
            kind="dataset_publish",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:dataset_publish",
            created_at=datetime(2026, 9, 2, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        captured: dict[str, Any] = {}

        async def _echo(self: Any, **kw: Any) -> Any:
            captured.update(kw)
            return (fake_row, True)

        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=_echo,
        ):
            result = await create_dataset_release(
                request, Response(status_code=202), session=mock_session
            )

        assert result.job_id == "BJ-TESTPUB2"
        assert captured["payload"]["symbols"] == ["000001.SZ", "600519.SH"]
        assert captured["payload"]["symbols_source"] == {
            "mode": "from_release",
            "release_id": "RL-SRC",
        }
        assert captured["idempotency_key"] == "publish:api-r78-v1"

    @pytest.mark.asyncio
    async def test_create_release_symbols_from_release_not_found(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:来源发布不存在 → 422 具名拒绝,不入队。"""
        from fastapi import HTTPException, Response

        from finboard_api.routes.instruments import create_dataset_release
        from finboard_api.schemas import ResearchDatasetReleaseCreate
        from finboard_persistence import ResearchDatasetReleaseRepository

        async def _fake_get(self: Any, release_id: str) -> Any:
            return None

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", _fake_get)
        request = ResearchDatasetReleaseCreate(
            release_kind="a_share_tushare",
            source="tushare",
            adjustment="qfq",
            symbols_from_release="RL-MISSING",
            **self._release_create_base(),
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_dataset_release(
                request, Response(status_code=202), session=mock_session
            )
        assert exc_info.value.status_code == 422
        assert "source_release_not_found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_create_release_full_market_expands(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#261:full_market 按 kind 语义展开活跃标的并记录溯源。"""
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import Response

        from finboard_api.routes.instruments import create_dataset_release
        from finboard_api.schemas import ResearchDatasetReleaseCreate
        from finboard_persistence import InstrumentRepository

        mock_session.commit = AsyncMock()

        async def _fake_list_codes(
            self: Any,
            *,
            market: str | None = None,
            instrument_type: str | None = None,
            **_kw: Any,
        ) -> list[str]:
            return {"stock": ["600519.SH"]}.get(instrument_type or "", [])

        monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
        request = ResearchDatasetReleaseCreate(
            release_kind="a_share_tushare",
            source="tushare",
            adjustment="qfq",
            full_market=True,
            **self._release_create_base(),
        )
        fake_row = SimpleNamespace(
            job_id="BJ-TESTPUB3",
            kind="dataset_publish",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:dataset_publish",
            created_at=datetime(2026, 9, 2, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        captured: dict[str, Any] = {}

        async def _echo(self: Any, **kw: Any) -> Any:
            captured.update(kw)
            return (fake_row, True)

        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=_echo,
        ):
            result = await create_dataset_release(
                request, Response(status_code=202), session=mock_session
            )

        assert result.job_id == "BJ-TESTPUB3"
        assert captured["payload"]["symbols"] == ["600519.SH"]
        assert captured["payload"]["symbols_source"] == {"mode": "full_market"}

    @pytest.mark.asyncio
    async def test_create_release_full_market_board_filter(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#385:full_market + listing_boards/exchange 过滤,溯源进 symbols_source。"""
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import Response

        from finboard_api.routes.instruments import create_dataset_release
        from finboard_api.schemas import ResearchDatasetReleaseCreate
        from finboard_persistence import InstrumentRepository

        mock_session.commit = AsyncMock()

        async def _fake_list_codes(
            self: Any,
            *,
            market: str | None = None,
            instrument_type: str | None = None,
            exchange: str | None = None,
            listing_boards: list[str] | None = None,
        ) -> list[str]:
            # 模拟 SQL 过滤:股票按 boards 过滤(无 bse 请求则北交所不出)。
            if instrument_type == "stock":
                boards = listing_boards or ["sse_main", "szse_main", "star", "chinext", "bse"]
                return (
                    ["600519.SH"]
                    if "sse_main" in boards and "bse" not in boards
                    else ["600519.SH", "920023.BJ"]
                )
            return []

        monkeypatch.setattr(InstrumentRepository, "list_codes", _fake_list_codes)
        request = ResearchDatasetReleaseCreate(
            release_kind="a_share_tushare",
            source="tushare",
            adjustment="qfq",
            full_market=True,
            exchange="sse",
            listing_boards=["SSE_MAIN", "SZSE_MAIN", "STAR", "CHINEXT", "CDR"],
            **self._release_create_base(),
        )
        fake_row = SimpleNamespace(
            job_id="BJ-TESTPUB4",
            kind="dataset_publish",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:dataset_publish",
            created_at=datetime(2026, 9, 8, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
        )
        captured: dict[str, Any] = {}

        async def _echo(self: Any, **kw: Any) -> Any:
            captured.update(kw)
            return (fake_row, True)

        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=_echo,
        ):
            result = await create_dataset_release(
                request, Response(status_code=202), session=mock_session
            )

        assert result.job_id == "BJ-TESTPUB4"
        # 北交所标的被过滤掉,溯源记录归一化后的过滤条件。
        assert captured["payload"]["symbols"] == ["600519.SH"]
        assert captured["payload"]["symbols_source"] == {
            "mode": "full_market",
            "exchange": "SSE",
            "listing_boards": ["cdr", "chinext", "sse_main", "star", "szse_main"],
        }


class TestLifecycleEvents:
    @pytest.mark.asyncio
    async def test_list_lifecycle(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_lifecycle_events

        row = MagicMock()
        row.id = 1
        row.symbol = "600519.SH"
        row.event_type = "dividend"
        row.effective_date = date(2024, 6, 19)
        from datetime import datetime

        row.available_at = datetime(2024, 6, 19, 9, 0, tzinfo=UTC)
        row.source = "tushare"
        row.dataset_version = "2024Q2"
        row.details = {"per_share": "25.91"}

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_lifecycle_events("600519.SH", limit=100, session=mock_session)
        assert len(items) == 1
        assert items[0].event_type == "dividend"
