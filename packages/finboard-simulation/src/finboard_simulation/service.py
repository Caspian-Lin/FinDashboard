"""持久化模拟交易应用服务(issue #83)。

链路固定为:

``published no-code strategy + completed ResearchRun → structured targets
→ simulation risk/reservation → simulation order manager → matching
→ fill → simulation position/account/ledger``。

本模块没有 BrokerAdapter 参数, 也不导入实盘交易内核。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import distinct, func, select

from finboard_backtest.research_code import is_promoted_artifact, promotion_status
from finboard_persistence.models import (
    SimulationAccountModel,
    SimulationDecisionModel,
    SimulationFillModel,
    SimulationLedgerModel,
    SimulationMarketEventModel,
    SimulationOrderModel,
    SimulationPositionModel,
    SimulationSessionModel,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    BarPeriod,
    InstrumentType,
    Market,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from finboard_simulation.contracts import (
    SIMULATION_MODE,
    SimulationAccountStatus,
    SimulationBarEvent,
    SimulationConfig,
    SimulationConflictError,
    SimulationDecision,
    SimulationIsolationError,
    SimulationMarketEventStatus,
    SimulationNotFoundError,
    SimulationPositionEffect,
    SimulationProcessResult,
    SimulationPromotionStatus,
    SimulationRiskError,
    SimulationSessionStatus,
    SimulationSourceMode,
    SimulationTarget,
    SimulationTransitionError,
    new_simulation_id,
    stable_checksum,
)
from finboard_simulation.matching import (
    MatchAction,
    PendingOrder,
    match_order,
)
from finboard_simulation.repository import SimulationRepository
from finboard_simulation.rules import (
    SimulationAssetRule,
    resolve_simulation_rule,
)

_ACTIVE_ORDER_STATUSES = ("submitted", "acknowledged", "partially_filled")
_TERMINAL_SESSION_STATUSES = (
    SimulationSessionStatus.STOPPED.value,
    SimulationSessionStatus.ARCHIVED.value,
)
_ZERO = Decimal("0")
_MONEY_QUANT = Decimal("0.00000001")


class SimulationService:
    def __init__(self, repository: SimulationRepository) -> None:
        self.repo = repository

    async def create_account(
        self,
        *,
        name: str,
        initial_cash: Decimal,
        actor: str,
        currency: str = "CNY",
    ) -> SimulationAccountModel:
        if not name.strip() or not actor.strip():
            raise ValueError("模拟账户名称与 actor 不能为空")
        if initial_cash <= 0:
            raise ValueError("模拟初始资金必须为正")
        if currency != "CNY":
            raise ValueError("第一阶段模拟账户仅支持 CNY")
        account = await self.repo.add_account(
            account_id=new_simulation_id("A"),
            name=name.strip(),
            initial_cash=_money(initial_cash),
            currency=currency,
        )
        payload: dict[str, object] = {
            "mode": SIMULATION_MODE,
            "initial_cash": str(account.initial_cash),
            "currency": account.currency,
        }
        await self.repo.append_audit(
            audit_id=new_simulation_id("X"),
            event_key=f"{account.simulation_account_id}:account_created",
            account_id=account.simulation_account_id,
            session_id=None,
            sequence=0,
            actor=actor,
            action="account_created",
            target=account.simulation_account_id,
            payload=payload,
            checksum=stable_checksum(payload),
        )
        return account

    async def create_session(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_version: int,
        validation_run_id: str,
        data_release_id: str,
        source_mode: SimulationSourceMode,
        config: SimulationConfig,
        actor: str,
        reset_of_session_id: str | None = None,
    ) -> SimulationSessionModel:
        account = await self._account(account_id, for_update=True)
        self._require_active_account(account)
        if not actor:
            raise ValueError("actor 不能为空")
        existing = await self.repo.list_sessions(
            account_id=account_id,
            statuses=(
                SimulationSessionStatus.CREATED.value,
                SimulationSessionStatus.RUNNING.value,
                SimulationSessionStatus.PAUSED.value,
            ),
            limit=1,
        )
        if existing:
            raise SimulationConflictError("同一模拟账户只能有一个活动会话")

        strategy = await self.repo.get_strategy_version(strategy_id, strategy_version)
        if strategy is None:
            raise SimulationNotFoundError("研究策略版本不存在")
        if strategy.published_at is None or strategy.validation_errors:
            raise SimulationIsolationError("模拟会话只能引用已发布且校验通过的策略版本")

        run = await self.repo.get_research_run(validation_run_id)
        if run is None:
            raise SimulationNotFoundError("机器验证 ResearchRun 不存在")
        if run.status != "completed" or run.result is None:
            raise SimulationIsolationError("模拟会话只能引用已完成的机器验证运行")
        manifest = run.manifest
        if str(manifest.get("strategy_spec_checksum")) != strategy.checksum:
            raise SimulationIsolationError("机器验证使用的策略 checksum 与会话版本不一致")
        manifest_spec = manifest.get("strategy_spec")
        if (
            not isinstance(manifest_spec, dict)
            or str(manifest_spec.get("strategy_id")) != strategy_id
        ):
            raise SimulationIsolationError("机器验证与模拟策略不一致")
        code_artifact_ref = manifest_spec.get("code_artifact")
        if manifest_spec.get("strategy_kind") == "user_code":
            if not isinstance(code_artifact_ref, dict):
                raise SimulationIsolationError("user_code 模拟策略缺少冻结 code_artifact 引用")
            artifact_name = code_artifact_ref.get("name")
            artifact_commit = code_artifact_ref.get("commit")
            if not isinstance(artifact_name, str) or not isinstance(artifact_commit, str):
                raise SimulationIsolationError(
                    "user_code 模拟策略必须冻结 code_artifact.name 与 commit"
                )
            code_artifact = await self.repo.get_code_artifact(
                kind="strategy", name=artifact_name, commit=artifact_commit
            )
            if not is_promoted_artifact(code_artifact):
                state = (
                    "不存在"
                    if code_artifact is None
                    else f"status={code_artifact.status} "
                    f"promotion_status={promotion_status(code_artifact)}"
                )
                raise SimulationIsolationError(
                    "user_code artifact 未通过 screen + #57 OOS 晋级门,"
                    f"禁止进入模拟盘: {artifact_name}@{artifact_commit[:12]}({state})"
                )
        release_ids = {
            str(item.get("artifact_id"))
            for item in cast(list[dict[str, object]], manifest.get("dataset_releases", []))
        }
        if data_release_id not in release_ids:
            raise SimulationIsolationError("模拟数据发布未包含在机器验证冻结清单中")

        session = await self.repo.add_session(
            session_id=new_simulation_id("S"),
            account_id=account_id,
            source_mode=source_mode.value,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            strategy_checksum=strategy.checksum,
            validation_run_id=validation_run_id,
            data_release_id=data_release_id,
            config=config.as_dict(),
            reset_of_session_id=reset_of_session_id,
        )
        await self._audit(
            session,
            actor=actor,
            action="session_created",
            target=session.simulation_session_id,
            payload={
                "mode": SIMULATION_MODE,
                "source_mode": source_mode.value,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "validation_run_id": validation_run_id,
                "data_release_id": data_release_id,
                "code_artifact": code_artifact_ref,
                "reset_of_session_id": reset_of_session_id,
            },
        )
        return session

    async def transition_session(
        self,
        session_id: str,
        *,
        target: SimulationSessionStatus,
        actor: str,
    ) -> SimulationSessionModel:
        session = await self._session(session_id, for_update=True)
        account = await self._account(session.simulation_account_id, for_update=True)
        self._require_active_account(account)
        current = SimulationSessionStatus(session.status)
        allowed: dict[SimulationSessionStatus, frozenset[SimulationSessionStatus]] = {
            SimulationSessionStatus.RUNNING: frozenset(
                {
                    SimulationSessionStatus.CREATED,
                    SimulationSessionStatus.PAUSED,
                }
            ),
            SimulationSessionStatus.PAUSED: frozenset({SimulationSessionStatus.RUNNING}),
            SimulationSessionStatus.STOPPED: frozenset(
                {
                    SimulationSessionStatus.CREATED,
                    SimulationSessionStatus.RUNNING,
                    SimulationSessionStatus.PAUSED,
                }
            ),
            SimulationSessionStatus.ARCHIVED: frozenset({SimulationSessionStatus.STOPPED}),
            SimulationSessionStatus.CREATED: frozenset(),
        }
        if current not in allowed[target]:
            raise SimulationTransitionError(f"模拟会话不能从 {current.value} 转为 {target.value}")
        now = datetime.now(UTC)
        session.status = target.value
        session.updated_at = now
        if target is SimulationSessionStatus.RUNNING:
            session.started_at = session.started_at or now
            session.paused_at = None
        elif target is SimulationSessionStatus.PAUSED:
            session.paused_at = now
        elif target is SimulationSessionStatus.STOPPED:
            await self._cancel_all_active(session, account, actor=actor, reason="session_stopped")
            await self._revalue_account(account)
            session.stopped_at = now
        elif target is SimulationSessionStatus.ARCHIVED:
            session.archived_at = now
            account.status = SimulationAccountStatus.ARCHIVED.value
            account.archived_at = now
        await self._audit(
            session,
            actor=actor,
            action=f"session_{target.value}",
            target=session_id,
            payload={"from": current.value, "to": target.value},
        )
        return session

    async def reset_session(
        self,
        session_id: str,
        *,
        actor: str,
        initial_cash: Decimal | None = None,
    ) -> tuple[SimulationAccountModel, SimulationSessionModel]:
        source = await self._session(session_id, for_update=True)
        if source.status not in _TERMINAL_SESSION_STATUSES:
            raise SimulationTransitionError("只有停止或归档会话可以基于新账户重置")
        old_account = await self._account(source.simulation_account_id)
        capital = initial_cash if initial_cash is not None else old_account.initial_cash
        new_account = await self.create_account(
            name=f"{old_account.name} reset",
            initial_cash=capital,
            actor=actor,
            currency=old_account.currency,
        )
        new_session = await self.create_session(
            account_id=new_account.simulation_account_id,
            strategy_id=source.strategy_id,
            strategy_version=source.strategy_version,
            validation_run_id=source.validation_run_id,
            data_release_id=source.data_release_id,
            source_mode=SimulationSourceMode(source.source_mode),
            config=SimulationConfig.from_dict(source.config),
            actor=actor,
            reset_of_session_id=source.simulation_session_id,
        )
        await self._audit(
            source,
            actor=actor,
            action="session_reset_child_created",
            target=new_session.simulation_session_id,
            payload={
                "new_account_id": new_account.simulation_account_id,
                "new_session_id": new_session.simulation_session_id,
            },
        )
        return new_account, new_session

    async def submit_decision(
        self,
        session_id: str,
        decision: SimulationDecision,
    ) -> tuple[SimulationDecisionModel, tuple[SimulationOrderModel, ...], bool]:
        session = await self._session(session_id, for_update=True)
        if session.status != SimulationSessionStatus.RUNNING.value:
            raise SimulationTransitionError("只有 running 模拟会话可以接收策略决策")
        account = await self._account(session.simulation_account_id, for_update=True)
        self._require_active_account(account)
        if decision.source_run_id != session.validation_run_id:
            raise SimulationIsolationError("决策来源必须等于会话批准的机器验证运行")

        existing = await self.repo.get_decision(session_id, decision.decision_id)
        if existing is not None:
            if existing.checksum != decision.checksum:
                raise SimulationConflictError("相同 decision_id 对应不同目标仓位")
            existing_orders = await self.repo.list_orders(session_id, limit=10000)
            return (
                existing,
                tuple(item for item in existing_orders if item.decision_id == decision.decision_id),
                True,
            )

        traces = {item.signal_trace_id for item in decision.targets}
        found = await self.repo.signal_traces(
            run_id=decision.source_run_id,
            source_decision_id=decision.source_decision_id,
            trace_ids=traces,
        )
        if found != traces:
            raise SimulationIsolationError(
                f"决策引用的信号 trace 不完整: missing={sorted(traces - found)}"
            )
        payload: dict[str, object] = {
            "decision_id": decision.decision_id,
            "source_run_id": decision.source_run_id,
            "source_decision_id": decision.source_decision_id,
            "targets": [item.as_dict() for item in decision.targets],
            "actor": decision.actor,
        }
        decision_row = await self.repo.add_decision(
            session_id=session_id,
            decision_id=decision.decision_id,
            source_run_id=decision.source_run_id,
            source_decision_id=decision.source_decision_id,
            signal_trace_ids=sorted(traces),
            payload=payload,
            checksum=decision.checksum,
        )
        orders: list[SimulationOrderModel] = []
        for index, target in enumerate(decision.targets):
            order = await self._target_to_order(
                session=session,
                account=account,
                decision=decision,
                target=target,
                intent_index=index,
            )
            if order is not None:
                orders.append(order)
        await self._revalue_account(account)
        await self._audit(
            session,
            actor=decision.actor,
            action="strategy_decision",
            target=decision.decision_id,
            payload={
                "source_run_id": decision.source_run_id,
                "source_decision_id": decision.source_decision_id,
                "order_ids": [item.simulation_order_id for item in orders],
            },
        )
        return decision_row, tuple(orders), False

    async def process_bar(
        self,
        session_id: str,
        event: SimulationBarEvent,
    ) -> SimulationProcessResult:
        session = await self._session(session_id, for_update=True)
        if session.status != SimulationSessionStatus.RUNNING.value:
            raise SimulationTransitionError("只有 running 模拟会话可以推进行情")
        account = await self._account(session.simulation_account_id, for_update=True)
        self._require_active_account(account)
        existing = await self.repo.get_market_event(
            session_id, event.source_event_id, for_update=True
        )
        if existing is not None:
            if existing.checksum != event.checksum:
                raise SimulationConflictError("相同 source_event_id 对应不同 Bar")
            if existing.status == SimulationMarketEventStatus.PROCESSED.value:
                duplicate_clock_at = _clock_at(session) or event.bar.timestamp
                return SimulationProcessResult(
                    source_event_id=event.source_event_id,
                    duplicate=True,
                    fill_ids=(),
                    rejected_order_ids=(),
                    equity=account.equity,
                    clock_at=duplicate_clock_at,
                )
            existing.status = SimulationMarketEventStatus.PROCESSING.value
            existing.error_summary = None
            market_event = existing
        else:
            market_event = await self.repo.add_market_event(
                simulation_session_id=session_id,
                source_event_id=event.source_event_id,
                checksum=event.checksum,
                status=SimulationMarketEventStatus.PROCESSING.value,
                symbol=event.bar.symbol.code,
                market=event.bar.symbol.market.value,
                timestamp=event.bar.timestamp,
                payload=event.as_dict(),
            )

        current_at = _clock_at(session)
        if current_at is not None and event.bar.timestamp < current_at:
            raise SimulationConflictError("模拟市场时钟禁止倒退")
        previous = await self.repo.latest_market_event(
            session_id,
            event.bar.symbol.code,
            before=event.bar.timestamp,
        )
        previous_close = Decimal(str(previous.payload["close"])) if previous is not None else None
        orders = await self.repo.list_orders(
            session_id,
            statuses=_ACTIVE_ORDER_STATUSES,
            symbol=event.bar.symbol.code,
            limit=10000,
            for_update=True,
        )
        fill_ids: list[str] = []
        rejected: list[str] = []
        config = SimulationConfig.from_dict(session.config)
        for order in orders:
            asset_rule = resolve_simulation_rule(
                key=order.asset_rule_key,
                market=Market(order.market),
                instrument_type=InstrumentType(order.instrument_type),
                symbol=order.symbol,
            )
            outcome = match_order(
                order=PendingOrder(
                    order_id=order.simulation_order_id,
                    side=Side(order.side),
                    order_type=OrderType(order.order_type),
                    time_in_force=TimeInForce(order.time_in_force),
                    remaining_quantity=order.quantity - order.filled_quantity,
                    limit_price=order.price,
                    eligible_after=order.eligible_after,
                ),
                bar=event.bar,
                previous_close=previous_close,
                asset_rule=asset_rule,
                config=config.matching,
            )
            if outcome.action is MatchAction.WAIT:
                continue
            if outcome.action in {MatchAction.REJECT, MatchAction.CANCEL}:
                await self._close_order(
                    session,
                    account,
                    order,
                    status=("rejected" if outcome.action is MatchAction.REJECT else "cancelled"),
                    reason=outcome.reason,
                    actor=event.actor,
                )
                if outcome.action is MatchAction.REJECT:
                    rejected.append(order.simulation_order_id)
                continue
            assert outcome.fill_price is not None
            assert outcome.raw_price is not None
            try:
                fill = await self._apply_fill(
                    session=session,
                    account=account,
                    order=order,
                    quantity=outcome.quantity,
                    fill_price=outcome.fill_price,
                    raw_price=outcome.raw_price,
                    filled_at=event.bar.timestamp,
                    source_event_id=event.source_event_id,
                    asset_rule=asset_rule,
                )
            except SimulationRiskError as exc:
                await self._close_order(
                    session,
                    account,
                    order,
                    status="rejected",
                    reason=exc.reason,
                    actor=event.actor,
                )
                rejected.append(order.simulation_order_id)
                continue
            fill_ids.append(fill.simulation_fill_id)
            if outcome.cancel_remainder and order.status != "filled":
                await self._close_order(
                    session,
                    account,
                    order,
                    status="cancelled",
                    reason="ioc_remainder",
                    actor=event.actor,
                )

        await self._mark_to_market_and_settle(session, account, event.bar, actor=event.actor)
        session.clock = {
            "current_at": event.bar.timestamp.isoformat(),
            "trade_date": event.bar.timestamp.date().isoformat(),
            "speed": session.config["clock_speed"],
            "last_source_event_id": event.source_event_id,
        }
        session.updated_at = datetime.now(UTC)
        await self._revalue_account(account)
        market_event.status = SimulationMarketEventStatus.PROCESSED.value
        market_event.processed_at = datetime.now(UTC)
        market_event.error_summary = None
        await self._audit(
            session,
            actor=event.actor,
            action="market_event_processed",
            target=event.source_event_id,
            payload={
                "symbol": event.bar.symbol.code,
                "timestamp": event.bar.timestamp.isoformat(),
                "fill_ids": fill_ids,
                "rejected_order_ids": rejected,
                "equity": str(account.equity),
            },
        )
        return SimulationProcessResult(
            source_event_id=event.source_event_id,
            duplicate=False,
            fill_ids=tuple(fill_ids),
            rejected_order_ids=tuple(rejected),
            equity=account.equity,
            clock_at=event.bar.timestamp,
        )

    async def cancel_order(
        self, session_id: str, order_id: str, *, actor: str
    ) -> SimulationOrderModel:
        session = await self._session(session_id, for_update=True)
        if session.status not in {
            SimulationSessionStatus.RUNNING.value,
            SimulationSessionStatus.PAUSED.value,
        }:
            raise SimulationTransitionError("当前会话状态不允许撤销模拟订单")
        account = await self._account(session.simulation_account_id, for_update=True)
        order = await self.repo.get_order(order_id, for_update=True)
        if order is None or order.simulation_session_id != session_id:
            raise SimulationNotFoundError("模拟订单不存在")
        if order.status not in _ACTIVE_ORDER_STATUSES:
            raise SimulationTransitionError("模拟订单已处于终态")
        await self._close_order(
            session,
            account,
            order,
            status="cancelled",
            reason="manual_cancel",
            actor=actor,
        )
        await self._revalue_account(account)
        return order

    async def recover_active_sessions(self) -> tuple[str, ...]:
        """从持久化账本恢复活动模拟会话; 不会访问任何 Broker。"""

        sessions = await self.repo.list_sessions(
            statuses=(
                SimulationSessionStatus.RUNNING.value,
                SimulationSessionStatus.PAUSED.value,
            ),
            limit=10000,
        )
        recovered: list[str] = []
        for listed in sessions:
            session = await self._session(listed.simulation_session_id, for_update=True)
            account = await self._account(session.simulation_account_id, for_update=True)
            active = await self.repo.list_orders(
                session.simulation_session_id,
                statuses=_ACTIVE_ORDER_STATUSES,
                limit=10000,
                for_update=True,
            )
            required_frozen = sum((item.reserved_cash for item in active), _ZERO)
            liquid = account.cash + account.frozen_cash
            if required_frozen > liquid:
                session.status = SimulationSessionStatus.PAUSED.value
                await self._audit(
                    session,
                    actor="system",
                    action="recovery_failed",
                    target=session.simulation_session_id,
                    payload={
                        "reason": "reserved_cash_exceeds_liquid",
                        "required_frozen": str(required_frozen),
                        "liquid": str(liquid),
                    },
                )
                continue
            account.frozen_cash = required_frozen
            account.cash = liquid - required_frozen
            positions = await self.repo.list_positions(
                account.simulation_account_id, for_update=True
            )
            for position in positions:
                reserved = sum(
                    (
                        order.reserved_quantity
                        for order in active
                        if order.symbol == position.symbol
                        and order.position_side == position.position_side
                    ),
                    _ZERO,
                )
                if reserved > position.total_quantity:
                    session.status = SimulationSessionStatus.PAUSED.value
                    await self._audit(
                        session,
                        actor="system",
                        action="recovery_failed",
                        target=position.symbol,
                        payload={
                            "reason": "reserved_position_exceeds_total",
                            "reserved": str(reserved),
                            "total": str(position.total_quantity),
                        },
                    )
                    break
                position.frozen_quantity = reserved
                recovered_at = _clock_at(session)
                position.available_quantity = max(
                    _ZERO,
                    _matured_quantity(
                        position,
                        recovered_at.date() if recovered_at is not None else date.min,
                    )
                    - reserved,
                )
            processing = await self.repo.list_market_events(
                session.simulation_session_id,
                statuses=(SimulationMarketEventStatus.PROCESSING.value,),
                limit=10000,
                for_update=True,
            )
            for item in processing:
                item.status = SimulationMarketEventStatus.FAILED.value
                item.error_summary = "process restart interrupted event; retry allowed"
            account.margin_used = sum((item.margin_used for item in positions), _ZERO)
            await self._revalue_account(account)
            session.recovery_count += 1
            await self._audit(
                session,
                actor="system",
                action="restart_recovered",
                target=session.simulation_session_id,
                payload={
                    "active_orders": len(active),
                    "interrupted_events": [item.source_event_id for item in processing],
                    "recovery_count": session.recovery_count,
                },
            )
            recovered.append(session.simulation_session_id)
        return tuple(recovered)

    async def evaluate_session(
        self,
        session_id: str,
        *,
        actor: str,
        minimum_trading_days: int = 2,
    ) -> dict[str, object]:
        session = await self._session(session_id, for_update=True)
        if session.status != SimulationSessionStatus.STOPPED.value:
            raise SimulationTransitionError("只有 stopped 会话可以评估模拟晋级")
        account = await self._account(session.simulation_account_id)
        day_stmt = select(
            func.count(distinct(func.date(SimulationMarketEventModel.timestamp)))
        ).where(
            SimulationMarketEventModel.simulation_session_id == session_id,
            SimulationMarketEventModel.status == SimulationMarketEventStatus.PROCESSED.value,
        )
        trading_days = int((await self.repo.session.execute(day_stmt)).scalar_one())
        failed_events = await self.repo.list_market_events(
            session_id,
            statuses=(SimulationMarketEventStatus.FAILED.value,),
            limit=1,
        )
        ledger = await self.repo.list_ledger(session_id)
        equities = [item.equity_after for item in ledger]
        max_drawdown = _max_drawdown(equities or [account.initial_cash])
        eligible = trading_days >= minimum_trading_days and not failed_events and account.equity > 0
        session.promotion_status = (
            SimulationPromotionStatus.ELIGIBLE.value
            if eligible
            else SimulationPromotionStatus.FAILED.value
        )
        report = await self.build_report(session_id)
        result = {
            **report,
            "minimum_trading_days": minimum_trading_days,
            "trading_days": trading_days,
            "max_drawdown": str(max_drawdown),
            "promotion_status": session.promotion_status,
            "automatic_live_promotion": False,
        }
        await self._audit(
            session,
            actor=actor,
            action="promotion_evaluated",
            target=session_id,
            payload=result,
        )
        return result

    async def build_report(self, session_id: str) -> dict[str, object]:
        session = await self._session(session_id)
        account = await self._account(session.simulation_account_id)
        fills = await self.repo.list_fills(session_id)
        orders = await self.repo.list_orders(session_id, limit=10000)
        ledger = await self.repo.list_ledger(session_id)
        run = await self.repo.get_research_run(session.validation_run_id)
        reference_equity = None
        if run is not None and isinstance(run.result, dict):
            reference_equity = run.result.get("final_equity")
        simulation_return = (
            account.equity / account.initial_cash - Decimal("1")
            if account.initial_cash > 0
            else _ZERO
        )
        deviation = (
            account.equity - Decimal(str(reference_equity))
            if reference_equity is not None
            else None
        )
        return {
            "mode": SIMULATION_MODE,
            "session_id": session_id,
            "account_id": account.simulation_account_id,
            "strategy_id": session.strategy_id,
            "strategy_version": session.strategy_version,
            "validation_run_id": session.validation_run_id,
            "data_release_id": session.data_release_id,
            "status": session.status,
            "promotion_status": session.promotion_status,
            "initial_cash": str(account.initial_cash),
            "final_equity": str(account.equity),
            "simulation_return": str(simulation_return),
            "reference_backtest_final_equity": (
                str(reference_equity) if reference_equity is not None else None
            ),
            "equity_deviation": str(deviation) if deviation is not None else None,
            "order_count": len(orders),
            "fill_count": len(fills),
            "commission_paid": str(sum((item.commission for item in fills), _ZERO)),
            "tax_paid": str(sum((item.tax for item in fills), _ZERO)),
            "max_drawdown": str(
                _max_drawdown(
                    [item.equity_after for item in ledger] or [account.initial_cash, account.equity]
                )
            ),
            "simulation_is_not_return_proof": True,
            "automatic_live_promotion": False,
        }

    async def _target_to_order(
        self,
        *,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        decision: SimulationDecision,
        target: SimulationTarget,
        intent_index: int,
    ) -> SimulationOrderModel | None:
        asset_rule = resolve_simulation_rule(
            key=target.asset_rule_key,
            market=target.market,
            instrument_type=target.instrument_type,
            symbol=target.symbol,
        )
        if (
            target.position_side is PositionSide.SHORT
            and not SimulationConfig.from_dict(session.config).risk.allow_short
        ):
            return await self._rejected_order(
                session=session,
                decision=decision,
                target=target,
                intent_index=intent_index,
                asset_rule=asset_rule,
                reason="short_not_allowed",
                message="模拟风险配置未允许空头",
            )
        latest = await self.repo.latest_market_event(session.simulation_session_id, target.symbol)
        if latest is None:
            raise SimulationRiskError(
                "missing_market_price",
                f"{target.symbol} 尚无已处理行情, 不能生成模拟订单",
            )
        market_at = latest.timestamp
        reference_price = (
            target.limit_price
            if target.limit_price is not None
            else Decimal(str(latest.payload["close"]))
        )
        position = await self.repo.get_position(
            account.simulation_account_id,
            target.symbol,
            target.position_side.value,
            for_update=True,
        )
        current_quantity = position.total_quantity if position is not None else _ZERO
        active = await self.repo.list_orders(
            session.simulation_session_id,
            statuses=_ACTIVE_ORDER_STATUSES,
            symbol=target.symbol,
            limit=10000,
            for_update=True,
        )
        projected = current_quantity
        for pending in active:
            if pending.position_side != target.position_side.value:
                continue
            remaining = pending.quantity - pending.filled_quantity
            if pending.position_effect == SimulationPositionEffect.OPEN.value:
                projected += remaining
            else:
                projected -= remaining
        delta = target.target_quantity - projected
        if delta == 0:
            return None
        effect = SimulationPositionEffect.OPEN if delta > 0 else SimulationPositionEffect.CLOSE
        quantity = asset_rule.rule.round_to_lot(abs(delta))
        if quantity <= 0:
            return await self._rejected_order(
                session=session,
                decision=decision,
                target=target,
                intent_index=intent_index,
                asset_rule=asset_rule,
                reason="invalid_quantity",
                message="目标差额按最小交易单位取整后为 0",
            )
        side = _order_side(target.position_side, effect)
        intent_key = (
            f"{session.simulation_session_id}:{decision.decision_id}:"
            f"{intent_index}:{target.symbol}:{target.position_side.value}"
        )
        try:
            reserve_cash, reserve_quantity = await self._risk_and_reserve(
                session=session,
                account=account,
                position=position,
                asset_rule=asset_rule,
                target=target,
                effect=effect,
                quantity=quantity,
                reference_price=reference_price,
            )
            status = "acknowledged"
            reject_reason = None
            reject_message = None
        except SimulationRiskError as exc:
            reserve_cash = _ZERO
            reserve_quantity = _ZERO
            status = "rejected"
            reject_reason = exc.reason
            reject_message = str(exc)

        order = await self.repo.add_order(
            simulation_order_id=new_simulation_id("O"),
            intent_key=intent_key,
            simulation_account_id=account.simulation_account_id,
            simulation_session_id=session.simulation_session_id,
            decision_id=decision.decision_id,
            strategy_id=session.strategy_id,
            signal_trace_id=target.signal_trace_id,
            symbol=target.symbol,
            market=target.market.value,
            instrument_type=target.instrument_type.value,
            asset_rule_key=target.asset_rule_key,
            position_side=target.position_side.value,
            position_effect=effect.value,
            side=side.value,
            order_type=target.order_type.value,
            time_in_force=target.time_in_force.value,
            quantity=quantity,
            price=target.limit_price,
            filled_quantity=_ZERO,
            average_fill_price=None,
            status=status,
            reject_reason=reject_reason,
            reject_message=reject_message,
            rule_snapshot=asset_rule.as_dict(),
            reserved_cash=reserve_cash,
            reserved_quantity=reserve_quantity,
            submitted_market_at=market_at,
            eligible_after=market_at,
        )
        await self._audit(
            session,
            actor=decision.actor,
            action=("order_submitted" if status == "acknowledged" else "risk_rejected"),
            target=order.simulation_order_id,
            payload={
                "decision_id": decision.decision_id,
                "signal_trace_id": target.signal_trace_id,
                "symbol": target.symbol,
                "position_side": target.position_side.value,
                "position_effect": effect.value,
                "side": side.value,
                "quantity": str(quantity),
                "status": status,
                "reject_reason": reject_reason,
            },
        )
        return order

    async def _risk_and_reserve(
        self,
        *,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        position: SimulationPositionModel | None,
        asset_rule: SimulationAssetRule,
        target: SimulationTarget,
        effect: SimulationPositionEffect,
        quantity: Decimal,
        reference_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        config = SimulationConfig.from_dict(session.config)
        active_count = await self.repo.count_active_orders(session.simulation_session_id)
        if active_count >= config.risk.max_active_orders:
            raise SimulationRiskError("max_active_orders", "活动模拟订单数量超限")
        if reference_price <= 0:
            raise SimulationRiskError("invalid_price", "模拟风控参考价格必须为正")
        notional = quantity * reference_price * asset_rule.multiplier
        if notional > config.risk.max_order_value:
            raise SimulationRiskError(
                "max_order_value",
                f"模拟订单金额 {notional} 超过 {config.risk.max_order_value}",
            )
        if effect is SimulationPositionEffect.CLOSE:
            available = position.available_quantity if position is not None else _ZERO
            if available < quantity:
                raise SimulationRiskError(
                    "insufficient_position",
                    f"可用模拟持仓 {available} 小于平仓数量 {quantity}",
                )
            assert position is not None
            position.frozen_quantity += quantity
            position.available_quantity -= quantity
            return _ZERO, quantity

        current_notional = (
            position.total_quantity * reference_price * asset_rule.multiplier
            if position is not None
            else _ZERO
        )
        active_total, active_symbol = await self._active_open_notional(
            session.simulation_session_id, symbol=target.symbol
        )
        if current_notional + active_symbol + notional > config.risk.max_symbol_position_value:
            raise SimulationRiskError(
                "max_symbol_position_value",
                "模拟单标的目标金额超过风险上限",
            )
        positions = await self.repo.list_positions(account.simulation_account_id)
        gross = sum(
            (
                item.total_quantity * item.last_price * _position_multiplier(item)
                for item in positions
            ),
            _ZERO,
        )
        if gross + active_total + notional > account.equity * config.risk.max_gross_exposure:
            raise SimulationRiskError("max_gross_exposure", "模拟组合总敞口超过风险上限")
        buffer = Decimal("1") + config.matching.market_price_buffer_bps / Decimal("10000")
        commission = max(
            notional * asset_rule.rule.commission_rate,
            asset_rule.rule.commission_min,
        )
        required = (
            notional * asset_rule.margin_rate + commission
            if asset_rule.is_futures
            else notional * buffer + commission
        )
        if asset_rule.is_futures:
            projected_margin = account.margin_used + required - commission
            if projected_margin > account.equity * config.risk.max_margin_usage:
                raise SimulationRiskError("max_margin_usage", "模拟期货保证金占用超过上限")
        if account.cash < required:
            raise SimulationRiskError(
                "insufficient_cash",
                f"模拟可用现金 {account.cash} 小于预占 {required}",
            )
        account.cash -= required
        account.frozen_cash += required
        return _money(required), _ZERO

    async def _active_open_notional(
        self, session_id: str, *, symbol: str
    ) -> tuple[Decimal, Decimal]:
        """Count outstanding opens so sequential/concurrent targets cannot
        bypass gross and per-symbol exposure caps before they fill.
        """

        total = _ZERO
        active_for_symbol = _ZERO
        for order in await self.repo.list_orders(
            session_id,
            statuses=_ACTIVE_ORDER_STATUSES,
            limit=10000,
        ):
            if order.position_effect != SimulationPositionEffect.OPEN.value:
                continue
            remaining = order.quantity - order.filled_quantity
            if remaining <= 0:
                continue
            latest = await self.repo.latest_market_event(session_id, order.symbol)
            if order.price is not None:
                pending_price = order.price
            elif latest is not None:
                pending_price = Decimal(str(latest.payload["close"]))
            else:
                raise SimulationRiskError(
                    "missing_market_price",
                    f"{order.symbol} 活动订单缺少可恢复的风险参考价",
                )
            pending_rule = resolve_simulation_rule(
                key=order.asset_rule_key,
                market=Market(order.market),
                instrument_type=InstrumentType(order.instrument_type),
                symbol=order.symbol,
            )
            pending_notional = remaining * pending_price * pending_rule.multiplier
            total += pending_notional
            if order.symbol == symbol:
                active_for_symbol += pending_notional
        return total, active_for_symbol

    async def _rejected_order(
        self,
        *,
        session: SimulationSessionModel,
        decision: SimulationDecision,
        target: SimulationTarget,
        intent_index: int,
        asset_rule: SimulationAssetRule,
        reason: str,
        message: str,
    ) -> SimulationOrderModel:
        clock_at = _clock_at(session)
        if clock_at is None:
            clock_at = datetime.now(UTC)
        order = await self.repo.add_order(
            simulation_order_id=new_simulation_id("O"),
            intent_key=(
                f"{session.simulation_session_id}:{decision.decision_id}:"
                f"{intent_index}:{target.symbol}:{target.position_side.value}"
            ),
            simulation_account_id=session.simulation_account_id,
            simulation_session_id=session.simulation_session_id,
            decision_id=decision.decision_id,
            strategy_id=session.strategy_id,
            signal_trace_id=target.signal_trace_id,
            symbol=target.symbol,
            market=target.market.value,
            instrument_type=target.instrument_type.value,
            asset_rule_key=target.asset_rule_key,
            position_side=target.position_side.value,
            position_effect=SimulationPositionEffect.OPEN.value,
            side=_order_side(target.position_side, SimulationPositionEffect.OPEN).value,
            order_type=target.order_type.value,
            time_in_force=target.time_in_force.value,
            quantity=target.target_quantity,
            price=target.limit_price,
            filled_quantity=_ZERO,
            average_fill_price=None,
            status="rejected",
            reject_reason=reason,
            reject_message=message,
            rule_snapshot=asset_rule.as_dict(),
            reserved_cash=_ZERO,
            reserved_quantity=_ZERO,
            submitted_market_at=clock_at,
            eligible_after=clock_at,
        )
        await self._audit(
            session,
            actor=decision.actor,
            action="risk_rejected",
            target=order.simulation_order_id,
            payload={"reason": reason, "message": message},
        )
        return order

    async def _apply_fill(
        self,
        *,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        order: SimulationOrderModel,
        quantity: Decimal,
        fill_price: Decimal,
        raw_price: Decimal,
        filled_at: datetime,
        source_event_id: str,
        asset_rule: SimulationAssetRule,
    ) -> SimulationFillModel:
        before_remaining = order.quantity - order.filled_quantity
        event_key = (
            f"{session.simulation_session_id}:{source_event_id}:"
            f"{order.simulation_order_id}:{order.filled_quantity}"
        )
        existing = await self.repo.get_fill_by_event(event_key)
        if existing is not None:
            return existing
        position = await self.repo.get_position(
            account.simulation_account_id,
            order.symbol,
            order.position_side,
            for_update=True,
        )
        if position is None:
            position = await self.repo.add_position(
                simulation_account_id=account.simulation_account_id,
                symbol=order.symbol,
                market=order.market,
                instrument_type=order.instrument_type,
                asset_rule_key=order.asset_rule_key,
                position_side=order.position_side,
                total_quantity=_ZERO,
                available_quantity=_ZERO,
                frozen_quantity=_ZERO,
                average_price=_ZERO,
                market_value=_ZERO,
                realized_pnl=_ZERO,
                unrealized_pnl=_ZERO,
                margin_used=_ZERO,
                last_price=fill_price,
                last_settlement_price=_ZERO,
                last_settlement_date=None,
                lots=[],
            )
        turnover = quantity * fill_price * asset_rule.multiplier
        commission = _money(
            max(
                turnover * asset_rule.rule.commission_rate,
                asset_rule.rule.commission_min,
            )
        )
        tax = _money(
            turnover * asset_rule.rule.stamp_tax_rate
            if Side(order.side) is Side.SELL and not asset_rule.is_futures
            else _ZERO
        )
        slippage_cost = _money(abs(fill_price - raw_price) * quantity * asset_rule.multiplier)
        cash_before = account.cash
        margin_before = account.margin_used
        realized = _ZERO
        if SimulationPositionEffect(order.position_effect) is SimulationPositionEffect.OPEN:
            reserve_release = _reservation_slice(
                order.reserved_cash,
                quantity,
                before_remaining,
            )
            required_before_mutation = (
                _money(turnover * asset_rule.margin_rate) + commission
                if asset_rule.is_futures
                else turnover + commission + tax
            )
            if account.cash + reserve_release < required_before_mutation:
                raise SimulationRiskError(
                    "fill_cash_shortfall",
                    (f"成交实际需求 {required_before_mutation} 超过已预占及可用现金"),
                )
            account.frozen_cash -= reserve_release
            order.reserved_cash -= reserve_release
            if asset_rule.is_futures:
                margin = _money(turnover * asset_rule.margin_rate)
                needed = margin + commission
                self._consume_released_reservation(account, reserve_release, needed)
                account.margin_used += margin
                position.margin_used += margin
                existing_quantity = position.total_quantity
                existing_settlement = (
                    position.last_settlement_price
                    if position.last_settlement_price > 0
                    else position.average_price
                )
                _increase_position(
                    position,
                    quantity,
                    fill_price,
                    available_on=filled_at.date(),
                    as_of=filled_at.date(),
                    track_lot=False,
                )
                # A newly opened contract participates in the current bar's
                # settlement. Preserve the previous settlement date and blend
                # the old settlement basis with the new fill price; otherwise
                # the first day's variation margin would be skipped.
                position.last_settlement_price = _money(
                    (existing_settlement * existing_quantity + fill_price * quantity)
                    / position.total_quantity
                )
            else:
                needed = turnover + commission + tax
                self._consume_released_reservation(account, reserve_release, needed)
                available_on = (
                    filled_at.date() + timedelta(days=1)
                    if asset_rule.rule.enforce_t_plus_1
                    else filled_at.date()
                )
                _increase_position(
                    position,
                    quantity,
                    fill_price,
                    available_on=available_on,
                    as_of=filled_at.date(),
                    track_lot=True,
                )
        else:
            reserved_release = min(order.reserved_quantity, quantity)
            order.reserved_quantity -= reserved_release
            position.frozen_quantity = max(_ZERO, position.frozen_quantity - reserved_release)
            if asset_rule.is_futures:
                direction = (
                    Decimal("1")
                    if order.position_side == PositionSide.LONG.value
                    else Decimal("-1")
                )
                settlement = (
                    position.last_settlement_price
                    if position.last_settlement_price > 0
                    else position.average_price
                )
                realized = _money(
                    (fill_price - settlement) * asset_rule.multiplier * quantity * direction
                )
                margin_release = _reservation_slice(
                    position.margin_used,
                    quantity,
                    position.total_quantity,
                )
                account.margin_used -= margin_release
                position.margin_used -= margin_release
                account.cash += margin_release + realized - commission
                _decrease_position(
                    position,
                    quantity,
                    as_of=filled_at.date(),
                    track_lot=False,
                )
            else:
                realized = _money((fill_price - position.average_price) * quantity)
                account.cash += turnover - commission - tax
                _decrease_position(
                    position,
                    quantity,
                    as_of=filled_at.date(),
                    track_lot=True,
                )
            position.realized_pnl += realized

        position.last_price = fill_price
        if not asset_rule.is_futures:
            # Keep every persisted fill-ledger snapshot internally balanced.
            # Market-event close marking may update this again later in the
            # same transaction, but a fill must not temporarily value an
            # existing cash position at zero.
            position.market_value = _money(position.total_quantity * fill_price)
            position.unrealized_pnl = _money(
                (fill_price - position.average_price) * position.total_quantity
            )
        order.average_fill_price = _weighted_fill_price(
            old_average=order.average_fill_price,
            old_quantity=order.filled_quantity,
            fill_price=fill_price,
            fill_quantity=quantity,
        )
        order.filled_quantity += quantity
        order.status = "filled" if order.filled_quantity >= order.quantity else "partially_filled"
        order.updated_at = datetime.now(UTC)
        if order.status == "filled":
            await self._release_residual_reservation(account, position, order)
        await self._revalue_account(account)
        fill = await self.repo.add_fill(
            simulation_fill_id=new_simulation_id("F"),
            fill_event_key=event_key,
            simulation_order_id=order.simulation_order_id,
            simulation_account_id=account.simulation_account_id,
            simulation_session_id=session.simulation_session_id,
            symbol=order.symbol,
            market=order.market,
            position_side=order.position_side,
            position_effect=order.position_effect,
            side=order.side,
            quantity=quantity,
            price=fill_price,
            commission=commission,
            tax=tax,
            slippage_cost=slippage_cost,
            filled_at=filled_at,
        )
        await self._ledger(
            session,
            account,
            event_type="fill",
            reference_id=fill.simulation_fill_id,
            occurred_at=filled_at,
            cash_delta=account.cash - cash_before,
            margin_delta=account.margin_used - margin_before,
            realized_pnl=realized,
            commission=commission,
            tax=tax,
            payload={
                "order_id": order.simulation_order_id,
                "symbol": order.symbol,
                "position_side": order.position_side,
                "position_effect": order.position_effect,
                "quantity": str(quantity),
                "price": str(fill_price),
                "slippage_cost": str(slippage_cost),
            },
        )
        await self._audit(
            session,
            actor="simulation-matcher",
            action="order_filled",
            target=fill.simulation_fill_id,
            payload={
                "order_id": order.simulation_order_id,
                "source_event_id": source_event_id,
                "status": order.status,
            },
        )
        return fill

    def _consume_released_reservation(
        self,
        account: SimulationAccountModel,
        released: Decimal,
        needed: Decimal,
    ) -> None:
        account.cash += released
        if account.cash < needed:
            raise SimulationRiskError(
                "fill_cash_shortfall",
                f"成交实际需求 {needed} 超过已预占及可用现金",
            )
        account.cash -= needed

    async def _close_order(
        self,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        order: SimulationOrderModel,
        *,
        status: str,
        reason: str,
        actor: str,
    ) -> None:
        position = await self.repo.get_position(
            account.simulation_account_id,
            order.symbol,
            order.position_side,
            for_update=True,
        )
        await self._release_residual_reservation(account, position, order)
        order.status = status
        if status == "rejected":
            order.reject_reason = reason
            order.reject_message = f"模拟撮合拒绝: {reason}"
        order.updated_at = datetime.now(UTC)
        await self._audit(
            session,
            actor=actor,
            action=f"order_{status}",
            target=order.simulation_order_id,
            payload={"reason": reason},
        )

    async def _release_residual_reservation(
        self,
        account: SimulationAccountModel,
        position: SimulationPositionModel | None,
        order: SimulationOrderModel,
    ) -> None:
        if order.reserved_cash > 0:
            release = min(order.reserved_cash, account.frozen_cash)
            account.frozen_cash -= release
            account.cash += release
            order.reserved_cash -= release
        if order.reserved_quantity > 0 and position is not None:
            release_qty = min(order.reserved_quantity, position.frozen_quantity)
            position.frozen_quantity -= release_qty
            order.reserved_quantity -= release_qty
            position.available_quantity = min(
                position.total_quantity,
                position.available_quantity + release_qty,
            )

    async def _mark_to_market_and_settle(
        self,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        bar: Bar,
        *,
        actor: str,
    ) -> None:
        positions = await self.repo.list_positions(account.simulation_account_id, for_update=True)
        for position in positions:
            if position.symbol != bar.symbol.code:
                continue
            asset_rule = resolve_simulation_rule(
                key=position.asset_rule_key,
                market=Market(position.market),
                instrument_type=InstrumentType(position.instrument_type),
                symbol=position.symbol,
            )
            if asset_rule.is_futures:
                await self._settle_future(session, account, position, bar, asset_rule, actor=actor)
            else:
                position.last_price = bar.close
                position.market_value = _money(position.total_quantity * bar.close)
                position.unrealized_pnl = _money(
                    (bar.close - position.average_price) * position.total_quantity
                )
                position.available_quantity = max(
                    _ZERO,
                    _matured_quantity(position, bar.timestamp.date()) - position.frozen_quantity,
                )
                position.updated_at = datetime.now(UTC)

    async def _settle_future(
        self,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        position: SimulationPositionModel,
        bar: Bar,
        asset_rule: SimulationAssetRule,
        *,
        actor: str,
    ) -> None:
        trade_date = bar.timestamp.date()
        if position.last_settlement_date == trade_date:
            position.last_price = bar.close
            return
        cash_before = account.cash
        margin_before = account.margin_used
        direction = (
            Decimal("1") if position.position_side == PositionSide.LONG.value else Decimal("-1")
        )
        settlement = (
            position.last_settlement_price
            if position.last_settlement_price > 0
            else position.average_price
        )
        pnl = _money(
            (bar.close - settlement) * asset_rule.multiplier * position.total_quantity * direction
        )
        account.cash += pnl
        position.realized_pnl += pnl
        required_margin = _money(
            bar.close * asset_rule.multiplier * position.total_quantity * asset_rule.margin_rate
        )
        delta = required_margin - position.margin_used
        if delta > 0:
            if account.cash < delta:
                session.status = SimulationSessionStatus.PAUSED.value
                session.paused_at = datetime.now(UTC)
                await self._audit(
                    session,
                    actor=actor,
                    action="margin_call",
                    target=position.symbol,
                    payload={
                        "required_delta": str(delta),
                        "available_cash": str(account.cash),
                    },
                )
                delta = max(_ZERO, account.cash)
            account.cash -= delta
            account.margin_used += delta
            position.margin_used += delta
        elif delta < 0:
            release = -delta
            account.margin_used -= release
            position.margin_used -= release
            account.cash += release
        position.last_settlement_price = bar.close
        position.last_settlement_date = trade_date
        position.last_price = bar.close
        position.market_value = _ZERO
        position.unrealized_pnl = _ZERO
        position.updated_at = datetime.now(UTC)
        await self._revalue_account(account)
        await self._ledger(
            session,
            account,
            event_type="daily_settlement",
            reference_id=position.symbol,
            occurred_at=bar.timestamp,
            cash_delta=account.cash - cash_before,
            margin_delta=account.margin_used - margin_before,
            realized_pnl=pnl,
            commission=_ZERO,
            tax=_ZERO,
            payload={
                "symbol": position.symbol,
                "position_side": position.position_side,
                "settlement_price": str(bar.close),
            },
        )

    async def _cancel_all_active(
        self,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        *,
        actor: str,
        reason: str,
    ) -> None:
        orders = await self.repo.list_orders(
            session.simulation_session_id,
            statuses=_ACTIVE_ORDER_STATUSES,
            limit=10000,
            for_update=True,
        )
        for order in orders:
            await self._close_order(
                session,
                account,
                order,
                status="cancelled",
                reason=reason,
                actor=actor,
            )

    async def _ledger(
        self,
        session: SimulationSessionModel,
        account: SimulationAccountModel,
        *,
        event_type: str,
        reference_id: str | None,
        occurred_at: datetime,
        cash_delta: Decimal,
        margin_delta: Decimal,
        realized_pnl: Decimal,
        commission: Decimal,
        tax: Decimal,
        payload: dict[str, object],
    ) -> SimulationLedgerModel:
        sequence = await self.repo.next_sequence(session)
        return await self.repo.add_ledger(
            ledger_id=new_simulation_id("L"),
            simulation_account_id=account.simulation_account_id,
            simulation_session_id=session.simulation_session_id,
            sequence=sequence,
            event_type=event_type,
            reference_id=reference_id,
            cash_delta=_money(cash_delta),
            margin_delta=_money(margin_delta),
            realized_pnl=_money(realized_pnl),
            commission=_money(commission),
            tax=_money(tax),
            cash_after=_money(account.cash),
            frozen_cash_after=_money(account.frozen_cash),
            margin_used_after=_money(account.margin_used),
            equity_after=_money(account.equity),
            payload=payload,
            occurred_at=occurred_at,
        )

    async def _audit(
        self,
        session: SimulationSessionModel,
        *,
        actor: str,
        action: str,
        target: str | None,
        payload: dict[str, object],
    ) -> None:
        sequence = await self.repo.next_sequence(session)
        body = {
            "mode": SIMULATION_MODE,
            "session_id": session.simulation_session_id,
            **payload,
        }
        await self.repo.append_audit(
            audit_id=new_simulation_id("X"),
            event_key=(f"{session.simulation_session_id}:{sequence}:{action}:{target or '-'}"),
            account_id=session.simulation_account_id,
            session_id=session.simulation_session_id,
            sequence=sequence,
            actor=actor,
            action=action,
            target=target,
            payload=body,
            checksum=stable_checksum(body),
        )

    async def _revalue_account(self, account: SimulationAccountModel) -> None:
        positions = await self.repo.list_positions(account.simulation_account_id)
        securities = sum(
            (
                item.market_value
                for item in positions
                if item.instrument_type != InstrumentType.FUTURES.value
            ),
            _ZERO,
        )
        account.margin_used = sum((item.margin_used for item in positions), _ZERO)
        account.equity = _money(
            account.cash + account.frozen_cash + account.margin_used + securities
        )
        account.updated_at = datetime.now(UTC)

    async def _account(
        self, account_id: str, *, for_update: bool = False
    ) -> SimulationAccountModel:
        if not account_id.startswith("SIM-A-"):
            raise SimulationIsolationError("模拟接口只接受 SIM-A- 账户 ID")
        account = await self.repo.get_account(account_id, for_update=for_update)
        if account is None:
            raise SimulationNotFoundError("模拟账户不存在")
        if account.mode != SIMULATION_MODE:
            raise SimulationIsolationError("账户 mode 不是 simulation")
        return account

    async def _session(
        self, session_id: str, *, for_update: bool = False
    ) -> SimulationSessionModel:
        if not session_id.startswith("SIM-S-"):
            raise SimulationIsolationError("模拟接口只接受 SIM-S- 会话 ID")
        session = await self.repo.get_session(session_id, for_update=for_update)
        if session is None:
            raise SimulationNotFoundError("模拟会话不存在")
        if session.mode != SIMULATION_MODE:
            raise SimulationIsolationError("会话 mode 不是 simulation")
        return session

    @staticmethod
    def _require_active_account(account: SimulationAccountModel) -> None:
        if account.status != SimulationAccountStatus.ACTIVE.value:
            raise SimulationTransitionError("模拟账户已归档, 只允许查询")


def _order_side(
    position_side: PositionSide,
    effect: SimulationPositionEffect,
) -> Side:
    if position_side is PositionSide.LONG:
        return Side.BUY if effect is SimulationPositionEffect.OPEN else Side.SELL
    return Side.SELL if effect is SimulationPositionEffect.OPEN else Side.BUY


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT)


def _clock_at(session: SimulationSessionModel) -> datetime | None:
    value = session.clock.get("current_at")
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _position_multiplier(position: SimulationPositionModel) -> Decimal:
    if position.instrument_type != InstrumentType.FUTURES.value:
        return Decimal("1")
    rule = resolve_simulation_rule(
        key=position.asset_rule_key,
        market=Market(position.market),
        instrument_type=InstrumentType(position.instrument_type),
        symbol=position.symbol,
    )
    return rule.multiplier


def _reservation_slice(
    reserved: Decimal,
    fill_quantity: Decimal,
    remaining_before: Decimal,
) -> Decimal:
    if reserved <= 0 or remaining_before <= 0:
        return _ZERO
    if fill_quantity >= remaining_before:
        return reserved
    return _money(reserved * fill_quantity / remaining_before)


def _weighted_fill_price(
    *,
    old_average: Decimal | None,
    old_quantity: Decimal,
    fill_price: Decimal,
    fill_quantity: Decimal,
) -> Decimal:
    if old_average is None or old_quantity <= 0:
        return fill_price
    total = old_quantity + fill_quantity
    return _money((old_average * old_quantity + fill_price * fill_quantity) / total)


def _increase_position(
    position: SimulationPositionModel,
    quantity: Decimal,
    price: Decimal,
    *,
    available_on: date,
    as_of: date,
    track_lot: bool,
) -> None:
    old_total = position.total_quantity
    new_total = old_total + quantity
    position.average_price = (
        _money((position.average_price * old_total + price * quantity) / new_total)
        if new_total > 0
        else _ZERO
    )
    position.total_quantity = new_total
    if track_lot:
        position.lots = [
            *position.lots,
            {"quantity": str(quantity), "available_on": available_on.isoformat()},
        ]
        position.available_quantity = max(
            _ZERO,
            _matured_quantity(position, as_of) - position.frozen_quantity,
        )
    else:
        position.available_quantity = max(_ZERO, new_total - position.frozen_quantity)
    position.updated_at = datetime.now(UTC)


def _decrease_position(
    position: SimulationPositionModel,
    quantity: Decimal,
    *,
    as_of: date,
    track_lot: bool,
) -> None:
    if quantity > position.total_quantity:
        raise SimulationRiskError("position_underflow", "成交将导致模拟持仓小于 0")
    if track_lot:
        remaining = quantity
        lots: list[dict[str, object]] = []
        for lot in position.lots:
            lot_quantity = Decimal(str(lot["quantity"]))
            consumed = min(lot_quantity, remaining)
            lot_quantity -= consumed
            remaining -= consumed
            if lot_quantity > 0:
                lots.append(
                    {
                        "quantity": str(lot_quantity),
                        "available_on": str(lot["available_on"]),
                    }
                )
        if remaining > 0:
            raise SimulationRiskError("lot_underflow", "模拟持仓 lot 数量不足")
        position.lots = lots
    position.total_quantity -= quantity
    if position.total_quantity <= 0:
        position.total_quantity = _ZERO
        position.average_price = _ZERO
        position.available_quantity = _ZERO
        position.frozen_quantity = _ZERO
        position.lots = []
    else:
        available_base = (
            _matured_quantity(position, as_of) if track_lot else position.total_quantity
        )
        position.available_quantity = max(_ZERO, available_base - position.frozen_quantity)
    position.updated_at = datetime.now(UTC)


def _matured_quantity(position: SimulationPositionModel, on_date: date) -> Decimal:
    if position.instrument_type == InstrumentType.FUTURES.value:
        return position.total_quantity
    return sum(
        (
            Decimal(str(item["quantity"]))
            for item in position.lots
            if date.fromisoformat(str(item["available_on"])) <= on_date
        ),
        _ZERO,
    )


def _max_drawdown(equities: list[Decimal]) -> Decimal:
    high = _ZERO
    maximum = _ZERO
    for equity in equities:
        high = max(high, equity)
        if high > 0:
            maximum = max(maximum, (high - equity) / high)
    return _money(maximum)


def bar_from_payload(payload: dict[str, object]) -> Bar:
    """API/恢复层把持久化纯 JSON 恢复为强类型 Bar。"""

    return Bar(
        symbol=Symbol(code=str(payload["symbol"]), market=Market(str(payload["market"]))),
        period=BarPeriod(str(payload["period"])),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        open=Decimal(str(payload["open"])),
        high=Decimal(str(payload["high"])),
        low=Decimal(str(payload["low"])),
        close=Decimal(str(payload["close"])),
        volume=Decimal(str(payload["volume"])),
        amount=Decimal(str(payload["amount"])),
    )


__all__ = ["SimulationService", "bar_from_payload"]
