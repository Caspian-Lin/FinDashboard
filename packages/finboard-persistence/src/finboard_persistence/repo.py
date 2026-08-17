"""Repository 层。

职责:
* 把领域 ``dataclass`` (:class:`finboard_shared.models.Order` 等) 与 ORM 行互转;
* 提供按业务场景命名的方法,不暴露原始 ``select(...)`` 给上层;
* 不在这里做事务边界控制 —— ``commit`` 由调用方(应用层)决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    AccountModel,
    AuditLogModel,
    BacktestRunModel,
    FillModel,
    InstrumentModel,
    InstrumentNameModel,
    OrderModel,
    PositionModel,
    ReconciliationLogModel,
    StrategyPresetModel,
    WatchlistItemModel,
    WatchlistModel,
)
from finboard_shared.identifiers import AccountId, ClientOrderId, StrategyId
from finboard_shared.models import Account, Fill, Order, Position, Symbol
from finboard_shared.types import (
    BrokerKind,
    ListingStatus,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)

logger = structlog.get_logger(__name__)

_POSITION_SOURCE_LOCAL = "local"
_POSITION_SOURCE_BROKER = "broker"


# --------------------------------------------------------------------------- 转换
def _symbol_to_str(symbol: Symbol) -> str:
    return symbol.code


def _str_to_symbol(code: str, market: str) -> Symbol:
    return Symbol(code=code, market=Market(market))


def order_from_orm(row: OrderModel) -> Order:
    return Order(
        client_order_id=ClientOrderId(row.client_order_id),
        account_id=AccountId(row.account_id),
        broker_kind=BrokerKind(row.broker_kind),
        symbol=_str_to_symbol(row.symbol, row.market),
        side=Side(row.side),
        order_type=OrderType(row.order_type),
        quantity=row.quantity,
        strategy_id=StrategyId(row.strategy_id) if row.strategy_id else None,
        price=row.price,
        time_in_force=TimeInForce(row.time_in_force),
        position_side=PositionSide(row.position_side),
        broker_order_id=row.broker_order_id,
        filled_quantity=row.filled_quantity,
        average_fill_price=row.average_fill_price,
        status=OrderStatus(row.status),
        created_at=row.created_at,
        risk_checked_at=row.risk_checked_at,
        submitted_at=row.submitted_at,
        acknowledged_at=row.acknowledged_at,
        updated_at=row.updated_at,
    )


def fill_from_orm(row: FillModel) -> Fill:
    return Fill(
        fill_id=row.fill_id,
        client_order_id=ClientOrderId(row.client_order_id),
        symbol=Symbol(
            code=row.symbol,
            market=Market(row.market) if row.market else Market.A_SHARE,
        ),
        side=Side(row.side),
        quantity=row.quantity,
        price=row.price,
        position_side=PositionSide(row.position_side),
        commission=row.commission,
        tax=row.tax,
        broker_order_id=row.broker_order_id,
        filled_at=row.filled_at,
    )


def position_from_orm(row: PositionModel) -> Position:
    return Position(
        account_id=AccountId(row.account_id),
        symbol=Symbol(
            code=row.symbol,
            market=Market(row.market) if row.market else Market.A_SHARE,
        ),
        position_side=PositionSide(row.position_side),
        total_quantity=row.total_quantity,
        available_quantity=row.available_quantity,
        frozen_quantity=row.frozen_quantity,
        average_price=row.average_price,
        market_value=row.market_value,
        unrealized_pnl=row.unrealized_pnl,
        updated_at=row.updated_at,
    )


# --------------------------------------------------------------------------- Order
class OrderRepository:
    """订单仓储。

    重点:**下单前**必须调用 :meth:`exists_by_client_order_id` 做幂等校验,
    DB 的 UNIQUE 索引是最后一道兜底。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_by_client_order_id(self, client_order_id: str) -> bool:
        stmt = (
            select(OrderModel.id)
            .where(OrderModel.client_order_id == client_order_id)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.first() is not None

    async def add(self, order: Order) -> OrderModel:
        if await self.exists_by_client_order_id(str(order.client_order_id)):
            raise ValueError(
                f"client_order_id {order.client_order_id} 已存在 — 拒绝重复下单"
            )
        row = OrderModel(
            client_order_id=str(order.client_order_id),
            broker_order_id=order.broker_order_id,
            account_id=str(order.account_id),
            broker_kind=order.broker_kind.value,
            strategy_id=str(order.strategy_id) if order.strategy_id else None,
            symbol=order.symbol.code,
            market=order.symbol.market.value,
            side=order.side.value,
            order_type=order.order_type.value,
            time_in_force=order.time_in_force.value,
            position_side=order.position_side.value,
            price=order.price,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            average_fill_price=order.average_fill_price,
            status=order.status.value,
            reject_reason=order.reject_reason.value if order.reject_reason else None,
            reject_message=order.reject_message,
            created_at=order.created_at,
            risk_checked_at=order.risk_checked_at,
            submitted_at=order.submitted_at,
            acknowledged_at=order.acknowledged_at,
            updated_at=order.updated_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, client_order_id: str) -> Order | None:
        stmt = select(OrderModel).where(OrderModel.client_order_id == client_order_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return order_from_orm(row) if row is not None else None

    async def list_active(self, account_id: str) -> list[Order]:
        stmt = (
            select(OrderModel)
            .where(
                OrderModel.account_id == account_id,
                OrderModel.status.in_(
                    [
                        OrderStatus.CREATED.value,
                        OrderStatus.RISK_CHECKED.value,
                        OrderStatus.SUBMITTING.value,
                        OrderStatus.SUBMITTED.value,
                        OrderStatus.ACKNOWLEDGED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                        OrderStatus.CANCEL_PENDING.value,
                        OrderStatus.UNKNOWN.value,
                    ]
                ),
            )
            .order_by(OrderModel.created_at)
        )
        result = await self._session.execute(stmt)
        return [order_from_orm(r) for r in result.scalars().all()]

    async def list_all(
        self,
        account_id: str,
        *,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Order], int]:
        """分页查询订单(含终态),返回 (orders, total_count)。"""
        from sqlalchemy import func

        base = select(OrderModel).where(OrderModel.account_id == account_id)
        count_base = select(func.count()).select_from(OrderModel).where(
            OrderModel.account_id == account_id
        )
        if status is not None:
            base = base.where(OrderModel.status == status)
            count_base = count_base.where(OrderModel.status == status)
        if symbol is not None:
            base = base.where(OrderModel.symbol == symbol)
            count_base = count_base.where(OrderModel.symbol == symbol)

        total_result = await self._session.execute(count_base)
        total = total_result.scalar_one()

        base = base.order_by(OrderModel.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(base)
        orders = [order_from_orm(r) for r in result.scalars().all()]
        return orders, total

    async def update_state(self, order: Order) -> None:
        """以领域对象为真值,把状态字段写回 ORM 行。"""
        stmt = select(OrderModel).where(
            OrderModel.client_order_id == str(order.client_order_id)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise ValueError(f"未找到订单 {order.client_order_id} 不能更新")
        row.broker_order_id = order.broker_order_id
        row.filled_quantity = order.filled_quantity
        row.average_fill_price = order.average_fill_price
        row.status = order.status.value
        row.reject_reason = order.reject_reason.value if order.reject_reason else None
        row.reject_message = order.reject_message
        row.submitted_at = order.submitted_at
        row.acknowledged_at = order.acknowledged_at
        row.risk_checked_at = order.risk_checked_at
        row.updated_at = datetime.now(UTC)
        await self._session.flush()


# --------------------------------------------------------------------------- Fill
class FillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, fill: Fill) -> FillModel:
        # 幂等:券商回报可能重放,以 fill_id 为准去重
        existing = await self.get(fill.fill_id)
        if existing is not None:
            return existing
        row = FillModel(
            fill_id=fill.fill_id,
            broker_fill_id=fill.fill_id,  # mock 下二者相同;实盘以券商编号为准
            client_order_id=str(fill.client_order_id),
            broker_order_id=fill.broker_order_id,
            symbol=fill.symbol.code,
            market=fill.symbol.market.value,  # issue #58
            side=fill.side.value,
            position_side=fill.position_side.value,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.commission,
            tax=fill.tax,
            filled_at=fill.filled_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, fill_id: str) -> FillModel | None:
        stmt = select(FillModel).where(FillModel.fill_id == fill_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_order(self, client_order_id: str) -> list[Fill]:
        stmt = (
            select(FillModel)
            .where(FillModel.client_order_id == client_order_id)
            .order_by(FillModel.filled_at)
        )
        result = await self._session.execute(stmt)
        return [fill_from_orm(r) for r in result.scalars().all()]

    async def list_by_account(
        self,
        account_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Fill], int]:
        """分页查询某账户的所有成交,返回 (fills, total_count)。"""
        from sqlalchemy import func

        count_stmt = (
            select(func.count())
            .select_from(FillModel)
            .where(
                FillModel.client_order_id.in_(
                    select(OrderModel.client_order_id).where(
                        OrderModel.account_id == account_id
                    )
                )
            )
        )
        total_result = await self._session.execute(count_stmt)
        total = total_result.scalar_one()

        stmt = (
            select(FillModel)
            .where(
                FillModel.client_order_id.in_(
                    select(OrderModel.client_order_id).where(
                        OrderModel.account_id == account_id
                    )
                )
            )
            .order_by(FillModel.filled_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        fills = [fill_from_orm(r) for r in result.scalars().all()]
        return fills, total

    async def sum_buy_value_since(
        self, account_id: str, since: datetime
    ) -> Decimal:
        """指定时间后该账户所有买入成交的金额合计(price * quantity)。"""
        from sqlalchemy import func

        stmt = (
            select(func.coalesce(func.sum(FillModel.price * FillModel.quantity), 0))
            .where(
                FillModel.client_order_id.in_(
                    select(OrderModel.client_order_id).where(
                        OrderModel.account_id == account_id
                    )
                ),
                FillModel.side == Side.BUY.value,
                FillModel.filled_at >= since,
            )
        )
        result = await self._session.execute(stmt)
        return Decimal(str(result.scalar_one()))


# --------------------------------------------------------------------------- Position
class PositionRepository:
    """持仓仓储。本地(local)和券商(broker)两份快照分别存储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_local(self, position: Position) -> None:
        await self._upsert(position, source=_POSITION_SOURCE_LOCAL)

    async def upsert_broker(self, position: Position) -> None:
        await self._upsert(position, source=_POSITION_SOURCE_BROKER)

    async def list_local(self, account_id: str) -> list[Position]:
        return await self._list(account_id, source=_POSITION_SOURCE_LOCAL)

    async def list_broker(self, account_id: str) -> list[Position]:
        return await self._list(account_id, source=_POSITION_SOURCE_BROKER)

    async def _upsert(self, position: Position, *, source: str) -> None:
        stmt = select(PositionModel).where(
            PositionModel.account_id == str(position.account_id),
            PositionModel.symbol == position.symbol.code,
            PositionModel.position_side == position.position_side.value,
            PositionModel.source == source,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = PositionModel(
                account_id=str(position.account_id),
                symbol=position.symbol.code,
                market=position.symbol.market.value,  # issue #58
                position_side=position.position_side.value,
                source=source,
            )
            self._session.add(row)
        row.total_quantity = position.total_quantity
        row.available_quantity = position.available_quantity
        row.frozen_quantity = position.frozen_quantity
        row.average_price = position.average_price
        row.market_value = position.market_value
        row.unrealized_pnl = position.unrealized_pnl
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def _list(self, account_id: str, *, source: str) -> list[Position]:
        stmt = (
            select(PositionModel)
            .where(
                PositionModel.account_id == account_id,
                PositionModel.source == source,
            )
            .order_by(PositionModel.symbol)
        )
        result = await self._session.execute(stmt)
        return [position_from_orm(r) for r in result.scalars().all()]


# --------------------------------------------------------------------------- Account
class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, account: Account) -> None:
        stmt = select(AccountModel).where(
            AccountModel.account_id == str(account.account_id)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = AccountModel(
                account_id=str(account.account_id),
                broker_kind=account.broker_kind.value,
            )
            self._session.add(row)
        row.broker_kind = account.broker_kind.value
        row.total_asset = account.total_asset
        row.cash = account.cash
        row.frozen_cash = account.frozen_cash
        row.margin_used = account.margin_used
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def get(self, account_id: str) -> Account | None:
        stmt = select(AccountModel).where(AccountModel.account_id == account_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return Account(
            account_id=AccountId(row.account_id),
            broker_kind=BrokerKind(row.broker_kind),
            total_asset=row.total_asset,
            cash=row.cash,
            frozen_cash=row.frozen_cash,
            margin_used=row.margin_used,
            updated_at=row.updated_at,
        )


# --------------------------------------------------------------------------- Audit
class AuditLogRepository:
    """审计日志(非业务关键路径,纯写入)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        actor: str,
        action: str,
        target: str | None = None,
        payload: str | None = None,
    ) -> None:
        self._session.add(
            AuditLogModel(
                actor=actor,
                action=action,
                target=target,
                payload=payload,
            )
        )
        # 审计日志不单独 flush —— 随下一次业务 flush / commit 一并写入。
        # 单独 flush 会在 event-loop 让出时与 broker-event 消费者竞争
        # ("Session is already flushing")。

    async def list_recent(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLogModel]:
        """查询最近的审计日志(按时间倒序)。"""
        stmt = (
            select(AuditLogModel)
            .order_by(AuditLogModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# --------------------------------------------------------------------------- Reconcile log
class ReconciliationLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        account_id: str,
        kind: str,
        key: str,
        local_state: str | None = None,
        broker_state: str | None = None,
        diff: str | None = None,
        action_taken: str | None = None,
        notes: str | None = None,
    ) -> ReconciliationLogModel:
        row = ReconciliationLogModel(
            account_id=account_id,
            kind=kind,
            key=key,
            local_state=local_state,
            broker_state=broker_state,
            diff=diff,
            action_taken=action_taken,
            notes=notes,
        )
        self._session.add(row)
        await self._session.flush()
        return row


@dataclass
class InstrumentSyncResult:
    """``sync_with_diff`` 的变更摘要(issue #35 生命周期检测)。"""

    new: int = 0
    updated: int = 0
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    pending_delist: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.new + self.updated


@dataclass(frozen=True, slots=True)
class InstrumentMetadataBackfillResult:
    """profiles → instruments 元数据回填摘要(issue #185)。

    ``scoped`` 是本次审查的 instruments 行数(按入参 symbols 或全部未退市标的);
    ``backfilled_*`` 是本次真实回填的行数;``missing_*`` 是回填完成后仍缺失的
    行数 —— 让缺失可被发现而非静默。
    """

    profile_batch_available: bool
    scoped: int = 0
    backfilled_list_date: int = 0
    backfilled_industry: int = 0
    missing_list_date: int = 0
    missing_industry: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_batch_available": self.profile_batch_available,
            "scoped": self.scoped,
            "backfilled_list_date": self.backfilled_list_date,
            "backfilled_industry": self.backfilled_industry,
            "missing_list_date": self.missing_list_date,
            "missing_industry": self.missing_industry,
        }


class InstrumentRepository:
    """标的元数据仓储(instruments 表)。

    由 UniverseDiscovery 调用 ``upsert_many`` 做批量同步。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(
        self,
        instruments: list[dict[str, object]],
    ) -> int:
        """批量 upsert(按 code 去重;存在则更新 name/status/exchange)。

        :param instruments: dict 列表,每个含 code/name/market/instrument_type/exchange
        :returns: 影响行数
        """
        if not instruments:
            return 0
        codes = [ins["code"] for ins in instruments]
        stmt = select(InstrumentModel).where(InstrumentModel.code.in_(codes))
        existing = {
            row.code: row
            for row in (await self._session.execute(stmt)).scalars().all()
        }

        new_count = 0
        for ins in instruments:
            row = existing.get(str(ins["code"]))
            if row is None:
                row = InstrumentModel(
                    code=str(ins["code"]),
                    name=str(ins.get("name", "")),
                    market=str(ins.get("market", "a_share")),
                    instrument_type=str(ins.get("instrument_type", "stock")),
                    exchange=ins.get("exchange"),
                    listing_board=str(ins.get("listing_board", "unknown")),
                    status=str(ins.get("status", "active")),
                )
                self._session.add(row)
                new_count += 1
            else:
                row.name = str(ins.get("name", row.name))
                row.exchange = ins.get("exchange", row.exchange)  # type: ignore[assignment]
                row.listing_board = str(ins.get("listing_board", row.listing_board))
                if "status" in ins:
                    row.status = str(ins["status"])

        await self._session.flush()
        logger.info(
            "instrument.upsert_done",
            total=len(instruments),
            new=new_count,
            updated=len(instruments) - new_count,
        )
        return len(instruments)

    async def list_active(
        self,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
        q: str | None = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> tuple[list[InstrumentModel], int]:
        """查询活跃标的(分页,可选模糊搜索)。"""
        return await self.list_page(
            market=market,
            instrument_type=instrument_type,
            exchange=exchange,
            listing_boards=listing_boards,
            q=q,
            limit=limit,
            offset=offset,
            status="active",
        )

    async def list_page(
        self,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
        status: str | None = "active",
        q: str | None = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> tuple[list[InstrumentModel], int]:
        """分页查询标的, ``status=None`` 时包含全部生命周期状态。"""
        conditions: list[Any] = []
        if status:
            conditions.append(InstrumentModel.status == status)
        if market:
            conditions.append(InstrumentModel.market == market)
        if instrument_type:
            conditions.append(InstrumentModel.instrument_type == instrument_type)
        if exchange:
            conditions.append(InstrumentModel.exchange == exchange)
        if listing_boards:
            conditions.append(InstrumentModel.listing_board.in_(listing_boards))
        if q:
            pattern = f"%{q}%"
            conditions.append(
                (InstrumentModel.code.ilike(pattern))
                | (InstrumentModel.name.ilike(pattern))
            )

        count_stmt = select(InstrumentModel).where(*conditions)
        total = len((await self._session.execute(count_stmt)).scalars().all())

        stmt = (
            select(InstrumentModel)
            .where(*conditions)
            .order_by(InstrumentModel.code)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows), total

    async def search(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> list[InstrumentModel]:
        """按代码或名称模糊搜索。"""
        pattern = f"%{query}%"
        stmt = (
            select(InstrumentModel)
            .where(
                (InstrumentModel.code.ilike(pattern))
                | (InstrumentModel.name.ilike(pattern))
            )
            .filter(InstrumentModel.status == "active")
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_codes(
        self,
        *,
        market: str | None = None,
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
        q: str | None = None,
    ) -> list[str]:
        """返回匹配条件的全部标的代码(不分页,轻量)。"""
        conditions: list[Any] = [InstrumentModel.status == "active"]
        if market:
            conditions.append(InstrumentModel.market == market)
        if instrument_type:
            conditions.append(InstrumentModel.instrument_type == instrument_type)
        if exchange:
            conditions.append(InstrumentModel.exchange == exchange)
        if listing_boards:
            conditions.append(InstrumentModel.listing_board.in_(listing_boards))
        if q:
            pattern = f"%{q}%"
            conditions.append(
                (InstrumentModel.code.ilike(pattern))
                | (InstrumentModel.name.ilike(pattern))
            )
        stmt = (
            select(InstrumentModel.code).where(*conditions).order_by(InstrumentModel.code)
        )
        return [row[0] for row in (await self._session.execute(stmt)).all()]

    # ----------------------------------------------------------------- 生命周期 (#35)

    async def sync_with_diff(
        self,
        instruments: list[dict[str, object]],
        *,
        as_of: date,
        delist_confirm_runs: int = 2,
    ) -> InstrumentSyncResult:
        """带反向 diff 的标的同步(issue #35)。

        正向:新标的 INSERT;已有标的更新 name/exchange,检测改名。
        反向:DB 有但本次发现列表中消失的标的,``missing_runs`` 累加;
        连续 ``delist_confirm_runs`` 次消失才标记 ``status=delisted`` + ``delist_date``。

        反向 diff 仅在本次入参覆盖的 ``(market, instrument_type)`` 组合范围内做,
        避免只拉 A 股时误把 ETF / 港股标记为退市。改名同时写入
        ``instrument_names`` 历史区间表。

        :param as_of:               本次同步的基准日期(用于 valid_from/delist_date)
        :param delist_confirm_runs: 连续消失多少次才确认退市(默认 2,二次确认)
        """
        result = InstrumentSyncResult()
        if not instruments:
            return result
        if delist_confirm_runs < 1:
            raise ValueError("delist_confirm_runs 必须 >= 1")

        by_code: dict[str, dict[str, object]] = {}
        discovered_keys: set[tuple[str, str]] = set()
        for ins in instruments:
            code = str(ins["code"])
            by_code[code] = ins
            discovered_keys.add(
                (
                    str(ins.get("market", "a_share")),
                    str(ins.get("instrument_type", "stock")),
                )
            )

        # 仅查本次发现覆盖的 (market, type) 组合,缩小反向 diff 范围
        scope_conds = [
            and_(
                InstrumentModel.market == mk,
                InstrumentModel.instrument_type == ty,
            )
            for mk, ty in discovered_keys
        ]
        db_rows = {
            row.code: row
            for row in (
                await self._session.execute(select(InstrumentModel).where(or_(*scope_conds)))
            ).scalars().all()
        }

        # ---- 正向:新增 / 更新 / 改名 / 复活计数归零 ----
        for code, ins in by_code.items():
            name = str(ins.get("name", ""))
            row = db_rows.get(code)
            if row is None:
                row = InstrumentModel(
                    code=code,
                    name=name,
                    market=str(ins.get("market", "a_share")),
                    instrument_type=str(ins.get("instrument_type", "stock")),
                    exchange=ins.get("exchange"),
                    listing_board=str(ins.get("listing_board", "unknown")),
                    status=ListingStatus.ACTIVE.value,
                    missing_runs=0,
                )
                self._session.add(row)
                db_rows[code] = row
                result.new += 1
                await self._open_name_record(code, name, as_of)
            else:
                result.updated += 1
                if name and row.name != name:
                    result.renamed.append((code, row.name, name))
                    await self._close_name_record(code, as_of)
                    await self._open_name_record(code, name, as_of)
                    row.name = name
                exchange = ins.get("exchange")
                if exchange is not None:
                    row.exchange = exchange  # type: ignore[assignment]
                if "listing_board" in ins:
                    row.listing_board = str(ins["listing_board"])
                # 重新出现:未退市的归零计数并提示复活
                if row.missing_runs > 0 and row.status != ListingStatus.DELISTED.value:
                    result.reactivated.append(code)
                row.missing_runs = 0

        # ---- 反向:退市二次确认(仅当前 scope 内未发现的标的) ----
        discovered_by_key: dict[tuple[str, str], set[str]] = {}
        for code, ins in by_code.items():
            key = (
                str(ins.get("market", "a_share")),
                str(ins.get("instrument_type", "stock")),
            )
            discovered_by_key.setdefault(key, set()).add(code)

        for code, row in db_rows.items():
            key = (row.market, row.instrument_type)
            if code in discovered_by_key.get(key, set()):
                continue
            if row.status == ListingStatus.DELISTED.value:
                continue
            row.missing_runs += 1
            if row.missing_runs >= delist_confirm_runs:
                row.status = ListingStatus.DELISTED.value
                row.delist_date = as_of
                result.delisted.append(code)
            else:
                result.pending_delist.append(code)

        await self._session.flush()
        logger.info(
            "instrument.sync_with_diff",
            new=result.new,
            updated=result.updated,
            renamed=len(result.renamed),
            pending_delist=len(result.pending_delist),
            delisted=len(result.delisted),
            reactivated=len(result.reactivated),
        )
        return result

    async def backfill_metadata_from_profiles(
        self,
        *,
        symbols: list[str] | None = None,
        source: str | None = None,
    ) -> InstrumentMetadataBackfillResult:
        """从 ``research_instrument_profiles`` 回填 list_date / industry(issue #185)。

        只回填当前为 null 的字段,不覆盖已存在的主数据;profiles 是 tushare
        ``stock_basic`` 的版本化快照,``instruments`` 的 akshare 发现链路不携带
        这两个字段。``symbols=None`` 时审查全部未退市标的(适合一次性修复)。

        返回回填前后缺失统计,缺批次(未发布过 profiles)时不视为错误。
        """
        from finboard_persistence.profile_metadata import ProfileMetadataLookup

        lookup = ProfileMetadataLookup(self._session)
        batch = await lookup.latest_batch(source=source)
        if batch is None:
            return InstrumentMetadataBackfillResult(profile_batch_available=False)

        stmt = select(InstrumentModel)
        if symbols is not None:
            stmt = stmt.where(InstrumentModel.code.in_(symbols))
        else:
            stmt = stmt.where(InstrumentModel.status != ListingStatus.DELISTED.value)
        rows = list((await self._session.execute(stmt)).scalars().all())

        profiles = await lookup.profiles([row.code for row in rows], source=source)
        backfilled_list_date = 0
        backfilled_industry = 0
        for row in rows:
            profile = profiles.get(row.code)
            if profile is None:
                continue
            if row.list_date is None and profile.list_date is not None:
                row.list_date = profile.list_date
                backfilled_list_date += 1
            if row.industry is None and profile.industry:
                row.industry = profile.industry
                backfilled_industry += 1
        await self._session.flush()
        result = InstrumentMetadataBackfillResult(
            profile_batch_available=True,
            scoped=len(rows),
            backfilled_list_date=backfilled_list_date,
            backfilled_industry=backfilled_industry,
            missing_list_date=sum(1 for row in rows if row.list_date is None),
            missing_industry=sum(1 for row in rows if row.industry is None),
        )
        logger.info("instrument.backfill_from_profiles", **result.as_dict())
        return result

    async def update_listing_status(
        self,
        code: str,
        status: ListingStatus,
        *,
        delist_date: date | None = None,
        reset_missing_runs: bool = False,
    ) -> bool:
        """更新单个标的的上市状态(停牌检测 / 人工校准用)。

        :returns: 标的是否存在
        """
        row = await self._get_by_code(code)
        if row is None:
            return False
        row.status = status.value
        if delist_date is not None:
            row.delist_date = delist_date
        if reset_missing_runs:
            row.missing_runs = 0
        await self._session.flush()
        return True

    async def get_by_code(self, code: str) -> InstrumentModel | None:
        """按 code 查询单个标的(公开)。"""
        return await self._get_by_code(code)

    async def name_history(self, code: str) -> list[InstrumentNameModel]:
        """查询某标的的名称变更历史(按 valid_from 升序)。"""
        stmt = (
            select(InstrumentNameModel)
            .where(InstrumentNameModel.instrument_code == code)
            .order_by(InstrumentNameModel.valid_from)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_status_map(self, codes: set[str]) -> dict[str, str]:
        """批量返回 ``{code: status}``(停牌检测 / 同步 diff 用)。"""
        if not codes:
            return {}
        stmt = select(InstrumentModel.code, InstrumentModel.status).where(
            InstrumentModel.code.in_(codes)
        )
        return {str(c): str(s) for c, s in (await self._session.execute(stmt)).all()}

    async def _get_by_code(self, code: str) -> InstrumentModel | None:
        stmt = select(InstrumentModel).where(InstrumentModel.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _close_name_record(self, code: str, as_of: date) -> None:
        stmt = select(InstrumentNameModel).where(
            InstrumentNameModel.instrument_code == code,
            InstrumentNameModel.valid_to.is_(None),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.valid_to = as_of

    async def _open_name_record(self, code: str, name: str, as_of: date) -> None:
        self._session.add(
            InstrumentNameModel(
                instrument_code=code,
                name=name,
                valid_from=as_of,
            )
        )


class WatchlistRepository:
    """标的组(watchlist)仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, name: str, description: str | None = None) -> WatchlistModel:
        row = WatchlistModel(name=name, description=description)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_all(self) -> list[WatchlistModel]:
        stmt = select(WatchlistModel).order_by(WatchlistModel.created_at.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, watchlist_id: int) -> WatchlistModel | None:
        stmt = select(WatchlistModel).where(WatchlistModel.id == watchlist_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def rename(
        self, watchlist_id: int, name: str, description: str | None = None
    ) -> WatchlistModel | None:
        row = await self.get(watchlist_id)
        if row is None:
            return None
        row.name = name
        if description is not None:
            row.description = description
        await self._session.flush()
        return row

    async def delete(self, watchlist_id: int) -> bool:
        row = await self.get(watchlist_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def items(self, watchlist_id: int) -> list[WatchlistItemModel]:
        stmt = (
            select(WatchlistItemModel)
            .where(WatchlistItemModel.watchlist_id == watchlist_id)
            .order_by(WatchlistItemModel.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def add_symbols(
        self, watchlist_id: int, codes: list[str]
    ) -> list[WatchlistItemModel]:
        existing = {it.symbol_code for it in await self.items(watchlist_id)}
        added: list[WatchlistItemModel] = []
        for code in codes:
            if code in existing:
                continue
            row = WatchlistItemModel(watchlist_id=watchlist_id, symbol_code=code)
            self._session.add(row)
            added.append(row)
        await self._session.flush()
        return added

    async def remove_symbol(self, watchlist_id: int, code: str) -> bool:
        stmt = select(WatchlistItemModel).where(
            WatchlistItemModel.watchlist_id == watchlist_id,
            WatchlistItemModel.symbol_code == code,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


class BacktestRunRepository:
    """回测运行历史仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, run: BacktestRunModel) -> BacktestRunModel:
        self._session.add(run)
        await self._session.flush()
        return run

    async def list_recent(
        self, *, limit: int = 50
    ) -> list[BacktestRunModel]:
        stmt = (
            select(BacktestRunModel)
            .order_by(BacktestRunModel.created_at.desc(), BacktestRunModel.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, run_id: int) -> BacktestRunModel | None:
        stmt = select(BacktestRunModel).where(BacktestRunModel.id == run_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete(self, run_id: int) -> bool:
        row = await self.get(run_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


class StrategyPresetRepository:
    """内置策略参数预设仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        name: str,
        strategy: str,
        params: dict[str, Any],
        selection: dict[str, Any] | None = None,
    ) -> StrategyPresetModel:
        row = StrategyPresetModel(
            name=name,
            strategy=strategy,
            params=params,
            selection=selection or {},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_all(self) -> list[StrategyPresetModel]:
        stmt = select(StrategyPresetModel).order_by(
            StrategyPresetModel.updated_at.desc(),
            StrategyPresetModel.id.desc(),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, preset_id: int) -> StrategyPresetModel | None:
        stmt = select(StrategyPresetModel).where(StrategyPresetModel.id == preset_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, name: str) -> StrategyPresetModel | None:
        stmt = select(StrategyPresetModel).where(StrategyPresetModel.name == name)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(
        self,
        preset_id: int,
        *,
        name: str,
        strategy: str,
        params: dict[str, Any],
        selection: dict[str, Any] | None = None,
    ) -> StrategyPresetModel | None:
        row = await self.get(preset_id)
        if row is None:
            return None
        row.name = name
        row.strategy = strategy
        row.params = params
        if selection is not None:
            row.selection = selection
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def delete(self, preset_id: int) -> bool:
        row = await self.get(preset_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


# --------------------------------------------------------------------------- 占位避免 Decimal import 警告
_ = Decimal
