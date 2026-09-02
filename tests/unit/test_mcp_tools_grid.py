"""``finboard.backtest.grid.*`` 工具 —— 批量参数网格回测测试(issue #175)。

用 ``AsyncMock`` 模拟 ``AsyncSession`` + monkeypatch repository 方法,验证:

* 组合展开:显式列表 / 笛卡尔积、label 确定性、base params 合并;
* 选股维度展开(#259):selection_grid 单独使用 / 与 params 侧笛卡尔积 /
  逐组合 FactorSelectionParams 校验 / label 结构化差异 / 上限约束;
* 上限与校验:组合数超限 / max_combos 超硬上限 / 非法组合 /
  互斥形态 / 空列表 / 未知策略 → invalid_argument;
* 提交:成功入队 N 个 backtest_run job(同一事务、幂等键、grid 元数据)、
  幂等重提交返回既有网格 / checksum 不一致 → conflict、入队冲突 → conflict;
* 聚合:指标矩阵 / 竞争排名与最优标注 / 部分失败传播 / equity 降采样。

无需 DB。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import grid as grid_tools
from finboard_persistence import BacktestGridRunModel, BacktestGridRunRepository
from finboard_persistence.background_job_repo import BackgroundJobRepository

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    return value


def _session_maker() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or _session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _get_session(app: McpAppContext) -> AsyncMock:
    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _job_row(
    job_id: str = "BJ-1",
    *,
    status: str = "succeeded",
    result_ref: str | None = "1",
    error_code: str | None = None,
    error_summary: str | None = None,
) -> SimpleNamespace:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    return SimpleNamespace(
        job_id=job_id,
        kind="backtest_run",
        queue="default",
        status=status,
        priority=0,
        payload={},
        payload_checksum="abc",
        idempotency_key="btg-x",
        progress_total=0,
        progress_done=0,
        phase=None,
        result_ref=result_ref,
        error_code=error_code,
        error_summary=error_summary,
        attempt=0,
        max_attempts=3,
        worker_id=None,
        heartbeat_at=None,
        lease_until=None,
        requested_by="tester",
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )


def _run_row(run_id: int = 1, *, metrics: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        metrics=metrics
        or {
            "total_return": 0.1,
            "annualized_return": 0.2,
            "sharpe_ratio": 1.5,
            "max_drawdown": -0.05,
            "win_rate": 0.6,
            "trade_count": 10,
            "turnover": 2.0,
            "commission_paid": "100.0000",
            "stamp_tax_paid": "50.0000",
            "benchmark_return": 0.05,
            "excess_return": 0.05,
            "initial_capital": "100000.0000",
            "final_equity": "110000.0000",
        },
        equity_curve=[
            {"date": "2024-01-01", "equity": 100000.0},
            {"date": "2024-02-01", "equity": 105000.0},
            {"date": "2024-03-01", "equity": 110000.0},
        ],
    )


def _grid_row(
    grid_id: str = "BTG-ABCDEF1234567890",
    *,
    combos: list[dict[str, Any]] | None = None,
    combos_checksum: str = "abc",
) -> SimpleNamespace:
    return SimpleNamespace(
        grid_id=grid_id,
        idempotency_key="grid-key-1",
        combos_checksum=combos_checksum,
        strategy="ma_cross",
        symbols=["000001.SZ"],
        start="2024-01-01",
        end="2024-06-30",
        capital=Decimal("100000"),
        adjust="qfq",
        selection={"enabled": False},
        combos=combos
        or [
            {"index": 0, "label": "{}", "params": {"short_window": 5}, "job_id": "BJ-1"},
            {"index": 1, "label": "{}", "params": {"short_window": 10}, "job_id": "BJ-2"},
        ],
        combo_count=2,
        max_combos=20,
    )


def _submit_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "strategy": "ma_cross",
        "symbols": ["000001.SZ"],
        "start": "2024-01-01",
        "end": "2024-06-30",
        "capital": Decimal("100000"),
        "adjust": "qfq",
        "params": None,
        "selection": None,
        "params_list": [{"short_window": 5}, {"short_window": 10}],
        "params_grid": None,
        "selection_grid": None,
        "max_combos": 20,
        "grid_idempotency_key": "grid-key-1",
        "requested_by": "agent",
        "commission_rate": Decimal("0.0003"),
        "commission_min": Decimal("1"),
        "stamp_tax_rate": Decimal("0.0005"),
        "slippage_bps": Decimal("0"),
    }
    kwargs.update(overrides)
    return kwargs


def _no_existing_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等查重为空(提交类测试的默认前提)。"""

    monkeypatch.setattr(
        BacktestGridRunRepository,
        "get_by_idempotency_key",
        lambda self, key: _async_return(None),
    )


