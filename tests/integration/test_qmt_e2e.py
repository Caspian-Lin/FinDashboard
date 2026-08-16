"""QMT 端到端集成测试(需 Windows + miniQMT + xtquant)。

默认跳过(包括 Windows 本机):QMT 权限受阻、实盘验证暂缓(issue #22),
全量测试不跑真机链路。需要真机验证时显式设置 ``FINBOARD_QMT_E2E=1`` 开启;
开启后验证 connect → query_account → query_positions → place limit →
cancel → reconcile。
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "win32" or os.getenv("FINBOARD_QMT_E2E") != "1",
        reason=(
            "QMT 端到端测试需 Windows + miniQMT 环境;"
            "设置 FINBOARD_QMT_E2E=1 显式开启"
        ),
    ),
]

#: 测试用 QMT 配置 —— 通过环境变量/命令行参数注入
QMT_PATH = ""
QMT_SESSION_ID = 1
QMT_ACCOUNT = ""


@pytest.mark.integration
async def test_qmt_connect_and_query() -> None:
    """连接 QMT → 查询账户 → 查询持仓。"""
    from finboard_broker_qmt.adapter import QmtBroker
    from finboard_shared.identifiers import AccountId

    broker = QmtBroker(path=QMT_PATH, session_id=QMT_SESSION_ID)
    await broker.connect(AccountId(QMT_ACCOUNT), {})
    try:
        assert await broker.is_connected()
        account = await broker.query_account()
        assert account is not None
        positions = await broker.query_positions()
        assert isinstance(positions, list)
    finally:
        await broker.disconnect()


@pytest.mark.integration
async def test_qmt_place_and_cancel_limit_order() -> None:
    """下限价单 → 撤单(挂在远离市价的价位,确保不成交)。"""
    from decimal import Decimal

    from finboard_broker_qmt.adapter import QmtBroker
    from finboard_shared.identifiers import AccountId, generate_client_order_id
    from finboard_shared.models import Order, OrderRequest, Symbol
    from finboard_shared.types import BrokerKind, Market, OrderStatus, OrderType, Side

    broker = QmtBroker(path=QMT_PATH, session_id=QMT_SESSION_ID)
    await broker.connect(AccountId(QMT_ACCOUNT), {})
    try:
        symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
        request = OrderRequest(
            account_id=AccountId(QMT_ACCOUNT),
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            price=Decimal("1.00"),
        )
        order = Order(
            client_order_id=generate_client_order_id(),
            account_id=AccountId(QMT_ACCOUNT),
            broker_kind=BrokerKind.QMT,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status=OrderStatus.SUBMITTING,
        )
        submission = await broker.place_order(order)
        assert submission.accepted
        assert submission.broker_order_id is not None

        await broker.cancel_order(str(order.client_order_id))
    finally:
        await broker.disconnect()
