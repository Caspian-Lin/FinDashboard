"""``finboard.validation_experiment.*`` 工具 ——
#57 机器验证实验工具测试(mock session / monkeypatch,无需 DB)。

覆盖:

* 只读查询(list / get)—— monkeypatch repository 返回真实领域对象;
* 写操作(create / reject / add_trial / delete)—— 验证
  ``write_tools_enabled=False`` 时拒绝(permission_denied);
* 参数校验(plan 缺字段 / 非法状态 / 空 reason / 短 hypothesis);
* 状态机约束(REST 409 语义 → ``conflict``:终态实验拒绝 / 预算耗尽)。
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ResearchExperiment,
    RobustnessPlan,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import validation_experiments as ve_tools

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    return value


def _experiment(status: str = "hypothesis") -> ResearchExperiment:
    """构造真实 ResearchExperiment 领域对象(不落库)。"""
    experiment = new_experiment(
        hypothesis="均线交叉在宽基 ETF 上有 alpha 的样本外验证",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version="v1",
            dataset_versions={"daily_metrics": "2024-01-01"},
            selection_config={"enabled": False},
            strategy_kind="ma_cross",
        ),
        plan=ValidationPlan(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2020, 12, 31),
            validation_start=date(2021, 1, 1),
            validation_end=date(2021, 6, 30),
            test_start=date(2021, 7, 1),
            test_end=date(2021, 12, 31),
            train_window_days=252,
            test_window_days=63,
            step_days=21,
            trial_budget=20,
        ),
        thresholds=AcceptanceThresholds(),
        robustness=RobustnessPlan(),
        strategy_params_space={"window": [5, 10, 20]},
        notes="测试",
    )
    if status == "in_sample":
        experiment = transition_status(experiment, ExperimentStatus.IN_SAMPLE)
    elif status == "validated_oos":
        experiment = transition_status(experiment, ExperimentStatus.IN_SAMPLE)
        experiment = mark_final_test_unsealed(experiment)
        experiment = transition_status(
            experiment, ExperimentStatus.VALIDATED_OOS
        )
    elif status == "rejected":
        experiment = transition_status(
            experiment,
            ExperimentStatus.REJECTED,
            rejection_reason="样本内失败",
        )
    return experiment


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hypothesis": "均线交叉在宽基 ETF 上有 alpha 的样本外验证",
        "version_stamp": {
            "matching_model_version": "v2",
            "asset_rules_version": "v1",
            "factor_version": "v1",
            "dataset_versions": {"daily_metrics": "2024-01-01"},
            "selection_config": {"enabled": False},
            "strategy_kind": "ma_cross",
        },
        "plan": {
            "mode": "rolling",
            "train_start": "2020-01-01",
            "train_end": "2020-12-31",
            "validation_start": "2021-01-01",
            "validation_end": "2021-06-30",
            "test_start": "2021-07-01",
            "test_end": "2021-12-31",
            "train_window_days": 252,
            "test_window_days": 63,
            "step_days": 21,
            "trial_budget": 20,
            "random_seed": 0,
            "benchmark_symbol": None,
        },
        # thresholds / robustness 不传 → 用领域默认值
        "strategy_params_space": {"window": [5, 10, 20]},
        "supersedes_id": None,
        "notes": "MCP 创建测试",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# finboard.validation_experiment.list(只读)
# ---------------------------------------------------------------------------


class TestValidationExperimentList:
    async def test_returns_experiments(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "list_by_status",
            lambda self, status, **kw: _async_return([_experiment()]),
        )
        monkeypatch.setattr(
            ResearchTrialRepository,
            "map_by_experiment",
            lambda self, ids: _async_return({}),
        )
        env = await ve_tools.validation_experiment_list(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert env.data[0]["experiment_id"]
        assert env.data[0]["status"] == "hypothesis"
        assert env.data[0]["version_checksum"]
        # issue #310:list 附带派生 oos_outcome(无 OOS 证据 → inconclusive)
        assert env.data[0]["oos_outcome"] == "inconclusive"

    async def test_status_filter_passthrough(self, monkeypatch: Any) -> None:
        app = _make_app()
        captured: dict[str, Any] = {}

        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        async def _fake_list(self: Any, status: Any, **kw: Any) -> list[Any]:
            captured["status"] = status
            return []

        monkeypatch.setattr(
            ResearchExperimentRepository, "list_by_status", _fake_list
        )
        env = await ve_tools.validation_experiment_list(
            app, status="validated_oos"
        )
        assert env.status == "ok"
        assert captured["status"] is ExperimentStatus.VALIDATED_OOS

    async def test_invalid_status_rejected(self) -> None:
        app = _make_app()
        env = await ve_tools.validation_experiment_list(app, status="nope")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_read_allowed_in_readonly_mode(self, monkeypatch: Any) -> None:
        app = _make_app(write_enabled=False)
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "list_by_status",
            lambda self, status, **kw: _async_return([]),
        )
        env = await ve_tools.validation_experiment_list(app)
        assert env.status == "ok"

    async def test_records_audit(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "list_by_status",
            lambda self, status, **kw: _async_return([]),
        )
        await ve_tools.validation_experiment_list(app)
        assert app.audit.records[0].tool_name == (
            "finboard.validation_experiment.list"
        )
        assert app.audit.records[0].status == "ok"


# ---------------------------------------------------------------------------
# finboard.validation_experiment.get(只读)
# ---------------------------------------------------------------------------


class TestValidationExperimentGet:
    async def test_returns_detail_with_trials(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        exp = _experiment()
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        monkeypatch.setattr(
            ResearchTrialRepository,
            "list_by_experiment",
            lambda self, eid, **kw: _async_return([]),
        )
        env = await ve_tools.validation_experiment_get(app, exp.experiment_id)
        assert env.status == "ok"
        assert env.data["experiment_id"] == exp.experiment_id
        assert env.data["trials"] == []
        # issue #310:get 附带派生 oos_outcome(无 OOS 证据 → inconclusive)
        assert env.data["oos_outcome"] == "inconclusive"

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(None),
        )
        env = await ve_tools.validation_experiment_get(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_read_allowed_in_readonly_mode(self, monkeypatch: Any) -> None:
        app = _make_app(write_enabled=False)
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(_experiment()),
        )
        monkeypatch.setattr(
            ResearchTrialRepository,
            "list_by_experiment",
            lambda self, eid, **kw: _async_return([]),
        )
        env = await ve_tools.validation_experiment_get(app, "EXP-1")
        assert env.status == "ok"


# ---------------------------------------------------------------------------
# finboard.validation_experiment.create(写)
# ---------------------------------------------------------------------------


class TestValidationExperimentCreate:
    async def test_creates_with_defaults(self, monkeypatch: Any) -> None:
        """thresholds / robustness 不传 → 领域默认值。"""

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        save_mock = AsyncMock()
        monkeypatch.setattr(ResearchExperimentRepository, "save", save_mock)
        env = await ve_tools.validation_experiment_create(
            app, **_create_payload()
        )
        assert env.status == "ok"
        assert env.data["status"] == "hypothesis"
        assert env.data["trials_used"] == 0
        assert env.data["final_test_unsealed"] is False
        assert env.data["plan"]["trial_budget"] == 20
        assert env.data["thresholds"]["min_in_sample_sharpe"] == 1.0
        assert env.data["robustness"]["neighbourhood_steps"] == 5
        assert env.data["strategy_params_space"] == {"window": [5, 10, 20]}
        save_mock.assert_awaited_once()
        # session.commit 被调用(落库)
        session = app.session_maker.return_value.__aenter__.return_value  # type: ignore[attr-defined]
        session.commit.assert_awaited_once()

    async def test_creates_with_explicit_thresholds(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository, "save", AsyncMock()
        )
        payload = _create_payload(
            thresholds={"min_in_sample_sharpe": 2.0, "min_oos_sharpe": 1.0},
            robustness={"neighbourhood_steps": 3, "stress_phases": ["2020-Q1"]},
        )
        env = await ve_tools.validation_experiment_create(app, **payload)
        assert env.status == "ok"
        assert env.data["thresholds"]["min_in_sample_sharpe"] == 2.0
        assert env.data["thresholds"]["min_oos_sharpe"] == 1.0
        assert env.data["robustness"]["neighbourhood_steps"] == 3
        assert env.data["robustness"]["stress_phases"] == ["2020-Q1"]

    async def test_short_hypothesis_rejected(self) -> None:
        app = _make_app()
        payload = _create_payload(hypothesis="太短")
        env = await ve_tools.validation_experiment_create(app, **payload)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_plan_missing_field_rejected(self) -> None:
        app = _make_app()
        payload = _create_payload()
        del payload["plan"]["train_end"]
        env = await ve_tools.validation_experiment_create(app, **payload)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_plan_bad_dates_rejected(self) -> None:
        app = _make_app()
        payload = _create_payload()
        payload["plan"]["train_end"] = "not-a-date"
        env = await ve_tools.validation_experiment_create(app, **payload)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_version_stamp_missing_field_rejected(self) -> None:
        app = _make_app()
        payload = _create_payload()
        del payload["version_stamp"]["strategy_kind"]
        env = await ve_tools.validation_experiment_create(app, **payload)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await ve_tools.validation_experiment_create(
            app, **_create_payload()
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_records_audit(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository, "save", AsyncMock()
        )
        await ve_tools.validation_experiment_create(app, **_create_payload())
        assert app.audit.records[0].tool_name == (
            "finboard.validation_experiment.create"
        )
        assert app.audit.records[0].status == "ok"


# ---------------------------------------------------------------------------
# finboard.validation_experiment.reject(写)
# ---------------------------------------------------------------------------


class TestValidationExperimentReject:
    async def test_rejects_hypothesis(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        exp = _experiment()
        save_mock = AsyncMock()
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        monkeypatch.setattr(ResearchExperimentRepository, "save", save_mock)
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id=exp.experiment_id, reason="样本内不达标"
        )
        assert env.status == "ok"
        assert env.data["status"] == "rejected"
        assert env.data["rejection_reason"] == "样本内不达标"
        save_mock.assert_awaited_once()

    async def test_reject_validated_oos_conflict(self, monkeypatch: Any) -> None:
        """终态实验不可拒绝(REST 409 语义 → conflict)。"""

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        exp = _experiment("validated_oos")
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id=exp.experiment_id, reason="事后反悔"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_reject_superseded_conflict(self, monkeypatch: Any) -> None:
        from dataclasses import replace

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        exp = replace(_experiment(), status=ExperimentStatus.SUPERSEDED)
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id=exp.experiment_id, reason="x"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(None),
        )
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id="missing", reason="x"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_empty_reason_rejected(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(_experiment()),
        )
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id="EXP-1", reason="   "
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await ve_tools.validation_experiment_reject(
            app, experiment_id="EXP-1", reason="x"
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# finboard.validation_experiment.add_trial(写)
# ---------------------------------------------------------------------------


class TestValidationExperimentAddTrial:
    async def test_adds_trial_and_increments(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        exp = _experiment()
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        monkeypatch.setattr(
            ResearchTrialRepository,
            "list_by_experiment",
            lambda self, eid, **kw: _async_return([]),
        )
        exp_save = AsyncMock()
        trial_save = AsyncMock()
        monkeypatch.setattr(ResearchExperimentRepository, "save", exp_save)
        monkeypatch.setattr(ResearchTrialRepository, "save", trial_save)

        env = await ve_tools.validation_experiment_add_trial(
            app,
            experiment_id=exp.experiment_id,
            parameters={"window": 20, "threshold": 0.02},
        )
        assert env.status == "ok"
        assert env.data["experiment_id"] == exp.experiment_id
        assert env.data["trial_index"] == 0
        assert env.data["status"] == "candidate"
        assert env.data["trial_id"].startswith(f"{exp.experiment_id}-mcp-")
        assert env.data["parameters"] == {"window": 20, "threshold": 0.02}
        trial_save.assert_awaited_once()
        exp_save.assert_awaited_once()
        # 落库的 experiment trials_used 递增
        assert exp_save.await_args is not None
        saved_exp = exp_save.await_args.args[0]
        assert saved_exp.trials_used == 1

    async def test_explicit_status_and_failure_reason(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(_experiment()),
        )
        monkeypatch.setattr(
            ResearchTrialRepository,
            "list_by_experiment",
            lambda self, eid, **kw: _async_return([]),
        )
        monkeypatch.setattr(
            ResearchExperimentRepository, "save", AsyncMock()
        )
        monkeypatch.setattr(ResearchTrialRepository, "save", AsyncMock())
        env = await ve_tools.validation_experiment_add_trial(
            app,
            experiment_id="EXP-1",
            parameters={},
            status="failed",
            failure_reason="数据缺失",
        )
        assert env.status == "ok"
        assert env.data["status"] == "failed"
        assert env.data["failure_reason"] == "数据缺失"

    async def test_budget_exhausted_conflict(self, monkeypatch: Any) -> None:
        """预算耗尽不可再登记(REST 409 语义 → conflict)。"""

        from dataclasses import replace

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        exp = replace(
            _experiment(), trials_used=20  # trial_budget=20 → 已耗尽
        )
        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(exp),
        )
        env = await ve_tools.validation_experiment_add_trial(
            app, experiment_id=exp.experiment_id, parameters={}
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_terminal_status_conflict(self, monkeypatch: Any) -> None:
        """终态实验不可再登记 trial → conflict。"""

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(_experiment("rejected")),
        )
        env = await ve_tools.validation_experiment_add_trial(
            app, experiment_id="EXP-1", parameters={}
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_invalid_trial_status_rejected(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(_experiment()),
        )
        env = await ve_tools.validation_experiment_add_trial(
            app, experiment_id="EXP-1", parameters={}, status="nope"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "get",
            lambda self, eid: _async_return(None),
        )
        env = await ve_tools.validation_experiment_add_trial(
            app, experiment_id="missing", parameters={}
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await ve_tools.validation_experiment_add_trial(
            app, experiment_id="EXP-1", parameters={}
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# finboard.validation_experiment.delete(写)
# ---------------------------------------------------------------------------


class TestValidationExperimentDelete:
    async def test_deletes(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "delete",
            lambda self, eid: _async_return(True),
        )
        env = await ve_tools.validation_experiment_delete(app, "EXP-1")
        assert env.status == "ok"
        assert env.data == {"deleted": True, "experiment_id": "EXP-1"}

    async def test_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        monkeypatch.setattr(
            ResearchExperimentRepository,
            "delete",
            lambda self, eid: _async_return(False),
        )
        env = await ve_tools.validation_experiment_delete(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_referenced_by_factor_experiment_conflict(
        self, monkeypatch: Any
    ) -> None:
        """因子实验外键仍引用时,DB IntegrityError → conflict。"""

        from sqlalchemy.exc import IntegrityError

        app = _make_app()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        def _raise_fk(self: Any, eid: str) -> Any:
            raise IntegrityError(
                "delete",
                {},
                Exception(
                    'update or delete on table "research_experiments" violates '
                    'foreign key constraint "factor_experiments_'
                    'validation_experiment_id_fkey"'
                ),
            )

        monkeypatch.setattr(ResearchExperimentRepository, "delete", _raise_fk)
        env = await ve_tools.validation_experiment_delete(app, "EXP-1")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"
        # 回滚被调用,不留脏事务
        session = app.session_maker.return_value.__aenter__.return_value  # type: ignore[attr-defined]
        session.rollback.assert_awaited_once()

    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await ve_tools.validation_experiment_delete(app, "EXP-1")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"