# ---------------------------------------------------------------------------
# finboard.backtest.grid_submit —— 校验与展开
# ---------------------------------------------------------------------------


class TestGridSubmitValidation:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_requires_single_combo_source(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app, **_submit_kwargs(params_list=None, params_grid=None)
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_grid={"short_window": [5]},
            ),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_empty_sources_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs(params_list=[]))
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

        env = await grid_tools.backtest_grid_submit(
            app, **_submit_kwargs(params_list=None, params_grid={})
        )
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                params_grid={"short_window": []},
            ),
        )
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_params_list_item_must_be_dict(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs(params_list=[5]))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_unknown_strategy_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs(strategy="bogus"))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_invalid_combo_params_rejected(self) -> None:
        """组合内参数非法(short_window < 1)→ invalid_argument,且不落库不入队。"""

        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app, **_submit_kwargs(params_list=[{"short_window": 0}])
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        session = _get_session(app)
        session.add.assert_not_called()

    async def test_combo_count_over_max_combos_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                max_combos=2,
                params_list=[
                    {"short_window": 5},
                    {"short_window": 10},
                    {"short_window": 15},
                ],
            ),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_max_combos_over_hard_limit_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs(max_combos=51))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_short_idempotency_key_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app, **_submit_kwargs(grid_idempotency_key="short")
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_invalid_dates_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs(start="not-a-date"))
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# finboard.backtest.grid_submit —— 成功提交
# ---------------------------------------------------------------------------


