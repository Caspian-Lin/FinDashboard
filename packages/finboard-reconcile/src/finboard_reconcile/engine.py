"""核对引擎。

流程:
1. 从本地 DB 拉取活动订单 / 持仓 / 账户快照;
2. 从 broker 拉取相同维度数据;
3. 逐项比对,把差异写入 :class:`ReconciliationReport` 与 ``reconciliation_logs`` 表;
4. 返回报告;调用方根据报告决定是否触发 Kill Switch。

红线:核对未通过时上层必须暂停新订单(AGENTS.md §重启恢复)。
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from finboard_broker.base import BrokerAdapter
from finboard_persistence.repo import (
    AccountRepository,
    OrderRepository,
    PositionRepository,
    ReconciliationLogRepository,
)
from finboard_reconcile.report import (
    AccountMismatch,
    OrderMismatch,
    PositionMismatch,
    ReconciliationReport,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Order, Position

logger = structlog.get_logger(__name__)


#: 持仓数量/金额容忍差异(用于 Decimal 浮点误差,如分红送股的零头)
POSITION_TOLERANCE = Decimal("0.0001")
ACCOUNT_TOLERANCE = Decimal("0.01")


class ReconciliationEngine:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        account_id: AccountId,
        order_repo: OrderRepository,
        position_repo: PositionRepository,
        account_repo: AccountRepository,
        log_repo: ReconciliationLogRepository,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._orders = order_repo
        self._positions = position_repo
        self._accounts = account_repo
        self._logs = log_repo

    async def run(self) -> ReconciliationReport:
        report = ReconciliationReport()
        account_id_str = str(self._account_id)

        local_orders = await self._orders.list_active(account_id_str)
        broker_orders = await self._broker.query_active_orders()
        await self._reconcile_orders(report, local_orders, broker_orders)

        local_positions = await self._positions.list_local(account_id_str)
        broker_positions = await self._broker.query_positions()
        for pos in broker_positions:
            await self._positions.upsert_broker(pos)
        await self._reconcile_positions(report, local_positions, broker_positions)

        local_account = await self._accounts.get(account_id_str)
        broker_account = await self._broker.query_account()
        await self._accounts.upsert(broker_account)
        if local_account is not None:
            await self._reconcile_account(report, local_account, broker_account)

        await self._persist_logs(account_id_str, report)

        logger.info("reconciliation.completed", summary=report.summary())
        return report

    async def _persist_logs(
        self, account_id_str: str, report: ReconciliationReport
    ) -> None:
        for order in report.order_mismatches:
            await self._logs.add(
                account_id=account_id_str,
                kind="order",
                key=order.client_order_id,
                local_state=order.local_status,
                broker_state=order.broker_status,
                diff=f"filled local={order.local_filled} broker={order.broker_filled}",
                action_taken=None,
                notes=order.notes,
            )
        for pos in report.position_mismatches:
            await self._logs.add(
                account_id=account_id_str,
                kind="position",
                key=pos.symbol,
                local_state=str(pos.local_total),
                broker_state=str(pos.broker_total),
                diff=str(pos.diff),
            )
        for acct in report.account_mismatches:
            await self._logs.add(
                account_id=account_id_str,
                kind="account",
                key=acct.field_name,
                local_state=str(acct.local_value),
                broker_state=str(acct.broker_value),
                diff=str(acct.diff),
            )

    async def _reconcile_orders(
        self,
        report: ReconciliationReport,
        local_orders: list[Order],
        broker_orders: list[Order],
    ) -> None:
        broker_by_id: dict[str, Order] = {
            str(o.client_order_id): o for o in broker_orders if o.client_order_id
        }
        local_ids: set[str] = set()
        for local in local_orders:
            local_id = str(local.client_order_id)
            local_ids.add(local_id)
            broker = broker_by_id.get(local_id)
            if broker is None:
                report.local_only_orders.append(local_id)
                continue
            if (
                local.status != broker.status
                or abs(local.filled_quantity - broker.filled_quantity)
                > POSITION_TOLERANCE
            ):
                report.order_mismatches.append(
                    OrderMismatch(
                        client_order_id=local_id,
                        local_status=local.status.value,
                        broker_status=broker.status.value,
                        local_filled=local.filled_quantity,
                        broker_filled=broker.filled_quantity,
                    )
                )
        for broker_id in broker_by_id:
            if broker_id not in local_ids:
                report.broker_only_orders.append(broker_id)

    async def _reconcile_positions(
        self,
        report: ReconciliationReport,
        local_positions: list[Position],
        broker_positions: list[Position],
    ) -> None:
        broker_by_symbol: dict[str, Position] = {
            p.symbol.code: p for p in broker_positions
        }
        local_codes: set[str] = set()
        for local in local_positions:
            code = local.symbol.code
            local_codes.add(code)
            broker = broker_by_symbol.get(code)
            if broker is None:
                if abs(local.total_quantity) > POSITION_TOLERANCE:
                    report.position_mismatches.append(
                        PositionMismatch(
                            symbol=code,
                            local_total=local.total_quantity,
                            broker_total=Decimal("0"),
                            local_avg_price=local.average_price,
                            broker_avg_price=Decimal("0"),
                            diff=-local.total_quantity,
                            notes="broker 无此持仓",
                        )
                    )
                continue
            diff = local.total_quantity - broker.total_quantity
            if abs(diff) > POSITION_TOLERANCE or abs(
                local.average_price - broker.average_price
            ) > POSITION_TOLERANCE:
                report.position_mismatches.append(
                    PositionMismatch(
                        symbol=code,
                        local_total=local.total_quantity,
                        broker_total=broker.total_quantity,
                        local_avg_price=local.average_price,
                        broker_avg_price=broker.average_price,
                        diff=diff,
                    )
                )
        for code, broker in broker_by_symbol.items():
            if code not in local_codes and abs(broker.total_quantity) > POSITION_TOLERANCE:
                report.position_mismatches.append(
                    PositionMismatch(
                        symbol=code,
                        local_total=Decimal("0"),
                        broker_total=broker.total_quantity,
                        local_avg_price=Decimal("0"),
                        broker_avg_price=broker.average_price,
                        diff=broker.total_quantity,
                        notes="本地无此持仓",
                    )
                )

    async def _reconcile_account(
        self,
        report: ReconciliationReport,
        local_account: Account,
        broker_account: Account,
    ) -> None:
        for field_name in ("cash", "frozen_cash", "total_asset", "margin_used"):
            local_value: Decimal = getattr(local_account, field_name)
            broker_value: Decimal = getattr(broker_account, field_name)
            diff = local_value - broker_value
            if abs(diff) > ACCOUNT_TOLERANCE:
                report.account_mismatches.append(
                    AccountMismatch(
                        field_name=field_name,
                        local_value=local_value,
                        broker_value=broker_value,
                        diff=diff,
                    )
                )
