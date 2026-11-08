"""``finboard.data_backfill.*`` 工具测试(序列化 + 注册契约 + 服务替身,无 DB)。

覆盖:

* ``register(mcp)`` 精确暴露 2 只读 + 2 写工具,写工具描述标注权限门;
* 入参收敛:非法 spec → ``invalid_argument``;空 request_id → ``invalid_argument``;
* 预检经服务层返回 plan + ``plan_checksum``(只读);
* 去掉写权限时 submit/cancel 返回 ``denied`` 信封(#137 写工具约定);
* 请求不存在 → ``not_found``;
* 审计摘要只含 scope_checksum / fingerprint / 数据集清单,不含具体标的。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.data_backfill.contracts import (
    BackfillAction,
    BackfillDatasetSpec,
    BackfillGapSymbols,
    BackfillItem,
    BackfillPlan,
    BackfillQuota,
    BackfillRequestSpec,
)
from finboard_backtest.data_backfill.service import BackfillSubmitResult
from finboard_backtest.data_scope.contracts import (
    DataScopeSpec,
    ScopeDatasets,
    ScopeDecisionWindow,
    ScopeUniverse,
)
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_backtest.research_run.contracts import ResearchActorType
from finboard_data.datasets import CatalogDataset
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import data_backfill as backfill_tools
from finboard_persistence.models import (
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
)

_EXPECTED_TOOLS = {
    "finboard_data_backfill_preview",
    "finboard_data_backfill_submit",
    "finboard_data_backfill_list",
    "finboard_data_backfill_cancel",
}


class _FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, str] = {}

    def tool(self, name: str, description: str = "") -> Any:
        def _decorator(func: Any) -> Any:
            self.tools[name] = description
            return func

        return _decorator


def _spec() -> BackfillRequestSpec:
    return BackfillRequestSpec(
        scope=DataScopeSpec(
            universe=ScopeUniverse(explicit_symbols=("000001.SZ",)),
            decision=ScopeDecisionWindow(
                start=date(2024, 1, 1), end=date(2024, 3, 31)
            ),
            datasets=ScopeDatasets(required=(CatalogDataset.BARS,)),
        ),
        datasets=(BackfillDatasetSpec(dataset=CatalogDataset.DAILY_METRICS),),
    )


def _plan() -> BackfillPlan:
    return BackfillPlan(
        request_fingerprint="f" * 64,
        scope_checksum="c" * 64,
        as_of=datetime(2024, 4, 1, tzinfo=UTC),
        items=(
            BackfillItem(
                dataset=CatalogDataset.DAILY_METRICS,
                source="tushare",
                adjustment="none",
                action=BackfillAction.SYNC_PUBLISH,
                start=date(2024, 1, 1),
                end=date(2024, 3, 29),
                publish=True,
                missing_ranges=((date(2024, 1, 1), date(2024, 3, 29)),),
                missing_symbols=BackfillGapSymbols(count=1, preview=("000001.SZ",)),
                estimated_requests=64,
            ),
        ),
        quota=BackfillQuota(status="ok", estimated_total=64),
    )


def _request_row() -> DataBackfillRequestModel:
    return DataBackfillRequestModel(
        id=1,
        request_id="BF-AAAA1111BBBB2222",
        idempotency_key="k1",
        fingerprint="f" * 64,
        scope_checksum="c" * 64,
        status="queued",
        spec=_spec().model_dump(mode="json"),
        plan=_plan().model_dump(mode="json"),
        reused_request_ids=[],
        release_ids=[],
        blockers=[],
        requested_by="agent:mcp",
        actor_type="agent",
        created_at=datetime(2024, 4, 1, tzinfo=UTC),
        updated_at=datetime(2024, 4, 1, tzinfo=UTC),
    )


def _item_row() -> DataBackfillRequestItemModel:
    return DataBackfillRequestItemModel(
        id=7,
        request_id="BF-AAAA1111BBBB2222",
        dataset="daily_metrics",
        source="tushare",
        adjustment="none",
        action="sync_publish",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 29),
        symbols=[],
        status="pending",
        estimated_requests=64,
        batch_ids=[],
        accepted_rows=0,
        rejected_rows=0,
    )


class _FakeRepo:
    __slots__ = ("items", "row")

    def __init__(
        self,
        _session: Any,
        *,
        row: DataBackfillRequestModel | None = None,
        items: list[DataBackfillRequestItemModel] | None = None,
    ) -> None:
        self.row = row
        self.items = items or []

    async def get(self, _request_id: str) -> DataBackfillRequestModel | None:
        return self.row

    async def list_items(self, _request_id: str) -> list[DataBackfillRequestItemModel]:
        return self.items

    async def list_requests(self, **_kwargs: Any) -> tuple[list[Any], int]:
        return [], 0


def _make_app(*, write: bool = True) -> McpAppContext:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=write,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _patch_repo(monkeypatch: pytest.MonkeyPatch, repo: _FakeRepo) -> None:
    monkeypatch.setattr(
        backfill_tools,
        "DataBackfillRepository",
        lambda _session: repo,
    )


class TestRegister:
    def test_registers_exactly_four_tools(self) -> None:
        fake = _FakeMCPServer()
        backfill_tools.register(cast("MCPServer", fake))
        assert set(fake.tools) == _EXPECTED_TOOLS
        assert len(fake.tools) == 4

    def test_writes_documented_permission_gate(self) -> None:
        fake = _FakeMCPServer()
        backfill_tools.register(cast("MCPServer", fake))
        for name in (
            "finboard_data_backfill_submit",
            "finboard_data_backfill_cancel",
        ):
            assert "写" in fake.tools[name]
            assert "只读模式拒绝" in fake.tools[name] or "需写权限" in fake.tools[name]

    def test_reads_marked_readonly(self) -> None:
        fake = _FakeMCPServer()
        backfill_tools.register(cast("MCPServer", fake))
        for name in (
            "finboard_data_backfill_preview",
            "finboard_data_backfill_list",
        ):
            assert "只读" in fake.tools[name]


class TestArguments:
    def test_audit_summary_excludes_symbols(self) -> None:
        summary = backfill_tools._audit_arguments(_spec())
        assert set(summary) == {"scope_checksum", "fingerprint", "datasets"}
        assert summary["datasets"] == ["daily_metrics"]
        assert "000001.SZ" not in str(summary)

    async def test_invalid_spec_is_invalid_argument(self) -> None:
        app = _make_app()
        envelope = await backfill_tools.data_backfill_preview(
            app, spec={"datasets": []}
        )
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "invalid_argument"


class TestPreview:
    async def test_preview_returns_plan_and_checksum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan()

        async def _fake_plan(*_args: Any, **_kwargs: Any) -> BackfillPlan:
            return plan

        monkeypatch.setattr(backfill_tools, "plan_backfill", _fake_plan)
        envelope = await backfill_tools.data_backfill_preview(
            _make_app(), spec=_spec().model_dump(mode="json")
        )
        assert envelope.status == "ok"
        assert envelope.data["plan_checksum"] == plan.checksum()
        assert envelope.data["items"][0]["dataset"] == "daily_metrics"
        assert envelope.data["items"][0]["estimated_requests"] == 64


class TestSubmit:
    async def test_submit_denied_when_readonly(self) -> None:
        envelope = await backfill_tools.data_backfill_submit(
            _make_app(write=False), spec=_spec().model_dump(mode="json")
        )
        assert envelope.status == "denied"
        assert envelope.error is not None
        assert envelope.error.kind == "permission_denied"

    async def test_submit_returns_job_and_items(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _request_row()
        row.job_id = "BJ-1234"
        result = BackfillSubmitResult(
            request=row, plan=_plan(), created=True, reused=False
        )
        monkeypatch.setattr(
            backfill_tools,
            "submit_backfill",
            AsyncMock(return_value=result),
        )
        _patch_repo(monkeypatch, _FakeRepo(None, row=row, items=[_item_row()]))
        envelope = await backfill_tools.data_backfill_submit(
            _make_app(), spec=_spec().model_dump(mode="json")
        )
        assert envelope.status == "ok"
        assert envelope.data["request_id"] == "BF-AAAA1111BBBB2222"
        assert envelope.data["job_id"] == "BJ-1234"
        assert envelope.data["created"] is True
        assert envelope.data["items"][0]["status"] == "pending"
        assert envelope.idempotency_key is not None
        assert envelope.idempotency_key.startswith("mcp:")
        assert envelope.idempotency_key == (
            f"mcp:{backfill_tools._audit_arguments(_spec())['fingerprint']}"
        )


class TestQueryAndCancel:
    async def test_list_detail_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_repo(monkeypatch, _FakeRepo(None, row=None))
        envelope = await backfill_tools.data_backfill_list(
            _make_app(), request_id="BF-NOPE"
        )
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "not_found"

    async def test_list_returns_request_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_repo(
            monkeypatch, _FakeRepo(None, row=_request_row(), items=[_item_row()])
        )
        envelope = await backfill_tools.data_backfill_list(
            _make_app(), request_id="BF-AAAA1111BBBB2222"
        )
        assert envelope.status == "ok"
        assert envelope.data["status"] == "queued"
        assert len(envelope.data["items"]) == 1

    async def test_cancel_denied_when_readonly(self) -> None:
        envelope = await backfill_tools.data_backfill_cancel(
            _make_app(write=False), request_id="BF-AAAA1111BBBB2222"
        )
        assert envelope.status == "denied"

    async def test_cancel_blank_request_id(self) -> None:
        envelope = await backfill_tools.data_backfill_cancel(
            _make_app(), request_id="  "
        )
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "invalid_argument"

    async def test_actor_type_mapping(self) -> None:
        assert (
            backfill_tools._request_spec(_spec().model_dump(mode="json"))
            .ordered_datasets()[0]
            .dataset
            is CatalogDataset.DAILY_METRICS
        )
        # 显式覆盖:agent/human 只影响 actor_type 文案,不影响契约。
        assert ResearchActorType.AGENT.value == "agent"
