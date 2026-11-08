"""``finboard.data_scope.preview`` 只读工具测试(注册契约 + mock resolver,无需 DB)。

覆盖:精确注册 1 个只读工具 / summary 与 detail 视图差异 / 入参校验
(未知 mode/view、existing_spec 缺 strategy_id、非法 scope) → invalid_argument。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.data_scope import (
    ResolvedRelease,
    ScopeBlocker,
    ScopeGaps,
    ScopeResolution,
    ScopeResolutionError,
    ScopeUniverseResult,
    ScopeWindow,
)
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_data.datasets import CatalogDataset
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.execution import McpToolError
from finboard_mcp.tools import data_scope as scope_tools

_EXPECTED_TOOLS = {"finboard_data_scope_preview"}


class _FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, str] = {}

    def tool(self, *, name: str, description: str) -> Any:
        def _decorator(func: Any) -> Any:
            self.tools[name] = description
            return func

        return _decorator


def _make_app() -> McpAppContext:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=False,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _scope_payload() -> dict[str, Any]:
    return {
        "universe": {"explicit_symbols": ["000001.SZ"]},
        "decision": {"start": "2024-01-02", "end": "2024-03-28"},
        "warmup_sessions": 20,
        "tail_sessions": 5,
        "datasets": {"required": ["bars"]},
    }


def _resolution() -> ScopeResolution:
    return ScopeResolution(
        resolution_id="SC-0123456789abcdef",
        scope_checksum="f" * 64,
        as_of=datetime(2024, 1, 1, tzinfo=UTC),
        selection_mode="auto",
        window=ScopeWindow(
            warmup_start=date(2023, 12, 1),
            decision_start=date(2024, 1, 2),
            decision_end=date(2024, 3, 28),
            valuation_end=date(2024, 4, 8),
            warmup_sessions=20,
            decision_sessions=60,
            tail_sessions=5,
            calendar_markets=("SSE",),
            calendar_source="trade_cal",
        ),
        releases=(
            ResolvedRelease(
                artifact_id="DR-bars-510",
                dataset=CatalogDataset.BARS,
                role="bars_primary",
                version="v1",
                checksum="a" * 64,
                published_at=datetime(2024, 1, 1, tzinfo=UTC),
                start_date=date(2023, 12, 1),
                end_date=date(2024, 4, 8),
                coverage_pct=Decimal("0.99"),
                symbol_count=6,
                reason="窗口覆盖 + 标的齐全",
            ),
        ),
        universe=ScopeUniverseResult(
            requested_count=1,
            resolved_count=1,
            truncated=False,
            symbols_preview=("000001.SZ",),
        ),
        gaps=ScopeGaps(missing_in_bars=("000002.SZ",)),
        warnings=("预热不足:请求 30 个交易日",),
        blockers=(ScopeBlocker(code="scope_x", message="阻塞示例"),),
    )


class TestRegister:
    def test_registers_single_readonly_tool(self) -> None:
        fake = _FakeMCPServer()
        scope_tools.register(cast("MCPServer", fake))
        assert set(fake.tools) == _EXPECTED_TOOLS
        description = fake.tools["finboard_data_scope_preview"]
        assert "只读" in description
        assert "resolution_id" in description
        assert "不触发同步" in description


class TestPreview:
    async def test_summary_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scope_tools,
            "resolve_data_scope",
            AsyncMock(return_value=_resolution()),
        )
        env = await scope_tools.data_scope_preview(
            _make_app(), scope=_scope_payload()
        )
        assert env.status == "ok", env.error
        data = env.data
        assert data["resolution_id"] == "SC-0123456789abcdef"
        assert data["window"]["decision_start"] == "2024-01-02"
        assert data["releases"][0]["artifact_id"] == "DR-bars-510"
        assert data["releases"][0]["coverage_pct"] == 0.99
        assert data["blockers"][0]["code"] == "scope_x"
        assert data["alternative_count"] == 0
        assert data["affected_strategy_count"] == 0
        assert "alternatives" not in data

    async def test_detail_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scope_tools,
            "resolve_data_scope",
            AsyncMock(return_value=_resolution()),
        )
        env = await scope_tools.data_scope_preview(
            _make_app(), scope=_scope_payload(), view="detail"
        )
        assert env.status == "ok", env.error
        assert env.data["alternatives"] == []
        assert env.data["affected_strategies"] == []

    async def test_invalid_mode_and_view_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scope_tools,
            "resolve_data_scope",
            AsyncMock(return_value=_resolution()),
        )
        with pytest.raises(McpToolError) as mode_error:
            await scope_tools.data_scope_preview(
                _make_app(), scope=_scope_payload(), mode="weird"
            )
        assert mode_error.value.kind == "invalid_argument"
        with pytest.raises(McpToolError) as view_error:
            await scope_tools.data_scope_preview(
                _make_app(), scope=_scope_payload(), view="weird"
            )
        assert view_error.value.kind == "invalid_argument"

    async def test_existing_spec_requires_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(McpToolError) as excinfo:
            await scope_tools.data_scope_preview(
                _make_app(), scope=_scope_payload(), mode="existing_spec"
            )
        assert excinfo.value.kind == "invalid_argument"

    async def test_invalid_scope_rejected(self) -> None:
        with pytest.raises(McpToolError) as excinfo:
            await scope_tools.data_scope_preview(
                _make_app(), scope={"universe": {}}
            )
        assert excinfo.value.kind == "invalid_argument"
        assert "scope 非法" in str(excinfo.value)

    async def test_resolver_error_mapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scope_tools,
            "resolve_data_scope",
            AsyncMock(
                side_effect=ScopeResolutionError(
                    "scope_calendar_unavailable", "缺少交易日历"
                )
            ),
        )
        env = await scope_tools.data_scope_preview(
            _make_app(), scope=_scope_payload()
        )
        # run_tool 把工具内异常收敛成错误信封(不向 MCP 客户端抛原始异常)。
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "scope_calendar_unavailable" in env.error.message