class TestGridSubmitOk:
    async def test_explicit_list_submits_n_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        _no_existing_grid(monkeypatch)
        calls: list[dict[str, Any]] = []

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(job_id=f"BJ-{len(calls)}"), True

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())

        assert env.status == "ok"
        data = env.data
        assert data["grid_id"].startswith("BTG-")
        assert data["created"] is True
        assert data["combo_count"] == 2
        assert len(data["jobs"]) == 2
        assert [job["combo_index"] for job in data["jobs"]] == [0, 1]
        assert all(job["job_id"].startswith("BJ-") for job in data["jobs"])
        # 每个组合一个 backtest_run job,同一事务落库
        assert len(calls) == 2
        for index, kw in enumerate(calls):
            assert kw["kind"] == "backtest_run"
            assert kw["status"] == "queued"
            assert kw["idempotency_key"] == f"btg:{data['grid_id']}:{index}"
            assert kw["payload"]["grid"] == {
                "grid_id": data["grid_id"],
                "combo_index": index,
            }
            assert kw["payload"]["request"]["strategy"] == "ma_cross"
            # base params 默认值与覆盖合并(validated 全量参数)
            assert kw["payload"]["request"]["params"]["long_window"] == 20
            assert kw["payload"]["request"]["params"]["short_window"] in (5, 10)
            assert kw["payload"]["provider_name"] == app.settings.data_provider
        # 网格行落库且 combos 回填 job_id
        session = _get_session(app)
        grid_row = session.add.call_args.args[0]
        assert isinstance(grid_row, BacktestGridRunModel)
        assert grid_row.combo_count == 2
        assert all(combo["job_id"] for combo in grid_row.combos)

    async def test_cartesian_grid_expands_product(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        _no_existing_grid(monkeypatch)
        calls: list[dict[str, Any]] = []

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(job_id=f"BJ-{len(calls)}"), True

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                params_grid={
                    "short_window": [5, 10],
                    "long_window": [20, 30],
                },
            ),
        )
        assert env.status == "ok"
        assert env.data["combo_count"] == 4
        combos = sorted(
            (
                kw["payload"]["request"]["params"]["short_window"],
                kw["payload"]["request"]["params"]["long_window"],
            )
            for kw in calls
        )
        assert combos == [
            (5, 20),
            (5, 30),
            (10, 20),
            (10, 30),
        ]
        # label 是覆盖参数的确定性 JSON
        labels = [job["label"] for job in env.data["jobs"]]
        assert labels[0] == '{"long_window": 20, "short_window": 5}'

    async def test_base_params_merged_with_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        _no_existing_grid(monkeypatch)
        calls: list[dict[str, Any]] = []

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(job_id=f"BJ-{len(calls)}"), True

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params={"max_position_pct": 0.9},
                params_list=[{"short_window": 8}],
            ),
        )
        assert env.status == "ok"
        request_params = calls[0]["payload"]["request"]["params"]
        assert request_params["max_position_pct"] == 0.9
        assert request_params["short_window"] == 8
        assert request_params["long_window"] == 20

    async def test_idempotent_resubmit_returns_existing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        existing = _grid_row()
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_idempotency_key",
            lambda self, key: _async_return(existing),
        )
        calls: list[dict[str, Any]] = []

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(job_id="BJ-x"), True

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        # checksum 对齐(组合定义一致)
        monkeypatch.setattr(grid_tools, "_combos_checksum", lambda combos: "abc")
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())
        assert env.status == "ok"
        assert env.data["created"] is False
        assert env.data["grid_id"] == existing.grid_id
        assert env.data["jobs"][0]["job_id"] == "BJ-1"
        assert calls == []  # 幂等命中,不再入队

    async def test_idempotent_checksum_mismatch_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        existing = _grid_row(combos_checksum="old-checksum")
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_idempotency_key",
            lambda self, key: _async_return(existing),
        )
        monkeypatch.setattr(grid_tools, "_combos_checksum", lambda combos: "new")
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_enqueue_conflict_rolls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        _no_existing_grid(monkeypatch)

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            raise RuntimeError("unique violation")

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"
        session = _get_session(app)
        session.rollback.assert_awaited_once()

    async def test_records_audit_and_idempotency_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        _no_existing_grid(monkeypatch)

        async def fake_create_or_get(self: Any, **kw: Any) -> Any:
            return SimpleNamespace(job_id="BJ-1"), True

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)
        env = await grid_tools.backtest_grid_submit(app, **_submit_kwargs())
        assert env.idempotency_key == "grid-key-1"
        assert app.audit.records[0].tool_name == "finboard.backtest.grid_submit"
        assert app.audit.records[0].status == "ok"


# ---------------------------------------------------------------------------
# finboard.backtest.grid_submit —— selection_grid 选股维度展开(issue #259)
# ---------------------------------------------------------------------------


def _patch_enqueue(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
) -> None:
    """幂等查重为空 + 捕获逐 job 入队 payload。"""

    _no_existing_grid(monkeypatch)

    async def fake_create_or_get(self: Any, **kw: Any) -> Any:
        calls.append(kw)
        return SimpleNamespace(job_id=f"BJ-{len(calls)}"), True

    monkeypatch.setattr(BackgroundJobRepository, "create_or_get", fake_create_or_get)


