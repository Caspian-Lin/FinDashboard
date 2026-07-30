"""模拟域 SQLAlchemy 仓储。

本仓储只访问 ``simulation_*`` 表以及只读的研究策略/运行表。它不导入或访问
实盘 ``AccountModel``、``OrderModel``、``FillModel``、``PositionModel``。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchStrategySpecModel,
    SimulationAccountModel,
    SimulationAuditModel,
    SimulationDecisionModel,
    SimulationFillModel,
    SimulationLedgerModel,
    SimulationMarketEventModel,
    SimulationOrderModel,
    SimulationPositionModel,
    SimulationSessionModel,
)


class SimulationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def checkpoint(self) -> None:
        await self.session.commit()

    async def add_account(
        self,
        *,
        account_id: str,
        name: str,
        initial_cash: Decimal,
        currency: str,
    ) -> SimulationAccountModel:
        row = SimulationAccountModel(
            simulation_account_id=account_id,
            name=name,
            mode="simulation",
            status="active",
            currency=currency,
            initial_cash=initial_cash,
            cash=initial_cash,
            frozen_cash=Decimal("0"),
            margin_used=Decimal("0"),
            equity=initial_cash,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_account(
        self, account_id: str, *, for_update: bool = False
    ) -> SimulationAccountModel | None:
        stmt = select(SimulationAccountModel).where(
            SimulationAccountModel.simulation_account_id == account_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_accounts(self, *, limit: int = 100) -> list[SimulationAccountModel]:
        stmt = (
            select(SimulationAccountModel)
            .order_by(SimulationAccountModel.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_session(
        self,
        *,
        session_id: str,
        account_id: str,
        source_mode: str,
        strategy_id: str,
        strategy_version: int,
        strategy_checksum: str,
        validation_run_id: str,
        data_release_id: str,
        config: dict[str, object],
        reset_of_session_id: str | None = None,
    ) -> SimulationSessionModel:
        row = SimulationSessionModel(
            simulation_session_id=session_id,
            simulation_account_id=account_id,
            mode="simulation",
            source_mode=source_mode,
            status="created",
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            strategy_checksum=strategy_checksum,
            validation_run_id=validation_run_id,
            data_release_id=data_release_id,
            config=config,
            clock={
                "current_at": None,
                "speed": config["clock_speed"],
                "trade_date": None,
            },
            promotion_status="not_evaluated",
            reset_of_session_id=reset_of_session_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_session(
        self, session_id: str, *, for_update: bool = False
    ) -> SimulationSessionModel | None:
        stmt = select(SimulationSessionModel).where(
            SimulationSessionModel.simulation_session_id == session_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_sessions(
        self,
        *,
        account_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[SimulationSessionModel]:
        stmt = select(SimulationSessionModel)
        if account_id is not None:
            stmt = stmt.where(SimulationSessionModel.simulation_account_id == account_id)
        if statuses is not None:
            stmt = stmt.where(SimulationSessionModel.status.in_(tuple(statuses)))
        stmt = stmt.order_by(SimulationSessionModel.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_strategy_version(
        self, strategy_id: str, version: int
    ) -> ResearchStrategySpecModel | None:
        stmt = select(ResearchStrategySpecModel).where(
            ResearchStrategySpecModel.strategy_id == strategy_id,
            ResearchStrategySpecModel.version == version,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_research_run(self, run_id: str) -> ResearchRunModel | None:
        stmt = select(ResearchRunModel).where(ResearchRunModel.run_id == run_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def signal_traces(
        self,
        *,
        run_id: str,
        source_decision_id: str,
        trace_ids: Iterable[str],
    ) -> set[str]:
        expected = tuple(trace_ids)
        if not expected:
            return set()
        stmt = select(ResearchRunArtifactModel.trace_id).where(
            ResearchRunArtifactModel.run_id == run_id,
            ResearchRunArtifactModel.decision_id == source_decision_id,
            ResearchRunArtifactModel.stage == "signals",
            ResearchRunArtifactModel.trace_id.in_(expected),
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def add_decision(
        self,
        *,
        session_id: str,
        decision_id: str,
        source_run_id: str,
        source_decision_id: str,
        signal_trace_ids: list[str],
        payload: dict[str, object],
        checksum: str,
    ) -> SimulationDecisionModel:
        row = SimulationDecisionModel(
            simulation_session_id=session_id,
            decision_id=decision_id,
            source_run_id=source_run_id,
            source_decision_id=source_decision_id,
            source_signal_trace_ids=signal_trace_ids,
            payload=payload,
            checksum=checksum,
            status="accepted",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_decision(
        self, session_id: str, decision_id: str
    ) -> SimulationDecisionModel | None:
        stmt = select(SimulationDecisionModel).where(
            SimulationDecisionModel.simulation_session_id == session_id,
            SimulationDecisionModel.decision_id == decision_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_decisions(
        self, session_id: str, *, limit: int = 1000
    ) -> list[SimulationDecisionModel]:
        stmt = (
            select(SimulationDecisionModel)
            .where(SimulationDecisionModel.simulation_session_id == session_id)
            .order_by(
                SimulationDecisionModel.created_at,
                SimulationDecisionModel.id,
            )
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_order(self, **values: object) -> SimulationOrderModel:
        row = SimulationOrderModel(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_order(
        self, order_id: str, *, for_update: bool = False
    ) -> SimulationOrderModel | None:
        stmt = select(SimulationOrderModel).where(
            SimulationOrderModel.simulation_order_id == order_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_orders(
        self,
        session_id: str,
        *,
        statuses: Iterable[str] | None = None,
        symbol: str | None = None,
        limit: int = 1000,
        for_update: bool = False,
    ) -> list[SimulationOrderModel]:
        stmt = select(SimulationOrderModel).where(
            SimulationOrderModel.simulation_session_id == session_id
        )
        if statuses is not None:
            stmt = stmt.where(SimulationOrderModel.status.in_(tuple(statuses)))
        if symbol is not None:
            stmt = stmt.where(SimulationOrderModel.symbol == symbol)
        stmt = stmt.order_by(SimulationOrderModel.created_at, SimulationOrderModel.id).limit(limit)
        if for_update:
            stmt = stmt.with_for_update()
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_active_orders(self, session_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(SimulationOrderModel)
            .where(
                SimulationOrderModel.simulation_session_id == session_id,
                SimulationOrderModel.status.in_(("submitted", "acknowledged", "partially_filled")),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def add_fill(self, **values: object) -> SimulationFillModel:
        row = SimulationFillModel(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_fill_by_event(self, fill_event_key: str) -> SimulationFillModel | None:
        stmt = select(SimulationFillModel).where(
            SimulationFillModel.fill_event_key == fill_event_key
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_fills(self, session_id: str, *, limit: int = 5000) -> list[SimulationFillModel]:
        stmt = (
            select(SimulationFillModel)
            .where(SimulationFillModel.simulation_session_id == session_id)
            .order_by(SimulationFillModel.filled_at, SimulationFillModel.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_position(
        self,
        account_id: str,
        symbol: str,
        position_side: str,
        *,
        for_update: bool = False,
    ) -> SimulationPositionModel | None:
        stmt = select(SimulationPositionModel).where(
            SimulationPositionModel.simulation_account_id == account_id,
            SimulationPositionModel.symbol == symbol,
            SimulationPositionModel.position_side == position_side,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_position(self, **values: object) -> SimulationPositionModel:
        row = SimulationPositionModel(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_positions(
        self, account_id: str, *, for_update: bool = False
    ) -> list[SimulationPositionModel]:
        stmt = (
            select(SimulationPositionModel)
            .where(SimulationPositionModel.simulation_account_id == account_id)
            .order_by(
                SimulationPositionModel.symbol,
                SimulationPositionModel.position_side,
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_ledger(self, **values: object) -> SimulationLedgerModel:
        row = SimulationLedgerModel(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_ledger(
        self, session_id: str, *, limit: int = 10000
    ) -> list[SimulationLedgerModel]:
        stmt = (
            select(SimulationLedgerModel)
            .where(SimulationLedgerModel.simulation_session_id == session_id)
            .order_by(SimulationLedgerModel.sequence)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_market_event(
        self, session_id: str, source_event_id: str, *, for_update: bool = False
    ) -> SimulationMarketEventModel | None:
        stmt = select(SimulationMarketEventModel).where(
            SimulationMarketEventModel.simulation_session_id == session_id,
            SimulationMarketEventModel.source_event_id == source_event_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_market_event(self, **values: object) -> SimulationMarketEventModel:
        row = SimulationMarketEventModel(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def latest_market_event(
        self, session_id: str, symbol: str, *, before: datetime | None = None
    ) -> SimulationMarketEventModel | None:
        stmt = select(SimulationMarketEventModel).where(
            SimulationMarketEventModel.simulation_session_id == session_id,
            SimulationMarketEventModel.symbol == symbol,
            SimulationMarketEventModel.status == "processed",
        )
        if before is not None:
            stmt = stmt.where(SimulationMarketEventModel.timestamp < before)
        stmt = stmt.order_by(
            SimulationMarketEventModel.timestamp.desc(),
            SimulationMarketEventModel.id.desc(),
        ).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_market_events(
        self,
        session_id: str,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 10000,
        for_update: bool = False,
    ) -> list[SimulationMarketEventModel]:
        stmt = select(SimulationMarketEventModel).where(
            SimulationMarketEventModel.simulation_session_id == session_id
        )
        if statuses is not None:
            stmt = stmt.where(SimulationMarketEventModel.status.in_(tuple(statuses)))
        stmt = stmt.order_by(
            SimulationMarketEventModel.timestamp,
            SimulationMarketEventModel.id,
        ).limit(limit)
        if for_update:
            stmt = stmt.with_for_update()
        return list((await self.session.execute(stmt)).scalars().all())

    async def append_audit(
        self,
        *,
        audit_id: str,
        event_key: str,
        account_id: str,
        session_id: str | None,
        sequence: int,
        actor: str,
        action: str,
        target: str | None,
        payload: dict[str, object],
        checksum: str,
    ) -> SimulationAuditModel:
        row = SimulationAuditModel(
            audit_id=audit_id,
            event_key=event_key,
            simulation_account_id=account_id,
            simulation_session_id=session_id,
            sequence=sequence,
            actor=actor,
            action=action,
            target=target,
            payload=payload,
            checksum=checksum,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_audit(
        self, session_id: str, *, limit: int = 10000
    ) -> list[SimulationAuditModel]:
        stmt = (
            select(SimulationAuditModel)
            .where(SimulationAuditModel.simulation_session_id == session_id)
            .order_by(SimulationAuditModel.sequence, SimulationAuditModel.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_sequence(self, session: SimulationSessionModel) -> int:
        session.last_sequence += 1
        session.updated_at = datetime.now(UTC)
        await self.session.flush()
        return session.last_sequence


__all__ = ["SimulationRepository"]
