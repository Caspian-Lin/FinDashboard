"""API 集成测试 — FastAPI REST 端点。

使用 httpx.AsyncClient + ASGITransport,直接驱动 FastAPI app(含 lifespan)。
MockBroker + 真实 PG,验证完整 API 链路。``api`` fixture 与 ``ApiTestApp``
定义在 tests/integration/conftest.py(issue #167:共享给会话卫生等测试)。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests.integration.conftest import ApiTestApp


# --------------------------------------------------------------------------- health
@pytest.mark.integration
async def test_health(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["kernel_ready"] is True
    assert data["kill_switch_level"] == "off"


# --------------------------------------------------------------------------- account
@pytest.mark.integration
async def test_get_account(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/account")
    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "test-account-api"
    assert "total_asset" in data
    assert "cash" in data


@pytest.mark.integration
async def test_refresh_account(api: ApiTestApp) -> None:
    resp = await api.client.post("/api/account/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_asset" in data


# --------------------------------------------------------------------------- positions
@pytest.mark.integration
async def test_list_positions_empty(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.integration
async def test_list_positions_broker(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/positions?source=broker")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- orders
@pytest.mark.integration
async def test_place_limit_order_match_and_list(api: ApiTestApp) -> None:
    """下单 → 手工撮合 → 列表 → 详情 → 成交。"""
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510300.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "100",
            "price": "3.50",
        },
    )
    assert resp.status_code == 201
    order = resp.json()
    cid = order["client_order_id"]
    assert order["status"] in ("acknowledged", "submitted", "filled")
    assert order["symbol"] == "510300.SH"

    # 手工撮合(MockBroker 限价单不自动成交)
    await api.broker.match_limit_order(cid, Decimal("3.48"))
    await asyncio.sleep(0.2)

    # 列表
    resp = await api.client.get("/api/orders")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(o["client_order_id"] == cid for o in data["items"])

    # 详情
    resp = await api.client.get(f"/api/orders/{cid}")
    assert resp.status_code == 200
    assert resp.json()["client_order_id"] == cid

    # 成交记录
    resp = await api.client.get(f"/api/orders/{cid}/fills")
    assert resp.status_code == 200
    fills = resp.json()
    assert len(fills) >= 1
    assert fills[0]["client_order_id"] == cid


@pytest.mark.integration
async def test_place_market_order(api: ApiTestApp) -> None:
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "159919.SZ",
            "market": "a_share",
            "side": "buy",
            "order_type": "market",
            "quantity": "200",
        },
    )
    assert resp.status_code == 201
    order = resp.json()
    assert order["order_type"] == "market"

    # 市价单自动成交
    await asyncio.sleep(0.3)
    resp = await api.client.get(f"/api/orders/{order['client_order_id']}")
    assert resp.json()["status"] in ("filled", "partially_filled")


@pytest.mark.integration
async def test_get_order_not_found(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/orders/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_cancel_order(api: ApiTestApp) -> None:
    """下单后立刻撤单。"""
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510500.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "100",
            "price": "1.00",
        },
    )
    assert resp.status_code == 201
    cid = resp.json()["client_order_id"]

    resp = await api.client.delete(f"/api/orders/{cid}")
    assert resp.status_code == 204

    await asyncio.sleep(0.2)
    resp = await api.client.get(f"/api/orders/{cid}")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("cancelled", "cancel_pending")


# --------------------------------------------------------------------------- fills
@pytest.mark.integration
async def test_list_fills(api: ApiTestApp) -> None:
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510300.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "100",
            "price": "3.50",
        },
    )
    cid = resp.json()["client_order_id"]
    await api.broker.match_limit_order(cid, Decimal("3.49"))
    await asyncio.sleep(0.2)

    resp = await api.client.get("/api/fills")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


# --------------------------------------------------------------------------- kill switch
@pytest.mark.integration
async def test_kill_switch_get_and_activate(api: ApiTestApp) -> None:
    resp = await api.client.get("/api/kill-switch")
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "off"
    assert data["allows_new_orders"] is True

    resp = await api.client.post(
        "/api/kill-switch",
        json={"level": "no_new_orders", "reason": "test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "no_new_orders"
    assert data["allows_new_orders"] is False

    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510300.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "100",
            "price": "3.50",
        },
    )
    assert resp.status_code == 409

    resp = await api.client.post(
        "/api/kill-switch",
        json={"level": "off"},
    )
    assert resp.status_code == 200
    assert resp.json()["level"] == "off"


# --------------------------------------------------------------------------- reconcile
@pytest.mark.integration
async def test_trigger_reconcile(api: ApiTestApp) -> None:
    resp = await api.client.post("/api/reconcile")
    assert resp.status_code == 200
    data = resp.json()
    assert "ok" in data
    assert "summary" in data


# --------------------------------------------------------------------------- audit
@pytest.mark.integration
async def test_list_audit_logs(api: ApiTestApp) -> None:
    # 正常下单不产生 audit_log(仅 timeout/recovery 写),验证端点可正常返回
    resp = await api.client.get("/api/audit-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


# --------------------------------------------------------------------------- validation
@pytest.mark.integration
async def test_place_order_zero_quantity_rejected(api: ApiTestApp) -> None:
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510300.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "0",
            "price": "3.50",
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- order filter
@pytest.mark.integration
async def test_list_orders_filter_by_status(api: ApiTestApp) -> None:
    resp = await api.client.post(
        "/api/orders",
        json={
            "symbol": "510300.SH",
            "market": "a_share",
            "side": "buy",
            "order_type": "limit",
            "quantity": "100",
            "price": "3.50",
        },
    )
    cid = resp.json()["client_order_id"]
    await api.broker.match_limit_order(cid, Decimal("3.50"))
    await asyncio.sleep(0.2)

    resp = await api.client.get("/api/orders?status=filled")
    assert resp.status_code == 200
    data = resp.json()
    assert all(o["status"] == "filled" for o in data["items"])
    assert data["total"] >= 1
