"""Repository 层。

职责:
* 把领域 ``dataclass`` (:class:`finboard_shared.models.Order` 等) 与 ORM 行互转;
* 提供按业务场景命名的方法,不暴露原始 ``select(...)`` 给上层;
* 不在这里做事务边界控制 —— ``commit`` 由调用方(应用层)决定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import (
    AccountModel,
    AuditLogModel,
    FillModel,
    OrderModel,
    PositionModel,
    ReconciliationLogModel,
)
from finboard_shared.identifiers import AccountId, ClientOrderId, StrategyId
from finboard_shared.models import Account, Fill, Order, Position, Symbol
from finboard_shared.types import (
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)

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
        symbol=Symbol(code=row.symbol, market=Market.A_SHARE),  # FIXME: 加列后回填
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
        symbol=Symbol(code=row.symbol, market=Market.A_SHARE),  # FIXME: 加列后回填
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
        await self._session.flush()


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


# --------------------------------------------------------------------------- 占位避免 Decimal import 警告
_ = Decimal
