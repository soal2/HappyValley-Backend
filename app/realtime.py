"""Single-instance native WebSocket connection ownership."""

import asyncio

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, room_id: str, websocket: WebSocket, subprotocol: str | None = None
    ) -> None:
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            previous = self._connections.get(room_id)
            self._connections[room_id] = websocket
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=4001, reason="同一账户已在新设备接管此活动")
            except RuntimeError:
                # A reconnect may race with the old socket's own close frame.
                # The new socket already owns the room, so the stale close is done.
                pass

    async def disconnect(self, room_id: str, websocket: WebSocket) -> bool:
        """Remove only the current socket; return false for a replaced old socket."""
        async with self._lock:
            if self._connections.get(room_id) is not websocket:
                return False
            self._connections.pop(room_id, None)
            return True


connection_manager = ConnectionManager()
