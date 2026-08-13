"""""#138 端到端:验证实验 + 因子实验联动(MCP 工具,需要 DB)。

链路:finboard_validation_experiment_create(冻结假设 + 计划 + 门)→
finboard_validation_experiment_add_trial(登记 trial)→ 领域函数推进到
VALIDATED_OOS(揭盲端点不在 #138 范围,由 runner/领域层完成)→
finboard_factor_experiment_create(validation_experiment_id 引用)→
finboard_factor_experiment_sync_validation 同步终态 → validated_oos。

同时验证:MCP 工具与 REST/领域共用同一张表(MCP session 能读到
db_session 提交的数据);只读工具(list/get)与写工具在真实 DB 上往返一致。
"""""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_backtest.validation.contracts import (
    ExperimentStatus,
    StatisticalReport,
    TrialStatus,
    WindowMetrics,
    WindowRole,
    mark_final_test_unsealed,
    transition_status,
)
from finboard_data.cache import ParquetCache
from finboard_data.factor_lab import (
    FeatureObservation,
    build_feature_snapshot,
)
from finboard_data.releases import DatasetReleaseSpec
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import factors as factor_tools
from finboard_mcp.tools import validation_experiments as ve_tools
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchDatasetReleaseService,
    ResearchExperimentRepository,
    ResearchTrialRepository,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_SYMBOLS = ("510300.SH", "513100.SH", "518880.SH", "511010.SH")
_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_DECISION = datetime(2024, 1, 5, 8, tzinfo=UTC)
_RELEASE_ID = f"integration-mcp-validation-138-{uuid4().hex[:8]}"
_RELEASE_VERSION = f"e2e-138-{uuid4().hex[:8]}"


async def _publish_release(session: AsyncSession, tmp_path: Path) -> Any:
    cache = ParquetCache(tmp_path / "cache")
    for symbol_index, code in enumerate(_SYMBOLS):
        symbol = Symbol(code, Market.A_SHARE)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=index),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=Decimal(str(3 + symbol_index + index * 0.01)),
                high=Decimal(str(3.1 + symbol_index + index * 0.01)),
                low=Decimal(str(2.9 + symbol_index + index * 0.01)),
                close=Decimal(str(3 + symbol_index + index * 0.01)),
                volume=Decimal("1000000"),
                amount=Decimal("3000000"),
                source="fixed_sample",
            )
            for index in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    service = ResearchDatasetReleaseService(
        session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    return await service.publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="mcp_validation_e2e",
            source="fixed_sample",
            version=_RELEASE_VERSION,
            start_date=_START,
            end_date=_END,
            code_version="integration",
            required_capabilities=(
                "etf:index",
                "etf:cross_border",
                "etf:commodity",
                "etf:bond",
            ),
        ),
        list(_SYMBOLS),
    )


