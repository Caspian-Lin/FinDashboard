"""模拟 WebSocket 与实盘广播隔离, 断线后以数据库查询为准。"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import WebSocket

from finboard_api.simulation_ws import SimulationConnectionManager


class _FakeWebSocket:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.accepted = False
        self.fail_send = fail_send
        self.sent: list[dict[str, object]] = []
        self.send_attempts = 0

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, object]) -> None:
        self.send_attempts += 1
        if self.fail_send:
            raise RuntimeError("client disconnected")
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_simulation_websocket_is_session_scoped_and_prunes_stale() -> None:
    manager = SimulationConnectionManager()
    healthy = _FakeWebSocket()
    stale = _FakeWebSocket(fail_send=True)
    other = _FakeWebSocket()
    await manager.connect("SIM-S-one", cast(WebSocket, healthy))
    await manager.connect("SIM-S-one", cast(WebSocket, stale))
    await manager.connect("SIM-S-two", cast(WebSocket, other))

    payload: dict[str, object] = {
        "type": "market_event_processed",
        "mode": "simulation",
    }
    await manager.publish("SIM-S-one", payload)
    await manager.publish("SIM-S-one", payload)

    assert healthy.accepted
    assert stale.accepted
    assert other.accepted
    assert healthy.sent == [payload, payload]
    assert stale.send_attempts == 1
    assert other.sent == []

    await manager.disconnect("SIM-S-one", cast(WebSocket, healthy))
    await manager.publish("SIM-S-one", payload)
    assert healthy.sent == [payload, payload]
