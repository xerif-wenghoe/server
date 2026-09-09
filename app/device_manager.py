from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import WebSocket


@dataclass
class ConnectedDevice:
    websocket: WebSocket
    device_id: str
    firmware_version: str
    paired: bool = False


class DeviceManager:
    def __init__(self):
        self._connections: dict[str, ConnectedDevice] = {}
        self._lock = asyncio.Lock()

    async def register(self, conn: ConnectedDevice):
        async with self._lock:
            old = self._connections.get(conn.device_id)
            if old and old.websocket is not conn.websocket:
                try:
                    await old.websocket.close(code=1012)
                except Exception:
                    pass
            self._connections[conn.device_id] = conn

    async def remove(self, device_id: str, websocket: WebSocket):
        async with self._lock:
            current = self._connections.get(device_id)
            if current and current.websocket is websocket:
                self._connections.pop(device_id, None)

    async def get(self, device_id: str) -> Optional[ConnectedDevice]:
        async with self._lock:
            return self._connections.get(device_id)

    async def send(self, device_id: str, message: dict[str, Any]) -> bool:
        conn = await self.get(device_id)
        if not conn:
            return False
        try:
            await conn.websocket.send_json(message)
            return True
        except Exception:
            return False


device_manager = DeviceManager()
