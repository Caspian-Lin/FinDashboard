"""离线 ResearchRun 状态机、血缘编排、恢复和确定性重放。"""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import cast

import structlog

from finboard_backtest.research_run.adapters import ResearchStrategyAdapter
from finboard_backtest.research_run.contracts import (
    RESEARCH_PORTFOLIO_PIPELINE_VERSION,
    DecisionBundle,
    JsonValue,
    ResearchArtifact,
    ResearchConstraintViolationError,
    ResearchFillAction,
    ResearchOrderStatus,
    ResearchPositionSide,
    ResearchRunConflictError,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunStage,
    ResearchRunStatus,
    UnsupportedResearchCapabilityError,
    execution_mode_for,
    pipeline_output_checksum,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.failure_context import (
    FailureContext,
    build_failure_summary,
)
from finboard_backtest.research_run.store import ResearchRunStore
from finboard_data.cache import ParquetReadJobStats, collect_parquet_read_stats

logger = structlog.get_logger(__name__)

_TRANSITIONS: dict[ResearchRunStatus, frozenset[ResearchRunStatus]] = {
    ResearchRunStatus.QUEUED: frozenset(
        {
            ResearchRunStatus.RUNNING,
            ResearchRunStatus.REJECTED,
            ResearchRunStatus.CANCELLED,
        }
    ),
    ResearchRunStatus.RUNNING: frozenset(
        {
            ResearchRunStatus.COMPLETED,
            ResearchRunStatus.FAILED,
            ResearchRunStatus.INTERRUPTED,
            ResearchRunStatus.REJECTED,
            ResearchRunStatus.CANCELLED,
        }
    ),
    ResearchRunStatus.INTERRUPTED: frozenset(
        {ResearchRunStatus.RUNNING, ResearchRunStatus.CANCELLED}
    ),
    ResearchRunStatus.FAILED: frozenset(
        {ResearchRunStatus.RUNNING, ResearchRunStatus.CANCELLED}
    ),
    ResearchRunStatus.COMPLETED: frozenset(),
    ResearchRunStatus.REJECTED: frozenset(),
    ResearchRunStatus.CANCELLED: frozenset(),
}

_DECISION_STAGES = (
    ResearchRunStage.UNIVERSE,
    ResearchRunStage.FEATURES,
    ResearchRunStage.SIGNALS,
    ResearchRunStage.TARGETS_BEFORE_CONSTRAINTS,
    ResearchRunStage.CONSTRAINTS,
    ResearchRunStage.TARGETS_AFTER_CONSTRAINTS,
    ResearchRunStage.RISK_EXITS,
    ResearchRunStage.TARGETS_AFTER_RISK,
    ResearchRunStage.CAPITAL_FEASIBILITY,
    ResearchRunStage.REBALANCE_PLAN,
    ResearchRunStage.ORDERS,
    ResearchRunStage.FILLS,
    ResearchRunStage.LEDGER,
)

#: 单个 decision 内按 _DECISION_STAGES 逐一持久化的工件阶段数(13),与 REPORT
#: 工件一起构成「stage x decision」进度计量单位(issue #188)。决策总数 N 在执行
#: 期才得知,故 total 采用「随新决策被发现而递增」的自修正语义(worker 侧
#: ``update_progress`` 对 total 只增不减,二者兼容)。
DECISION_STAGE_COUNT = len(_DECISION_STAGES)

#: 进度回调钩子:(done, total, phase) → None,与 background_jobs 的
#: ``ProgressCallback`` 结构同构;None 表示不对外上报(内存 / 直连 REST 调用)。
ProgressHook = Callable[[int, int | None, str | None], Awaitable[None]]


@dataclass(slots=True)
class _DecisionTiming:
    """逐决策 execute 耗时聚合(issue #285):min/avg/max + 最慢决策日定位。"""

    durations: list[float] = field(default_factory=list)
    #: 初值 -1.0:首条记录必写入(0.0 耗时也算),平局保持最早的慢决策日。
    slowest_seconds: float = -1.0
    slowest_date: str | None = None

    def record(self, business_date: object, elapsed_seconds: float) -> None:
        self.durations.append(elapsed_seconds)
        if elapsed_seconds > self.slowest_seconds:
            self.slowest_seconds = elapsed_seconds
            self.slowest_date = str(business_date)

    def as_dict(self) -> dict[str, JsonValue]:
        if not self.durations:
            return {
                "count": 0,
                "min_seconds": 0.0,
                "avg_seconds": 0.0,
                "max_seconds": 0.0,
                "slowest_decision_date": None,
            }
        count = len(self.durations)
        total = sum(self.durations)
        return {
            "count": count,
            "min_seconds": round(min(self.durations), 6),
            "avg_seconds": round(total / count, 6),
            "max_seconds": round(max(self.durations), 6),
            "slowest_decision_date": self.slowest_date,
        }


def _build_run_timing(
    *,
    run_started: float,
    decision_load_elapsed: float,
    decision_timing: _DecisionTiming,
    report_elapsed: float,
    parquet_stats: ParquetReadJobStats,
) -> dict[str, JsonValue]:
    """聚合 research_run 分段耗时(issue #285)。

    纯可观测性:不进 report、不进任何 artifact/checksum,只随 ``save_result``
    落到 ``research_runs.result`` JSON 的 ``timing`` 键与 structlog。
    """

    return {
        "total_elapsed_seconds": round(time.monotonic() - run_started, 3),
        "decision_load_elapsed_seconds": round(decision_load_elapsed, 3),
        "decision_execute": decision_timing.as_dict(),
        "report_elapsed_seconds": round(report_elapsed, 3),
        # ParquetReadJobStats.as_dict 的值只有 int/float/str/dict[str, int],
        # 本身就是 JsonValue;finboard-data 不反向依赖研究 contracts,此处收窄。
        "parquet_reads": cast(
            dict[str, JsonValue], parquet_stats.as_dict()
        ),
    }


class ResearchRunCoordinator:
    def __init__(self, store: ResearchRunStore) -> None:
        self._store = store

    async def execute(
        self,
        manifest: ResearchRunManifest,
        adapter: ResearchStrategyAdapter,
        *,
        expected_result_checksum: str | None = None,
        progress: ProgressHook | None = None,
    ) -> ResearchRunRecord:
        record, created = await self._store.create_or_get(manifest)
        await self._store.checkpoint()
        if not created and record.status is ResearchRunStatus.COMPLETED:
            return record
        if not created and record.status in {
            ResearchRunStatus.REJECTED,
            ResearchRunStatus.CANCELLED,
            ResearchRunStatus.RUNNING,
        }:
            return record

        start_states = frozenset(
            {
                ResearchRunStatus.QUEUED,
                ResearchRunStatus.INTERRUPTED,
                ResearchRunStatus.FAILED,
            }
        )
        # issue #263:执行期失败上下文,随循环进度逐点更新;通用收口拼入
        # error_summary 头部(专项错误分支不经此路径,保持 str(exc) 原样)。
        failure_ctx = FailureContext()
        # issue #285:分段耗时(decision_load / 逐决策 execute / report),
        # 只观测,不改变 artifact/checkpoint/进度上报语义。
        run_started = time.monotonic()
        try:
            adapter.validate_manifest(manifest)
            try:
                await self._transition(
                    manifest.run_id,
                    expected=start_states,
                    target=ResearchRunStatus.RUNNING,
                )
            except ResearchRunConflictError:
                current = await self._require_run(manifest.run_id)
                if current.status in {
                    ResearchRunStatus.RUNNING,
                    ResearchRunStatus.COMPLETED,
                }:
                    return current
                raise
            await self._store.checkpoint()
            decisions: list[DecisionBundle] = []
            position_quantities: dict[tuple[str, ResearchPositionSide], Decimal] = (
                defaultdict(Decimal)
            )
            seen_fill_ids: set[str] = set()
            decision_timing = _DecisionTiming()
            decision_load_elapsed = 0.0

            # 输入构建(含 multi_period 全部期次的一次性预构建,issue #170)
            # 发生在首次迭代内,失败时尚无任何决策 —— 决策日期由 signal_engine
            # 的加载期标记补全。async for 等价展开为 __anext__ 手工迭代,把
            # 「产出下一条决策」的耗时计入 decision_load(issue #285)。
            failure_ctx.stage = "decision_load"
            decision_iterator = adapter.decisions(manifest)
            with collect_parquet_read_stats() as parquet_stats:
                while True:
                    load_started = time.monotonic()
                    try:
                        raw_decision = await decision_iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        decision_load_elapsed += time.monotonic() - load_started
                    execute_started = time.monotonic()
                    live_record = await self._store.get(manifest.run_id)
                    if live_record is None:
                        raise ResearchRunConflictError("运行记录在执行中消失")
                    if live_record.status is ResearchRunStatus.CANCELLED:
                        return live_record
                    if live_record.status is not ResearchRunStatus.RUNNING:
                        raise ResearchRunInterruptedError(
                            f"运行状态变为 {live_record.status.value}"
                        )
                    decision = self._with_decision_id(
                        manifest.run_id, len(decisions), raw_decision
                    )
                    failure_ctx.stage = "decision_execute"
                    failure_ctx.decision_date = decision.business_date
                    failure_ctx.decision_index = len(decisions) + 1
                    self._validate_decision(
                        decision,
                        manifest=manifest,
                        position_quantities=position_quantities,
                        seen_fill_ids=seen_fill_ids,
                    )
                    await self._persist_decision(
                        manifest.run_id,
                        len(decisions),
                        decision,
                        progress=progress,
                        failure_ctx=failure_ctx,
                    )
                    await self._store.checkpoint()
                    decisions.append(decision)
                    decision_timing.record(
                        decision.business_date, time.monotonic() - execute_started
                    )

            failure_ctx.stage = "report"
            report_started = time.monotonic()
            report = adapter.build_report(manifest, decisions)
            self._validate_report(manifest, decisions, report)
            await self._persist_report(
                manifest.run_id,
                len(decisions),
                report,
                progress=progress,
                failure_ctx=failure_ctx,
            )
            report_elapsed = time.monotonic() - report_started
            await self._store.checkpoint()
            artifacts = await self._store.list_artifacts(manifest.run_id)
            result_checksum = stable_checksum(
                [
                    {
                        "stage": item.stage.value,
                        "decision_id": _stable_decision_suffix(item.decision_id),
                        "checksum": item.checksum,
                    }
                    for item in artifacts
                ]
            )
            if (
                expected_result_checksum is not None
                and result_checksum != expected_result_checksum
            ):
                return await self._safe_terminal_transition(
                    manifest.run_id,
                    target=ResearchRunStatus.FAILED,
                    error_code="non_deterministic_replay",
                    error_summary=(
                        f"重放结果 {result_checksum} 与源结果"
                        f" {expected_result_checksum} 不一致"
                    ),
                )
            timing = _build_run_timing(
                run_started=run_started,
                decision_load_elapsed=decision_load_elapsed,
                decision_timing=decision_timing,
                report_elapsed=report_elapsed,
                parquet_stats=parquet_stats,
            )
            await self._store.save_result(
                manifest.run_id,
                report=report,
                result_checksum=result_checksum,
                timing=timing,
            )
            logger.info("research_run.timing", run_id=manifest.run_id, **timing)
            await self._store.checkpoint()
            completed = await self._transition(
                manifest.run_id,
                expected=frozenset({ResearchRunStatus.RUNNING}),
                target=ResearchRunStatus.COMPLETED,
            )
            await self._store.checkpoint()
            return completed
        except UnsupportedResearchCapabilityError as exc:
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.REJECTED,
                error_code="unsupported_capability",
                error_summary=str(exc),
            )
        except ResearchConstraintViolationError as exc:
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.REJECTED,
                error_code="hard_constraint_rejected",
                error_summary=str(exc),
            )
        except ResearchRunInterruptedError as exc:
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.INTERRUPTED,
                error_code="interrupted",
                error_summary=str(exc),
            )
        except Exception as exc:
            # issue #263:error_code 保持异常类型名不变;error_summary 头部
            # 拼结构化定位上下文(stage / 决策日期 / 发布绑定),加载期失败
            # 由 signal_engine 挂载的标记补全精确决策日。无上下文(循环前
            # 失败)时与既有行为一致,返回 str(exc) 原文。
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.FAILED,
                error_code=type(exc).__name__,
                error_summary=build_failure_summary(exc, failure_ctx, manifest),
            )

    async def cancel(self, run_id: str) -> ResearchRunRecord:
        record = await self._require_run(run_id)
        if record.status is ResearchRunStatus.CANCELLED:
            return record
        cancelled = await self._transition(
            run_id,
            expected=frozenset(
                {
                    ResearchRunStatus.QUEUED,
                    ResearchRunStatus.RUNNING,
                    ResearchRunStatus.INTERRUPTED,
                    ResearchRunStatus.FAILED,
                }
            ),
            target=ResearchRunStatus.CANCELLED,
        )
        await self._store.checkpoint()
        return cancelled

    async def mark_stale_running_as_interrupted(self) -> list[ResearchRunRecord]:
        stale = await self._store.list_by_status({ResearchRunStatus.RUNNING})
        recovered: list[ResearchRunRecord] = []
        for record in stale:
            recovered.append(
                await self._transition(
                    record.manifest.run_id,
                    expected=frozenset({ResearchRunStatus.RUNNING}),
                    target=ResearchRunStatus.INTERRUPTED,
                    error_code="process_restart",
                    error_summary="进程重启:已保留 checkpoint,可按冻结输入恢复",
                )
            )
            await self._store.checkpoint()
        return recovered

    async def replay(
        self,
        *,
        source_run_id: str,
        new_run_id: str,
        idempotency_key: str,
        requested_by: str,
        adapter: ResearchStrategyAdapter,
    ) -> ResearchRunRecord:
        source = await self._require_run(source_run_id)
        if source.status is not ResearchRunStatus.COMPLETED:
            raise ResearchRunConflictError("仅允许重放已完成运行")
        manifest = replace(
            source.manifest,
            run_id=new_run_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            replay_of_run_id=source_run_id,
        )
        assert source.result_checksum is not None
        return await self.execute(
            manifest,
            adapter,
            expected_result_checksum=source.result_checksum,
        )

    async def lineage(
        self, run_id: str, trace_id: str
    ) -> list[ResearchArtifact]:
        artifacts = await self._store.list_artifacts(run_id)
        by_trace = {item.trace_id: item for item in artifacts}
        if trace_id not in by_trace:
            return []
        pending = [trace_id]
        found: dict[str, ResearchArtifact] = {}
        while pending:
            current = pending.pop()
            if current in found:
                continue
            artifact = by_trace.get(current)
            if artifact is None:
                continue
            found[current] = artifact
            pending.extend(artifact.parent_trace_ids)
        return sorted(found.values(), key=lambda item: item.sequence)

    async def _persist_decision(
        self,
        run_id: str,
        decision_index: int,
        decision: DecisionBundle,
        *,
        progress: ProgressHook | None = None,
        failure_ctx: FailureContext | None = None,
    ) -> None:
        stage_payloads: dict[ResearchRunStage, dict[str, object]] = {
            ResearchRunStage.UNIVERSE: {"candidates": decision.candidates},
            ResearchRunStage.FEATURES: {"features": decision.features},
            ResearchRunStage.SIGNALS: {"signals": decision.signals},
            ResearchRunStage.TARGETS_BEFORE_CONSTRAINTS: {
                "targets": decision.targets_before_constraints
            },
            ResearchRunStage.CONSTRAINTS: {"constraints": decision.constraints},
            ResearchRunStage.TARGETS_AFTER_CONSTRAINTS: {
                "targets": decision.targets_after_constraints
            },
            ResearchRunStage.RISK_EXITS: {
                "outcomes": decision.risk_exits,
                "state": decision.risk_state,
            },
            ResearchRunStage.TARGETS_AFTER_RISK: {
                "targets": decision.targets_after_risk
            },
            ResearchRunStage.CAPITAL_FEASIBILITY: {
                "tiers": decision.capital_feasibility
            },
            ResearchRunStage.REBALANCE_PLAN: {"instructions": decision.rebalance_plan},
            ResearchRunStage.ORDERS: {"orders": decision.orders},
            ResearchRunStage.FILLS: {"fills": decision.fills},
            ResearchRunStage.LEDGER: {
                "positions": decision.positions,
                "ledger": decision.ledger,
                "pipeline_evidence": decision.pipeline_evidence,
            },
        }
        parent: tuple[str, ...] = ()
        for offset, stage in enumerate(_DECISION_STAGES):
            if failure_ctx is not None:
                failure_ctx.stage = stage.value
            payload_value = to_json_value(
                {
                    "business_date": decision.business_date,
                    "decision_at": decision.decision_at,
                    **stage_payloads[stage],
                }
            )
            assert isinstance(payload_value, dict)
            payload_checksum = stable_checksum(payload_value)
            stable_suffix = f"{decision_index:08d}:{stage.value}"
            trace_id = f"RRT-{stable_checksum(stable_suffix)[:24]}"
            artifact = ResearchArtifact(
                artifact_id=f"{run_id}:A:{stable_suffix}",
                run_id=run_id,
                decision_id=decision.decision_id,
                sequence=decision_index * len(_DECISION_STAGES) + offset,
                stage=stage,
                trace_id=trace_id,
                parent_trace_ids=parent,
                payload=payload_value,
                checksum=payload_checksum,
            )
            await self._store.append_artifact(artifact)
            if progress is not None:
                # 单位 = 已完成的 (decision x stage) 工件数;total 随当前已发现的
                # 决策递增(见 DECISION_STAGE_COUNT 注释)。
                done = decision_index * DECISION_STAGE_COUNT + offset + 1
                total = (decision_index + 1) * DECISION_STAGE_COUNT
                await _report_progress(
                    progress, done, total, f"research_run:{stage.value}"
                )
            parent = (trace_id,)

    async def _persist_report(
        self,
        run_id: str,
        decision_count: int,
        report: object,
        *,
        progress: ProgressHook | None = None,
        failure_ctx: FailureContext | None = None,
    ) -> None:
        if failure_ctx is not None:
            failure_ctx.stage = ResearchRunStage.REPORT.value
        payload = to_json_value({"report": report})
        assert isinstance(payload, dict)
        await self._store.append_artifact(
            ResearchArtifact(
                artifact_id=f"{run_id}:A:report",
                run_id=run_id,
                decision_id=None,
                sequence=decision_count * DECISION_STAGE_COUNT,
                stage=ResearchRunStage.REPORT,
                trace_id=f"RRT-{stable_checksum('report')[:24]}",
                parent_trace_ids=(),
                payload=payload,
                checksum=stable_checksum(payload),
            )
        )
        if progress is not None:
            done = decision_count * DECISION_STAGE_COUNT + 1
            await _report_progress(progress, done, done, "research_run:report")

    @staticmethod
    def _with_decision_id(
        run_id: str, index: int, decision: DecisionBundle
    ) -> DecisionBundle:
        expected = f"{run_id}:D:{index:08d}"
        if decision.decision_id and decision.decision_id != expected:
            raise ResearchRunConflictError(
                f"decision_id 应为 {expected},收到 {decision.decision_id}"
            )
        return replace(decision, decision_id=expected)

    @staticmethod
    def _validate_decision(
        decision: DecisionBundle,
        *,
        manifest: ResearchRunManifest,
        position_quantities: dict[tuple[str, ResearchPositionSide], Decimal],
        seen_fill_ids: set[str],
    ) -> None:
        evidence = decision.pipeline_evidence
        if evidence is None:
            raise ResearchConstraintViolationError(
                "缺少正式组合流水线证据,禁止跳过目标/约束/退出/sizing 阶段"
            )
        if evidence.pipeline_version != RESEARCH_PORTFOLIO_PIPELINE_VERSION:
            raise ResearchConstraintViolationError(
                f"不支持的组合流水线版本: {evidence.pipeline_version}"
            )
        if evidence.manifest_input_checksum != manifest.input_checksum:
            raise ResearchConstraintViolationError(
                "组合流水线证据未绑定当前冻结 manifest"
            )
        if evidence.output_checksum != pipeline_output_checksum(decision):
            raise ResearchConstraintViolationError("组合流水线产物校验和不一致")
        if not decision.constraints:
            raise ResearchConstraintViolationError("缺少组合约束阶段")
        failed_hard = [
            item.constraint
            for item in decision.constraints
            if item.hard and not item.passed
        ]
        if evidence.hard_constraints_passed != (not failed_hard):
            raise ResearchConstraintViolationError(
                "流水线证据的硬约束状态与约束产物不一致"
            )
        if failed_hard:
            raise ResearchConstraintViolationError(
                f"硬约束未通过,禁止生成研究订单或晋级: {failed_hard}"
            )
        tiers = {item.tier for item in decision.capital_feasibility}
        if tiers != {"100k", "200k", "500k"}:
            raise ResearchConstraintViolationError(
                f"资金可行性必须覆盖 100k/200k/500k,实际为 {sorted(tiers)}"
            )
        feasibility_checksums = {
            item.input_checksum for item in decision.capital_feasibility
        }
        if len(feasibility_checksums) != 1:
            raise ResearchConstraintViolationError(
                "三个资金档位必须引用同一冻结可行性输入"
            )
        candidate_symbols = {
            item.symbol for item in decision.candidates if item.included
        }
        signal_symbols = {item.symbol for item in decision.signals}
        if not signal_symbols <= candidate_symbols:
            raise ResearchConstraintViolationError(
                "信号包含未通过候选池筛选的标的"
            )
        target_symbols = {
            item.symbol
            for item in (
                *decision.targets_before_constraints,
                *decision.targets_after_constraints,
                *decision.targets_after_risk,
            )
        }
        if not target_symbols <= signal_symbols:
            raise ResearchConstraintViolationError(
                "目标仓位包含没有标准化信号的标的"
            )

        instruction_by_id = {
            item.instruction_id: item for item in decision.rebalance_plan
        }
        if len(instruction_by_id) != len(decision.rebalance_plan):
            raise ResearchRunConflictError("研究调仓指令不允许重复")
        order_by_id = {item.research_order_id: item for item in decision.orders}
        orders_by_instruction: dict[str, list[object]] = defaultdict(list)
        for order in decision.orders:
            instruction = instruction_by_id.get(order.instruction_id)
            if instruction is None:
                raise ResearchRunConflictError(
                    f"研究订单 {order.research_order_id} 找不到调仓指令"
                )
            if order.symbol != instruction.symbol or order.action is not instruction.action:
                raise ResearchRunConflictError("研究订单与调仓指令方向/标的不一致")
            if order.quantity > abs(instruction.delta_quantity):
                raise ResearchRunConflictError("研究订单数量超过调仓指令数量")
            orders_by_instruction[order.instruction_id].append(order)
        missing_orders = sorted(set(instruction_by_id) - set(orders_by_instruction))
        if missing_orders:
            raise ResearchRunConflictError(
                f"调仓指令缺少研究订单证据: {missing_orders}"
            )
        if any(len(items) != 1 for items in orders_by_instruction.values()):
            raise ResearchRunConflictError("第一阶段每条调仓指令必须对应唯一研究订单")
        fill_quantities: dict[str, Decimal] = defaultdict(Decimal)
        for fill in decision.fills:
            if fill.research_fill_id in seen_fill_ids:
                raise ResearchRunConflictError(f"重复成交: {fill.research_fill_id}")
            seen_fill_ids.add(fill.research_fill_id)
            matched_order = order_by_id.get(fill.research_order_id)
            if matched_order is None:
                raise ResearchRunConflictError(
                    f"成交 {fill.research_fill_id} 找不到研究订单"
                )
            if (
                fill.action is not matched_order.action
                or fill.symbol != matched_order.symbol
            ):
                raise ResearchRunConflictError("成交与订单方向/标的不一致")
            fill_quantities[matched_order.research_order_id] += fill.quantity
            side, delta = _position_delta(fill.action, fill.quantity)
            key = (fill.symbol, side)
            position_quantities[key] += delta
            if position_quantities[key] < 0:
                raise ResearchRunConflictError(f"{fill.symbol} 成交导致负持仓")
        for order_id, quantity in fill_quantities.items():
            if quantity > order_by_id[order_id].quantity:
                raise ResearchRunConflictError(f"订单 {order_id} 超量成交")
        for order in decision.orders:
            filled = fill_quantities.get(order.research_order_id, Decimal())
            if order.status is ResearchOrderStatus.FILLED and filled != order.quantity:
                raise ResearchRunConflictError("FILLED 研究订单的成交数量必须等于委托数量")
            if order.status is ResearchOrderStatus.PARTIALLY_FILLED and not (
                Decimal() < filled < order.quantity
            ):
                raise ResearchRunConflictError(
                    "PARTIALLY_FILLED 研究订单必须存在小于委托量的正成交"
                )
            if order.status is ResearchOrderStatus.REJECTED and filled != 0:
                raise ResearchRunConflictError("REJECTED 研究订单不能存在成交")

        reported = {
            (item.symbol, item.position_side): item.quantity
            for item in decision.positions
            if item.quantity != 0
        }
        expected = {
            key: quantity for key, quantity in position_quantities.items() if quantity != 0
        }
        if reported != expected:
            raise ResearchRunConflictError(
                f"持仓必须由成交驱动: expected={expected}, reported={reported}"
            )
        market_value = sum((item.market_value for item in decision.positions), Decimal())
        if abs(market_value - decision.ledger.market_value) > Decimal("0.01"):
            raise ResearchRunConflictError("ledger.market_value 与持仓市值不一致")
        if decision.ledger.realized_pnl != sum(
            (item.realized_pnl for item in decision.positions), Decimal()
        ):
            raise ResearchRunConflictError("已实现盈亏与持仓归集不一致")
        if decision.ledger.unrealized_pnl != sum(
            (item.unrealized_pnl for item in decision.positions), Decimal()
        ):
            raise ResearchRunConflictError("未实现盈亏与持仓归集不一致")

    @staticmethod
    def _validate_report(
        manifest: ResearchRunManifest,
        decisions: list[DecisionBundle],
        report: object,
    ) -> None:
        from finboard_backtest.research_run.contracts import ResearchRunReport

        if not isinstance(report, ResearchRunReport):
            raise ResearchRunConflictError("适配器必须返回 ResearchRunReport")
        if report.strategy_kind != manifest.strategy_kind:
            raise ResearchRunConflictError("报告策略类型与 manifest 不一致")
        if report.decision_count != len(decisions):
            raise ResearchRunConflictError("报告决策数量不一致")
        if report.order_count != sum(len(item.orders) for item in decisions):
            raise ResearchRunConflictError("报告订单数量不一致")
        if report.fill_count != sum(len(item.fills) for item in decisions):
            raise ResearchRunConflictError("报告成交数量不一致")
        if report.execution_mode is not execution_mode_for(manifest.parameters):
            raise ResearchRunConflictError("报告执行模式与 manifest 参数不一致")
        if decisions:
            last = decisions[-1].ledger
            if report.equity_curve:
                # 多期回放:期末权益按全区间每日权益曲线最后一个交易日计值
                # (行情会持续到发布末端,可能晚于最后一次成交执行日)。
                if report.final_equity != report.equity_curve[-1].equity:
                    raise ResearchRunConflictError("报告期末权益与权益曲线不一致")
            elif report.final_equity != last.equity:
                raise ResearchRunConflictError("报告期末权益与账本不一致")
            if report.final_cash != last.cash:
                raise ResearchRunConflictError("报告期末现金与账本不一致")
            if report.commission_paid != last.fees_paid:
                raise ResearchRunConflictError("报告佣金与账本不一致")
            if report.tax_paid != last.tax_paid:
                raise ResearchRunConflictError("报告税费与账本不一致")
            if report.slippage_paid != last.slippage_paid:
                raise ResearchRunConflictError("报告滑点与账本不一致")
            if report.fill_shortfall != last.fill_shortfall:
                raise ResearchRunConflictError("报告未成交缺口与账本不一致")
        if not report.accounting_invariants_passed:
            raise ResearchRunConflictError("会计恒等式未通过")

    async def _safe_terminal_transition(
        self,
        run_id: str,
        *,
        target: ResearchRunStatus,
        error_code: str,
        error_summary: str,
    ) -> ResearchRunRecord:
        record = await self._require_run(run_id)
        if record.status is ResearchRunStatus.CANCELLED:
            return record
        expected = frozenset(
            status
            for status, targets in _TRANSITIONS.items()
            if target in targets
        )
        if record.status not in expected:
            return record
        transitioned = await self._transition(
            run_id,
            expected=expected,
            target=target,
            error_code=error_code,
            error_summary=error_summary,
        )
        await self._store.checkpoint()
        return transitioned

    async def _transition(
        self,
        run_id: str,
        *,
        expected: frozenset[ResearchRunStatus],
        target: ResearchRunStatus,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunRecord:
        for source in expected:
            if target not in _TRANSITIONS[source]:
                raise ResearchRunConflictError(
                    f"非法状态迁移: {source.value} -> {target.value}"
                )
        return await self._store.transition(
            run_id,
            expected=expected,
            target=target,
            error_code=error_code,
            error_summary=error_summary,
        )

    async def _require_run(self, run_id: str) -> ResearchRunRecord:
        record = await self._store.get(run_id)
        if record is None:
            raise ResearchRunConflictError(f"研究运行不存在: {run_id}")
        return record


async def _report_progress(
    progress: ProgressHook,
    done: int,
    total: int,
    phase: str,
) -> None:
    """尽力而为上报进度;上报失败不得影响运行状态机(仅可观测性,issue #188)。

    ``asyncio.CancelledError``(BaseException)不被 suppress 捕获,协作式取消仍能
    透传;其余错误(如任务行被并发删除)静默忽略。
    """

    with contextlib.suppress(Exception):
        await progress(done, total, phase)


def _position_delta(
    action: ResearchFillAction, quantity: Decimal
) -> tuple[ResearchPositionSide, Decimal]:
    if action is ResearchFillAction.OPEN_LONG:
        return ResearchPositionSide.LONG, quantity
    if action is ResearchFillAction.CLOSE_LONG:
        return ResearchPositionSide.LONG, -quantity
    if action is ResearchFillAction.OPEN_SHORT:
        return ResearchPositionSide.SHORT, quantity
    return ResearchPositionSide.SHORT, -quantity


def _stable_decision_suffix(decision_id: str | None) -> str | None:
    if decision_id is None:
        return None
    marker = ":D:"
    return decision_id.split(marker, 1)[1] if marker in decision_id else decision_id


__all__ = ["ResearchRunCoordinator"]
