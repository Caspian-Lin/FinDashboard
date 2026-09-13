"""统一离线研究运行的排队、历史、血缘、取消与重放 API(issue #80)。

安全边界:本路由没有 execute/run 端点。它只冻结输入并登记 queued 任务;实际
执行由受控的离线 worker/CLI 调用 ``ResearchRunCoordinator``。LLM actor 被 schema
和领域 manifest 双重拒绝。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.research_run_schemas import (
    ResearchArtifactOut,
    ResearchLineageOut,
    ResearchRunOut,
    ResearchRunQueueIn,
    ResearchRunReplayIn,
)
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_code import (
    active_user_factor_names,
    active_user_strategy_commits,
    build_factor_series_refs,
    default_series_lookup,
    factor_series_rebuild_error,
    freeze_user_code_commit,
    resolve_screen_bindings,
    screen_factor_snapshot_gate_error,
    series_covered_factor_names,
    series_release_mismatches,
    snapshot_anchor_mismatch_error,
    snapshot_anchor_mismatches,
    user_code_reference_gate_error,
    user_factor_reference_gate_error,
    user_factor_series_coverage_gate_error,
)
from finboard_backtest.research_code.predefined_factors import (
    predefined_factor_reference_gate_error,
    predefined_factor_series_coverage_gate_error,
    referenced_predefined_factors,
)
from finboard_backtest.research_run import (
    REPLAYABLE_SOURCE_STATUSES,
    FrozenArtifactRef,
    ResearchActorType,
    ResearchRunConflictError,
    ResearchRunManifest,
    ResearchRunStatus,
    UnsupportedResearchCapabilityError,
    replay_guard_error,
    resolve_decision_schedule,
    to_json_value,
    validate_strategy_dataset_capabilities,
)
from finboard_backtest.research_run.config_overrides import (
    research_portfolio_gate_error,
)
from finboard_backtest.research_run.contracts import JsonValue, stable_checksum
from finboard_backtest.research_run.signal_engine import (
    decision_schedule_dates_gate_error,
    enqueue_decision_dates,
    enqueue_trading_days,
    multi_period_feature_gate_error,
    single_shot_snapshot_gate_error,
)
from finboard_backtest.strategy_spec import ResearchStrategySpec
from finboard_backtest.strategy_spec.contracts import FeatureKind
from finboard_backtest.strategy_spec.universe_precheck import (
    describe_empty_pool,
    preview_universe_pool,
    resolvable_feature_names,
)
from finboard_data.factor_lab import is_user_factor_name
from finboard_data.releases import (
    ReleaseCapabilityError,
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
    FactorSeriesRepository,
    FeatureSnapshotRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

router = APIRouter(prefix="/api/research/runs", tags=["research-runs"])


@router.post("", response_model=ResearchRunOut, status_code=201)
async def queue_research_run(
    body: ResearchRunQueueIn,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """冻结输入并登记 queued 运行;不会在 HTTP 请求内执行回测。"""

    strategy_row = await ResearchStrategySpecRepository(session).get_version(
        body.strategy_id, body.strategy_version
    )
    if strategy_row is None:
        raise HTTPException(status_code=404, detail="策略规格版本不存在")
    if strategy_row.status != "published":
        raise HTTPException(status_code=409, detail="仅已发布策略规格可以进入研究运行")
    spec = ResearchStrategySpec.model_validate(strategy_row.payload)
    if sorted(body.dataset_release_ids) != sorted(spec.validation_plan.dataset_release_ids):
        raise HTTPException(
            status_code=422,
            detail="运行数据发布必须与策略验证计划完全一致",
        )
    try:
        releases = [
            await ResearchDatasetReleaseRepository(session).require_usable(release_id)
            for release_id in body.dataset_release_ids
        ]
    except ReleaseCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        validate_strategy_dataset_capabilities(
            spec.strategy_kind,
            {item.key for release in releases for item in release.capabilities if item.ready},
        )
    except UnsupportedResearchCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    feature_repo = FeatureSnapshotRepository(session)
    snapshots = []
    for snapshot_id in body.factor_snapshot_ids:
        snapshot = await feature_repo.get(snapshot_id)
        if snapshot is None:
            raise HTTPException(
                status_code=422,
                detail=f"因子快照不存在: {snapshot_id}",
            )
        snapshots.append(snapshot)
    # issue #360:因子序列工件解析(内容寻址 FS-);声明时 u_ 因子观测按
    # 决策日从 series.values 取(加载器双轨优先),被覆盖因子跳过
    # single_shot 缺快照清单与 #217 multi_period 拒绝。
    series_repo = FactorSeriesRepository(session)
    series_records = []
    for series_id in body.factor_series_ids:
        record = await series_repo.get(series_id)
        if record is None:
            raise HTTPException(
                status_code=422,
                detail=f"因子序列不存在: {series_id}",
            )
        series_records.append(record)
    series_covered = series_covered_factor_names(series_records)
    required_factor_sources = {
        node.source
        for node in spec.feature_graph.nodes
        if node.source is not None and node.kind in {FeatureKind.FACTOR, FeatureKind.RISK_FACTOR}
    }
    # issue #398:平台预置因子(p_ 前缀)引用门控(规格形态错误,先于
    # #203/#217 具名拒绝)—— 目录注册存在性 + single_shot 必须以
    # factor_series_ids 声明(p_ 因子无快照路径)。multi_period 的覆盖检查
    # 在交易日历可读之后进行(下方)。
    uncovered_predefined = referenced_predefined_factors(
        required_factor_sources,
        series_covered_factors=series_covered,
    )
    predefined_gate_error = predefined_factor_reference_gate_error(
        required_factor_sources=required_factor_sources,
        series_covered_factors=series_covered,
        multi_period=resolve_decision_schedule(body.parameters) is not None,
    )
    if predefined_gate_error is not None:
        raise HTTPException(status_code=422, detail=predefined_gate_error)
    # issue #234:screen 绑定实绑校验(REST+MCP 共用)—— 声明了
    # screen_artifact_bindings 的规格按 DB 逐条校验(存在/非 retired/name/
    # commit 一致),通过后把绑定名并入可引用名单、绑定 commit/ID 冻结进
    # manifest;未声明绑定的普通规格行为完全不变。
    resolution, bind_error = await resolve_screen_bindings(session, spec=spec)
    if bind_error is not None:
        raise HTTPException(status_code=422, detail=bind_error)
    # issue #203:入队期 single_shot 缺快照秒级拒绝(与 MCP 共用同一门控函数,
    # 对齐 #186 预检风格)。multi_period 声明 decision_schedule(issue #361,
    # 含 legacy rebalance_frequency)后不受影响。issue #360:被声明因子序列
    # 覆盖的因子源不再要求快照(序列提供逐日观测;决策时点仍需快照提供,
    # 零快照拒绝分支保持)。
    gate_error = single_shot_snapshot_gate_error(
        strategy_kind=spec.strategy_kind,
        required_factor_sources=required_factor_sources - series_covered,
        frozen_snapshot_count=len(snapshots),
        parameters=body.parameters,
    )
    if gate_error is not None:
        raise HTTPException(status_code=422, detail=gate_error)
    # issue #361:decision_schedule(四频 + custom)。custom dates 必须落在
    # bars 主发布交易日历内(读取发布日历,复用 #334 并集日历 + 间隙哨兵);
    # kind / 形状 / 升序去重已由 ResearchRunQueueIn schema 拦截(与 MCP
    # ``parse_queue_payload`` 同一 schema、同一门控函数,两口径一致)。
    primary = _bars_release(releases)
    schedule = resolve_decision_schedule(body.parameters)
    trading_days: list[date] | None = None
    if schedule is not None and schedule.kind == "custom":
        trading_days = await _enqueue_trading_days(primary)
        dates_error = decision_schedule_dates_gate_error(schedule, trading_days)
        if dates_error is not None:
            raise HTTPException(status_code=422, detail=dates_error)
    # issue #217:用户因子(u_ 前缀)入队门控 —— retired/不存在拒绝;
    # issue #360:被声明因子序列覆盖的 u_ 因子跳过名单检查(序列按决策日
    # 索引,不绑定单一 decision_at;冻结工件不受 artifact 生命周期影响)。
    # issue #361:multi_period x u_ 改为 series 覆盖检查(不再一刀切秒拒,
    # 拒的是「数据没备齐」)—— 每个未声明的引用 u_ 因子需有锚定本次 bars
    # 主发布、覆盖全部决策日的已构建 series,缺失具名拒绝 + 重建命令提示。
    active_factors = await active_user_factor_names(session)
    if resolution is not None:
        active_factors = active_factors | resolution.user_factor_names
    user_gate_error = user_factor_reference_gate_error(
        required_factor_sources=required_factor_sources,
        active_user_factors=active_factors,
        series_covered_factors=series_covered,
    )
    if user_gate_error is not None:
        raise HTTPException(status_code=422, detail=user_gate_error)
    # issue #360:已按 factor_series_ids 声明的因子由 ID 直查校验(上游已
    # 完成),不进入 find_matching 反查——反查以运行参数计算期望 series_key,
    # 与构建期因子参数不同会误报「未构建」。
    referenced_user_factors = {
        name
        for name in required_factor_sources
        if is_user_factor_name(name) and name not in series_covered
    }
    if schedule is not None and referenced_user_factors:
        if trading_days is None:
            trading_days = await _enqueue_trading_days(primary)
        series_gate_error = await user_factor_series_coverage_gate_error(
            referenced_user_factors=referenced_user_factors,
            series_lookup=default_series_lookup(session),
            bars_release_id=primary.release_id,
            dataset_release_ids=[release.release_id for release in releases],
            parameters=body.parameters,
            decision_dates=enqueue_decision_dates(
                parameters=body.parameters,
                trading_days=trading_days,
            ),
            window_start=primary.start_date,
            window_end=primary.end_date,
        )
        if series_gate_error is not None:
            raise HTTPException(status_code=422, detail=series_gate_error)
    if schedule is not None and uncovered_predefined:
        if trading_days is None:
            trading_days = await _enqueue_trading_days(primary)
        predefined_series_error = await predefined_factor_series_coverage_gate_error(
            referenced_predefined=uncovered_predefined,
            series_lookup=default_series_lookup(session),
            bars_release_id=primary.release_id,
            dataset_release_ids=[release.release_id for release in releases],
            decision_dates=enqueue_decision_dates(
                parameters=body.parameters,
                trading_days=trading_days,
            ),
            window_start=primary.start_date,
            window_end=primary.end_date,
        )
        if predefined_series_error is not None:
            raise HTTPException(status_code=422, detail=predefined_series_error)
    # issue #253:multi_period 特征可用性入队门控(与 MCP 共用同一函数)——
    # 规格 identity 源必须 ⊆ 多期可解析集合(标准价格特征 / close / attached
    # 研究发布派生特征 / 快照观测),否则执行期才报「identity 节点缺少数据源」。
    # 放在用户因子门控之后:multi_period 引用 u_ 因子先按 #217 具名拒绝。
    identity_sources = {
        node.source for node in spec.feature_graph.nodes if node.source is not None
    }
    feature_gate_error = multi_period_feature_gate_error(
        identity_sources=identity_sources,
        parameters=body.parameters,
        research_release_kinds=[release.dataset_kind for release in releases],
        snapshot_feature_names=[
            observation.feature_name
            for snapshot in snapshots
            for observation in snapshot.observations
        ]
        + sorted(series_covered),
    )
    if feature_gate_error is not None:
        raise HTTPException(status_code=422, detail=feature_gate_error)
    # issue #218:user_code 策略入队门控 —— artifact active、commit 一致、
    # 沙箱已启用、single_shot 决策时点可用(REST+MCP 共用同一函数);放行时
    # 把 active commit 冻结进 spec(manifest input_checksum 覆盖代码版本);
    # issue #234:screen 绑定的 draft 产物 commit/ID 一并冻结。
    spec_checksum = strategy_row.checksum
    if spec.code_artifact is not None:
        from finboard_app.config import load_settings

        merged_strategies = dict(await active_user_strategy_commits(session))
        if resolution is not None:
            merged_strategies.update(resolution.strategy_commits)
        code_gate_error = user_code_reference_gate_error(
            code_artifact_name=spec.code_artifact.name,
            code_artifact_commit=spec.code_artifact.commit,
            active_user_strategies=merged_strategies,
            sandbox_enabled=load_settings().research_sandbox_enabled,
            frozen_snapshot_count=len(snapshots),
            parameters=cast(dict[str, JsonValue] | None, body.parameters),
        )
        if code_gate_error is not None:
            raise HTTPException(status_code=422, detail=code_gate_error)
        frozen_spec = freeze_user_code_commit(
            spec,
            merged_strategies,
            artifact_ids=(
                resolution.strategy_artifact_ids if resolution is not None else None
            ),
        )
        if frozen_spec is not spec:
            # 冻结 commit/artifact_id 改变了 payload:manifest 内的 checksum
            # 按 post_init 契约必须等于冻结后规格的 checksum(发布版本本身的
            # 追溯仍由 strategy_version + 冻结绑定字段承载)。
            spec_checksum = stable_checksum(frozen_spec.canonical_payload())
            spec = frozen_spec
    release_ids = {release.release_id for release in releases}
    # issue #360:换发布守卫 —— 引用序列锚定的 bars 主发布不在本次冻结清单
    # 即具名拒绝(失效 series 清单 + 重建代价预估;托管重建 = 一次入队 N 个
    # 既有 finboard_factor_series_build,不新造编排器)。
    series_mismatches = series_release_mismatches(series_records, release_ids)
    if series_mismatches:
        raise HTTPException(
            status_code=422,
            detail=factor_series_rebuild_error(series_mismatches),
        )
    # issue #217:#355 —— 快照锚定发布 ⊆ 本次冻结清单(数据一致性 fail-visible,
    # 约束零放松);失配改为逐快照全量收集后一次性具名拒绝(名称 / 锚定发布 /
    # 失配方向 + 两条修复路径),不再逐次提交只暴露第一个失配。
    anchor_mismatches = await snapshot_anchor_mismatches(
        session, snapshots=snapshots, requested_release_ids=release_ids
    )
    if anchor_mismatches:
        raise HTTPException(
            status_code=422,
            detail=snapshot_anchor_mismatch_error(anchor_mismatches),
        )
    # issue #234:factor 通道 screen 运行的快照证据预检 —— 绑定的 draft 产物
    # 必须已有其 RCR 产出的快照进入 factor_snapshot_ids,否则秒级拒绝
    # (没有证据的 screen run 完成后 promote 必失败,提前到入队暴露)。
    if resolution is not None:
        snapshot_gate_error = await screen_factor_snapshot_gate_error(
            session, resolution=resolution, snapshots=snapshots
        )
        if snapshot_gate_error is not None:
            raise HTTPException(status_code=422, detail=snapshot_gate_error)

    # issue #186:入队同步候选池非空校验。用 bars 主发布(信号引擎实际使用的
    # 发布)的 instruments 做静态评估,空池秒级 422(invalid_argument 语义),附
    # 各过滤条件排除统计与缺失字段名,不再等执行期跑 30 分钟后才报泛化错误。
    # issue #187:联合发布中研究数据 release(daily_metrics/financial_indicators)
    # 只提供因子观测,候选池评估必须落在 bars 主发布上。
    # (issue #361:``primary`` 已在 decision_schedule / u_ 因子门控段前置取得。)
    preview = preview_universe_pool(
        spec.universe,
        primary.instruments,
        decision_date=primary.end_date,
        available_features=resolvable_feature_names(
            feature_graph_sources=[
                node.source for node in spec.feature_graph.nodes if node.source is not None
            ],
            snapshot_feature_names=[
                observation.feature_name
                for snapshot in snapshots
                for observation in snapshot.observations
            ]
            + sorted(series_covered),
            research_release_kinds=[release.dataset_kind for release in releases],
        ),
    )
    if preview.is_empty:
        raise HTTPException(
            status_code=422,
            detail=(
                "运行数据发布候选池为空,拒绝入队: "
                f"{describe_empty_pool(preview, decision_date=primary.end_date)}"
            ),
        )

    # issue #303:入队组合可行性预检(与 MCP ``_build_queued_manifest`` 共用
    # 同一门控函数)—— 解析生效 max_risk_contribution(portfolio_config.overrides
    # 可覆盖,默认 0.35)后,静态候选池 < ceil(1/阈值) 时秒级 422(附排除统计、
    # 生效阈值与键位修复路径),不再等执行期组合阶段 RiskBudgetError 才
    # REJECTED;阈值非法值与 risk_config.overrides 形态错误同样入队即拒
    # (分区键位:组合约束在 portfolio_config,风险退出在 risk_config)。
    # 逐期真实买入池入队期不可精确预知,运行期 fail-closed 兜底(#91)不变。
    portfolio_gate_error = research_portfolio_gate_error(
        preview=preview,
        risk_exit_policy=spec.risk_exit_policy,
        portfolio_overrides=body.portfolio_config,
        risk_overrides=body.risk_config,
        decision_date=primary.end_date,
    )
    if portfolio_gate_error is not None:
        raise HTTPException(status_code=422, detail=portfolio_gate_error)

    run_id = _run_id(body.idempotency_key)
    try:
        manifest = ResearchRunManifest(
            run_id=run_id,
            idempotency_key=body.idempotency_key,
            strategy_spec=spec,
            strategy_spec_checksum=spec_checksum,
            dataset_releases=tuple(
                FrozenArtifactRef(
                    artifact_id=release.release_id,
                    version=release.version,
                    checksum=release.release_checksum,
                    capabilities=tuple(item.key for item in release.capabilities if item.ready),
                )
                for release in releases
            ),
            strategy_version=body.strategy_version,
            factor_snapshots=tuple(
                FrozenArtifactRef(
                    artifact_id=snapshot.snapshot_id,
                    version=snapshot.framework_version,
                    checksum=snapshot.checksum,
                    capabilities=tuple(
                        sorted({f"factor:{item.feature_name}" for item in snapshot.observations})
                    ),
                )
                for snapshot in snapshots
            ),
            # issue #360:因子序列冻结引用({series_id, content_checksum});
            # 空集合不传,历史 manifest checksum 零漂移。
            factor_series=(
                build_factor_series_refs(series_records) if series_records else ()
            ),
            parameters=cast(dict[str, JsonValue], body.parameters),
            validation_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.validation_plan.model_dump(mode="json"),
                    "overrides": body.validation_config,
                },
            ),
            portfolio_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.portfolio_policy.model_dump(mode="json"),
                    "overrides": body.portfolio_config,
                },
            ),
            risk_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.risk_exit_policy.model_dump(mode="json"),
                    "overrides": body.risk_config,
                },
            ),
            execution_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.execution_model.model_dump(mode="json"),
                    "overrides": body.execution_config,
                },
            ),
            fee_config=cast(
                dict[str, JsonValue],
                {
                    "commission_rate": spec.execution_model.commission_rate,
                    "minimum_commission": spec.execution_model.minimum_commission,
                    "sell_tax_rate": spec.execution_model.sell_tax_rate,
                    "slippage_bps": spec.execution_model.slippage_bps,
                    "overrides": body.fee_config,
                },
            ),
            benchmark_config=cast(
                dict[str, JsonValue],
                {
                    "symbol": spec.validation_plan.benchmark_symbol,
                    "overrides": body.benchmark_config,
                },
            ),
            code_version=body.code_version,
            initial_capital=body.initial_capital,
            requested_by=body.requested_by,
            # issue #312:REST 默认 human 不变;body 可显式声明 agent
            # (schema Literal 已放开,llm 由契约层 fail-closed 拒绝)。
            actor_type=ResearchActorType(body.actor_type),
        )
        row, _ = await ResearchRunRepository(session).create_or_get(
            run_id=manifest.run_id,
            idempotency_key=manifest.idempotency_key,
            replay_of_run_id=None,
            strategy_id=manifest.strategy_spec.strategy_id,
            strategy_kind=manifest.strategy_kind,
            status=ResearchRunStatus.QUEUED.value,
            schema_version=manifest.schema_version,
            manifest_checksum=manifest.checksum,
            manifest=cast(dict[str, object], to_json_value(manifest)),
            requested_by=manifest.requested_by,
        )
        # issue #143:同事务双写 background_jobs,共用 idempotency_key 保证幂等。
        # 已存在的 research_run(幂等命中)若已有 job_id 则保留,否则补建。
        if not row.job_id:
            row.job_id = await _enqueue_research_run_job(session, manifest=manifest)
        await session.commit()
    except (ValueError, ValidationError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        ResearchRunPersistenceConflictError,
        BackgroundJobPersistenceConflictError,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # 提交后重读整行:created_at/updated_at 是 server_default(RETURNING 不保证
    # 覆盖 onupdate 列),直接序列化新插入对象会在 async 上下文触发 lazy-load
    # MissingGreenlet。与 cancel/replay 的「提交后 get 重读」口径一致。
    fresh = await ResearchRunRepository(session).get(run_id)
    assert fresh is not None
    return ResearchRunOut.model_validate(fresh)


@router.get("", response_model=list[ResearchRunOut])
async def list_research_runs(
    status: list[str] | None = Query(default=None),
    strategy_kind: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchRunOut]:
    valid = {item.value for item in ResearchRunStatus}
    if status is not None and not set(status).issubset(valid):
        raise HTTPException(status_code=422, detail="未知 ResearchRun 状态")
    rows = await ResearchRunRepository(session).list_recent(
        statuses=status,
        strategy_kind=strategy_kind,
        limit=limit,
    )
    return [ResearchRunOut.model_validate(row) for row in rows]


@router.get("/{run_id}", response_model=ResearchRunOut)
async def get_research_run(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    row = await ResearchRunRepository(session).get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    return ResearchRunOut.model_validate(row)


@router.get("/{run_id}/artifacts", response_model=list[ResearchArtifactOut])
async def list_research_artifacts(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchArtifactOut]:
    repo = ResearchRunRepository(session)
    if await repo.get(run_id) is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    rows = await repo.list_artifacts(run_id)
    return [ResearchArtifactOut.model_validate(row) for row in rows]


@router.get(
    "/{run_id}/lineage/{trace_id}",
    response_model=ResearchLineageOut,
)
async def get_research_lineage(
    run_id: str,
    trace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchLineageOut:
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
    from finboard_backtest.research_run import ResearchRunCoordinator

    artifacts = await ResearchRunCoordinator(store).lineage(run_id, trace_id)
    if not artifacts:
        raise HTTPException(status_code=404, detail="血缘 trace 不存在")
    return ResearchLineageOut(
        run_id=run_id,
        leaf_trace_id=trace_id,
        artifacts=[
            ResearchArtifactOut(
                artifact_id=item.artifact_id,
                run_id=item.run_id,
                decision_id=item.decision_id,
                sequence=item.sequence,
                stage=item.stage.value,
                trace_id=item.trace_id,
                parent_trace_ids=list(item.parent_trace_ids),
                payload=cast(dict[str, object], item.payload),
                checksum=item.checksum,
                created_at=item.created_at,
            )
            for item in artifacts
        ],
    )


@router.post("/{run_id}/cancel", response_model=ResearchRunOut)
async def cancel_research_run(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    import contextlib

    from finboard_backtest.research_run import ResearchRunCoordinator

    coordinator = ResearchRunCoordinator(SqlAlchemyResearchRunStore(ResearchRunRepository(session)))
    try:
        record = await coordinator.cancel(run_id)
        # issue #143:协作式取消联动 background_jobs(running → cancel_requested,
        # worker 下次 checkpoint 时 research_run CANCELLED 退出,job 收口 cancelled)。
        # 已终态的 job request_cancel 抛 ConflictError,幂等 suppress。
        if record.job_id:
            with contextlib.suppress(BackgroundJobPersistenceConflictError):
                await BackgroundJobRepository(session).request_cancel(record.job_id)
        await session.commit()
    except (ResearchRunConflictError, ResearchRunPersistenceConflictError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = await ResearchRunRepository(session).get(record.manifest.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


@router.post("/{run_id}/replay", response_model=ResearchRunOut, status_code=201)
async def queue_research_replay(
    run_id: str,
    body: ResearchRunReplayIn,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """复制完整冻结清单为新 queued 运行;仍不在 HTTP 中执行。

    issue #305:interrupted 源放开为事故恢复通道 —— 新 run 自动继承原
    manifest 全部冻结输入(含 factor_snapshots 全部 ID),血缘标注
    ``replay_of_run_id`` + ``replay_source_status``;cancelled 仍拒绝,
    completed 重放行为不变。
    """

    store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
    source = await store.get(run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="源研究运行不存在")
    if source.status not in REPLAYABLE_SOURCE_STATUSES:
        raise HTTPException(status_code=409, detail=replay_guard_error(source.status))
    manifest = replace(
        source.manifest,
        run_id=_run_id(body.idempotency_key),
        idempotency_key=body.idempotency_key,
        requested_by=body.requested_by,
        # issue #312:REST 默认 human 不变;body 可显式声明 agent。
        actor_type=ResearchActorType(body.actor_type),
        replay_of_run_id=run_id,
        replay_source_status=source.status.value,
    )
    try:
        record, created = await store.create_or_get(manifest)
        # issue #143:重放也走双写;background_jobs 用重放后的新 idempotency_key。
        if created:
            row = await ResearchRunRepository(session).get(record.manifest.run_id)
            assert row is not None
            if not row.job_id:
                row.job_id = await _enqueue_research_run_job(session, manifest=manifest)
        await session.commit()
    except (
        ResearchRunPersistenceConflictError,
        BackgroundJobPersistenceConflictError,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = await ResearchRunRepository(session).get(record.manifest.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _bars_release(releases: list[ResearchDatasetRelease]) -> ResearchDatasetRelease:
    """取联合发布中的 bars 主发布(issue #187)。

    排除 daily_metrics / financial_indicators 研究数据发布;恰好一个 bars
    发布才有候选池评估意义,否则 422。
    """
    bars_releases = [
        item for item in releases if item.dataset_kind is ReleaseDatasetKind.BARS
    ]
    if len(bars_releases) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "联合发布必须恰好包含一个 bars 主发布(行情/候选池来源): "
                + ", ".join(f"{item.release_id}({item.dataset_kind.value})" for item in releases)
            ),
        )
    return bars_releases[0]


async def _enqueue_trading_days(primary: ResearchDatasetRelease) -> list[date]:
    """入队期读取 bars 主发布交易日历(issue #361;读失败具名 422)。

    仅 custom dates 校验与 multi_period x u_ 因子覆盖检查触达(非必要
    不读发布文件);与 MCP ``_build_queued_manifest`` 共用同一读取实现
    (``enqueue_trading_days``,#334 并集日历 + 间隙哨兵)。
    """
    try:
        return await enqueue_trading_days(
            bars_release_id=primary.release_id,
            bars_release_checksum=primary.release_checksum,
            release_root=Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "无法读取 bars 主发布交易日历以校验 decision_schedule / 用户因子"
                f" series 覆盖(release {primary.release_id}): {exc}"
            ),
        ) from exc


def _payload_checksum(payload: dict[str, object]) -> str:
    """计算 background_jobs payload checksum(与 jobs.py 路由保持一致)。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue_research_run_job(
    session: AsyncSession,
    *,
    manifest: ResearchRunManifest,
) -> str:
    """在同事务内为研究运行创建 background_jobs 行,返回 job_id(issue #143)。

    与 research_run 共用 idempotency_key,保证两系统幂等一致:任一系统命中即
    不重复创建。调用方负责把返回的 job_id 回填到 research_runs.job_id。
    """
    payload: dict[str, object] = {
        "run_id": manifest.run_id,
        "strategy_kind": manifest.strategy_kind,
    }
    job_repo = BackgroundJobRepository(session)
    job_row, _ = await job_repo.create_or_get(
        job_id=generate_background_job_id(),
        idempotency_key=manifest.idempotency_key,
        kind="research_run",
        queue="research",
        status=BackgroundJobStatus.QUEUED.value,
        priority=0,
        payload=payload,
        payload_checksum=_payload_checksum(payload),
        max_attempts=3,
        requested_by=manifest.requested_by,
    )
    return job_row.job_id


@router.get("/{run_id}/report/export")
async def export_research_run_report(
    run_id: str,
    format: str = Query(default="csv", pattern="^(csv|markdown)$"),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """把 ResearchRun 报告导出为 CSV / Markdown 下载(#157,#141 的 Web 闭环)。

    复用 ``finboard_mcp.reporting`` 的聚合与渲染(MCP ``finboard_report_export``
    同一逻辑,不复制实现);与 MCP 不同,这里直接以 HTTP 响应返回内容,不落盘。
    """
    from finboard_mcp import reporting

    repo = ResearchRunRepository(session)
    row = await repo.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    artifacts = await repo.list_artifacts(run_id)
    report = reporting.aggregate_run_report(row, artifacts, view="detail")
    content = await asyncio.to_thread(reporting.render_report, "run", report, format)
    filename = f"finboard_run_{_safe_filename(run_id)}.{_report_extension(format)}"
    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/markdown; charset=utf-8"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _safe_filename(value: str) -> str:
    """把 run_id 清理为安全的文件名片段(RR- 前缀本身安全,防御性处理)。"""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value) or "unknown"


def _report_extension(fmt: str) -> str:
    return "md" if fmt == "markdown" else fmt


__all__ = ["router"]