def _make_app(_engine: AsyncEngine) -> McpAppContext:
    provider = FakeLLMProvider()
    from finboard_app.config import Settings
    return McpAppContext(
        settings=Settings(),
        session_maker=session_factory(_engine),
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=_engine,
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


async def test_validation_experiment_mcp_full_chain(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    # 1) 准备冻结数据发布 + 特征快照(MCP 工具只消费这些研究产物)
    release = await _publish_release(db_session, tmp_path)
    snapshot = build_feature_snapshot(
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        decision_at=_DECISION,
        code_version="integration",
        observations=[
            FeatureObservation(
                symbol=code,
                feature_name="momentum",
                value=float(index - 1),
                observed_at=_DECISION - timedelta(minutes=30),
                available_at=_DECISION - timedelta(minutes=30),
                source="fixed_sample",
                source_version=release.version,
                market="a_share",
                asset_class=release.instrument(code).asset_class.value,
            )
            for index, code in enumerate(_SYMBOLS)
        ],
        calculation_windows={"momentum": 20},
    )
    await FeatureSnapshotRepository(db_session).publish(snapshot)
    # MCP 工具使用独立 session,必须先提交让跨 session 可见
    await db_session.commit()

    app = _make_app(_engine)

    # 2) 通过 MCP 创建 #57 验证实验(假设 + 计划 + 门一次性冻结)
    plan = {
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
        "trial_budget": 10,
        "random_seed": 0,
        "benchmark_symbol": "510300.SH",
    }
    create_env = await ve_tools.validation_experiment_create(
        app,
        hypothesis="ETF 动量策略通过冻结 OOS 与成本检验",
        version_stamp={
            "matching_model_version": "v2",
            "asset_rules_version": "v1",
            "factor_version": "v2",
            "dataset_versions": {"research_dataset_release": release.release_id},
            "selection_config": {"candidate_universe_version": "universe-v1"},
            "strategy_kind": "factor_lab",
        },
        plan=plan,
        strategy_params_space={"window": [5, 10, 20]},
        notes="MCP e2e",
    )
    assert create_env.status == "ok", create_env.error
    experiment_id = create_env.data["experiment_id"]
    assert create_env.data["status"] == "hypothesis"
    assert create_env.data["plan"]["trial_budget"] == 10

    # 3) 只读工具能在真实 DB 上读到刚创建的实验
    get_env = await ve_tools.validation_experiment_get(app, experiment_id)
    assert get_env.status == "ok"
    assert get_env.data["experiment_id"] == experiment_id
    assert get_env.data["trials"] == []

    list_env = await ve_tools.validation_experiment_list(app, limit=10)
    assert list_env.status == "ok"
    assert any(e["experiment_id"] == experiment_id for e in list_env.data)

    # 4) 通过 MCP 登记 trial(失败也算试验)
    trial_env = await ve_tools.validation_experiment_add_trial(
        app,
        experiment_id=experiment_id,
        parameters={"window": 20, "threshold": 0.02},
        status="candidate",
    )
    assert trial_env.status == "ok", trial_env.error
    trial_id = trial_env.data["trial_id"]
    assert trial_id.startswith(f"{experiment_id}-mcp-")
    assert trial_env.data["trial_index"] == 0

    # 5) 补全机器 OOS 证据:SELECTED trial + 统计报告,实验推进到 VALIDATED_OOS
    #    (#138 不含揭盲端点,这里用领域函数模拟 runner 的最终裁决)
    trial = await ResearchTrialRepository(db_session).get(trial_id)
    assert trial is not None
    trial = replace(
        trial,
        status=TrialStatus.SELECTED,
        oos_metrics=WindowMetrics(
            role=WindowRole.TEST,
            start=date(2021, 7, 1),
            end=date(2021, 12, 31),
            sharpe_ratio=0.8,
            total_return=0.1,
            max_drawdown=-0.08,
        ),
        statistical_report=StatisticalReport(
            deflated_sharpe_ratio=0.2,
            probabilistic_sharpe_ratio=0.96,
            pbo=0.2,
            bootstrap_sharpe_ci_low=0.1,
            bootstrap_sharpe_ci_high=1.2,
            bootstrap_mdd_ci_low=-0.15,
            bootstrap_mdd_ci_high=-0.03,
            n_trials=1,
        ),
    )
    await ResearchTrialRepository(db_session).save(trial)
    experiment = await ResearchExperimentRepository(db_session).get(experiment_id)
    assert experiment is not None
    experiment = transition_status(experiment, ExperimentStatus.IN_SAMPLE)
    experiment = mark_final_test_unsealed(experiment)
    experiment = transition_status(experiment, ExperimentStatus.VALIDATED_OOS)
    await ResearchExperimentRepository(db_session).save(experiment)
    await db_session.commit()

    # 6) 通过 MCP 创建因子实验,validation_experiment_id 引用 #57 实验
    factor_env = await factor_tools.factor_experiment_create(
        app,
        hypothesis="ETF 动量策略通过冻结 OOS 与成本检验",
        factor_names=["momentum"],
        dataset_release_id=release.release_id,
        feature_snapshot_id=snapshot.snapshot_id,
        plan={
            "in_sample_start": "2020-01-01",
            "in_sample_end": "2021-06-30",
            "oos_start": "2021-07-01",
            "oos_end": "2021-12-31",
            "trial_budget": 10,
            "benchmark_symbol": "510300.SH",
            "transaction_cost_bps": 10,
            "quantiles": 5,
        },
        comparison_group="e2e-mcp-138",
        validation_experiment_id=experiment_id,
    )
    assert factor_env.status == "ok", factor_env.error
    factor_experiment_id = factor_env.data["experiment_id"]
    assert factor_env.data["validation_experiment_id"] == experiment_id

    # 7) 通过 MCP 同步 #57 终态 → 因子实验 validated_oos
    sync_env = await factor_tools.factor_experiment_sync_validation(
        app, factor_experiment_id
    )
    assert sync_env.status == "ok", sync_env.error
    assert sync_env.data["status"] == "validated_oos"
    best_trial = cast(dict[str, Any], sync_env.data["result"]["best_trial"])
    assert best_trial["trial_id"] == trial_id

    # 8) 清理:因子实验仍引用验证实验时删除 → conflict(外键保护)
    delete_env = await ve_tools.validation_experiment_delete(app, experiment_id)
    assert delete_env.status == "error"
    assert delete_env.error is not None
    assert delete_env.error.kind == "conflict"

    # 先解除引用(删除因子实验行),再通过 MCP 删除验证实验(级联删除 trial)
    from sqlalchemy import delete as sa_delete

    from finboard_persistence.models import FactorExperimentModel

    await db_session.execute(
        sa_delete(FactorExperimentModel).where(
            FactorExperimentModel.experiment_id == factor_experiment_id
        )
    )
    await db_session.commit()
    delete_env = await ve_tools.validation_experiment_delete(app, experiment_id)
    assert delete_env.status == "ok", delete_env.error
    assert await ResearchExperimentRepository(db_session).get(experiment_id) is None
    assert await ResearchTrialRepository(db_session).get(trial_id) is None
