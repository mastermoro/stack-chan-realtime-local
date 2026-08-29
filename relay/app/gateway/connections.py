import asyncio

from fastapi import WebSocket


class DeviceConnectionRegistry:
    """Keeps exactly one accepted Relay WebSocket per device id."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def claim(self, device_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            previous = self._connections.get(device_id)
            self._connections[device_id] = websocket
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=4000, reason="superseded by a new transport")
            except RuntimeError:
                pass

    async def release(self, device_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(device_id) is websocket:
                del self._connections[device_id]
