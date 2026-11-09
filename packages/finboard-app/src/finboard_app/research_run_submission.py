"""ResearchRun 提交的唯一编排层(issue #513)。

REST(``finboard_api.routes.research_runs``)与 MCP(``finboard_mcp.tools.runs``)
此前各自维护一份「预检 → 冻结 → manifest → 双写 run+job」编排;#513 把两份收敛
到本模块,两条适配层只做**输入解析**与**输出渲染**。

分层与契约:

* :class:`ResearchRunSubmitInput` / :class:`ResearchRunReplayInput` 是入队与重放
  的唯一输入 DTO,字段、默认值与校验器与历史 ``ResearchRunQueueIn`` /
  ``ResearchRunReplayIn`` 逐项一致(``finboard_api.research_run_schemas`` 保留
  同名 re-export,外部 import 不破)。
* :class:`ResearchRunSubmissionService` 保持既有门控**顺序与函数调用**不变;
  manifest 构造逐字段保持原样(字段顺序 / 取值 / None 省略),历史
  ``manifest_checksum`` 逐字节稳定。
* **事务所有权在调用方**:服务只 ``flush``,绝不 ``commit``;REST 路由 / MCP
  handler / 未来的课题动作(#515)持有唯一一次 commit,才能把「课题链接 +
  run + job + 审计」放进同一事务。
* 失败一律抛 :class:`finboard_app.research_operation.ResearchOperationError`
  家族(不抛 ``HTTPException`` / ``McpToolError``),映射只发生在适配层。

边界:只写 ``research_runs`` / ``background_jobs``(研究域),不触实盘
``orders`` / ``fills`` / ``positions`` / ``audit_logs``;不执行回测。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.background_jobs import enqueue_job_core
from finboard_app.research_operation import (
    ResearchConflictError,
    ResearchInvalidInputError,
    ResearchNotFoundError,
    ResearchOperationContext,
    ResearchOperationError,
    ResearchPreconditionFailedError,
)
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.data_scope import ScopeGateError, apply_scope_to_parameters
from finboard_backtest.data_scope.contracts import DataScopeSpec
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
    research_policy_gate_error,
    research_portfolio_gate_error,
)
from finboard_backtest.research_run.contracts import (
    DECISION_SCHEDULE_KINDS,
    REBALANCE_FREQUENCIES,
    JsonValue,
    parse_decision_schedule,
    stable_checksum,
)
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
from finboard_data.factor_lab import FeatureSnapshot, is_user_factor_name
from finboard_data.releases import (
    ReleaseCapabilityError,
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    FactorSeriesRepository,
    FeatureSnapshotRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
)
from finboard_persistence.factor_series_repo import FactorSeriesRecord
from finboard_persistence.models import (
    BackgroundJobModel,
    ResearchRunModel,
    ResearchStrategySpecModel,
)

# --------------------------------------------------------------------------- #
# 输入 DTO(字段 / 默认值 / 校验器与历史 ResearchRunQueueIn 逐项一致)
# --------------------------------------------------------------------------- #


class ResearchRunSubmitInput(BaseModel):
    """研究运行入队输入(issue #80 / #510;原 ``ResearchRunQueueIn``)。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=64)
    strategy_version: int = Field(ge=1)
    dataset_release_ids: list[str] = Field(min_length=1)
    factor_snapshot_ids: list[str] = Field(default_factory=list)
    # issue #360:内容寻址因子序列工件引用(FS- 前缀);声明时 u_ 因子观测
    # 按决策日从 series.values 取(加载器双轨优先),空 = 既有快照路径。
    factor_series_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    validation_config: dict[str, Any] = Field(default_factory=dict)
    portfolio_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] = Field(default_factory=dict)
    execution_config: dict[str, Any] = Field(default_factory=dict)
    fee_config: dict[str, Any] = Field(default_factory=dict)
    benchmark_config: dict[str, Any] = Field(default_factory=dict)
    code_version: str = Field(min_length=7, max_length=64)
    initial_capital: Decimal = Field(ge=Decimal("100000"), le=Decimal("500000"))
    requested_by: str = Field(min_length=1, max_length=128)
    # issue #312:REST 默认 human 不变;放开 agent(网关/自动化通道显式声明),
    # llm 仍被拒绝(契约层 fail-closed,红线不变)。MCP 通道由
    # ``ResearchOperationContext.for_mcp()`` 固定 agent,忽略此字段。
    actor_type: Literal["human", "agent"] = "human"
    # issue #510:统一数据范围预览结论的回填。声明时必须同时给
    # ``data_scope_resolution_id``(预览返回,SC- 前缀):提交期重算复验,
    # 解析集合必须与 dataset_release_ids 一致,窗口合并进
    # ``parameters.research_window``。None = 旧入口,行为不变。
    data_scope: DataScopeSpec | None = None
    data_scope_resolution_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_data_scope(self) -> ResearchRunSubmitInput:
        if self.data_scope is None and self.data_scope_resolution_id is not None:
            raise ValueError("data_scope_resolution_id 必须与 data_scope 一起声明")
        return self

    @field_validator("parameters")
    @classmethod
    def _validate_decision_schedule(cls, value: dict[str, Any]) -> dict[str, Any]:
        """决策日历声明参数门(issue #361,#183 legacy 扩展)。

        * ``decision_schedule``:kind ∈ {daily, weekly, monthly, quarterly,
          custom};custom 必须声明非空 ``dates``(YYYY-MM-DD 字符串,严格
          升序去重);形状规则与执行期 ``parse_decision_schedule`` 同源
          (单一事实来源,不双份漂移);
        * legacy ``rebalance_frequency`` 兼容扩展接受 daily/weekly(旧值
          monthly/quarterly 零变化);与 ``decision_schedule`` 不可同时声明;
        * ``dates ⊆ 发布交易日`` 不是形状规则(需要发布日历),由服务内
          日历门控校验(REST+MCP 共用同一实现)。

        非法值提前在入队时拒绝(研究运行执行时也会 fail-closed,这里给 agent
        更早、更清晰的错误)。
        """
        if "decision_schedule" in value:
            if value.get("rebalance_frequency") is not None:
                raise ValueError(
                    "parameters.decision_schedule 与 legacy rebalance_frequency"
                    " 不可同时声明(声明 decision_schedule 即 multi_period)"
                )
            try:
                parse_decision_schedule(value["decision_schedule"])
            except ValueError as exc:
                raise ValueError(f"decision_schedule 非法: {exc}") from exc
        frequency = value.get("rebalance_frequency")
        if frequency is not None and (
            not isinstance(frequency, str) or frequency not in REBALANCE_FREQUENCIES
        ):
            raise ValueError(
                f"rebalance_frequency 仅支持 {sorted(REBALANCE_FREQUENCIES)}"
                f"(decision_schedule.kind 支持 {sorted(DECISION_SCHEDULE_KINDS)}),"
                f"收到 {frequency!r}"
            )
        return value

    @classmethod
    def from_json_payload(cls, payload: Mapping[str, Any]) -> ResearchRunSubmitInput:
        """把 JSON payload 规范化后校验(``initial_capital`` 允许 str / number)。

        MCP 工具(``finboard_run_queue`` / ``finboard_backtest_run`` 的
        strategy_spec 形态)走这里,替代历史 ``parse_queue_payload``;校验失败抛
        ``pydantic.ValidationError``,由适配层映射为 ``invalid_argument``。
        """

        payload_copy = dict(payload)
        if "initial_capital" in payload_copy and not isinstance(
            payload_copy["initial_capital"], Decimal
        ):
            payload_copy["initial_capital"] = Decimal(str(payload_copy["initial_capital"]))
        return cls.model_validate(payload_copy)


