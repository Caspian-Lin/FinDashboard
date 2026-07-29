"""模拟域独立 WebSocket 广播。

WebSocket 只提供低延迟通知; 数据库中的 ``simulation_audit`` 才是恢复和审计
真值。该 manager 不复用实盘 EventBus。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import WebSocket


class SimulationConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[session_id].add(websocket)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(session_id)
            if connections is None:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(session_id, None)

    async def publish(self, session_id: str, payload: dict[str, object]) -> None:
        async with self._lock:
            connections = tuple(self._connections.get(session_id, ()))
        stale: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(session_id, websocket)


__all__ = ["SimulationConnectionManager"]