class TestSelectionGrid:
    async def test_selection_grid_alone_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """selection_grid 单独使用(纯选股扫描,如因子 x 窗口),params 全部为基础值。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                selection_grid={
                    "ranking_factor": ["momentum", "market_cap"],
                    "momentum_lookback": [20, 60],
                },
            ),
        )
        assert env.status == "ok"
        assert env.data["combo_count"] == 4  # 2 因子 x 2 窗口笛卡尔积
        assert len(calls) == 4
        selection_pairs = sorted(
            (
                kw["payload"]["request"]["selection"]["ranking_factor"],
                kw["payload"]["request"]["selection"]["momentum_lookback"],
            )
            for kw in calls
        )
        assert selection_pairs == [
            ("market_cap", 20),
            ("market_cap", 60),
            ("momentum", 20),
            ("momentum", 60),
        ]
        # 未提供 params_list/params_grid → 逐组合 params 都是基础参数(long_window=20 默认)
        assert all(
            kw["payload"]["request"]["params"]["long_window"] == 20 for kw in calls
        )

    async def test_selection_grid_cartesian_with_params_grid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """selection_grid 与 params_grid 做笛卡尔积(2 x 2 = 4)。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                params_grid={"short_window": [5, 10]},
                selection_grid={"ranking_factor": ["momentum", "pb"]},
            ),
        )
        assert env.status == "ok"
        assert env.data["combo_count"] == 4
        combos = sorted(
            (
                kw["payload"]["request"]["params"]["short_window"],
                kw["payload"]["request"]["selection"]["ranking_factor"],
            )
            for kw in calls
        )
        assert combos == [
            (5, "momentum"),
            (5, "pb"),
            (10, "momentum"),
            (10, "pb"),
        ]
        # label 为结构化差异:{"params": ..., "selection": ...}(空 params 侧省略)
        labels = sorted(job["label"] for job in env.data["jobs"])
        assert labels == sorted(
            [
                '{"params": {"short_window": 5}, "selection": {"ranking_factor": "momentum"}}',
                '{"params": {"short_window": 5}, "selection": {"ranking_factor": "pb"}}',
                '{"params": {"short_window": 10}, "selection": {"ranking_factor": "momentum"}}',
                '{"params": {"short_window": 10}, "selection": {"ranking_factor": "pb"}}',
            ]
        )
        # 提交回执 jobs 与落库 combos 均携带组合级 selection(校验后全量)
        assert all("selection" in job for job in env.data["jobs"])
        session = _get_session(app)
        grid_row = session.add.call_args.args[0]
        assert all("selection" in combo for combo in grid_row.combos)
        # 网格级 selection 列仍是基础 selection(None → 默认全量 dump)
        assert grid_row.selection["ranking_factor"] == "market_cap"

    async def test_selection_grid_with_params_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """params_list(显式列表)与 selection_grid 做笛卡尔积。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=[{"short_window": 5}, {"short_window": 10}],
                selection_grid={"max_symbols": [10, 30]},
            ),
        )
        assert env.status == "ok"
        assert env.data["combo_count"] == 4
        combos = sorted(
            (
                kw["payload"]["request"]["params"]["short_window"],
                kw["payload"]["request"]["selection"]["max_symbols"],
            )
            for kw in calls
        )
        assert combos == [(5, 10), (5, 30), (10, 10), (10, 30)]

    async def test_selection_grid_invalid_field_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知 selection 字段(extra=forbid)→ 具名 invalid_argument,不落库不入队。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                selection_grid={"bogus_factor": ["momentum"]},
            ),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        session = _get_session(app)
        session.add.assert_not_called()
        assert calls == []

    async def test_selection_grid_invalid_value_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非法 selection 值(momentum_lookback=0 违反 gt=0)→ invalid_argument。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                selection_grid={"momentum_lookback": [0]},
            ),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        session = _get_session(app)
        session.add.assert_not_called()
        assert calls == []

    async def test_base_selection_merged_into_combo_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """组合级 selection = 网格级基础 selection + 覆盖,全量进 payload。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                selection={"enabled": True, "source": "akshare"},
                selection_grid={"ranking_factor": ["momentum"]},
            ),
        )
        assert env.status == "ok"
        selection = calls[0]["payload"]["request"]["selection"]
        assert selection["enabled"] is True
        assert selection["source"] == "akshare"
        assert selection["ranking_factor"] == "momentum"

    async def test_selection_grid_product_over_max_combos_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 params x 2 selection = 6 > max_combos=5 → invalid_argument(列上限)。"""

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                params_grid={"short_window": [5, 10, 15]},
                selection_grid={"ranking_factor": ["momentum", "pb"]},
                max_combos=5,
            ),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        session = _get_session(app)
        session.add.assert_not_called()
        assert calls == []

    async def test_selection_grid_empty_or_bad_shape_rejected(self) -> None:
        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app, **_submit_kwargs(params_list=None, selection_grid={})
        )
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                selection_grid={"ranking_factor": []},
            ),
        )
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_no_combo_dimension_rejected(self) -> None:
        """params_list / params_grid / selection_grid 全部缺省 → invalid_argument。"""

        app = _make_app()
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(params_list=None, params_grid=None, selection_grid=None),
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_without_selection_grid_label_and_payload_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 selection_grid 时行为与历史完全一致:label 扁平 JSON、
        combos 不携带 selection 键(checksum 不漂移,幂等重提交仍命中)。"""

        combos = grid_tools._expand_combos(
            params_list=None,
            params_grid={"short_window": [5]},
            selection_grid=None,
        )
        assert combos[0]["label"] == '{"short_window": 5}'
        assert combos[0]["selection"] == {}

        app = _make_app()
        calls: list[dict[str, Any]] = []
        _patch_enqueue(monkeypatch, calls)
        env = await grid_tools.backtest_grid_submit(
            app,
            **_submit_kwargs(
                params_list=None,
                params_grid={"short_window": [5]},
            ),
        )
        assert env.status == "ok"
        assert env.data["jobs"][0]["label"] == '{"short_window": 5}'
        assert "selection" not in env.data["jobs"][0]
        session = _get_session(app)
        grid_row = session.add.call_args.args[0]
        assert all("selection" not in combo for combo in grid_row.combos)
        # payload selection 仍是网格级基础 selection
        assert calls[0]["payload"]["request"]["selection"]["ranking_factor"] == "market_cap"

    async def test_checksum_stable_for_legacy_combos(self) -> None:
        """旧网格组合(无 selection 键)的 checksum 与引入 #259 前的公式一致。"""

        import hashlib
        import json as json_module

        legacy = [
            {"index": 0, "label": "{}", "params": {"short_window": 5}, "job_id": "BJ-1"},
            {"index": 1, "label": "{}", "params": {"short_window": 10}, "job_id": "BJ-2"},
        ]
        expected = hashlib.sha256(
            json_module.dumps(
                [
                    {"index": c["index"], "label": c["label"], "params": c["params"]}
                    for c in legacy
                ],
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        assert grid_tools._combos_checksum(legacy) == expected


# ---------------------------------------------------------------------------
# finboard.backtest.grid_get —— 聚合
# ---------------------------------------------------------------------------


class TestGridGet:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(None),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id="BTG-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_header_includes_base_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #259:基础 selection 在网格头部出现一次,组合差异由 label 承载。"""

        app = _make_app()
        grid_row = _grid_row(
            combos=[
                {
                    "index": 0,
                    "label": '{"selection": {"ranking_factor": "momentum"}}',
                    "params": {"short_window": 5},
                    "selection": {"enabled": False, "ranking_factor": "momentum"},
                    "job_id": "BJ-1",
                }
            ]
        )
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(_job_row(job_id, status="queued", result_ref=None)),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id)
        assert env.status == "ok"
        assert env.data["selection"] == {"enabled": False}
        combo = env.data["combos"][0]
        # #206 精神:combo 保持精简,selection 差异由 label 承载
        assert "selection" not in combo
        assert "selection" in combo["label"]

    async def test_aggregates_succeeded_failed_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        grid_row = _grid_row(
            combos=[
                {"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"},
                {"index": 1, "label": "b", "params": {}, "job_id": "BJ-2"},
                {"index": 2, "label": "c", "params": {}, "job_id": "BJ-3"},
            ]
        )
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        jobs = {
            "BJ-1": _job_row("BJ-1", result_ref="10"),
            "BJ-2": _job_row(
                "BJ-2",
                status="failed",
                result_ref=None,
                error_code="data_error",
                error_summary="数据源连接失败",
            ),
            "BJ-3": _job_row("BJ-3", status="queued", result_ref=None),
        }
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(jobs.get(job_id)),
        )
        from finboard_persistence import BacktestRunRepository

        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(_run_row(run_id)),
        )
        env = await grid_tools.backtest_grid_get(
            app, grid_id=grid_row.grid_id, equity_mode="summary"
        )
        assert env.status == "ok"
        data = env.data
        assert data["complete"] is False
        assert data["completed_count"] == 2  # succeeded + failed 都是终态
        assert data["pending_count"] == 1
        assert data["failed_count"] == 1
        assert len(data["combos"]) == 2  # 成功 + 未终态
        succeeded = data["combos"][0]
        assert succeeded["run_id"] == 10
        assert succeeded["metrics"]["total_return"] == 0.1
        assert "equity_curve" in succeeded  # 显式 equity_mode=summary 才返回曲线
        assert "rank" in succeeded
        assert "best" in succeeded
        assert len(data["failures"]) == 1
        failure = data["failures"][0]
        assert failure["combo_index"] == 1
        assert failure["error_code"] == "data_error"
        assert failure["error_summary"] == "数据源连接失败"

    async def test_complete_when_all_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        grid_row = _grid_row(
            combos=[
                {"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"},
                {"index": 1, "label": "b", "params": {}, "job_id": "BJ-2"},
            ]
        )
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        jobs = {
            "BJ-1": _job_row("BJ-1", result_ref="1"),
            "BJ-2": _job_row("BJ-2", status="cancelled", result_ref=None),
        }
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(jobs.get(job_id)),
        )
        from finboard_persistence import BacktestRunRepository

        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(_run_row(run_id)),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id)
        assert env.status == "ok"
        assert env.data["complete"] is True
        assert env.data["pending_count"] == 0
        assert env.data["failed_count"] == 1  # cancelled 组合单列

    async def test_ranking_and_best_annotation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        grid_row = _grid_row(
            combos=[
                {"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"},
                {"index": 1, "label": "b", "params": {}, "job_id": "BJ-2"},
                {"index": 2, "label": "c", "params": {}, "job_id": "BJ-3"},
            ]
        )
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        metrics_list = [
            {"total_return": 0.05, "max_drawdown": -0.08},
            {"total_return": 0.20, "max_drawdown": -0.05},
            {"total_return": 0.20, "max_drawdown": -0.15},
        ]
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(
                _job_row(job_id, result_ref=str({"BJ-1": 1, "BJ-2": 2, "BJ-3": 3}[job_id]))
            ),
        )
        from finboard_persistence import BacktestRunRepository

        def fake_run_get(self: Any, run_id: int) -> Any:
            return _async_return(_run_row(run_id, metrics=metrics_list[run_id - 1]))

        monkeypatch.setattr(BacktestRunRepository, "get", fake_run_get)
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id)
        assert env.status == "ok"
        data = env.data
        # total_return:0.20 并列第 1(组合 1/2),0.05 第 3(组合 0)
        by_index = {row["combo_index"]: row for row in data["combos"]}
        assert by_index[1]["rank"]["total_return"] == 1
        assert by_index[2]["rank"]["total_return"] == 1
        assert by_index[0]["rank"]["total_return"] == 3
        assert by_index[1]["best"]["total_return"] is True
        assert by_index[0]["best"]["total_return"] is False
        # 最优标注:max_drawdown 越高越好(-0.05 > -0.08 > -0.15)
        assert data["ranking"]["total_return"]["combo_index"] in (1, 2)
        assert data["ranking"]["max_drawdown"]["combo_index"] == 1
        # 指标矩阵列按规范顺序出现
        assert data["metric_fields"][0] == "total_return"

    async def test_run_missing_surfaces_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        grid_row = _grid_row(combos=[{"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"}])
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(_job_row(job_id, result_ref="999")),
        )
        from finboard_persistence import BacktestRunRepository

        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(None),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id)
        assert env.status == "ok"
        assert env.data["combos"] == []
        assert env.data["failures"][0]["error_code"] == "run_missing"

    async def test_equity_summary_downsampled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        grid_row = _grid_row(combos=[{"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"}])
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(_job_row(job_id, result_ref="1")),
        )
        from datetime import timedelta

        from finboard_persistence import BacktestRunRepository

        start_day = datetime(2024, 1, 1, tzinfo=UTC)
        long_equity = [
            {
                "date": (start_day + timedelta(days=i)).date().isoformat(),
                "equity": 100000.0 + i,
            }
            for i in range(500)
        ]
        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(
                SimpleNamespace(
                    id=run_id,
                    metrics={"total_return": 0.1},
                    equity_curve=long_equity,
                )
            ),
        )
        env = await grid_tools.backtest_grid_get(
            app, grid_id=grid_row.grid_id, equity_mode="summary", max_points=50
        )
        assert env.status == "ok"
        combo = env.data["combos"][0]
        assert combo["equity_point_count"] == 500  # 落库全量
        assert len(combo["equity_curve"]) <= 50  # 返回降采样
        assert combo["equity_curve"][0]["date"] == "2024-01-01"
        assert combo["equity_curve"][-1]["date"] == long_equity[-1]["date"]  # 末点保留

    async def test_equity_full_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        grid_row = _grid_row(combos=[{"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"}])
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(_job_row(job_id, result_ref="1")),
        )
        from finboard_persistence import BacktestRunRepository

        long_equity = [{"date": f"2024-{i:02d}-01", "equity": 100000.0 + i} for i in range(1, 501)]
        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(
                SimpleNamespace(
                    id=run_id,
                    metrics={"total_return": 0.1},
                    equity_curve=long_equity,
                )
            ),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id, equity_mode="full")
        assert env.status == "ok"
        assert len(env.data["combos"][0]["equity_curve"]) == 500

    async def test_default_no_equity_curve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """issue #190:grid_get 默认(none)不返回曲线,只留点数提示,响应最轻。"""
        app = _make_app()
        grid_row = _grid_row(combos=[{"index": 0, "label": "a", "params": {}, "job_id": "BJ-1"}])
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(grid_row),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, job_id: _async_return(_job_row(job_id, result_ref="1")),
        )
        from finboard_persistence import BacktestRunRepository

        long_equity = [{"date": f"2024-{i:02d}-01", "equity": 100000.0 + i} for i in range(1, 501)]
        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(
                SimpleNamespace(
                    id=run_id,
                    metrics={"total_return": 0.1},
                    equity_curve=long_equity,
                )
            ),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id=grid_row.grid_id)
        assert env.status == "ok"
        combo = env.data["combos"][0]
        assert combo["metrics"]["total_return"] == 0.1
        assert "equity_curve" not in combo  # 默认不返回曲线
        assert combo["equity_point_count"] == 500  # 仅保留点数提示
        assert "rank" in combo  # 指标矩阵 + 排名仍返回

    async def test_invalid_equity_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BacktestGridRunRepository,
            "get_by_grid_id",
            lambda self, grid_id: _async_return(_grid_row()),
        )
        env = await grid_tools.backtest_grid_get(app, grid_id="BTG-1", equity_mode="bogus")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
