"""Local media paths cannot cross ownership or storage boundaries."""

from pathlib import Path

import pytest

from app.media.local_storage import LocalMediaStorage


async def test_local_media_rejects_other_owner_and_absolute_path() -> None:
    storage = LocalMediaStorage()
    path = await storage.save(b"image", "owner-a", "room-a", "generated")
    assert await storage.read(path, "owner-a") == b"image"
    with pytest.raises(PermissionError):
        await storage.read(path, "owner-b")
    with pytest.raises(PermissionError):
        await storage.read(str(Path("/tmp/outside.png")), "owner-a")
    with pytest.raises(PermissionError):
        await storage.read("../outside.png", "owner-a")
