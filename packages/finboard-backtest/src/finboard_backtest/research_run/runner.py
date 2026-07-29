"""离线 ResearchRun 状态机、血缘编排、恢复和确定性重放。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from finboard_backtest.research_run.adapters import ResearchStrategyAdapter
from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    ResearchArtifact,
    ResearchFillAction,
    ResearchPositionSide,
    ResearchRunConflictError,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunStage,
    ResearchRunStatus,
    UnsupportedResearchCapabilityError,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.store import ResearchRunStore

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
    ResearchRunStage.REBALANCE_PLAN,
    ResearchRunStage.ORDERS,
    ResearchRunStage.FILLS,
    ResearchRunStage.LEDGER,
)


class ResearchRunCoordinator:
    def __init__(self, store: ResearchRunStore) -> None:
        self._store = store

    async def execute(
        self,
        manifest: ResearchRunManifest,
        adapter: ResearchStrategyAdapter,
        *,
        expected_result_checksum: str | None = None,
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

            async for raw_decision in adapter.decisions(manifest):
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
                self._validate_decision(
                    decision,
                    position_quantities=position_quantities,
                    seen_fill_ids=seen_fill_ids,
                )
                await self._persist_decision(manifest.run_id, len(decisions), decision)
                await self._store.checkpoint()
                decisions.append(decision)

            report = adapter.build_report(manifest, decisions)
            self._validate_report(manifest, decisions, report)
            await self._persist_report(manifest.run_id, len(decisions), report)
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
            await self._store.save_result(
                manifest.run_id,
                report=report,
                result_checksum=result_checksum,
            )
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
        except ResearchRunInterruptedError as exc:
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.INTERRUPTED,
                error_code="interrupted",
                error_summary=str(exc),
            )
        except Exception as exc:
            return await self._safe_terminal_transition(
                manifest.run_id,
                target=ResearchRunStatus.FAILED,
                error_code=type(exc).__name__,
                error_summary=str(exc),
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
        self, run_id: str, decision_index: int, decision: DecisionBundle
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
            ResearchRunStage.REBALANCE_PLAN: {"instructions": decision.rebalance_plan},
            ResearchRunStage.ORDERS: {"orders": decision.orders},
            ResearchRunStage.FILLS: {"fills": decision.fills},
            ResearchRunStage.LEDGER: {
                "positions": decision.positions,
                "ledger": decision.ledger,
            },
        }
        parent: tuple[str, ...] = ()
        for offset, stage in enumerate(_DECISION_STAGES):
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
            parent = (trace_id,)

    async def _persist_report(
        self, run_id: str, decision_count: int, report: object
    ) -> None:
        payload = to_json_value({"report": report})
        assert isinstance(payload, dict)
        await self._store.append_artifact(
            ResearchArtifact(
                artifact_id=f"{run_id}:A:report",
                run_id=run_id,
                decision_id=None,
                sequence=decision_count * len(_DECISION_STAGES),
                stage=ResearchRunStage.REPORT,
                trace_id=f"RRT-{stable_checksum('report')[:24]}",
                parent_trace_ids=(),
                payload=payload,
                checksum=stable_checksum(payload),
            )
        )

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
        position_quantities: dict[tuple[str, ResearchPositionSide], Decimal],
        seen_fill_ids: set[str],
    ) -> None:
        order_by_id = {item.research_order_id: item for item in decision.orders}
        fill_quantities: dict[str, Decimal] = defaultdict(Decimal)
        for fill in decision.fills:
            if fill.research_fill_id in seen_fill_ids:
                raise ResearchRunConflictError(f"重复成交: {fill.research_fill_id}")
            seen_fill_ids.add(fill.research_fill_id)
            order = order_by_id.get(fill.research_order_id)
            if order is None:
                raise ResearchRunConflictError(
                    f"成交 {fill.research_fill_id} 找不到研究订单"
                )
            if fill.action is not order.action or fill.symbol != order.symbol:
                raise ResearchRunConflictError("成交与订单方向/标的不一致")
            fill_quantities[order.research_order_id] += fill.quantity
            side, delta = _position_delta(fill.action, fill.quantity)
            key = (fill.symbol, side)
            position_quantities[key] += delta
            if position_quantities[key] < 0:
                raise ResearchRunConflictError(f"{fill.symbol} 成交导致负持仓")
        for order_id, quantity in fill_quantities.items():
            if quantity > order_by_id[order_id].quantity:
                raise ResearchRunConflictError(f"订单 {order_id} 超量成交")

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
        if decisions:
            last = decisions[-1].ledger
            if report.final_equity != last.equity or report.final_cash != last.cash:
                raise ResearchRunConflictError("报告期末权益/现金与账本不一致")
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
