"""Single-instance native WebSocket connection ownership."""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, room_id: str, websocket: WebSocket, subprotocol: str | None = None
    ) -> None:
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            previous = self._connections.get(room_id)
            self._connections[room_id] = websocket
            if room_id not in self._send_locks:
                self._send_locks[room_id] = asyncio.Lock()
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
            self._send_locks.pop(room_id, None)
            return True

    async def has_connection(self, room_id: str) -> bool:
        """Whether the room currently owns a live WebSocket (liveness guard)."""
        async with self._lock:
            return room_id in self._connections

    async def send_to_room(self, room_id: str, payload: dict[str, object]) -> bool:
        """Send a payload to the room's current socket, serialized via a per-room send lock.

        Safe to call concurrently from background generation tasks and the WebSocket
        command handler: the per-room send lock prevents interleaved ``send_json`` writes
        to the same socket. Returns False if no connection exists or the send fails.
        """
        async with self._lock:
            websocket = self._connections.get(room_id)
            send_lock = self._send_locks.get(room_id)
            if send_lock is None and websocket is not None:
                send_lock = asyncio.Lock()
                self._send_locks[room_id] = send_lock
        if websocket is None or send_lock is None:
            return False
        async with send_lock:
            try:
                await websocket.send_json(payload)
                return True
            except (RuntimeError, WebSocketDisconnect):
                return False


connection_manager = ConnectionManager()
