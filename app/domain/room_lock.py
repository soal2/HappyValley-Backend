"""Room lock manager for concurrency control."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class RoomLockManager:
    """Manages per-room asyncio locks to serialize state transitions."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, room_id: str) -> asyncio.Lock:
        if room_id not in self._locks:
            self._locks[room_id] = asyncio.Lock()
        return self._locks[room_id]

    @asynccontextmanager
    async def acquire(self, room_id: str) -> AsyncGenerator[None, None]:
        """Acquire the lock for a specific room."""
        lock = self._get_lock(room_id)
        async with lock:
            yield

    def release(self, room_id: str) -> None:
        """Remove lock for a completed/terminated room to free memory."""
        self._locks.pop(room_id, None)


# Singleton instance
room_lock_manager = RoomLockManager()