class ResearchRunReplayInput(BaseModel):
    """研究运行重放输入(原 ``ResearchRunReplayIn``)。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    # issue #312:同 queue —— 默认 human 不变,放开 agent,llm 仍拒绝。
    actor_type: Literal["human", "agent"] = "human"


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PreflightReport:
    """只读预检结论(不落库):冻结后的 manifest + 来源行。

    供观测 / 测试使用;``submit`` 内部也走同一份 ``preflight``,保证「预检
    看到的 manifest」与「真正入队的 manifest」是同一个对象语义。
    """

    manifest: ResearchRunManifest
    strategy_row: ResearchStrategySpecModel
    run_id: str
    manifest_checksum: str


@dataclass(slots=True)
class SubmitOutcome:
    """提交结果(调用方负责 commit / rollback)。"""

    run_row: ResearchRunModel
    job_row: BackgroundJobModel | None
    created: bool
    manifest: ResearchRunManifest


def research_run_id_for(idempotency_key: str) -> str:
    """``RR-`` 由幂等键确定性派生(与历史实现逐字节一致)。"""

    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #


class ResearchRunSubmissionService:
    """研究运行提交编排(单一事实来源)。

    ``sandbox_enabled`` 由调用方从进程配置注入(服务层不自行读环境);
    ``release_root`` 是 bars 主发布文件根目录(默认沿用
    ``FINBOARD_DATA_RELEASE_ROOT`` / ``data_releases``)。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        sandbox_enabled: bool = False,
        release_root: Path | None = None,
    ) -> None:
        self._session = session
        self._sandbox_enabled = sandbox_enabled
        self._release_root = release_root or Path(
            os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
        )

    # -- 只读预检 ---------------------------------------------------------- #
    async def preflight(
        self,
        payload: ResearchRunSubmitInput,
        ctx: ResearchOperationContext | None = None,
    ) -> PreflightReport:
        """跑完全部入队门控并构造 manifest,**不写任何表**。

        ``ctx`` 只影响 manifest 的 ``actor_type``(服务端身份权威);缺省时按
        ``payload.actor_type`` 解析,便于离线预检与测试复现。
        """

        actor_type = (
            ctx.actor_type if ctx is not None else ResearchActorType(payload.actor_type)
        )

        session = self._session
        strategy_row = await ResearchStrategySpecRepository(session).get_version(
            payload.strategy_id, payload.strategy_version
        )
        if strategy_row is None:
            raise ResearchNotFoundError("策略规格版本不存在", code="strategy_version_not_found")
        if strategy_row.status != "published":
            raise ResearchConflictError(
                "仅已发布策略规格可以进入研究运行", code="strategy_not_published"
            )
        spec = ResearchStrategySpec.model_validate(strategy_row.payload)
        if sorted(payload.dataset_release_ids) != sorted(
            spec.validation_plan.dataset_release_ids
        ):
            raise ResearchInvalidInputError(
                "运行数据发布必须与策略验证计划完全一致",
                code="dataset_release_mismatch",
            )
        try:
            releases = [
                await ResearchDatasetReleaseRepository(session).require_usable(release_id)
                for release_id in payload.dataset_release_ids
            ]
        except ReleaseCapabilityError as exc:
            raise ResearchInvalidInputError(
                str(exc), code="release_not_usable"
            ) from exc
        try:
            validate_strategy_dataset_capabilities(
                spec.strategy_kind,
                {item.key for release in releases for item in release.capabilities if item.ready},
            )
        except UnsupportedResearchCapabilityError as exc:
            raise ResearchInvalidInputError(
                str(exc), code="unsupported_capability"
            ) from exc
        # issue #510:统一数据范围提交复验。用同一份 DataScopeSpec 重算,比对
        # 预览返回的 resolution_id —— 预览后若上线了新发布,auto 选择会变、
        # resolution_id 也变,这里 409 要求重新预览(不静默换最新发布)。通过后
        # 得到合并了 ``research_window`` 的参数副本:只在构造 manifest 时使用,
        # **不**改写 ``payload.parameters``——#361 的因子序列反查把 run parameters
        # 当 series_key 输入,凭空多一个窗口键会让合法 run 报「未构建 series」。
        scope_parameters: dict[str, Any] | None = None
        scope_manifest: dict[str, Any] | None = None
        if payload.data_scope is not None:
            try:
                scope_parameters, scope_manifest = await apply_scope_to_parameters(
                    session,
                    payload.data_scope,
                    resolution_id=payload.data_scope_resolution_id,
                    release_ids=payload.dataset_release_ids,
                    strategy_id=payload.strategy_id,
                    strategy_version=payload.strategy_version,
                    parameters=payload.parameters,
                )
            except ScopeGateError as exc:
                raise _scope_gate_error(exc) from exc
        feature_repo = FeatureSnapshotRepository(session)
        snapshots: list[FeatureSnapshot] = []
        for snapshot_id in payload.factor_snapshot_ids:
            snapshot = await feature_repo.get(snapshot_id)
            if snapshot is None:
                raise ResearchInvalidInputError(
                    f"因子快照不存在: {snapshot_id}", code="factor_snapshot_not_found"
                )
            snapshots.append(snapshot)
        # issue #360:因子序列工件解析(内容寻址 FS-);声明时 u_ 因子观测按
        # 决策日从 series.values 取(加载器双轨优先),被覆盖因子跳过
        # single_shot 缺快照清单与 #217 multi_period 拒绝。
        series_repo = FactorSeriesRepository(session)
        series_records: list[FactorSeriesRecord] = []
        for series_id in payload.factor_series_ids:
            record = await series_repo.get(series_id)
            if record is None:
                raise ResearchInvalidInputError(
                    f"因子序列不存在: {series_id}", code="factor_series_not_found"
                )
            series_records.append(record)
        series_covered = series_covered_factor_names(series_records)
        required_factor_sources = {
            node.source
            for node in spec.feature_graph.nodes
            if node.source is not None
            and node.kind in {FeatureKind.FACTOR, FeatureKind.RISK_FACTOR}
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
            multi_period=resolve_decision_schedule(payload.parameters) is not None,
        )
        if predefined_gate_error is not None:
            raise ResearchInvalidInputError(
                predefined_gate_error, code="predefined_factor_reference"
            )
        # issue #234:screen 绑定实绑校验(REST+MCP 共用)—— 声明了
        # screen_artifact_bindings 的规格按 DB 逐条校验(存在/非 retired/name/
        # commit 一致),通过后把绑定名并入可引用名单、绑定 commit/ID 冻结进
        # manifest;未声明绑定的普通规格行为完全不变。
        resolution, bind_error = await resolve_screen_bindings(session, spec=spec)
        if bind_error is not None:
            raise ResearchInvalidInputError(bind_error, code="screen_binding_invalid")
        # issue #203:入队期 single_shot 缺快照秒级拒绝(与 MCP 共用同一门控函数,
        # 对齐 #186 预检风格)。multi_period 声明 decision_schedule(issue #361,
        # 含 legacy rebalance_frequency)后不受影响。issue #360:被声明因子序列
        # 覆盖的因子源不再要求快照(序列提供逐日观测;决策时点仍需快照提供,
        # 零快照拒绝分支保持)。
        gate_error = single_shot_snapshot_gate_error(
            strategy_kind=spec.strategy_kind,
            required_factor_sources=required_factor_sources - series_covered,
            frozen_snapshot_count=len(snapshots),
            parameters=payload.parameters,
        )
        if gate_error is not None:
            raise ResearchInvalidInputError(gate_error, code="snapshot_required")
        # issue #361:decision_schedule(四频 + custom)。custom dates 必须落在
        # bars 主发布交易日历内(读取发布日历,复用 #334 并集日历 + 间隙哨兵);
        # kind / 形状 / 升序去重已由输入 schema 拦截(与 MCP 同一 schema、同一
        # 门控函数,两口径一致)。
        primary = _bars_release(releases)
        schedule = resolve_decision_schedule(payload.parameters)
        trading_days: list[date] | None = None
        if schedule is not None and schedule.kind == "custom":
            trading_days = await self._enqueue_trading_days(primary)
            dates_error = decision_schedule_dates_gate_error(schedule, trading_days)
            if dates_error is not None:
                raise ResearchInvalidInputError(
                    dates_error, code="decision_schedule_dates"
                )
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
            raise ResearchInvalidInputError(
                user_gate_error, code="user_factor_not_active"
            )
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
                trading_days = await self._enqueue_trading_days(primary)
            series_gate_error = await user_factor_series_coverage_gate_error(
                referenced_user_factors=referenced_user_factors,
                series_lookup=default_series_lookup(session),
                bars_release_id=primary.release_id,
                dataset_release_ids=[release.release_id for release in releases],
                parameters=payload.parameters,
                decision_dates=enqueue_decision_dates(
                    parameters=payload.parameters,
                    trading_days=trading_days,
                ),
                window_start=primary.start_date,
                window_end=primary.end_date,
            )
            if series_gate_error is not None:
                raise ResearchInvalidInputError(
                    series_gate_error, code="user_factor_series_coverage"
                )
        if schedule is not None and uncovered_predefined:
            if trading_days is None:
                trading_days = await self._enqueue_trading_days(primary)
            predefined_series_error = await predefined_factor_series_coverage_gate_error(
                referenced_predefined=uncovered_predefined,
                series_lookup=default_series_lookup(session),
                bars_release_id=primary.release_id,
                dataset_release_ids=[release.release_id for release in releases],
                decision_dates=enqueue_decision_dates(
                    parameters=payload.parameters,
                    trading_days=trading_days,
                ),
                window_start=primary.start_date,
                window_end=primary.end_date,
            )
            if predefined_series_error is not None:
                raise ResearchInvalidInputError(
                    predefined_series_error, code="predefined_factor_series_coverage"
                )
        # issue #253:multi_period 特征可用性入队门控(与 MCP 共用同一函数)——
        # 规格 identity 源必须 ⊆ 多期可解析集合(标准价格特征 / close / attached
        # 研究发布派生特征 / 快照观测),否则执行期才报「identity 节点缺少数据源」。
        # 放在用户因子门控之后:multi_period 引用 u_ 因子先按 #217 具名拒绝。
        identity_sources = {
            node.source for node in spec.feature_graph.nodes if node.source is not None
        }
        feature_gate_error = multi_period_feature_gate_error(
            identity_sources=identity_sources,
            parameters=payload.parameters,
            research_release_kinds=[release.dataset_kind for release in releases],
            snapshot_feature_names=[
                observation.feature_name
                for snapshot in snapshots
                for observation in snapshot.observations
            ]
            + sorted(series_covered),
        )
        if feature_gate_error is not None:
            raise ResearchInvalidInputError(
                feature_gate_error, code="multi_period_feature_unavailable"
            )
        # issue #218:user_code 策略入队门控 —— artifact active、commit 一致、
        # 沙箱已启用、single_shot 决策时点可用(REST+MCP 共用同一函数);放行时
        # 把 active commit 冻结进 spec(manifest input_checksum 覆盖代码版本);
        # issue #234:screen 绑定的 draft 产物 commit/ID 一并冻结。
        spec_checksum = strategy_row.checksum
        if spec.code_artifact is not None:
            merged_strategies = dict(await active_user_strategy_commits(session))
            if resolution is not None:
                merged_strategies.update(resolution.strategy_commits)
            code_gate_error = user_code_reference_gate_error(
                code_artifact_name=spec.code_artifact.name,
                code_artifact_commit=spec.code_artifact.commit,
                active_user_strategies=merged_strategies,
                sandbox_enabled=self._sandbox_enabled,
                frozen_snapshot_count=len(snapshots),
                parameters=cast(dict[str, JsonValue] | None, payload.parameters),
            )
            if code_gate_error is not None:
                raise ResearchInvalidInputError(
                    code_gate_error, code="user_code_reference"
                )
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
            raise ResearchInvalidInputError(
                factor_series_rebuild_error(series_mismatches),
                code="factor_series_release_mismatch",
            )
        # issue #217:#355 —— 快照锚定发布 ⊆ 本次冻结清单(数据一致性 fail-visible,
        # 约束零放松);失配改为逐快照全量收集后一次性具名拒绝(名称 / 锚定发布 /
        # 失配方向 + 两条修复路径),不再逐次提交只暴露第一个失配。
        anchor_mismatches = await snapshot_anchor_mismatches(
            session, snapshots=snapshots, requested_release_ids=release_ids
        )
        if anchor_mismatches:
            raise ResearchInvalidInputError(
                snapshot_anchor_mismatch_error(anchor_mismatches),
                code="snapshot_anchor_mismatch",
            )
        # issue #234:factor 通道 screen 运行的快照证据预检 —— 绑定的 draft 产物
        # 必须已有其 RCR 产出的快照进入 factor_snapshot_ids,否则秒级拒绝
        # (没有证据的 screen run 完成后 promote 必失败,提前到入队暴露)。
        if resolution is not None:
            snapshot_gate_error = await screen_factor_snapshot_gate_error(
                session, resolution=resolution, snapshots=snapshots
            )
            if snapshot_gate_error is not None:
                raise ResearchInvalidInputError(
                    snapshot_gate_error, code="screen_snapshot_evidence"
                )

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
                    node.source
                    for node in spec.feature_graph.nodes
                    if node.source is not None
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
            raise ResearchInvalidInputError(
                "运行数据发布候选池为空,拒绝入队: "
                f"{describe_empty_pool(preview, decision_date=primary.end_date)}",
                code="universe_empty",
            )

        # issue #303:入队组合可行性预检(与 MCP 共用同一门控函数)—— 解析生效
        # max_risk_contribution(portfolio_config.overrides 可覆盖,默认 0.35)后,
        # 静态候选池 < ceil(1/阈值) 时秒级 422(附排除统计、生效阈值与键位修复
        # 路径),不再等执行期组合阶段 RiskBudgetError 才 REJECTED;阈值非法值与
        # risk_config.overrides 形态错误同样入队即拒(分区键位:组合约束在
        # portfolio_config,风险退出在 risk_config)。逐期真实买入池入队期不可
        # 精确预知,运行期 fail-closed 兜底(#91)不变。
        portfolio_gate_error = research_portfolio_gate_error(
            preview=preview,
            risk_exit_policy=spec.risk_exit_policy,
            portfolio_overrides=payload.portfolio_config,
            risk_overrides=payload.risk_config,
            decision_date=primary.end_date,
        )
        if portfolio_gate_error is not None:
            raise ResearchInvalidInputError(
                portfolio_gate_error, code="portfolio_infeasible"
            )

        # issue #482:入队政策覆盖预检(与 MCP 共用同一门控函数)——
        # fee_config.overrides 按费用键名合并进生效执行模型,未知键 / 非法值秒级
        # 422;execution_config / validation_config 非空具名拒绝(此前为静默
        # no-op 死分区,覆盖 run 与基线逐位相同)。
        policy_gate_error = research_policy_gate_error(
            execution_model=spec.execution_model,
            fee_overrides=payload.fee_config,
            execution_overrides=payload.execution_config,
            validation_overrides=payload.validation_config,
        )
        if policy_gate_error is not None:
            raise ResearchInvalidInputError(
                policy_gate_error, code="policy_override_invalid"
            )

        run_id = research_run_id_for(payload.idempotency_key)
        try:
            manifest = ResearchRunManifest(
                run_id=run_id,
                idempotency_key=payload.idempotency_key,
                strategy_spec=spec,
                strategy_spec_checksum=spec_checksum,
                dataset_releases=tuple(
                    FrozenArtifactRef(
                        artifact_id=release.release_id,
                        version=release.version,
                        checksum=release.release_checksum,
                        capabilities=tuple(
                            item.key for item in release.capabilities if item.ready
                        ),
                    )
                    for release in releases
                ),
                strategy_version=payload.strategy_version,
                factor_snapshots=tuple(
                    FrozenArtifactRef(
                        artifact_id=snapshot.snapshot_id,
                        version=snapshot.framework_version,
                        checksum=snapshot.checksum,
                        capabilities=tuple(
                            sorted(
                                {
                                    f"factor:{item.feature_name}"
                                    for item in snapshot.observations
                                }
                            )
                        ),
                    )
                    for snapshot in snapshots
                ),
                # issue #360:因子序列冻结引用({series_id, content_checksum});
                # 空集合不传,历史 manifest checksum 零漂移。
                factor_series=(
                    build_factor_series_refs(series_records) if series_records else ()
                ),
                parameters=cast(
                    dict[str, JsonValue],
                    scope_parameters
                    if scope_parameters is not None
                    else payload.parameters,
                ),
                validation_config=cast(
                    dict[str, JsonValue],
                    {
                        "strategy_spec": spec.validation_plan.model_dump(mode="json"),
                        "overrides": payload.validation_config,
                    },
                ),
                portfolio_config=cast(
                    dict[str, JsonValue],
                    {
                        "strategy_spec": spec.portfolio_policy.model_dump(mode="json"),
                        "overrides": payload.portfolio_config,
                    },
                ),
                risk_config=cast(
                    dict[str, JsonValue],
                    {
                        "strategy_spec": spec.risk_exit_policy.model_dump(mode="json"),
                        "overrides": payload.risk_config,
                    },
                ),
                execution_config=cast(
                    dict[str, JsonValue],
                    {
                        "strategy_spec": spec.execution_model.model_dump(mode="json"),
                        "overrides": payload.execution_config,
                    },
                ),
                fee_config=cast(
                    dict[str, JsonValue],
                    {
                        "commission_rate": spec.execution_model.commission_rate,
                        "minimum_commission": spec.execution_model.minimum_commission,
                        "sell_tax_rate": spec.execution_model.sell_tax_rate,
                        "slippage_bps": spec.execution_model.slippage_bps,
                        "overrides": payload.fee_config,
                    },
                ),
                benchmark_config=cast(
                    dict[str, JsonValue],
                    {
                        "symbol": spec.validation_plan.benchmark_symbol,
                        "overrides": payload.benchmark_config,
                    },
                ),
                code_version=payload.code_version,
                initial_capital=payload.initial_capital,
                requested_by=payload.requested_by,
                # issue #312:通道归属由服务端上下文决定 —— REST 默认 human
                # (body 可显式声明 agent),MCP 恒 agent。契约层仍 fail-closed
                # 拒绝 llm。
                actor_type=actor_type,
                # issue #510:scope 解析结论只在声明时冻结;None 时 manifest
                # checksum 与历史逐字节一致。
                data_scope=cast(dict[str, JsonValue] | None, scope_manifest),
            )
        except (ValueError, ValidationError) as exc:
            raise ResearchInvalidInputError(str(exc), code="manifest_invalid") from exc
        return PreflightReport(
            manifest=manifest,
            strategy_row=strategy_row,
            run_id=manifest.run_id,
            manifest_checksum=manifest.checksum,
        )

    # -- 提交(不 commit) -------------------------------------------------- #
    async def submit(
        self,
        payload: ResearchRunSubmitInput,
        ctx: ResearchOperationContext,
    ) -> SubmitOutcome:
        """冻结输入并登记 queued run + 同事务 background_jobs 行(不 commit)。"""

        report = await self.preflight(payload, ctx)
        manifest = report.manifest
        job_row: BackgroundJobModel | None = None
        try:
            row, created = await ResearchRunRepository(self._session).create_or_get(
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
                job_row = await self._enqueue_research_run_job(manifest)
                row.job_id = job_row.job_id
        except (
            # ``SqlAlchemyResearchRunStore.create_or_get`` 把仓储层的
            # ``ResearchRunPersistenceConflictError`` 包装成领域错误
            # ``ResearchRunConflictError``(#513 复核对齐):两条都要收口,
            # 否则业务层会漏出非 ``ResearchOperationError`` 异常。
            ResearchRunConflictError,
            ResearchRunPersistenceConflictError,
            BackgroundJobPersistenceConflictError,
            IntegrityError,
        ) as exc:
            raise ResearchConflictError(str(exc), code="run_conflict") from exc
        return SubmitOutcome(
            run_row=row,
            job_row=job_row,
            created=created,
            manifest=manifest,
        )

    async def replay(
        self,
        run_id: str,
        payload: ResearchRunReplayInput,
        ctx: ResearchOperationContext,
    ) -> SubmitOutcome:
        """复制完整冻结清单为新 queued 运行(不 commit)。

        issue #305:interrupted 源放开为事故恢复通道 —— 新 run 自动继承原
        manifest 全部冻结输入(含 factor_snapshots 全部 ID),血缘标注
        ``replay_of_run_id`` + ``replay_source_status``;cancelled 仍拒绝,
        completed 重放行为不变。
        """

        store = SqlAlchemyResearchRunStore(ResearchRunRepository(self._session))
        source = await store.get(run_id)
        if source is None:
            raise ResearchNotFoundError("源研究运行不存在", code="source_run_not_found")
        if source.status not in REPLAYABLE_SOURCE_STATUSES:
            raise ResearchConflictError(
                replay_guard_error(source.status), code="run_not_replayable"
            )
        manifest = replace(
            source.manifest,
            run_id=research_run_id_for(payload.idempotency_key),
            idempotency_key=payload.idempotency_key,
            requested_by=payload.requested_by,
            # issue #312:通道归属由服务端上下文决定(REST 默认 human,MCP 恒 agent)。
            actor_type=ctx.actor_type,
            replay_of_run_id=run_id,
            replay_source_status=source.status.value,
        )
        job_row: BackgroundJobModel | None = None
        try:
            _record, created = await store.create_or_get(manifest)
            row = await ResearchRunRepository(self._session).get(manifest.run_id)
            if row is None:  # pragma: no cover - create_or_get 已保证存在
                raise ResearchNotFoundError("研究运行不存在", code="run_not_found")
            # issue #143:重放也走双写;background_jobs 用重放后的新 idempotency_key。
            if created and not row.job_id:
                job_row = await self._enqueue_research_run_job(manifest)
                row.job_id = job_row.job_id
        except (
            # ``SqlAlchemyResearchRunStore.create_or_get`` 把仓储层的
            # ``ResearchRunPersistenceConflictError`` 包装成领域错误
            # ``ResearchRunConflictError``(#513 复核对齐):两条都要收口,
            # 否则业务层会漏出非 ``ResearchOperationError`` 异常。
            ResearchRunConflictError,
            ResearchRunPersistenceConflictError,
            BackgroundJobPersistenceConflictError,
            IntegrityError,
        ) as exc:
            raise ResearchConflictError(str(exc), code="run_conflict") from exc
        return SubmitOutcome(
            run_row=row,
            job_row=job_row,
            created=created,
            manifest=manifest,
        )

    # -- 内部 -------------------------------------------------------------- #
    async def _enqueue_research_run_job(
        self, manifest: ResearchRunManifest
    ) -> BackgroundJobModel:
        """在同事务内为研究运行创建 background_jobs 行(issue #143)。"""

        payload: dict[str, object] = {
            "run_id": manifest.run_id,
            "strategy_kind": manifest.strategy_kind,
        }
        job_row, _created = await enqueue_job_core(
            self._session,
            kind="research_run",
            queue="research",
            idempotency_key=manifest.idempotency_key,
            payload=payload,
            requested_by=manifest.requested_by,
            priority=0,
            max_attempts=3,
        )
        return job_row

    async def _enqueue_trading_days(
        self, primary: ResearchDatasetRelease
    ) -> list[date]:
        """入队期读取 bars 主发布交易日历(issue #361;读失败具名 422)。

        仅 custom dates 校验与 multi_period x u_ 因子覆盖检查触达(非必要
        不读发布文件);与 MCP 共用同一读取实现(``enqueue_trading_days``,
        #334 并集日历 + 间隙哨兵)。
        """

        try:
            return await enqueue_trading_days(
                bars_release_id=primary.release_id,
                bars_release_checksum=primary.release_checksum,
                release_root=self._release_root,
            )
        except Exception as exc:
            raise ResearchInvalidInputError(
                "无法读取 bars 主发布交易日历以校验 decision_schedule / 用户因子"
                f" series 覆盖(release {primary.release_id}): {exc}",
                code="release_calendar_unreadable",
            ) from exc


def _scope_gate_error(exc: ScopeGateError) -> ResearchOperationError:
    """把 #510 的 ``ScopeGateError`` 映射进统一错误家族。

    409 只用于「数据已变,需重新预览」(``precondition_failed``),其余为
    ``invalid_input``;``exc.detail``(如 ``actual_release_ids``)原样进
    ``details``,保持既有结构化 409 体不变。
    """

    if exc.status_code == 409:
        return ResearchPreconditionFailedError(
            exc.message, code=exc.code, details=exc.detail
        )
    return ResearchInvalidInputError(exc.message, code=exc.code, details=exc.detail)


def _bars_release(releases: list[ResearchDatasetRelease]) -> ResearchDatasetRelease:
    """取联合发布中的 bars 主发布(issue #187)。

    排除 daily_metrics / financial_indicators 研究数据发布;恰好一个 bars
    发布才有候选池评估意义,否则 422。
    """

    bars_releases = [
        item for item in releases if item.dataset_kind is ReleaseDatasetKind.BARS
    ]
    if len(bars_releases) != 1:
        raise ResearchInvalidInputError(
            "联合发布必须恰好包含一个 bars 主发布(行情/候选池来源): "
            + ", ".join(
                f"{item.release_id}({item.dataset_kind.value})" for item in releases
            ),
            code="bars_release_ambiguous",
        )
    return bars_releases[0]


__all__ = [
    "PreflightReport",
    "ResearchRunReplayInput",
    "ResearchRunSubmissionService",
    "ResearchRunSubmitInput",
    "SubmitOutcome",
    "research_run_id_for",
]
