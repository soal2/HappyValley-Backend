"""Abstract media storage interface."""

from abc import ABC, abstractmethod


class MediaStorage(ABC):
    @abstractmethod
    async def save(self, data: bytes, owner_id: str, room_id: str, category: str) -> str: ...

    @abstractmethod
    async def read(self, path: str, requester_id: str) -> bytes: ...

    @abstractmethod
    async def delete(self, path: str) -> None: ...
